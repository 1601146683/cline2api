#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cline2API 多用户网关 / Multi-User Gateway
========================================

把一个"单人自用"的 Cline2API 实例，变成可以给多人（团队 / 朋友 / 客户）共用的
API 网关：每个用户一套独立 Key、独立限流、独立用量账本。

零第三方依赖（Python 3.8+ 标准库），扔到服务器上直接跑。

功能
----
* 每用户独立 API Key（xk-...），支持多 Key、禁用、过期
* 限流：RPM（每分钟请求） / TPM（每分钟 token） / Daily（每日 token）
* 模型白名单：每用户可限定只能用哪些模型
* 用量账本：sqlite 记录每次请求 tokens / 耗时 / 状态，usage 命令出报表
* 流式透传：SSE 原样转发，chunked 编码，不缓冲，不改协议
* AUTH_MODE=auto（默认）：库里还没有任何 Key 时透明转发（零配置起步），
  一旦建了第一个用户就自动强制鉴权 —— 老客户端不会突然断
* 协议全透传：/v1/chat/completions、/v1/messages、/v1/responses、/v1/models

用法
----
启动:
    python gateway.py serve --host 0.0.0.0 --port 8080
管理:
    python gateway.py user add alice --rpm 60 --tpm 200000 --daily 2000000
    python gateway.py user list
    python gateway.py user set alice --rpm 120 --models deepseek-v4-flash-free,big-pickle
    python gateway.py user disable alice
    python gateway.py user rm alice
    python gateway.py key add --user alice --label laptop
    python gateway.py key list [--user alice]
    python gateway.py key rm <key前12位>
    python gateway.py usage --days 7 [--user alice]
    python gateway.py whoami <key>          # 用 Key 反查用户（排障）
    python gateway.py check                 # 自检：配置 / 库 / 上游连通性

