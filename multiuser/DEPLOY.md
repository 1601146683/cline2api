# Cline2API 多人公网共用部署方案

> 位置：`multiuser/` —— 本目录**不修改上游任何文件**，`git pull` 永远干净。

**结论先说**：是，走服务器 + Docker 最方便。但**不能只 `docker compose up` 就完事**——上游那份 compose 有几个坑，
直接照抄公网会用出问题。下面是修正过的整套，加一个多用户网关。

---

## 一、上游原方案的问题（逐个查过源码）

| # | 问题 | 依据 | 后果 |
|---|---|---|---|
| 1 | **管理后台无鉴权** | `admin.go` `requireAdminAuth`：`if loadPool().AdminPasswordHash == "" { next(w,r); return }` | 默认没设密码时，任何能访问 3457 的人都能进 `/admin/`，看到账号池、导出 refreshToken、生成/删除 API Key |
| 2 | **官方 compose 把 3457 映射到 `0.0.0.0`** | `docker-compose.yml`: `ports: - "3457:3457"` | 公网直接暴露后台，等于把账号池挂公网 |
| 3 | **单账号池 = 单写者** | `pool.go` 的 `poolPath` 是文件级 `sync.Mutex`，非跨进程锁 | 多个共享同一份 `.cline-accounts.json` 的实例会互相覆盖；账号池只能给一个实例用 |
| 4 | **全局 API Key，没有用户概念** | `AccountPool.Keys []string` | 没有 per-user Key、没有限流、没有用量归属；给谁用就等于把总钥匙给谁 |
| 5 | **账号文件是明文 refreshToken** | `types.go`: `RefreshToken string \`json:"refreshToken"\``，README 也标注"明文 refreshToken" | 备份/镜像/日志泄露 = 账号被接管 |
| 6 | **请求日志写到 exe 同目录** | `request_logs.go` `saveRequestLogsLocked` 用 `tmp + os.Rename` | 把**文件**挂载成 volume 会 `EBUSY`（rename 跨挂载点）；必须挂**目录** |
| 7 | **Dockerfile 声明 `VOLUME ["/app/data"]`** | 上游 Dockerfile | 生成匿名卷，和宿主机目录方案打架 |
| 8 | **上游数据文件按 exe目录→cwd→~/ 查找** | `pool.go` `resolveDataPath` | 容器里 exe 在 `/app`，不处理的话数据落 `/app`（镜像层，重建即丢）。要么挂 `/data` + `working_dir`，要么挂 `/app`（会盖掉二进制） |

> 第 8 条特别容易踩：很多人第一反应是 `- ./data/upstream:/app`，**这会把镜像里的 `cline-proxy` 二进制整个盖掉**，容器直接起不来。

---

## 二、目标架构

```
                        公网
                         │
                  443 (Caddy/nginx, HTTPS)
                         │
        ┌────────────────┴─────────────────┐
        │                                  │
   api.example.com                    admin.example.com
   （给用户/客户端）                    （只放行你自己的 IP）
        │                                  │
        ▼                                  ▼
  ┌───────────────┐               ┌──────────────────────┐
  │  gateway      │               │  cline-proxy 后台     │
  │  :8080        │               │  127.0.0.1:3457 /admin/│
  │  Key 鉴权      │               │  （绑回环，公网碰不到） │
  │  每用户限流    │               └──────────┬───────────┘
  │  用量账本      │                          │
  └───────┬───────┘                          │
          │                                  │
          └──────────► cline-proxy :3457 ◄───┘
                       （账号轮询 / 双协议 / 模型同步）
                              │
                              ▼
                    api.cline.bot  +  opencode.ai/zen
```

**关键点：公网只暴露网关。** 上游 3457 只绑 `127.0.0.1`，管理后台换账号时用 SSH 隧道或只对你 IP 开放的域名进。

目录（都在 `multiuser/` 下）：

* `docker-compose.yml` —— 双服务栈（上游只绑回环 + 网关对外）
* `gateway/` —— 多用户网关 `gateway.py`（零依赖单文件）+ 自测 + 镜像
* `upstream/Dockerfile.gw` —— 上游构建用 Dockerfile（+GOPROXY）
* `Caddyfile` / `systemd/` / `verify-stack.sh` / `.env.example`

