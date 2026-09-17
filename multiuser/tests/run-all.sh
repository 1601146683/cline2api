#!/usr/bin/env bash
# ============================================================================
# multiuser/ 全套本地检查（不需要 Docker，不需要真 Cline 账号，不联网）
#   1) compose 路径自检   —— context/dockerfile/卷挂载是否正确
#   2) .dockerignore 自检 —— 凭据必须被排除、源码必须保留
#   3) 网关 26 项自测     —— 假上游 + 真网关
#
# 用法: ./run-all.sh
# ============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MU="$(cd "$HERE/.." && pwd)"

# 找一个"真的能跑"的 Python。
# 注意：Windows 上 `python3` 常是 Microsoft Store 的占位程序，
# command -v 能找到但执行即失败，所以必须试跑一次。
find_python() {
  local cand
  for cand in "${PYTHON:-}" python3 python py; do
    [ -n "$cand" ] || continue
    if command -v "$cand" >/dev/null 2>&1 && \
       "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
      printf '%s' "$cand"; return 0
    fi
  done
  return 1
}
PY="$(find_python)" || {
  echo "找不到可用的 Python 3.8+（可设 PYTHON=/path/to/python 指定）" >&2
  exit 2
}
echo "python: $PY  ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"

PASS=0; FAIL=0
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
ok()   { printf '  PASS  %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  FAIL  %s  | %s\n' "$1" "$2"; FAIL=$((FAIL+1)); }

step "1/3 compose 路径自检"
if "$PY" "$HERE/check_compose.py"; then ok "compose 路径与安全约束"; else bad "compose 自检" "见上方输出"; fi

step "2/3 .dockerignore 凭据排除自检"
if "$PY" "$HERE/check_dockerignore.py"; then ok ".dockerignore 规则"; else bad ".dockerignore 自检" "凭据可能被打进构建上下文"; fi

step "3/3 网关自测（26 项，假上游，不联网）"
if (cd "$MU/gateway" && PYTHONIOENCODING=utf-8 "$PY" test_gateway.py); then
  ok "网关全部用例"
else
  bad "网关自测" "见上方输出"
fi

printf '\n==== %d passed, %d failed ====\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
