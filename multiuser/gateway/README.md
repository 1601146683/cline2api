# Cline2API 多用户网关（gateway.py）

把单人自用的 Cline2API 变成多人可共用的入口：**每用户独立 Key / 独立限流 / 独立用量账本**。

零第三方依赖（Python 3.8+ 标准库），单文件，能跑在服务器、容器、systemd 里。

## 为什么需要它

上游 cline2api 本身：

* 只有**一组全局 API Key**（`/admin/api/keys/generate` 生成，存在 `.cline-accounts.json` 的 `keys` 数组）
* 没有 per-user 概念，没有 per-user 限流、没有用量归属
* 管理后台 `/admin/` 是**无鉴权**的（除非手动设密码）——公网直接暴露等于把账号池和 Key 管理交出去

所以多人共用不能直接把 3457 开公网。本网关放在前面：

```
用户客户端 ──► gateway :8080  (Key 鉴权 / 限流 / 账本)
                   │
                   └──► cline-proxy :3457  (只绑回环，账号轮询 / 协议转换)
                              │
                              └──► api.cline.bot / opencode zen
```

## 快速开始

```bash
# 1) 启动（默认连 http://127.0.0.1:3457）
python gateway.py serve --host 0.0.0.0 --port 8080 --wait-upstream 60

# 2) 第一次建用户（会同时打印一个 Key）
python gateway.py user add alice --rpm 60 --tpm 200000 --daily 2000000 --key --key-label laptop

# 3) 用户客户端配置
#    Base URL: http://<你的服务器>:8080/v1
#    API Key : xk-xxxx
#    Model   : deepseek-v4-flash-free   (或 /v1/models 里列出的任意一个)
```

> `AUTH_MODE=auto`（默认）下，**还没建任何用户时**网关透明转发——你原来的客户端不用改就能继续用；
> 一旦用 `user add` 建了第一个用户，就会自动要求 Key。这样切到多用户不会突然打断现有调用。
> 公网生产建议直接设 `AUTH_MODE=key`。

## 支持端点

全部原样透传（含 SSE 流式、chunked、请求/响应头）：

| 端点 | 说明 |
|---|---|
| `/v1/chat/completions` | OpenAI 格式 |
| `/v1/messages` | Anthropic Messages 格式（Claude Code 等） |
| `/v1/responses` | OpenAI Responses 格式（Cursor 等） |
| `/v1/models` | 模型列表 |

（不带 `/v1` 前缀的简写路径同样支持。）

## 命令行

```bash
# 用户
python gateway.py user add  <name> [--rpm 60] [--tpm 200000] [--daily 2000000] \
                                   [--models m1,m2|all] [--note "..."] [--key] [--key-label X] [--ttl 30]
python gateway.py user list
python gateway.py user set  <name> [--rpm N] [--tpm N] [--daily N] [--models ...] [--note ...]
python gateway.py user enable|disable <name>
python gateway.py user rm   <name>              # 连带删除其所有 Key

# Key
python gateway.py key add  --user alice [--label mac] [--ttl 30]
python gateway.py key list [--user alice]
python gateway.py key rm   xk-xxxxxx            # 前缀匹配，前 10 位就够

# 运维
python gateway.py usage --days 7 [--user alice]
python gateway.py whoami xk-xxxx                # Key 反查用户（排障用）
python gateway.py check                         # 配置 / 库 / 上游连通性自检
```

`usage` 输出：

```
== 最近 7 天 按用户 ==
USER             REQS     TOKENS         ERRORS   AVG_MS
alice            1841     2384711        7        3120
bob              402      501233         0        2870

== 按模型 ==
MODEL                              REQS     TOKENS
deepseek-v4-flash-free             1902     2503110
big-pickle                         341      382834
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `UPSTREAM` | `http://127.0.0.1:3457` | 上游 cline2api 地址 |
| `UPSTREAM_KEY` | 空 | 上游 Key（上游配了 `keys` 就必须填） |
| `DB_PATH` | `./gateway.db` | sqlite 路径 |
| `AUTH_MODE` | `auto` | `auto` / `key` / `pass` |
| `GATEWAY_HOST` / `GATEWAY_PORT` | `127.0.0.1` / `8080` | 监听地址（`serve --host/--port` 优先） |
| `ADMIN_TOKEN` | 空 | 设置后开放 `/__gw/*` HTTP 管理接口 |
| `MAX_BODY_MB` | `32` | 请求体上限 |
| `UPSTREAM_TIMEOUT` | `600` | 上游连接超时（秒），长生成要留够 |
| `DEV_JSON` | 空 | 测试用：从 JSON 读用户配置，不落盘 |

## HTTP 管理接口（可选）

设了 `ADMIN_TOKEN` 后可用：

```bash
curl -H "X-Admin-Token: $ADMIN_TOKEN" http://127.0.0.1:8080/__gw/health
curl -H "X-Admin-Token: $ADMIN_TOKEN" http://127.0.0.1:8080/__gw/users
curl -H "X-Admin-Token: $ADMIN_TOKEN" http://127.0.0.1:8080/__gw/stats
```

## 错误码约定（客户端可照这个重试）

| 状态码 | 含义 | 建议动作 |
|---|---|---|
| 401 | Key 缺失 / 无效 / 被禁用 / 过期 | 换 Key，别重试 |
| 403 | 模型不在该用户白名单 | 换模型 |
| 429 | RPM / TPM / 日额度超限 | 读 `Retry-After` 后重试 |
| 502 | 上游不可达 | 检查 cline-proxy 容器 |
| 413 | body 超过 `MAX_BODY_MB` | 调大或压缩上下文 |

`429` 响应带 `Retry-After` 头（秒）。

## 自测

```bash
python test_gateway.py
```

假上游 + 真网关，覆盖 21 项：透明转发、Key 鉴权、错误 Key、流式完整性与不缓冲、
模型白名单、RPM 限流、用户间限流隔离、禁用/启用、SQLite 账本、CLI 自检。不联网、不需要真账号。

## 设计取舍

* **鉴权只卡 LLM 端点**：`/v1/*` 要 Key；其它路径（如健康检查）放行，方便探活。
* **账本写 sqlite**：单进程内串行写，WAL 模式。用户量不大时够用；要更高并发再换 Postgres。
* **限流在内存**：重启清零，日额度按自然日（本地时区）。要跨进程共享限流再上 Redis。
* **Key 明文存库**：方便 `whoami` 排障。数据库文件权限靠目录控制，别提交到 Git。
* **上游不存在时不在网关缓存模型列表**：`/v1/models` 原样转发，避免两份真实来源。