---

## 三、快速部署（Linux + Docker）

```bash
# 0) 进仓库里的 multiuser 目录（构建上下文是仓库根，无需再拉一份源码）
cd multiuser

# 1) 配置
cp .env.example .env
vi .env                         # 至少改这三项：
#   ADMIN_PASSWORD      上游后台密码（强口令）
#   UPSTREAM_KEY        上游 API Key（openssl rand -hex 24）
#   ADMIN_TOKEN         网关管理令牌（openssl rand -hex 16）

# 2) 起服务
docker compose up -d --build
docker compose logs -f gateway

# 3) 建第一个用户（同时打印 Key）—— 这一步之后网关自动从"透明转发"切到"强制 Key"
docker compose exec gateway python gateway.py user add alice \
    --rpm 60 --tpm 200000 --daily 2000000 --key --key-label laptop

# 4) 配置上游：设后台密码 + 加 Cline 账号 + 生成上游 Key
#    后台只绑了回环，用 SSH 隧道进：
ssh -L 3457:127.0.0.1:3457 user@your-server
#    然后本机浏览器打开 http://127.0.0.1:3457/admin/
#    - 访问设置 → 管理后台密码 → 填 ADMIN_PASSWORD
#    - 账号管理 → 导入账号（OAuth 或 refreshToken）
#    - 设置 → 生成 API 密钥 → 复制出来填回 .env 的 UPSTREAM_KEY
docker compose up -d            # 让网关拿到 UPSTREAM_KEY

# 5) 验证
TEST_KEY=xk-xxxxx ./verify-stack.sh
```

> 上游更新：`git pull && docker compose up -d --build`（在 `multiuser/` 下执行）。
> 因为不碰上游文件，pull 不会冲突。

用户侧配置：

```
Base URL: https://api.example.com/v1
API Key : xk-xxxxxxxx
Model   : deepseek-v4-flash-free      # 或 /v1/models 里的任意一个
```

兼容 OpenAI（`/v1/chat/completions`）、Anthropic（`/v1/messages`）、Responses（`/v1/responses`）三种协议，
流式（SSE）原样透传。

---

## 四、为什么需要那个网关（不能只用上游的 Key 吗）

上游的 `Keys` 是一组**全局** Key，谁能用就等于是完整权限：

* 所有人共用一个 Key → 一个人被打爆/banned，全体陪葬，且查不出是谁
* 没有限流 → 一个脚本循环就能把账号池打到全 429
* 没有用量归属 → 不知道谁在烧额度，团队分摊/计费无从下手
* Key 泄露只能全量轮换

网关补上这些（都已实测，见第五节）：

| 能力 | 实现 |
|---|---|
| 每用户独立 Key（`xk-...`，可多把、可禁用、可设过期） | `keys` 表，`user add --key` |
| 限流 | RPM / TPM / 每日 token 三档滑动窗口，超限返回 `429` + `Retry-After` |
| 模型白名单 | 每用户可限定 `--models deepseek-v4-flash-free,big-pickle`，越权用返回 `403` |
| 用量账本 | sqlite `usage_log`，`usage --days 7` 出按用户 / 按模型报表 |
| 流式不缓冲 | SSE chunked 原样转发（实测首块 0.6s 内到，不是攒完再发） |
| 零配置切换 | `AUTH_MODE=auto`：还没建用户时透明转发 → 建了第一个用户就永久强制鉴权（**删光 Key 也不会退回开放**，有持久化闸门） |
| 排障 | `whoami <key>` 反查用户、`check` 自检 + 安全体检 |

---

## 五、已验证项（不是纸上设计）

`python gateway.py` 那套逻辑跑了 26 项自动化用例，全绿：

```
==== 26/26 passed ====
  透明转发 / Key 鉴权（Authorization: Bearer 与 x-api-key 两种写法）
  错误 Key 401 / 禁用用户 401 / 删除 Key 后 401 且闸门不回退
  流式：SSE 完整（含 [DONE]）+ 首块不被缓冲（0.6s 内到达）
  模型白名单 403 / RPM 限流 429 / 用户间限流互不影响
  sqlite 账本按用户与按模型统计 / 上游不可达 502 / CLI 自检
```

跑法：

