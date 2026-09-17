# cline2api 反代公共头
# 部署位置：/etc/nginx/snippets/cline2api-proxy.inc
#
# 注意：这里**只放 proxy_set_header**。超时/缓冲类指令由各 location 自己声明，
# 因为 /v1/ 需要覆盖成更长的超时和关闭缓冲，如果在 include 里声明再覆盖会
# 触发 `"proxy_read_timeout" directive is duplicate`。
proxy_http_version 1.1;
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host  $host;
# WebSocket / SSE 升级
proxy_set_header Upgrade    $http_upgrade;
proxy_set_header Connection "upgrade";
