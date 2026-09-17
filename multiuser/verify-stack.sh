#!/usr/bin/env bash
# ============================================================================
# 部署后冒烟验证 / post-deploy smoke test
#   跑之前: .env 已配好，容器已起
#   用法:   ./verify-stack.sh [GATEWAY_URL] [UPSTREAM_URL]
#           默认 http://127.0.0.1:8080 和 http://127.0.0.1:3457
#           TEST_KEY=xk-... ./verify-stack.sh   # 顺带跑真实调用
#   退出码: 0 = 全部通过
# ============================================================================
set -uo pipefail

GW="${1:-http://127.0.0.1:8080}"
UP="${2:-http://127.0.0.1:3457}"
PASS=0
FAIL=0

ok()  { printf '  PASS  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  FAIL  %s  | %s\n' "$1" "$2"; FAIL=$((FAIL+1)); }
code() { curl -s -o /dev/null -w '%{http_code}' -m 15 "$@"; }

echo "== 1. 上游只绑回环（公网不应可达）=="
if curl -s -m 5 -o /dev/null "$UP/health"; then
  ok "宿主机能访问上游 $UP/health"
else
  bad "宿主机访问上游失败" "$UP/health 不通，检查 docker compose ps"
fi
EXT_IP="$(curl -s -m 8 https://api.ipify.org 2>/dev/null || true)"
if [ -n "$EXT_IP" ]; then
  if curl -s -m 5 -o /dev/null "http://$EXT_IP:${UP##*:}/health"; then
    bad "上游端口对公网开放" "http://$EXT_IP:3457 可达 —— 立刻改回 127.0.0.1:3457:3457 并检查防火墙"
  else
    ok "上游端口公网不可达（外网 IP $EXT_IP）"
  fi
else
  printf '  SKIP  拿不到外网 IP，跳过公网探测\n'
fi

echo "== 2. 网关健康 =="
H="$(curl -s -m 10 "$GW/health")"
if echo "$H" | grep -q '"status"'; then ok "网关 /health -> $H"; else bad "网关 /health" "$H"; fi

echo "== 3. 鉴权闸门 =="
C="$(code -X POST "$GW/v1/chat/completions" -H 'Content-Type: application/json' -d '{"model":"test","messages":[]}')"
if [ "$C" = "401" ]; then
  ok "无 Key 请求被拒（401）"
elif [ "$C" = "200" ]; then
  bad "无 Key 也能通" "当前是透明转发。跑: docker compose exec gateway python gateway.py user add <name> --key"
else
  bad "无 Key 请求返回异常码" "$C（期望 401）"
fi

echo "== 4. 传一个真 Key 试（可选）=="
if [ -n "${TEST_KEY:-}" ]; then
  C="$(code -X POST "$GW/v1/chat/completions" -H "Authorization: Bearer $TEST_KEY" -H 'Content-Type: application/json' \
        -d '{"model":"big-pickle","messages":[{"role":"user","content":"ping"}],"max_tokens":8}')"
  if [ "$C" = "200" ]; then ok "真 Key 调用成功（200）"; else bad "真 Key 调用失败" "$C"; fi
  C="$(code -X POST "$GW/v1/chat/completions" -H "Authorization: Bearer $TEST_KEY" -H 'Content-Type: application/json' \
        -d '{"model":"big-pickle","messages":[{"role":"user","content":"ping"}],"stream":true,"max_tokens":8}')"
  if [ "$C" = "200" ]; then ok "流式调用成功（200）"; else bad "流式调用失败" "$C"; fi
else
  printf '  SKIP  未设置 TEST_KEY，跳过真实调用（用法: TEST_KEY=xk-... ./verify-stack.sh）\n'
fi

echo "== 5. 明文凭据保护 =="
if [ -f ./data/upstream/.cline-accounts.json ]; then
  PERM="$(stat -c '%a' ./data/upstream/.cline-accounts.json 2>/dev/null || stat -f '%Lp' ./data/upstream/.cline-accounts.json)"
  if [ "$PERM" = "600" ]; then ok "账号文件权限 $PERM"; else bad "账号文件权限是 $PERM" "改一下: chmod 600 ./data/upstream/.cline-accounts.json"; fi
else
  printf '  SKIP  还没生成 ./data/upstream/.cline-accounts.json\n'
fi
# .git 在仓库根（本目录是 multiuser/，通常是仓库子目录）
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "$REPO_ROOT" ]; then
  TRACKED="$(cd "$REPO_ROOT" && git ls-files -- multiuser/data 2>/dev/null || true)"
  if [ -n "$TRACKED" ]; then
    bad "data/ 已被 Git 跟踪！" "明文 refreshToken 正在被提交：$TRACKED —— 立刻 git rm --cached 并确认忽略规则"
  else
    ok "data/ 未被 Git 跟踪（仓库: $REPO_ROOT）"
  fi
  # 顺带确认忽略规则真的生效
  if [ -f "$REPO_ROOT/multiuser/.env" ]; then
    if (cd "$REPO_ROOT" && git check-ignore -q multiuser/.env 2>/dev/null); then
      ok ".env 已被忽略规则覆盖"
    else
      bad ".env 未被忽略" "含 ADMIN_PASSWORD / UPSTREAM_KEY / ADMIN_TOKEN，必须加进 .gitignore"
    fi
  fi
else
  ok "本目录不在 Git 仓库内（跳过跟踪检查）"
fi

echo
echo "==== $PASS passed, $FAIL failed ===="
[ "$FAIL" -eq 0 ]
