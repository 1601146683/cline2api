#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compose 自检：路径解析 + 安全约束。

不需要 Docker 守护进程，纯静态校验，可在 Windows/macOS/Linux 上跑。
校验项：
  1) build.context / build.dockerfile 真实存在（含 ${VAR:-default} 插值）
  2) 上游构建上下文必须是仓库根（有 go.mod）
  3) 上游端口必须绑 127.0.0.1（不能对公网开放）
  4) 上游数据卷不能挂到 /app（会盖掉二进制），且 working_dir 必须是 /data
  5) 网关必须等上游 healthy 才启动（depends_on.condition）

    python check_compose.py
"""
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
MU = os.path.normpath(os.path.join(HERE, ".."))
ROOT = os.path.normpath(os.path.join(MU, ".."))
COMPOSE = os.path.join(MU, "docker-compose.yml")

VARPAT = re.compile(r"\$\{(\w+):-([^}]*)\}")


def interp(s):
    """模拟 compose 的 ${VAR:-default} 插值。"""
    return VARPAT.sub(lambda m: os.environ.get(m.group(1), m.group(2)), s)


def main():
    with open(COMPOSE, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    svcs = data["services"]
    problems = []

    def expect(cond, msg):
        print(("  OK   " if cond else "  FAIL ") + msg)
        if not cond:
            problems.append(msg)

    print("--- 服务 ---")
    expect(set(svcs) == {"cline-proxy", "gateway"}, "只有 cline-proxy + gateway 两个服务: %s" % sorted(svcs))
    if problems:
        return 1
    up, gw = svcs["cline-proxy"], svcs["gateway"]

    print("--- 上游构建 ---")
    b = up["build"]
    ctx = os.path.normpath(os.path.join(MU, b["context"]))
    dfile = os.path.normpath(os.path.join(ctx, interp(b["dockerfile"])))
    expect(os.path.isdir(ctx), "context 存在: %s" % ctx)
    expect(os.path.isfile(os.path.join(ctx, "go.mod")), "context 是仓库根（有 go.mod）")
    expect(os.path.isfile(dfile), "dockerfile 存在: %s" % os.path.relpath(dfile, ROOT))

    print("--- 上游安全 ---")
    ports = up["ports"]
    expect(all(str(p).startswith("127.0.0.1:") for p in ports),
           "上游端口只绑回环（公网不可达）: %s" % ports)
    vols = [str(v) for v in up["volumes"]]
    expect(not any(v.split(":")[1:2] == ["/app"] for v in vols),
           "数据卷没有挂到 /app（否则会盖掉 cline-proxy 二进制）: %s" % vols)
    expect(up.get("working_dir") == "/data",
           "working_dir=/data 让 resolveDataPath 命中持久化目录: %r" % up.get("working_dir"))
    hc = up.get("healthcheck", {}).get("test")
    expect(bool(hc) and "wget" in " ".join(hc),
           "上游 healthcheck 用 alpine 自带的 wget")

    print("--- 网关 ---")
    gb = gw["build"]
    gctx = os.path.normpath(os.path.join(MU, gb["context"]))
    expect(os.path.isfile(os.path.join(gctx, "gateway.py")), "网关 context 含 gateway.py: %s" % gctx)
    expect(os.path.isfile(os.path.join(gctx, "Dockerfile")), "网关 Dockerfile 存在")
    dep = gw.get("depends_on", {}).get("cline-proxy", {})
    expect(dep.get("condition") == "service_healthy",
           "网关等上游 healthy 才启动（避免启动即 502）")

    print("--- 仓库根 .dockerignore（凭据不得进构建上下文）---")
    di = os.path.join(ROOT, ".dockerignore")
    expect(os.path.isfile(di), "存在 %s" % di)
    if os.path.isfile(di):
        rules = [l.strip() for l in open(di, encoding="utf-8")
                 if l.strip() and not l.strip().startswith("#")]
        expect("multiuser/data/" in rules, "显式排除 multiuser/data/（明文 refreshToken）")
        expect(any(r in ("multiuser/.env", "**/.env") for r in rules), "排除 multiuser/.env（含密钥）")

    print()
    if problems:
        print("%d PROBLEM(S)" % len(problems))
        return 1
    print("COMPOSE CHECKS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
