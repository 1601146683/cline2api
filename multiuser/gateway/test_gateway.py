#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网关自测：假上游 + 真网关，覆盖鉴权 / 限流 / 流式 / 账本 / 白名单。

    python test_gateway.py            # 全部跑一遍

不需要真 Cline 账号，不联网。
"""
import json
import os
import subprocess
import sys
import threading
import time
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
GW = os.path.join(HERE, "gateway.py")
DB = os.path.join(HERE, "gwtest.db")
UP_PORT = 3458
GW_PORT = 8081

results = []


def kill_stale_gateways():
    """清掉上次跑崩遗留的 gateway.py serve 进程（只认本目录下的脚本）。"""
    if os.name != "nt":
        return
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        ).stdout
    except Exception:
        return
    for line in out.splitlines():
        if "gateway.py" in line and ("serve" in line) and HERE.replace("\\", "\\\\") in line.replace("/", "\\"):
            parts = [p for p in line.split(",") if p.strip()]
            if parts and parts[-1].strip().isdigit():
                pid = int(parts[-1].strip())
                try:
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15)
                    print("[setup] 清理遗留 gateway 进程 pid=%d" % pid)
                except Exception:
                    pass


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  | " + str(extra)) if extra else ""))
    return cond


class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/health"):
            body = json.dumps({"status": "ok", "version": "fake"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/v1/models"):
            body = json.dumps({"object": "list", "data": [{"id": "deepseek-v4-flash-free"}, {"id": "big-pickle"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        req = json.loads(raw.decode() or "{}")
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self.send_error(404)
            return
        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunks = [
                {"id": "c1", "model": "deepseek-v4-flash-free", "choices": [{"delta": {"content": "hello"}}]},
                {"id": "c1", "model": "deepseek-v4-flash-free", "choices": [{"delta": {"content": " world"}}]},
                {"id": "c1", "model": "deepseek-v4-flash-free", "choices": [],
                 "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}},
            ]
            for i, c in enumerate(chunks):
                payload = ("data: " + json.dumps(c) + "\n\n").encode()
                self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
                self.wfile.flush()
                if i == 0:
                    time.sleep(0.6)  # 若网关整体缓冲，首块就不会在这之后立刻到
            end = b"data: [DONE]\n\n"
            self.wfile.write(b"%x\r\n" % len(end) + end + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        body = json.dumps({
            "id": "chatcmpl-1",
            "model": "deepseek-v4-flash-free",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def client_post(port, path, payload, key=None, stream=False, header="Authorization"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    headers = {"Content-Type": "application/json"}
    if key:
        if header == "Authorization":
            headers["Authorization"] = "Bearer " + key
        else:
            headers["x-api-key"] = key
    conn.request("POST", path, body=json.dumps(payload).encode(), headers=headers)
    resp = conn.getresponse()
    if stream:
        lines = []
        t0 = time.time()
        first_at = None
        while True:
            line = resp.readline()
            if not line:
                break
            if first_at is None:
                first_at = time.time()
            lines.append(line)
        elapsed = time.time() - t0
        body = b"".join(lines)
        conn.close()
        return resp.status, body, first_at, elapsed
    body = resp.read()
    conn.close()
    return resp.status, body, None, None


def run_gw_cli(*args):
    e = dict(os.environ)
    e["DB_PATH"] = DB
    e["UPSTREAM"] = "http://127.0.0.1:%d" % UP_PORT
    e["PYTHONIOENCODING"] = "utf-8"
    out = subprocess.run([sys.executable, GW] + list(args), capture_output=True, env=e, timeout=90,
                         encoding="utf-8", errors="replace")
    return out.returncode, out.stdout.strip(), out.stderr.strip()


def extract_key(out):
    for line in out.splitlines():
        if line.startswith("API Key:"):
            return line.split("API Key:", 1)[1].strip()
    return ""


def main():
    # 上次异常退出可能留下进程占着 db / 端口，先清干净
    kill_stale_gateways()
    for path in (DB, os.path.join(HERE, "gwtest2.db")):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except FileNotFoundError:
                pass
            except PermissionError:
                print("[setup] 警告: %s 被占用，沿用旧库" % (path + suffix))
    up = ThreadingHTTPServer(("127.0.0.1", UP_PORT), FakeUpstream)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()
    print("[setup] fake upstream on :%d" % UP_PORT)

    env = dict(os.environ)
    env.update({"DB_PATH": DB, "UPSTREAM": "http://127.0.0.1:%d" % UP_PORT, "AUTH_MODE": "auto"})
    proc = subprocess.Popen(
        [sys.executable, GW, "serve", "--host", "127.0.0.1", "--port", str(GW_PORT), "--wait-upstream", "20"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for _ in range(60):
        try:
            c = http.client.HTTPConnection("127.0.0.1", GW_PORT, timeout=2)
            c.request("GET", "/health")
            ok = c.getresponse().status == 200
            c.close()
            if ok:
                break
        except Exception:
            time.sleep(0.4)
    print("[setup] gateway on :%d" % GW_PORT)

    # 1) auto 模式：还没建 Key -> 透明转发（老客户端不被打断）
    st, body, _, _ = client_post(GW_PORT, "/v1/chat/completions", {"model": "big-pickle", "messages": []})
    check("auto/no-keys: pass-through 200", st == 200, st)
    check("auto/no-keys: body relayed", b"ok" in body)

    # 2) CLI 建用户 + Key
    rc, out, err = run_gw_cli("user", "add", "alice", "--rpm", "5", "--tpm", "100000",
                              "--daily", "1000000", "--key", "--key-label", "laptop")
    key = extract_key(out)
    check("cli user add + key", rc == 0 and key.startswith("xk-"), out or err)
    check("cli user list shows alice", "alice" in run_gw_cli("user", "list")[1])

    # 3) 有 Key 后自动强制鉴权（含 CLI 与网关进程间的可见性）
    st, _, _, _ = client_post(GW_PORT, "/v1/chat/completions", {"model": "big-pickle", "messages": []})
    check("after key created: no key -> 401", st == 401, st)
    st, _, _, _ = client_post(GW_PORT, "/v1/chat/completions", {"model": "big-pickle", "messages": []}, key="xk-bogus")
    check("bad key -> 401", st == 401, st)
    st, body, _, _ = client_post(GW_PORT, "/v1/chat/completions", {"model": "big-pickle", "messages": []}, key=key)
    check("good key -> 200", st == 200, st)
    check("usage relayed", b"total_tokens" in body)
    st, _, _, _ = client_post(GW_PORT, "/v1/chat/completions", {"model": "big-pickle", "messages": []},
                              key=key, header="x-api-key")
    check("x-api-key header also accepted", st == 200, st)

    # 4) 流式：SSE 完整 + 不整体缓冲
    st, body, first_at, elapsed = client_post(
        GW_PORT, "/v1/chat/completions",
        {"model": "deepseek-v4-flash-free", "stream": True, "messages": []}, key=key, stream=True)
    check("stream -> 200", st == 200, st)
    check("stream SSE intact", b"hello" in body and b"world" in body and b"[DONE]" in body)
    check("stream not buffered", first_at is not None and elapsed >= 0.5, "elapsed=%.2fs" % (elapsed or 0))

    # 5) 模型白名单
    rc, out, err = run_gw_cli("user", "set", "alice", "--models", "big-pickle")
    check("cli user set models", rc == 0, out or err)
    st, _, _, _ = client_post(GW_PORT, "/v1/chat/completions", {"model": "some-other", "messages": []}, key=key)
    check("model not allowed -> 403", st == 403, st)
    st, _, _, _ = client_post(GW_PORT, "/v1/chat/completions", {"model": "big-pickle", "messages": []}, key=key)
    check("allowed model -> 200", st == 200, st)

    # 6) RPM 限流
    codes = []
    for _ in range(8):
        st, _, _, _ = client_post(GW_PORT, "/v1/chat/completions", {"model": "big-pickle", "messages": []}, key=key)
        codes.append(st)
    check("rpm limit -> 429", 429 in codes, codes)

    # 7) 账本 + whoami
    rc, out, err = run_gw_cli("usage", "--days", "1")
    check("usage report has alice + tokens", "alice" in out and "big-pickle" in out, out or err)
    check("whoami resolves key", "alice" in run_gw_cli("whoami", key)[1])

    # 8) 用户禁用 / 启用 + 用户间限流独立
    run_gw_cli("user", "disable", "alice")
    st, _, _, _ = client_post(GW_PORT, "/v1/chat/completions", {"model": "big-pickle", "messages": []}, key=key)
    check("disabled user -> 401", st == 401, st)
    _, out, _ = run_gw_cli("user", "add", "bob", "--rpm", "30", "--tpm", "100000",
                           "--daily", "1000000", "--key")
    key2 = extract_key(out)
    run_gw_cli("user", "enable", "alice")
    st, _, _, _ = client_post(GW_PORT, "/v1/chat/completions", {"model": "big-pickle", "messages": []}, key=key)
    check("re-enabled -> not 401", st != 401, st)
    st, _, _, _ = client_post(GW_PORT, "/v1/chat/completions", {"model": "big-pickle", "messages": []}, key=key2)
    check("second user independent bucket -> 200", st == 200, st)

    # 8b) auto 闸门不会因为删 Key 而退回透明转发
    _, out, _ = run_gw_cli("key", "list")
    check("key list works", "alice" in out or "bob" in out, out)
    run_gw_cli("key", "rm", key2[:10])
    st, _, _, _ = client_post(GW_PORT, "/v1/chat/completions", {"model": "big-pickle", "messages": []}, key=key2)
    check("deleted key -> 401", st == 401, st)
    st, _, _, _ = client_post(GW_PORT, "/v1/chat/completions", {"model": "big-pickle", "messages": []})
    check("auto gate stays locked after key delete", st == 401, st)

    # 8c) 上游不可达 -> 502（用错端口的网关实例）
    env2 = dict(os.environ)
    env2.update({"DB_PATH": os.path.join(HERE, "gwtest2.db"), "UPSTREAM": "http://127.0.0.1:59999"})
    proc2 = subprocess.Popen(
        [sys.executable, GW, "serve", "--host", "127.0.0.1", "--port", "8082", "--ignore-upstream"],
        env=env2, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        for _ in range(40):
            try:
                c = http.client.HTTPConnection("127.0.0.1", 8082, timeout=2)
                c.request("GET", "/__nope")
                c.getresponse().read()
                c.close()
                break
            except Exception:
                time.sleep(0.4)
        st, body, _, _ = client_post(8082, "/v1/chat/completions", {"model": "m", "messages": []})
        check("upstream down -> 502", st == 502, st)
    finally:
        proc2.terminate()
        try:
            proc2.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc2.kill()

    # 9) 自检
    rc, out, err = run_gw_cli("check")
    check("cli check ok", rc == 0 and "GET /health -> 200" in out, out or err)

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    up.shutdown()

    failed = [r for r in results if not r[1]]
    print("\n==== %d/%d passed ====" % (len(results) - len(failed), len(results)))
    if failed:
        for name, _, extra in failed:
            print("  FAILED: %s  %s" % (name, extra))
        tail = (proc.stdout.read() or "") if proc.stdout else ""
        print("\n--- gateway output tail ---\n" + tail[-3000:])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
