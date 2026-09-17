"""验证仓库根 .dockerignore 是否真的挡住凭据文件。

实现 dockerignore 匹配语义的关键部分：
  - pattern 相对 context 根
  - `*` 不跨 `/`，`**` 跨目录
  - 前导 `/` 锚定到根
  - 目录匹配会连带排除其下所有内容
"""
import os, re

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

def load_patterns(path):
    pats = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pats.append(line)
    return pats

def to_regex(pat):
    # 前导斜杠 -> 锚定根；否则可匹配任意层级（docker 对含 / 的模式按根锚定处理）
    anchored = pat.startswith("/")
    p = pat.lstrip("/")
    # 目录模式（以 / 结尾）匹配其下所有内容
    dir_only = p.endswith("/")
    p = p.rstrip("/")
    out, i = "", 0
    while i < len(p):
        c = p[i]
        if p.startswith("**", i):
            out += ".*"
            i += 2
        elif c == "*":
            out += "[^/]*"
            i += 1
        elif c == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(c)
            i += 1
    if anchored or "/" in p:
        rx = "^" + out
    else:
        rx = "^(?:.*/)?" + out
    if dir_only:
        rx += "(?:/.*)?$"
    else:
        rx += "(?:/.*)?$"   # 匹配到目录名时连带排除内容
    return re.compile(rx)

def is_ignored(relpath, patterns):
    rel = relpath.replace("\\", "/")
    ignored = False
    for pat in patterns:
        negate = pat.startswith("!")
        p = pat[1:] if negate else pat
        if to_regex(p).match(rel):
            ignored = not negate
    return ignored

pats = load_patterns(os.path.join(ROOT, ".dockerignore"))

# 造出"部署后真实会存在"的敏感文件 + 必须保留的源码文件
sensitive = [
    "multiuser/data/upstream/.cline-accounts.json",
    "multiuser/data/upstream/.cline-credentials.json",
    "multiuser/data/upstream/.cline-request-logs.json",
    "multiuser/data/gateway/gateway.db",
    "multiuser/data/gateway/gateway.db-wal",
    "multiuser/.env",
    ".cline-accounts.json",
    ".env",
]
required = [
    "go.mod", "go.sum", "main.go", "proxy.go", "admin.go", "admin_html.go",
    "types.go", "pool.go", "auth.go", "zen.go", "responses.go", "request_logs.go",
    "models_sync.go", "http.go", "i18n.go", "compact.go", "capture.go", "version.go",
    "resource_windows_amd64.syso",
    "multiuser/upstream/Dockerfile.gw",
    "multiuser/gateway/gateway.py",
    "multiuser/gateway/Dockerfile",
    "Dockerfile",
]

fails = 0
print("--- 必须被排除（凭据/数据）---")
for f in sensitive:
    ig = is_ignored(f, pats)
    print(("  OK   " if ig else "  LEAK ") + f + ("" if ig else "   <-- 会被打进构建上下文!"))
    if not ig:
        fails += 1

print()
print("--- 必须保留（构建需要）---")
for f in required:
    ig = is_ignored(f, pats)
    print(("  OK   " if not ig else "  MISSING ") + f + ("" if not ig else "   <-- 被误排除，构建会失败!"))
    if ig:
        fails += 1

print()
print("RESULT:", "ALL GOOD" if fails == 0 else "%d PROBLEM(S)" % fails)
