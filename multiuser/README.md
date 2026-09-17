# multiuser/ — 多人公网共用部署层

把上游 Cline2API（单人自用）改造成**多人公网可共用**的入口。

**不修改上游任何已有文件**（`git status` 里上游 0 改动），所以 `git pull` 永远干净、不会冲突。

> 唯一在 `multiuser/` 之外新增的文件是**仓库根的 `.dockerignore`**。
> 它必须放在根目录：因为上游构建上下文是仓库根，而上游 Dockerfile 有 `COPY . .`，
> 没有它的话 `multiuser/data/upstream/.cline-accounts.json`（明文 refreshToken）
> 会被打进构建上下文和 builder 缓存。详见该文件顶部注释。

## 30 秒上手

```bash
cd multiuser

cp .env.example .env
vi .env                # 必改 3 项：ADMIN_PASSWORD / UPSTREAM_KEY / ADMIN_TOKEN

docker compose up -d --build
docker compose logs -f gateway

# 建第一个用户（打印 Key）—— 之后网关自动从"透明转发"切到"强制 Key 鉴权"
docker compose exec gateway python gateway.py user add alice --rpm 60 --daily 2000000 --key

TEST_KEY=xk-xxxxx ./verify-stack.sh     # 部署后冒烟验证
```

用户侧配置：

```
Base URL: https://api.example.com/v1
API Key : xk-xxxxxxxx
Model   : deepseek-v4-flash-free     # 或 /v1/models 里的任意一个
```

## 架构

```
             公网
              │
       443 (Caddy, HTTPS)
              │
      ┌───────┴────────┐
      │                │
 api.example.com   admin.example.com
  （给用户）        （只放行你的 IP）
      │                │
      ▼                ▼
 ┌──────────┐   ┌──────────────────┐
 │ gateway  │   │ cline-proxy 后台 │
 │  :8080   │   │ 127.0.0.1:3457   │
 │Key/限流/ │   │（绑回环，安全）  │
 │用量账本  │   └────────┬─────────┘
 └────┬─────┘            │
      └────────┬─────────┘
               ▼
       cline-proxy :3457
     （账号轮询/双协议/模型同步）
               │
               ▼
    api.cline.bot + opencode zen
```

**公网只暴露网关**，上游 3457 只绑 `127.0.0.1`。

## 为什么不直接用上游的 Key

上游只有**一组全局 API Key**（`AccountPool.Keys`），没有用户概念：

* 所有人共用一个 Key → 泄露只能全量轮换，查不出是谁
* 没有限流 → 一个脚本就能把账号池打到全 429，全体陪葬
* 没有用量归属 → 团队分摊/计费无从下手
* `/admin/` 默认**无鉴权**（`admin.go` 里 `AdminPasswordHash == ""` 就放行）→ 公网暴露等于交出账号池

网关补齐：每用户独立 Key、RPM/TPM/日额度三档限流、模型白名单、sqlite 用量账本、SSE 流式不缓冲。

## 目录

| 文件 | 作用 |
|---|---|
| `DEPLOY.md` | **完整部署文档**：上游 8 个坑逐条对照源码、架构、安全清单、故障速查 |
| `docker-compose.yml` | 双服务栈（上游只绑回环 + 网关对外） |
| `.env.example` | 环境变量模板 |
| `gateway/gateway.py` | 多用户网关（零依赖单文件，Python 3.8+） |
| `gateway/README.md` | 网关完整文档：CLI 全量命令、环境变量、管理接口、错误码、设计取舍 |
| `gateway/test_gateway.py` | 26 项自测：假上游 + 真网关，不联网、不需要真账号 |
| `gateway/Dockerfile` | 网关镜像（python:3.12-alpine） |
| `upstream/Dockerfile.gw` | 上游构建用（+GOPROXY，去掉匿名卷声明） |
| `Caddyfile` | HTTPS 反代（`flush_interval -1` 保证 SSE 实时；后台按 IP 白名单） |
| `verify-stack.sh` | 部署后冒烟（含"上游端口是否意外对公网开放"探测、明文凭据权限检查） |
| `systemd/cline2api.service` | 裸机跑网关 + docker 跑上游的方案 |
| `tests/run-all.sh` | 一键跑下面全部本地检查（不需要 Docker / 不联网） |
| `tests/check_compose.py` | 静态校验 compose 路径与安全约束（回环绑定、卷挂载、healthcheck） |
| `tests/check_dockerignore.py` | 校验凭据确实被排除、源码确实被保留 |

## 自测（三件套，一条命令）

```bash
cd multiuser && ./tests/run-all.sh
```

不需要 Docker、不需要真 Cline 账号、不联网。包含：

1. **compose 自检** —— context/dockerfile 路径、上游端口是否只绑回环、数据卷是否避开 `/app`、healthcheck 是否用 alpine 自带 wget
2. **.dockerignore 自检** —— 8 类凭据文件必须被排除、23 个构建必需文件必须保留
3. **网关 26 项自测** —— 透明转发、Key 鉴权（`Bearer` 与 `x-api-key` 两种写法）、错误 Key、禁用用户、删除 Key 后闸门不回退、SSE 完整性与不缓冲、模型白名单、RPM 限流、用户间限流隔离、sqlite 账本、上游不可达 502、CLI 自检

也可以单独跑：

```bash
python multiuser/tests/check_compose.py
python multiuser/tests/check_dockerignore.py
python multiuser/gateway/test_gateway.py
```

## 更新上游

```bash
cd multiuser && git pull && docker compose up -d --build
```

因为不碰上游已有文件，pull 不会冲突。

> 万一上游自己加了 `.dockerignore`，`git pull` 会报
> `untracked working tree file '.dockerignore' would be overwritten`。
> 处理：先 `rm ../.dockerignore` 再 pull（上游版本里若已含排除规则就无需它了）。

## 注意

`.gitignore` 已排除 `data/`（内含明文 refreshToken）和 `.env`（含密钥）。
**别手贱 `git add -f`**。

`tests/run-all.sh` 与 `verify-stack.sh` 的第 5 项都会帮你复查这一点。

## 唯一技术栈解决不了的风险

Cline 上游 ToS 对"反代多人共享"的态度。这个自己判断。