环境变量
--------
UPSTREAM      上游 cline2api 地址，默认 http://127.0.0.1:3457
UPSTREAM_KEY  上游 API Key（cline2api 后台生成的 key；上游没配 key 就不填）
GATEWAY_HOST / GATEWAY_PORT   监听地址，默认 127.0.0.1:8080
DB_PATH       sqlite 路径，默认 ./gateway.db
AUTH_MODE     auto | key | pass   默认 auto
MAX_BODY_MB   请求体上限，默认 32
LOG_LEVEL     INFO / DEBUG，默认 INFO
ADMIN_TOKEN   设置后开放 /__gw/* HTTP 管理接口（请求头 X-Admin-Token）
DEV_JSON      指向 JSON 文件则走内存配置（不写 sqlite），便于本地测试
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

VERSION = "1.0.0"

LLM_PATHS = {
    "/v1/chat/completions",
    "/chat/completions",
    "/v1/messages",
    "/messages",
    "/v1/responses",
    "/responses",
    "/v1/models",
    "/models",
}

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

KEY_PREFIX = "xk-"

log = logging.getLogger("gateway")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
class Config:
    def __init__(self, **kw):
        self.upstream = kw.get("upstream") or os.environ.get("UPSTREAM", "http://127.0.0.1:3457")
        self.upstream_key = kw.get("upstream_key") or os.environ.get("UPSTREAM_KEY", "")
        self.db_path = kw.get("db_path") or os.environ.get("DB_PATH", "./gateway.db")
        self.auth_mode = (kw.get("auth_mode") or os.environ.get("AUTH_MODE", "auto")).lower()
        self.max_body = int(float(kw.get("max_body_mb") or os.environ.get("MAX_BODY_MB", 32)) * 1024 * 1024)
        self.admin_token = kw.get("admin_token") or os.environ.get("ADMIN_TOKEN", "")
        self.dev_json = kw.get("dev_json") or os.environ.get("DEV_JSON", "")
        self.upstream_timeout = float(os.environ.get("UPSTREAM_TIMEOUT", 600))
        self.connect_timeout = float(os.environ.get("UPSTREAM_CONNECT_TIMEOUT", 30))
        u = urlsplit(self.upstream)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise SystemExit("UPSTREAM 必须是 http(s)://host[:port] 形式，当前: %r" % self.upstream)
        self.up_scheme = u.scheme
        self.up_host = u.hostname
        self.up_port = u.port or (443 if u.scheme == "https" else 80)
        self.up_base_path = (u.path or "").rstrip("/")
        if self.auth_mode not in ("auto", "key", "pass"):
            raise SystemExit("AUTH_MODE 只能是 auto / key / pass")


# --------------------------------------------------------------------------- #
# 限流桶（内存滑动窗口）
# --------------------------------------------------------------------------- #
class Bucket:
    __slots__ = ("lock", "req", "tok", "day")

    def __init__(self):
        self.lock = threading.Lock()
        self.req = deque()      # 请求时间戳
        self.tok = deque()      # (时间戳, token 数)
        self.day = {}           # 'YYYY-MM-DD' -> tokens

    def take_request(self, rpm: int, now: float):
        """返回 (允许?, 需要等待秒数)"""
        with self.lock:
            while self.req and now - self.req[0] > 60.0:
                self.req.popleft()
            if rpm and len(self.req) >= rpm:
                return False, max(1, int(60.0 - (now - self.req[0])) + 1)
            self.req.append(now)
            return True, 0

    def snapshot(self, now: float):
        with self.lock:
            while self.tok and now - self.tok[0][0] > 60.0:
                self.tok.popleft()
            tpm = sum(n for _, n in self.tok)
            day_key = time.strftime("%Y-%m-%d", time.localtime(now))
            daily = self.day.get(day_key, 0)
            return tpm, daily

    def add_tokens(self, n: int, now: float):
        if n <= 0:
            return
        with self.lock:
            self.tok.append((now, n))
            while self.tok and now - self.tok[0][0] > 60.0:
                self.tok.popleft()
            day_key = time.strftime("%Y-%m-%d", time.localtime(now))
            # 只保留今天，避免字典无限增长
            if len(self.day) > 4:
                self.day = {day_key: self.day.get(day_key, 0)}
            self.day[day_key] = self.day.get(day_key, 0) + n


# --------------------------------------------------------------------------- #
# 存储层：sqlite（生产）/ 内存 JSON（DEV_JSON，本地测试）
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id           INTEGER PRIMARY KEY,
  name         TEXT UNIQUE NOT NULL,
  rpm          INTEGER NOT NULL DEFAULT 60,
  tpm          INTEGER NOT NULL DEFAULT 200000,
  daily_tokens INTEGER NOT NULL DEFAULT 2000000,
  models       TEXT    NOT NULL DEFAULT 'all',
  enabled      INTEGER NOT NULL DEFAULT 1,
  note         TEXT    NOT NULL DEFAULT '',
  created_at   TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS keys (
  id           INTEGER PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  key          TEXT UNIQUE NOT NULL,
  label        TEXT NOT NULL DEFAULT '',
  enabled      INTEGER NOT NULL DEFAULT 1,
  expires_at   TEXT,
  created_at   TEXT NOT NULL,
  last_seen_ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_keys_enabled ON keys(enabled);
CREATE INDEX IF NOT EXISTS idx_keys_key     ON keys(key);
CREATE TABLE IF NOT EXISTS usage_log (
  id                INTEGER PRIMARY KEY,
  ts                TEXT NOT NULL,
  user_id           INTEGER,
  user_name         TEXT,
  key_id            INTEGER,
  model             TEXT,
  path              TEXT,
  stream            INTEGER,
  prompt_tokens     INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  total_tokens      INTEGER DEFAULT 0,
  status            INTEGER,
  duration_ms       INTEGER,
  error             TEXT,
  client_ip         TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_ts      ON usage_log(ts);
CREATE INDEX IF NOT EXISTS idx_usage_user_ts ON usage_log(user_id, ts);
-- 持久化开关：一旦建过 Key 就永久置 1，AUTH_MODE=auto 用它做"是否已进入多用户模式"的判据。
-- 这样即使把所有 Key 都禁用/删掉，也不会意外退回透明转发。
CREATE TABLE IF NOT EXISTS settings (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
"""


def now_str() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class Store:
    """sqlite 存储。所有写操作串行化，读也走同一把锁（吞吐足够，先保证正确）。"""

    def __init__(self, path: str):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ---- 用户 ----
    def user_add(self, name, rpm, tpm, daily, models, note) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO users(name,rpm,tpm,daily_tokens,models,note,created_at) VALUES(?,?,?,?,?,?,?)",
                (name, rpm, tpm, daily, models, note, now_str()),
            )
            self.conn.execute(
                "INSERT INTO settings(k,v) VALUES('auth_locked','1')"
                " ON CONFLICT(k) DO UPDATE SET v='1'"
            )
            self.conn.commit()
            return cur.lastrowid

    def user_get(self, name):
        with self.lock:
            row = self.conn.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def user_get_by_id(self, uid):
        with self.lock:
            row = self.conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None

    def user_list(self):
        with self.lock:
            rows = self.conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def user_update(self, name, **fields):
        allowed = {"rpm", "tpm", "daily_tokens", "models", "enabled", "note"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed and v is not None:
                sets.append("%s=?" % k)
                vals.append(v)
        if not sets:
            return False
        vals.append(name)
        with self.lock:
            cur = self.conn.execute("UPDATE users SET %s WHERE name=?" % ",".join(sets), vals)
            self.conn.commit()
            return cur.rowcount > 0

    def user_delete(self, name) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM users WHERE name=?", (name,))
            self.conn.commit()
            return cur.rowcount > 0

    # ---- Key ----
    def key_add(self, user_name, label, ttl_days):
        u = self.user_get(user_name)
        if not u:
            return None
        key = KEY_PREFIX + secrets.token_urlsafe(24)
        exp = None
        if ttl_days:
            exp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + ttl_days * 86400))
        with self.lock:
            self.conn.execute(
                "INSERT INTO keys(user_id,key,label,expires_at,created_at) VALUES(?,?,?,?,?)",
                (u["id"], key, label or "", exp, now_str()),
            )
            self.conn.commit()
        self._key_count_cache = (0.0, 0)
        return key

    def key_lookup(self, key):
        """返回 (user, key_row) 或 (None, None)"""
        if not key:
            return None, None
        with self.lock:
            row = self.conn.execute(
                "SELECT k.*, u.name AS user_name, u.rpm, u.tpm, u.daily_tokens, u.models, u.enabled AS user_enabled"
                " FROM keys k JOIN users u ON u.id = k.user_id WHERE k.key=?",
                (key,),
            ).fetchone()
        if not row:
            return None, None
        k = dict(row)
        if not k["enabled"] or not k["user_enabled"]:
            return None, k
        if k["expires_at"] and k["expires_at"] < now_str():
            return None, k
        user = {
            "id": k["user_id"],
            "name": k["user_name"],
            "rpm": k["rpm"],
            "tpm": k["tpm"],
            "daily_tokens": k["daily_tokens"],
            "models": k["models"],
        }
        return user, k

    def key_list(self, user_name=None):
        q = ("SELECT k.id,k.key,k.label,k.enabled,k.expires_at,k.created_at,k.last_seen_ip,u.name AS user_name"
             " FROM keys k JOIN users u ON u.id=k.user_id")
        args = ()
        if user_name:
            q += " WHERE u.name=?"
            args = (user_name,)
        q += " ORDER BY k.id"
        with self.lock:
            rows = self.conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def key_delete(self, prefix) -> int:
        with self.lock:
            cur = self.conn.execute("DELETE FROM keys WHERE key LIKE ?", (prefix + "%",))
            self.conn.commit()
            return cur.rowcount

    def key_touch(self, key_id, ip):
        with self.lock:
            self.conn.execute("UPDATE keys SET last_seen_ip=? WHERE id=?", (ip, key_id))
            self.conn.commit()

    def enabled_key_count(self) -> int:
        # 不做缓存：这是安全闸门，多进程（CLI 建 Key / 网关进程）之间必须立刻可见。
        # keys 表极小且有 idx_keys_enabled，COUNT(*) 开销可忽略。
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM keys WHERE enabled=1").fetchone()
        return int(row["n"])

    def auth_locked(self) -> bool:
        """AUTH_MODE=auto 的判据：建过用户/Key 就永久要求鉴权。"""
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
            if int(row["n"]) > 0:
                return True
            row = self.conn.execute("SELECT COUNT(*) AS n FROM keys").fetchone()
            if int(row["n"]) > 0:
                return True
            row = self.conn.execute("SELECT v FROM settings WHERE k='auth_locked'").fetchone()
        return bool(row and row["v"] == "1")

    # ---- 用量 ----
    def log_usage(self, rec: dict):
        with self.lock:
            self.conn.execute(
                "INSERT INTO usage_log(ts,user_id,user_name,key_id,model,path,stream,prompt_tokens,"
                "completion_tokens,total_tokens,status,duration_ms,error,client_ip)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rec.get("ts", now_str()),
                    rec.get("user_id"),
                    rec.get("user_name"),
                    rec.get("key_id"),
                    rec.get("model"),
                    rec.get("path"),
                    1 if rec.get("stream") else 0,
                    rec.get("prompt_tokens", 0),
                    rec.get("completion_tokens", 0),
                    rec.get("total_tokens", 0),
                    rec.get("status", 0),
                    rec.get("duration_ms", 0),
                    rec.get("error", ""),
                    rec.get("client_ip", ""),
                ),
            )
            self.conn.commit()

    def usage_report(self, days=7, user=None):
        since = time.strftime("%Y-%m-%dT00:00:00", time.localtime(time.time() - max(0, days - 1) * 86400))
        q = ("SELECT user_name, COUNT(*) AS reqs, COALESCE(SUM(total_tokens),0) AS tokens,"
             " SUM(CASE WHEN status>=400 THEN 1 ELSE 0 END) AS errors,"
             " COALESCE(AVG(duration_ms),0) AS avg_ms"
             " FROM usage_log WHERE ts>=?")
        args = [since]
        if user:
            q += " AND user_name=?"
            args.append(user)
        q += " GROUP BY user_name ORDER BY tokens DESC"
        with self.lock:
            rows = self.conn.execute(q, args).fetchall()
            by_model = self.conn.execute(
                "SELECT model, COUNT(*) AS reqs, COALESCE(SUM(total_tokens),0) AS tokens"
                " FROM usage_log WHERE ts>=?" + (" AND user_name=?" if user else "") +
                " GROUP BY model ORDER BY tokens DESC LIMIT 20",
                args,
            ).fetchall()
        return [dict(r) for r in rows], [dict(r) for r in by_model]


class MemoryStore:
    """DEV_JSON 模式：从 JSON 加载用户/Key，不落盘，仅本地测试用。"""

    def __init__(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.users, self.keys, self.usage = [], [], []
        for u in data.get("users", []):
            u = dict(u)
            u.setdefault("rpm", 60)
            u.setdefault("tpm", 200000)
            u.setdefault("daily_tokens", 2000000)
            u.setdefault("models", "all")
            u.setdefault("enabled", 1)
            u.setdefault("id", len(self.users) + 1)
            self.users.append(u)
        for k in data.get("keys", []):
            self.keys.append(dict(k))

    def key_lookup(self, key):
        for k in self.keys:
            if k["key"] == key:
                for u in self.users:
                    if u["name"] == k.get("user_name") and u.get("enabled", 1):
                        return u, k
        return None, None

    def enabled_key_count(self):
        return len(self.keys)

    def auth_locked(self):
        return bool(self.users or self.keys)

    def key_touch(self, key_id, ip):
        return None

    def log_usage(self, rec):
        self.usage.append(rec)


# --------------------------------------------------------------------------- #
# 鉴权 / 限流 判定
# --------------------------------------------------------------------------- #
def auth_required(cfg: Config, store) -> bool:
    if cfg.auth_mode == "key":
        return True
    if cfg.auth_mode == "pass":
        return False
    # auto：建过用户/Key 就永久要求鉴权，禁用全部 Key 也不会退回透明转发
    return store.auth_locked()


def extract_key(handler) -> str:
    k = handler.headers.get("x-api-key")
    if k:
        return k.strip()
    auth = handler.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def model_allowed(models_field: str, model: str) -> bool:
    if not models_field or models_field.strip().lower() in ("all", "*"):
        return True
    allowed = [m.strip() for m in models_field.split(",") if m.strip()]
    return model in allowed


# --------------------------------------------------------------------------- #
# 用量提取（OpenAI / Anthropic / Responses 三种协议）
# --------------------------------------------------------------------------- #
def merge_usage(dst: dict, obj) -> dict:
    if not isinstance(obj, dict):
        return dst
    u = obj.get("usage")
    if u is None and isinstance(obj.get("response"), dict):
        u = obj["response"].get("usage")
    if u is None and isinstance(obj.get("message"), dict):
        u = obj["message"].get("usage")
    if not isinstance(u, dict):
        return dst
    p = u.get("prompt_tokens", u.get("input_tokens"))
    c = u.get("completion_tokens", u.get("output_tokens"))
    t = u.get("total_tokens")
    if isinstance(p, int):
        dst["prompt"] = max(dst.get("prompt", 0), p)
    if isinstance(c, int):
        dst["completion"] = max(dst.get("completion", 0), c)
    if isinstance(t, int):
        dst["total"] = max(dst.get("total", 0), t)
    return dst


def finalize_usage(u: dict, fallback_text_len: int, ok: bool = True):
    p = int(u.get("prompt", 0))
    c = int(u.get("completion", 0))
    t = int(u.get("total", 0))
    if not (p or c or t) and ok:
        # 上游没给 usage：按 4 字符 ≈ 1 token 粗估
        t = max(1, fallback_text_len // 4)
    if not t:
        t = p + c
    return p, c, t


# --------------------------------------------------------------------------- #
# HTTP 处理
# --------------------------------------------------------------------------- #
class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cline2api-gw/" + VERSION
    sys_version = ""

    cfg: Config = None          # 由 server 注入
    store = None
    buckets = None
    buckets_lock = None

    # -------------------------- 日志 --------------------------
    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def log_error(self, fmt, *args):
        log.warning("%s - %s", self.address_string(), fmt % args)

    # -------------------------- 入口 --------------------------
    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, PATCH, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, x-api-key, anthropic-version, anthropic-beta, openai-beta, x-admin-token",
        )

    # -------------------------- 分发 --------------------------
    def _handle(self, method: str):
        path, _, query = self.path.partition("?")
        try:
            if path.startswith("/__gw/"):
                return self._admin(method, path, query)
            return self._proxy(method, path, query)
        except BrokenPipeError:
            log.debug("client disconnected")
        except ConnectionResetError:
            log.debug("client reset")
        except Exception as exc:  # noqa: BLE001
            log.exception("unhandled error: %s", exc)
            try:
                self._error(500, "gateway internal error: %s" % exc)
            except Exception:  # noqa: BLE001
                pass

    # -------------------------- 自管理接口 --------------------------
    def _admin(self, method, path, query):
        if not self.cfg.admin_token:
            return self._json(404, {"error": {"message": "admin api disabled (set ADMIN_TOKEN)", "type": "not_found"}})
        token = self.headers.get("x-admin-token") or ""
        if not secrets.compare_digest(token, self.cfg.admin_token):
            return self._json(401, {"error": {"message": "bad admin token", "type": "auth_error"}})

        store = self.store
        if path == "/__gw/health":
            return self._json(200, {"status": "ok", "version": VERSION, "auth_mode": self.cfg.auth_mode})
        if path == "/__gw/users":
            if isinstance(store, MemoryStore):
                return self._json(200, {"users": store.users, "note": "dev json mode"})
            return self._json(200, {"users": store.user_list()})
        if path == "/__gw/stats":
            if isinstance(store, MemoryStore):
                return self._json(200, {"dev": True, "requests": len(store.usage)})
            users, models = store.usage_report(days=7)
            return self._json(200, {"days": 7, "by_user": users, "by_model": models})
        return self._json(404, {"error": {"message": "unknown admin path", "type": "not_found"}})

    # -------------------------- 代理主流程 --------------------------
    def _proxy(self, method: str, path: str, query: str):
        cfg = self.cfg
        is_llm = path in LLM_PATHS

        # 1) 读 body
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > cfg.max_body:
            return self._error(413, "request body too large (limit %d MB)" % (cfg.max_body // 1024 // 1024))
        body = self.rfile.read(length) if length else b""

        parsed_body = None
        if is_llm and method == "POST" and body:
            try:
                parsed_body = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                parsed_body = None
        model = (parsed_body or {}).get("model") if isinstance(parsed_body, dict) else None
        stream_req = bool((parsed_body or {}).get("stream")) if isinstance(parsed_body, dict) else False

        # 2) 鉴权 + 限流（只对 LLM 端点强制）
        user = key_row = None
        if is_llm and auth_required(cfg, self.store):
            raw_key = extract_key(self)
            if not raw_key:
                return self._error(
                    401,
                    "missing API key. send it as 'Authorization: Bearer <key>' or 'x-api-key: <key>'",
                    etype="auth_error",
                )
            user, key_row = self.store.key_lookup(raw_key)
            if not user:
                reason = "invalid or disabled API key"
                if key_row and key_row.get("expires_at"):
                    reason = "API key expired"
                return self._error(401, reason, etype="auth_error")

            now = time.time()
            bucket = self._bucket(user["id"])
            ok, wait = bucket.take_request(int(user.get("rpm") or 0), now)
            if not ok:
                return self._error(
                    429,
                    "rate limit: %d requests/min exceeded for user '%s', retry in %ds"
                    % (user["rpm"], user["name"], wait),
                    etype="rate_limit_error",
                    extra_headers={"Retry-After": str(wait)},
                )
            tpm_used, daily_used = bucket.snapshot(now)
            tpm_limit = int(user.get("tpm") or 0)
            daily_limit = int(user.get("daily_tokens") or 0)
            if daily_limit and daily_used >= daily_limit:
                wait = int(86400 - (now % 86400)) + 1
                return self._error(
                    429,
                    "daily token quota exhausted for user '%s' (%d/%d)" % (user["name"], daily_used, daily_limit),
                    etype="rate_limit_error",
                    extra_headers={"Retry-After": str(wait)},
                )
            if tpm_limit and tpm_used >= tpm_limit:
                return self._error(
                    429,
                    "token rate limit: %d tokens/min exceeded for user '%s'" % (tpm_limit, user["name"]),
                    etype="rate_limit_error",
                    extra_headers={"Retry-After": "10"},
                )
            if model and not model_allowed(user.get("models", "all"), model):
                return self._error(
                    403,
                    "model '%s' is not allowed for user '%s'" % (model, user["name"]),
                    etype="permission_error",
                )

        # 3) 转发
        started = time.time()
        status, usage, err, model_seen = self._forward(method, path, query, body, stream_req)
        duration_ms = int((time.time() - started) * 1000)

        # 4) 记账
        # 失败请求不按长度估 token，避免错误响应污染用量报表
        p, c, t = finalize_usage(usage, len(body), ok=(status < 400))
        if user:
            self._bucket(user["id"]).add_tokens(t, time.time())
            if key_row:
                self.store.key_touch(key_row["id"], self._client_ip())
        rec = {
            "ts": now_str(),
            "user_id": user["id"] if user else None,
            "user_name": user["name"] if user else ("anonymous" if is_llm else None),
            "key_id": key_row["id"] if key_row else None,
            "model": model or model_seen,
            "path": path,
            "stream": stream_req,
            "prompt_tokens": p,
            "completion_tokens": c,
            "total_tokens": t,
            "status": status,
            "duration_ms": duration_ms,
            "error": err or "",
            "client_ip": self._client_ip(),
        }
        try:
            if is_llm or status >= 400:
                self.store.log_usage(rec)
        except Exception as exc:  # noqa: BLE001
            log.warning("usage log failed: %s", exc)
        if user:
            log.info(
                "%s user=%s model=%s status=%s %dms tokens=%d/%d/%d",
                self.command, user["name"], model or model_seen or "-", status, duration_ms, p, c, t,
            )

    # -------------------------- 上游转发 --------------------------
    def _forward(self, method, path, query, body, stream_req):
        cfg = self.cfg
        full_path = (cfg.up_base_path + path) + (("?" + query) if query else "")
        usage: dict = {}
        err_msg = ""
        model_seen = None

        headers = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in HOP_BY_HOP or lk in ("host", "content-length", "accept-encoding", "x-api-key", "authorization"):
                continue
            headers[k] = v
        headers["Host"] = "%s:%d" % (cfg.up_host, cfg.up_port)
        headers["Accept-Encoding"] = "identity"
        headers["Connection"] = "close"
        if cfg.upstream_key:
            headers["x-api-key"] = cfg.upstream_key
        if body:
            headers["Content-Length"] = str(len(body))

        conn_cls = http.client.HTTPSConnection if cfg.up_scheme == "https" else http.client.HTTPConnection
        try:
            conn = conn_cls(cfg.up_host, cfg.up_port, timeout=cfg.connect_timeout)
            conn.request(method, full_path, body=body if body else None, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:  # noqa: BLE001
            self._error(502, "upstream unreachable: %s" % exc, etype="api_error")
            return 502, usage, "upstream unreachable: %s" % exc, None

        status = resp.status
        ctype = (resp.getheader("Content-Type") or "").lower()
        is_stream = "text/event-stream" in ctype or (stream_req and status < 400)

        # 响应头回写
        self.send_response(status)
        for k, v in resp.getheaders():
            lk = k.lower()
            if lk in HOP_BY_HOP or lk in ("content-length", "content-encoding", "date", "server"):
                continue
            self.send_header(k, v)
        self._cors_headers()

        chunked = self.request_version >= "HTTP/1.1"
        if is_stream:
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Connection", "close")
            self.end_headers()
            self._pump_stream(resp, usage)
        else:
            data = resp.read()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            if data and "json" in ctype:
                try:
                    obj = json.loads(data.decode("utf-8", "replace"))
                    model_seen = obj.get("model") if isinstance(obj, dict) else None
                    merge_usage(usage, obj)
                except ValueError:
                    pass
            if status >= 400:
                try:
                    obj = json.loads(data.decode("utf-8", "replace"))
                    e = obj.get("error")
                    err_msg = (e.get("message") if isinstance(e, dict) else str(e)) if e else data[:200].decode("utf-8", "replace")
                except ValueError:
                    err_msg = data[:200].decode("utf-8", "replace")

        try:
            resp.close()
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return status, usage, err_msg, model_seen

    def _pump_stream(self, resp, usage: dict):
        """SSE 原样转发（chunked），边转边解析 usage。"""
        chunked = self.request_version >= "HTTP/1.1"
        buf = b""
        captured = 0
        while True:
            try:
                chunk = resp.read(4096)
            except Exception as exc:  # noqa: BLE001
                log.debug("upstream read error: %s", exc)
                break
            if not chunk:
                break
            if captured < 4 * 1024 * 1024:
                buf += chunk
                captured += len(chunk)
            try:
                if chunked:
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                log.debug("client gone, stop streaming")
                break
        if chunked:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:  # noqa: BLE001
                pass
        # 解析 SSE 里的 usage / model
        for line in buf.split(b"\n"):
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                obj = json.loads(payload.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(obj, dict):
                if isinstance(obj.get("model"), str) and not usage.get("model"):
                    usage["model"] = obj["model"]
                merge_usage(usage, obj)
        if not usage.get("completion"):
            usage["completion"] = max(1, len(buf) // 8)

    # -------------------------- 小工具 --------------------------
    def _client_ip(self):
        fwd = self.headers.get("X-Forwarded-For") or self.headers.get("X-Real-IP")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0] if self.client_address else ""

    def _json(self, status, payload, extra_headers=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors_headers()
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, message, etype="invalid_request_error", extra_headers=None):
        self._json(
            status,
            {"error": {"message": message, "type": etype, "code": status}},
            extra_headers=extra_headers,
        )

    # 限流桶：按 user_id 隔离
    def _bucket(self, uid):
        with self.buckets_lock:
            b = self.buckets.get(uid)
            if b is None:
                b = Bucket()
                self.buckets[uid] = b
            return b


# --------------------------------------------------------------------------- #
# 服务启动
# --------------------------------------------------------------------------- #
def build_gateway(cfg: Config):
    store = MemoryStore(cfg.dev_json) if cfg.dev_json else Store(cfg.db_path)
    GatewayHandler.cfg = cfg
    GatewayHandler.store = store
    GatewayHandler.buckets = {}
    GatewayHandler.buckets_lock = threading.Lock()
    return store


def serve(cfg: Config, host: str, port: int):
    store = build_gateway(cfg)
    httpd = ThreadingHTTPServer((host, port), GatewayHandler)
    httpd.daemon_threads = True
    n_keys = store.enabled_key_count()
    print("cline2api multi-user gateway v%s" % VERSION)
    print("  listen      : http://%s:%d" % (host, port))
    print("  upstream    : %s" % cfg.upstream)
    print("  auth mode   : %s (%s)" % (cfg.auth_mode, "keys active" if n_keys else "no keys yet -> pass-through"))
    print("  storage     : %s" % (cfg.dev_json or cfg.db_path))
    print("  admin api   : %s" % ("http://%s:%d/__gw/health (X-Admin-Token)" % (host, port) if cfg.admin_token else "disabled"))
    print("  endpoints   : /v1/chat/completions  /v1/messages  /v1/responses  /v1/models")
    print("  press Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()


# --------------------------------------------------------------------------- #
# 上游健康检查
# --------------------------------------------------------------------------- #
def upstream_health(cfg: Config):
    conn_cls = http.client.HTTPSConnection if cfg.up_scheme == "https" else http.client.HTTPConnection
    try:
        conn = conn_cls(cfg.up_host, cfg.up_port, timeout=10)
        conn.request("GET", (cfg.up_base_path + "/health") or "/health")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body[:400].decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def wait_for_upstream(cfg: Config, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = upstream_health(cfg)
        if status and status < 500:
            return True, "%s %s" % (status, body)
        time.sleep(1.0)
    return False, "upstream not ready after %ds" % timeout


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _open_store(cfg: Config):
    if cfg.dev_json:
        return MemoryStore(cfg.dev_json)
    return Store(cfg.db_path)


def _mask(key: str) -> str:
    return key if len(key) <= 14 else key[:10] + "..." + key[-4:]


def cli_serve(args, cfg: Config):
    host = args.host or os.environ.get("GATEWAY_HOST", "127.0.0.1")
    port = args.port or int(os.environ.get("GATEWAY_PORT", 8080))
    if args.wait_upstream:
        ok, msg = wait_for_upstream(cfg, args.wait_upstream)
        print("[upstream] %s" % msg)
        if not ok and not args.ignore_upstream:
            raise SystemExit("upstream 不可用，先起 cline2api 或加 --ignore-upstream")
    serve(cfg, host, port)


def cli_user(args, cfg: Config):
    store = _open_store(cfg)
    if isinstance(store, MemoryStore):
        raise SystemExit("DEV_JSON 模式下不支持写操作")
    cmd = args.user_cmd
    if cmd == "add":
        if store.user_get(args.name):
            raise SystemExit("用户已存在: %s" % args.name)
        uid = store.user_add(
            args.name,
            args.rpm,
            args.tpm,
            args.daily,
            args.models or "all",
            args.note or "",
        )
        print("已创建用户 #%d %s  (rpm=%s tpm=%s daily=%s models=%s)"
              % (uid, args.name, args.rpm, args.tpm, args.daily, args.models or "all"))
        if args.key:
            k = store.key_add(args.name, args.key_label or "default", args.ttl)
            print("API Key: %s" % k)
    elif cmd == "list":
        users = store.user_list()
        if not users:
            print("(没有用户 —— 网关处于透明转发模式，任何人都能用)")
            return
        keys = store.key_list()
        by_user = {}
        for k in keys:
            by_user.setdefault(k["user_name"], []).append(k)
        print("%-14s %-4s %-6s %-9s %-10s %-28s %s" % ("USER", "ID", "EN", "RPM", "TPM", "MODELS", "KEYS"))
        for u in users:
            ks = by_user.get(u["name"], [])
            print("%-14s %-4d %-6s %-9d %-10d %-28s %d"
                  % (u["name"], u["id"], "yes" if u["enabled"] else "NO", u["rpm"], u["tpm"],
                     u["models"][:28], len(ks)))
    elif cmd == "set":
        fields = {}
        if args.rpm is not None:
            fields["rpm"] = args.rpm
        if args.tpm is not None:
            fields["tpm"] = args.tpm
        if args.daily is not None:
            fields["daily_tokens"] = args.daily
        if args.models is not None:
            fields["models"] = args.models
        if args.note is not None:
            fields["note"] = args.note
        if not fields:
            raise SystemExit("没有要改的字段，用 --rpm/--tpm/--daily/--models/--note")
        if not store.user_update(args.name, **fields):
            raise SystemExit("用户不存在: %s" % args.name)
        print("已更新 %s: %s" % (args.name, fields))
    elif cmd in ("enable", "disable"):
        if not store.user_update(args.name, enabled=1 if cmd == "enable" else 0):
            raise SystemExit("用户不存在: %s" % args.name)
        print("%s 已%s" % (args.name, "启用" if cmd == "enable" else "禁用"))
    elif cmd == "rm":
        if not store.user_delete(args.name):
            raise SystemExit("用户不存在: %s" % args.name)
        print("已删除 %s（其 Key 一并删除）" % args.name)


def cli_key(args, cfg: Config):
    store = _open_store(cfg)
    if isinstance(store, MemoryStore):
        raise SystemExit("DEV_JSON 模式下不支持写操作")
    cmd = args.key_cmd
    if cmd == "add":
        k = store.key_add(args.user, args.label or "", args.ttl)
        if not k:
            raise SystemExit("用户不存在: %s" % args.user)
        print("用户 %s 的新 Key: %s" % (args.user, k))
    elif cmd == "list":
        rows = store.key_list(args.user)
        if not rows:
            print("(无 Key)")
            return
        for r in rows:
            print("%-8s %-12s %-16s %-6s %-20s %s"
                  % (r["id"], r["user_name"], _mask(r["key"]), "yes" if r["enabled"] else "NO",
                     r["label"], r["last_seen_ip"] or "-"))
    elif cmd == "rm":
        n = store.key_delete(args.prefix)
        print("删除 %d 个 Key" % n)


def cli_usage(args, cfg: Config):
    store = _open_store(cfg)
    if isinstance(store, MemoryStore):
        print("DEV_JSON 模式，内存中请求数: %d" % len(store.usage))
        return
    users, models = store.usage_report(args.days, args.user)
    print("== 最近 %d 天 按用户 ==" % args.days)
    print("%-16s %-8s %-14s %-8s %s" % ("USER", "REQS", "TOKENS", "ERRORS", "AVG_MS"))
    for r in users:
        print("%-16s %-8d %-14d %-8d %.0f" % (r["user_name"] or "-", r["reqs"], r["tokens"], r["errors"] or 0, r["avg_ms"] or 0))
    if models:
        print("\n== 按模型 ==")
        print("%-34s %-8s %s" % ("MODEL", "REQS", "TOKENS"))
        for r in models:
            print("%-34s %-8d %d" % (r["model"] or "-", r["reqs"], r["tokens"]))


def cli_whoami(args, cfg: Config):
    store = _open_store(cfg)
    user, row = store.key_lookup(args.key)
    if user and row:
        print("Key 属于用户 %s (id=%s), label=%s, 最后使用 IP=%s"
              % (user["name"], user["id"], row.get("label") or "-", row.get("last_seen_ip") or "-"))
    elif row:
        print("Key 存在但已失效（禁用/过期/用户被禁）")
    else:
        print("Key 不存在")


def cli_check(args, cfg: Config):
    print("== gateway ==")
    print("  version   : %s" % VERSION)
    print("  upstream  : %s" % cfg.upstream)
    print("  auth mode : %s" % cfg.auth_mode)
    print("  storage   : %s" % (cfg.dev_json or cfg.db_path))
    store = _open_store(cfg)
    if not isinstance(store, MemoryStore):
        print("  users     : %d" % len(store.user_list()))
        print("  keys      : %d" % len(store.key_list()))
        print("  gate      : %s" % ("LOCKED (需要 Key)" if store.auth_locked() else "OPEN (透明转发)"))
    print("== upstream health ==")
    status, body = upstream_health(cfg)
    print("  GET /health -> %s  %s" % (status or "ERR", body))
    if status == 0:
        print("  !! 上游没起来。先 docker compose up -d cline-proxy，或检查 UPSTREAM 地址")
    # 安全体检
    warns = []
    if not isinstance(store, MemoryStore):
        if not store.auth_locked():
            warns.append("还没建任何用户 -> 现在是透明转发，公网上任何人都能白嫖。跑 user add 建第一个用户")
        if cfg.auth_mode == "pass":
            warns.append("AUTH_MODE=pass：完全不鉴权，别放公网")
    if not cfg.upstream_key:
        warns.append("UPSTREAM_KEY 未设置：若上游后台生成过 Key，网关转发会被上游 401")
    if cfg.auth_mode == "auto" and not cfg.admin_token:
        warns.append("未设 ADMIN_TOKEN：/__gw/* 管理接口关闭（不是问题，但没法远程查用量）")
    for w in warns:
        print("  [warn] %s" % w)
    return 0 if status else 1


def build_parser():
    p = argparse.ArgumentParser(
        prog="gateway",
        description="Cline2API 多用户网关（独立 Key / 限流 / 用量账本）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python gateway.py serve --host 0.0.0.0 --port 8080 --wait-upstream 60
  python gateway.py user add alice --rpm 60 --tpm 200000 --daily 2000000 --key
  python gateway.py user list
  python gateway.py usage --days 7
  python gateway.py check
""",
    )
    p.add_argument("--db", dest="db_path", help="sqlite 路径（默认 $DB_PATH 或 ./gateway.db）")
    p.add_argument("--upstream", help="上游 cline2api 地址")
    p.add_argument("--upstream-key", help="上游 API Key")
    p.add_argument("--auth-mode", choices=["auto", "key", "pass"], help="鉴权模式，默认 auto")
    p.add_argument("--dev-json", help="用 JSON 文件作为内存配置（测试用）")
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG 日志")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("serve", help="启动网关")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.add_argument("--wait-upstream", type=int, default=0, help="等待上游就绪的秒数，0=不等")
    sp.add_argument("--ignore-upstream", action="store_true", help="上游不可用也启动")

    sp = sub.add_parser("user", help="用户管理")
    s2 = sp.add_subparsers(dest="user_cmd", required=True)
    a = s2.add_parser("add")
    a.add_argument("name")
    a.add_argument("--rpm", type=int, default=60)
    a.add_argument("--tpm", type=int, default=200000)
    a.add_argument("--daily", type=int, default=2000000, help="每日 token 上限，0=不限")
    a.add_argument("--models", help="逗号分隔白名单，all=不限")
    a.add_argument("--note")
    a.add_argument("--key", action="store_true", help="同时生成一个 Key")
    a.add_argument("--key-label", help="Key 备注")
    a.add_argument("--ttl", type=int, help="Key 有效期天数")
    s2.add_parser("list")
    a = s2.add_parser("set")
    a.add_argument("name")
    a.add_argument("--rpm", type=int)
    a.add_argument("--tpm", type=int)
    a.add_argument("--daily", type=int)
    a.add_argument("--models")
    a.add_argument("--note")
    for c in ("enable", "disable", "rm"):
        a = s2.add_parser(c)
        a.add_argument("name")

    sp = sub.add_parser("key", help="Key 管理")
    s2 = sp.add_subparsers(dest="key_cmd", required=True)
    a = s2.add_parser("add")
    a.add_argument("--user", required=True)
    a.add_argument("--label")
    a.add_argument("--ttl", type=int)
    a = s2.add_parser("list")
    a.add_argument("--user")
    a = s2.add_parser("rm")
    a.add_argument("prefix", help="Key 前缀（前 10 位即可）")

    sp = sub.add_parser("usage", help="用量报表")
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--user")

    sp = sub.add_parser("whoami", help="用 Key 反查用户")
    sp.add_argument("key")

    sub.add_parser("check", help="自检")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = Config(
        upstream=args.upstream,
        upstream_key=args.upstream_key,
        db_path=args.db_path,
        auth_mode=args.auth_mode,
        dev_json=args.dev_json,
    )
    if args.cmd == "serve":
        return cli_serve(args, cfg)
    if args.cmd == "user":
        return cli_user(args, cfg)
    if args.cmd == "key":
        return cli_key(args, cfg)
    if args.cmd == "usage":
        return cli_usage(args, cfg)
    if args.cmd == "whoami":
        return cli_whoami(args, cfg)
    if args.cmd == "check":
        return cli_check(args, cfg)
    build_parser().print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main() or 0)