```bash
cd gateway && python test_gateway.py     # 假上游 + 真网关，不联网、不需要真账号
```

部署后另有一套针对真实环境的冒烟检查（含"上游端口是否对公网开放"探测、明文凭据权限检查）：

```bash
cd stack && TEST_KEY=xk-... ./verify-stack.sh
```

---

## 六、安全清单（公网必做）

1. **上游 3457 只绑 `127.0.0.1`**，或直接不映射端口、只让 compose 内部网络访问
2. **一定要设上游后台密码**（`ADMIN_PASSWORD`），否则 `/admin/` 裸奔
3. **网关用 `AUTH_MODE=key`** 强制鉴权（`auto` 只适合"先平滑切换"的过渡期）
4. **HTTPS**：用 `stack/Caddyfile`，注意 `flush_interval -1`（否则 SSE 被攒批）
5. **`data/` 加进 `.gitignore`**，权限 `chmod 600 data/upstream/.cline-accounts.json`（明文 refreshToken）
6. **别把 `.env` 提交**；`.env.example` 只是模板
7. **上游更新**：`git -C upstream pull && docker compose up -d --build cline-proxy`
8. **每用户限流必设**——账号池被打爆是这套架构最常见的故障
9. **`ADMIN_TOKEN` 设上**，方便远程查用量；不设则 `/__gw/*` 直接关闭
10. Cline 上游 ToS 是"反代多人共享"这件事的**唯一真实风险点**，技术栈解决不了，自己判断

---

## 七、常见坑

| 现象 | 原因 | 处理 |
|---|---|---|
| 容器起来就退出，日志 `exec /app/cline-proxy: no such file` | 把宿主机目录挂到了 `/app`，盖掉二进制 | 挂 `/data` + `working_dir: /data`（本栈已处理） |
| 上游日志报 `Failed to save accounts: ... device or resource busy` | `.cline-request-logs.json` 用了**文件级**挂载，rename 到挂载点 `EBUSY` | 挂目录，不挂单文件（本栈已处理） |
| 重建容器后账号/Key 全没了 | 数据落在镜像层 `/app` | 检查 `resolveDataPath` 命中路径：容器内应是 `/data/.cline-accounts.json` |
| 客户端流式"卡半天一次性吐出来" | 反向代理缓冲了 SSE | Caddy `flush_interval -1`；nginx 关 `proxy_buffering` |
| 网关 401 但 Key 是对的 | 上游 `keys` 配了 Key 而 `UPSTREAM_KEY` 没填 | 网关 502/401 时先跑 `gateway.py check`，它会直接提示这条 |
| 想直连官方 Dockerfile 构建 | 受限网络拉不到 `proxy.golang.org` | `.env` 里 `UPSTREAM_DOCKERFILE=Dockerfile`；默认用 `Dockerfile.gw`（走 goproxy.cn） |
| `docker compose exec gateway ...` 报找不到 python | 网关镜像 `python:3.12-alpine`，命令是 `python`（不是 `python3`，两者都有） | 用 `python` 即可 |

---

## 八、文件清单

```
cline2api/                        ← 上游仓库（multiuser/ 之外完全未改动）
└── multiuser/
    ├── DEPLOY.md                 ← 本文档
    ├── docker-compose.yml        双服务栈（上游只绑回环 + 网关对外）
    ├── .env.example              环境变量模板（含生成命令注释）
    ├── .gitignore                排除 data/ 与 .env（明文凭据）
    ├── verify-stack.sh           部署后冒烟验证（含公网暴露探测）
    ├── Caddyfile                 HTTPS 反代（SSE 正确配置 + 后台 IP 白名单）
    ├── upstream/Dockerfile.gw    上游构建（+GOPROXY，去掉匿名卷）
    ├── systemd/cline2api.service 裸机跑网关 + docker 跑上游
    ├── tests/                    本地三件套自检（compose / dockerignore / 网关）
    └── gateway/
        ├── gateway.py            多用户网关（零依赖单文件）
        ├── test_gateway.py       26 项自测
        ├── Dockerfile            网关镜像
        └── README.md             网关完整文档（CLI / 环境变量 / 错误码）
```

网关详细用法（CLI 全量命令、环境变量表、HTTP 管理接口、错误码表、设计取舍）见 `gateway/README.md`。
