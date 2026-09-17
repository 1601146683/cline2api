# HTTPS 反代（nginx）

给 cline2api 套 HTTPS，并**只暴露必要路径**。

## 为什么不能整体代理上游

上游 `AccountPool.Keys` 为空时**允许匿名访问** —— 实测：

```bash
curl http://127.0.0.1:34581/v1/models   # → 200，无需任何 Key
```

所以「把上游整个反代出去」等于把账号池免费开放给全网。正确做法是**按路径分流**：

| 路径 | 转发到 | 鉴权 |
|---|---|---|
| `/admin/`、`/admin/api/` | 上游 `:34581` | 后台密码（entrypoint 自动设置） |
| `/v1/` | 网关 `:34580` | 每用户独立 Key + 限流 + 用量账本 |
| 其它 | — | `404` |

## 部署

```bash
# 1) 公共头片段
sudo mkdir -p /etc/nginx/snippets
sudo cp cline2api-proxy.inc /etc/nginx/snippets/cline2api-proxy.inc

# 2) 站点配置（先把 panel.example.com 改成你的域名）
sudo cp cline2api.conf /etc/nginx/sites-available/cline2api.conf
sudo ln -sfn /etc/nginx/sites-available/cline2api.conf /etc/nginx/sites-enabled/

# 3) 登录暴破限流（加进 nginx.conf 的 http{} 块内，只加一次）
sudo sed -i '0,/^http {/s//http {\n    limit_req_zone $binary_remote_addr zone=adminlogin:10m rate=10r\/m;/' /etc/nginx/nginx.conf

# 4) 证书
sudo certbot --nginx -d panel.example.com

# 5) 校验并重载
sudo nginx -t && sudo systemctl reload nginx
```

## 验证

```bash
DOMAIN=panel.example.com

# 后台可访问
curl -s -o /dev/null -w '%{http_code}\n' https://$DOMAIN/admin/            # 200

# 后台 API 需登录
curl -s -o /dev/null -w '%{http_code}\n' https://$DOMAIN/admin/api/stats   # 401

# API 需 Key
curl -s -o /dev/null -w '%{http_code}\n' https://$DOMAIN/v1/models         # 401

# 根路径拒绝（不泄露上游）
curl -s -o /dev/null -w '%{http_code}\n' https://$DOMAIN/                  # 404

# 带 Key 正常
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $KEY" https://$DOMAIN/v1/models   # 200

# 登录限流（连发 15 次错误密码，应出现 503）
for i in $(seq 1 15); do
  curl -s -o /dev/null -w '%{http_code} ' -X POST https://$DOMAIN/admin/api/login \
    -H 'Content-Type: application/json' -d '{"password":"x"}'
done
```

## 换成 Caddy

仓库里另有 `../Caddyfile`，功能等价。Caddy 会自动申请证书，配置更短，
但注意 `flush_interval -1` 必须保留（否则 SSE 被攒批）。
两种反代**选一个**即可，不要同时监听同一端口。

## 已知坑

| 现象 | 原因 | 处理 |
|---|---|---|
| `nginx: [emerg] "proxy_read_timeout" directive is duplicate` | 公共头片段里声明了超时，`/v1/` 块又覆盖 | 超时/缓冲只在各 location 内声明，片段里只放 `proxy_set_header`（本仓库已如此） |
| 客户端流式「卡半天一次性吐出来」 | 反代缓冲了 SSE | `/v1/` 必须 `proxy_buffering off` |
| 后台能开但登录接口被 503 | 限流 `rate` 太小或误触发 | 调大 `rate`，或去掉 `limit_req` 行 |
| 证书续期后 nginx 未重载 | certbot 钩子缺失 | `certbot renew --dry-run` 验证；`certbot.timer` 已启用 |
