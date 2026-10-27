#!/usr/bin/env python3
"""persona-bot MCP server: QQ (NapCat/OneBot11) + WeChat (ADB) persona companion.

Pure Python stdlib. Two modes:
  python persona_mcp.py              MCP stdio server (also starts a local
                                     event collector thread, best effort)
  python persona_mcp.py --collector  standalone collector daemon, catches
                                     NapCat HTTP events 24/7 into SQLite

Storage lives in PERSONA_BOT_DATA_DIR or ~/.persona-bot (persona.db, config.json, media/).
"""
import base64
import json
import os
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.6.0"
DATA_DIR = os.environ.get("PERSONA_BOT_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".persona-bot")
DB_PATH = os.path.join(DATA_DIR, "persona.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
MEDIA_DIR = os.path.join(DATA_DIR, "media")

LOCK = threading.RLock()
CONN = None

DEFAULT_CONFIG = {
    "qq": {"enabled": False, "napcat_base_url": "http://127.0.0.1:3000", "napcat_token": ""},
    "humanize": {"reply_delay_min_ms": 1200, "reply_delay_max_ms": 6000,
                 "max_per_chat_per_hour": 12, "outbox_ttl_min": 30},
    "proactive": {"enabled": False, "quiet_hours": "23:30-07:30", "max_per_day": 5,
                  "reply_probability_group": 0.3, "topic_reply": False, "topics": []},
    "admin": {"qq": [], "wechat": []},
    "memory": {"embed_base_url": "", "embed_api_key": "", "embed_model": "embedding-3",
               "half_life_days": 30},
    "mood": {"current": 0, "reason": ""},
    "video": {"ffmpeg_path": "ffmpeg"},
    "stickers": {"auto_collect": True},
    "wechat": {"enabled": False, "adb_path": "adb", "device_serial": "", "input_tap_x": 0,
               "input_tap_y": 0, "human_trace": True},
    "collector": {"host": "127.0.0.1", "port": 3001, "token": ""},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS personas(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
  prompt TEXT NOT NULL, style TEXT DEFAULT '', is_active INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS memories(
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT DEFAULT '', chat_id TEXT DEFAULT '',
  contact_id TEXT DEFAULT '', kind TEXT DEFAULT 'event', importance INTEGER DEFAULT 3,
  content TEXT NOT NULL, embedding TEXT DEFAULT '', emotion INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS profiles(
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, contact_id TEXT NOT NULL,
  display_name TEXT DEFAULT '', impression TEXT DEFAULT '', style_notes TEXT DEFAULT '',
  affinity INTEGER DEFAULT 0, updated_at TEXT, UNIQUE(channel, contact_id));
CREATE TABLE IF NOT EXISTS rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT, list_type TEXT NOT NULL, channel TEXT DEFAULT '*',
  scope TEXT DEFAULT 'contact', target_id TEXT NOT NULL, note TEXT DEFAULT '',
  created_at TEXT, UNIQUE(list_type, channel, scope, target_id));
CREATE TABLE IF NOT EXISTS stickers(
  id INTEGER PRIMARY KEY AUTOINCREMENT, file_path TEXT NOT NULL, emotion TEXT DEFAULT '',
  tags TEXT DEFAULT '', source TEXT DEFAULT '', source_url TEXT DEFAULT '',
  use_count INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, direction TEXT DEFAULT 'in', channel TEXT DEFAULT '',
  chat_type TEXT DEFAULT '', chat_id TEXT DEFAULT '', contact_id TEXT DEFAULT '',
  display_name TEXT DEFAULT '', msg_type TEXT DEFAULT 'text', content TEXT DEFAULT '',
  media_url TEXT DEFAULT '', media_path TEXT DEFAULT '', media_json TEXT DEFAULT '[]',
  processed INTEGER DEFAULT 0, out_kind TEXT DEFAULT 'reply', created_at TEXT);
CREATE TABLE IF NOT EXISTS counters(
  chat_key TEXT PRIMARY KEY, window_start REAL, count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT DEFAULT 'qq', chat_type TEXT DEFAULT 'private',
  chat_id TEXT DEFAULT '', contact_id TEXT DEFAULT '', text TEXT DEFAULT '', image_path TEXT DEFAULT '',
  out_kind TEXT DEFAULT 'reply', tries INTEGER DEFAULT 0, last_error TEXT DEFAULT '', created_at TEXT);
"""


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _migrate():
    for table, col, decl in (("memories", "embedding", "TEXT DEFAULT ''"),
                             ("memories", "emotion", "INTEGER DEFAULT 0"),
                             ("stickers", "source_url", "TEXT DEFAULT ''")):
        cols = {r[1] for r in CONN.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            CONN.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db():
    global CONN
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(MEDIA_DIR, exist_ok=True)
    CONN = sqlite3.connect(DB_PATH, check_same_thread=False)
    CONN.row_factory = sqlite3.Row
    with LOCK:
        CONN.executescript(SCHEMA)
        _migrate()
        CONN.commit()


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                stored = json.load(f)
            for k, v in stored.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- access rules

def access_decision(channel, chat_type, chat_id, contact_id):
    """Blacklist denies; if any whitelist entry exists for the channel, only
    whitelisted targets pass. Returns {"allowed": bool, "reason": str}."""
    with LOCK:
        def hit(list_type, scope, target):
            if target is None:
                return None
            return CONN.execute(
                "SELECT note FROM rules WHERE list_type=? AND channel IN (?, '*') AND scope=? AND target_id=?",
                (list_type, channel, scope, str(target)),
            ).fetchone()

        for scope, target in (("global", "*"), ("contact", contact_id),
                              ("group", chat_id if chat_type == "group" else None)):
            row = hit("blacklist", scope, target)
            if row:
                return {"allowed": False, "reason": f"黑名单拦截({scope})", "note": row["note"] or ""}
        wl = CONN.execute(
            "SELECT scope, target_id FROM rules WHERE list_type='whitelist' AND channel IN (?, '*')",
            (channel,)).fetchall()
        if wl:
            ok = any(s == "global"
                     or (s == "contact" and t == str(contact_id))
                     or (s == "group" and chat_type == "group" and t == str(chat_id))
                     for s, t in wl)
            if not ok:
                return {"allowed": False, "reason": "不在白名单内"}
    return {"allowed": True, "reason": ""}


# ---------------------------------------------------------------- humanize

def rate_limit_check(channel, chat_id):
    cfg = load_config()["humanize"]
    mx = int(cfg.get("max_per_chat_per_hour", 12))
    key = f"{channel}:{chat_id}"
    with LOCK:
        row = CONN.execute("SELECT window_start, count FROM counters WHERE chat_key=?", (key,)).fetchone()
        t = time.time()
        if not row or t - float(row["window_start"]) > 3600:
            CONN.execute("INSERT OR REPLACE INTO counters VALUES(?,?,1)", (key, t))
        elif int(row["count"]) >= mx:
            CONN.commit()
            return False, f"该会话每小时限频 {mx} 条已满，本次发送被拦截"
        else:
            CONN.execute("UPDATE counters SET count=count+1 WHERE chat_key=?", (key,))
        CONN.commit()
    return True, ""


def human_delay():
    cfg = load_config()["humanize"]
    lo = max(0, int(cfg.get("reply_delay_min_ms", 1200)))
    hi = max(lo, int(cfg.get("reply_delay_max_ms", 6000)))
    time.sleep(random.uniform(lo, hi) / 1000.0)


def in_quiet_hours(spec, now=None):
    if not spec:
        return False
    try:
        a, b = spec.split("-")
        ah, am = map(int, a.split(":"))
        bh, bm = map(int, b.split(":"))
        t = now.hour * 60 + now.minute if now else datetime.now().hour * 60 + datetime.now().minute
        s, e = ah * 60 + am, bh * 60 + bm
        return (t >= s or t < e) if s >= e else (s <= t < e)
    except Exception:
        return False


# ---------------------------------------------------------------- embeddings

def embed_text(text):
    cfg = load_config().get("memory", {})
    base = (cfg.get("embed_base_url") or "").strip()
    if not base:
        return None
    headers = {"Content-Type": "application/json"}
    if cfg.get("embed_api_key"):
        headers["Authorization"] = "Bearer " + cfg["embed_api_key"]
    body = {"model": cfg.get("embed_model", "embedding-3"), "input": text}
    req = urllib.request.Request(base.rstrip("/") + "/embeddings",
                                 data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [float(x) for x in data["data"][0]["embedding"]]
    except Exception:
        return None


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _parse_dt(s):
    try:
        return time.mktime(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timetuple())
    except Exception:
        return time.mktime(datetime.now().timetuple())


def _memory_score(row, now_ts, half_life_days):
    """重要级+情绪强度，随时间指数衰减（遗忘曲线）：强情绪记忆衰减更慢。"""
    age_days = max(0.0, (now_ts - _parse_dt(row["created_at"])) / 86400.0)
    decay = 0.5 ** (age_days / max(1.0, float(half_life_days)))
    return (int(row["importance"]) + abs(int(row.get("emotion") or 0))) * decay


# ---------------------------------------------------------------- NapCat (QQ)

def napcat_post(action, params):
    cfg = load_config()["qq"]
    if not cfg.get("enabled"):
        raise RuntimeError("QQ(NapCat) 渠道未启用，请先调用 configure_channel 并在 NapCat 配置 HTTP 服务")
    url = cfg["napcat_base_url"].rstrip("/") + "/" + action
    headers = {"Content-Type": "application/json"}
    if cfg.get("napcat_token"):
        headers["Authorization"] = "Bearer " + cfg["napcat_token"]
    req = urllib.request.Request(url, data=json.dumps(params).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("retcode") not in (0, 1) or body.get("status") not in ("ok", "async", None):
        raise RuntimeError("NapCat 调用失败: " + json.dumps(body, ensure_ascii=False)[:200])
    return body


def to_file_uri(path):
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file://" + p


# ---------------------------------------------------------------- ADB (WeChat)

def run_adb(args, timeout=30):
    adb = load_config()["wechat"].get("adb_path", "adb")
    if not shutil.which(adb) and os.path.sep not in adb:
        raise RuntimeError(f"找不到 adb（{adb}）。请安装 platform-tools 并在 configure_channel 里填 adb_path")
    exe = adb if os.path.isabs(adb) or os.path.sep in adb else shutil.which(adb)
    return subprocess.run([exe] + args, capture_output=True, timeout=timeout)


def resolve_device():
    serial = load_config()["wechat"].get("device_serial", "")
    if serial:
        return serial
    r = run_adb(["devices"])
    for line in r.stdout.decode("utf-8", "replace").splitlines()[1:]:
        line = line.strip()
        if line.endswith("\tdevice"):
            return line.split("\t")[0]
    raise RuntimeError("没有已连接的 ADB 设备。请先连接模拟器或 USB 手机并运行 adb devices 确认")


# ---------------------------------------------------------------- OneBot events

CQ_RE = re.compile(r"\[CQ:([a-zA-Z0-9_.-]+)((?:,[^\],]*)*)\]")
CQ_UNESCAPE = [("&#44;", ","), ("&#91;", "["), ("&#93;", "]"), ("&amp;", "&")]


def cq_params(raw):
    out = {}
    for kv in raw.split(",")[1:]:
        if "=" in kv:
            k, v = kv.split("=", 1)
            for a, b in CQ_UNESCAPE:
                v = v.replace(a, b)
            out[k] = v
    return out


def normalize_segments(segments):
    """-> (text, media_urls, types)"""
    text_parts, urls, types = [], [], []
    for seg in segments:
        stype = seg.get("type", "")
        data = seg.get("data") or {}
        types.append(stype)
        if stype == "text":
            text_parts.append(data.get("text", ""))
        elif stype == "image":
            urls.append(data.get("url") or data.get("file") or "")
        elif stype == "face":
            text_parts.append(f"[表情:{data.get('id', '')}]")
        elif stype == "at":
            text_parts.append(f"[at:{data.get('qq', '')}]")
        elif stype in ("record", "video", "file"):
            urls.append(data.get("url") or data.get("file") or "")
    return "".join(text_parts), [u for u in urls if u], types


def parse_cq_message(raw):
    segments, pos = [], 0
    for m in CQ_RE.finditer(raw or ""):
        if raw[pos:m.start()]:
            segments.append({"type": "text", "data": {"text": raw[pos:m.start()]}})
        params = cq_params("," + m.group(2))
        data = dict(params)
        if m.group(1) == "image":
            data.setdefault("url", params.get("url", params.get("file", "")))
        segments.append({"type": m.group(1), "data": data})
        pos = m.end()
    if raw and raw[pos:]:
        segments.append({"type": "text", "data": {"text": raw[pos:]}})
    return segments


def store_inbound(ev):
    mtype = ev.get("message_type") or ("group" if ev.get("group_id") else "private")
    chat_type = "group" if mtype == "group" else "private"
    chat_id = str(ev.get("group_id") or ev.get("user_id") or "")
    contact_id = str(ev.get("user_id") or "")
    sender = ev.get("sender") or {}
    display = sender.get("card") or sender.get("nickname") or ""
    raw = ev.get("message")
    if isinstance(raw, list):
        segments = raw
    elif isinstance(raw, str):
        segments = parse_cq_message(raw)
    else:
        segments = []
    text, urls, types = normalize_segments(segments)
    if not text and not urls:
        return
    if not text and ev.get("raw_message"):
        text = ev["raw_message"]
    msg_type = "text" if types and set(types) <= {"text", "at", "face"} else ("image" if "image" in types and not text else "mixed")
    with LOCK:
        CONN.execute(
            "INSERT INTO messages(direction, channel, chat_type, chat_id, contact_id, display_name,"
            " msg_type, content, media_url, media_json, processed, created_at)"
            " VALUES('in','qq',?,?,?,?,?,?,?,?,0,?)",
            (chat_type, chat_id, contact_id, display, msg_type, text, "|".join(urls),
             json.dumps(segments, ensure_ascii=False), now_str()))
        if display:
            CONN.execute(
                "INSERT INTO profiles(channel, contact_id, display_name, updated_at) VALUES('qq',?,?,?)"
                " ON CONFLICT(channel, contact_id) DO UPDATE SET display_name=excluded.display_name,"
                " updated_at=excluded.updated_at WHERE profiles.display_name='' OR profiles.display_name IS NULL",
                (contact_id, display, now_str()))
        CONN.commit()


def store_poke(ev):
    operator = str(ev.get("user_id") or "")
    if not operator:
        return
    target = str(ev.get("target_id") or "")
    chat_type = "group" if ev.get("group_id") else "private"
    chat_id = str(ev.get("group_id") or operator)
    with LOCK:
        CONN.execute(
            "INSERT INTO messages(direction, channel, chat_type, chat_id, contact_id, msg_type,"
            " content, media_json, processed, created_at)"
            " VALUES('in','qq',?,?,'','poke',?,'[]',0,?)",
            (chat_type, chat_id, f"[戳一戳] {operator} 戳了 {target or '我'}", now_str()))
        CONN.commit()


class EventHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/onebot/event":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        token = load_config()["collector"].get("token", "")
        if token:
            auth = self.headers.get("Authorization", "")
            if token not in (auth, auth.replace("Bearer ", "")):
                self.send_response(403)
                self.end_headers()
                return
        try:
            ev = json.loads(body.decode("utf-8"))
            if ev.get("post_type") == "message":
                store_inbound(ev)
            elif ev.get("post_type") == "notice" and (
                    ev.get("sub_type") == "poke" or "poke" in str(ev.get("notice_type", ""))):
                store_poke(ev)
        except Exception:
            pass
        self.send_response(204)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


COLLECTOR_SERVER = None


def start_collector_thread():
    global COLLECTOR_SERVER
    cfg = load_config()["collector"]
    host, port = cfg.get("host", "127.0.0.1"), int(cfg.get("port", 3001))
    try:
        COLLECTOR_SERVER = ThreadingHTTPServer((host, port), EventHandler)
        t = threading.Thread(target=COLLECTOR_SERVER.serve_forever, daemon=True)
        t.start()
        print(f"[glm-foxgirl] collector listening on http://{host}:{port}/onebot/event", file=sys.stderr)
    except OSError:
        print(f"[glm-foxgirl] port {port} busy, assuming an external collector is running", file=sys.stderr)


def collector_running():
    cfg = load_config()["collector"]
    try:
        with socket.create_connection((cfg.get("host", "127.0.0.1"), int(cfg.get("port", 3001))), timeout=0.5):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- tool handlers

def t_bot_status(**_):
    cfg = load_config()
    with LOCK:
        def one(sql):
            return CONN.execute(sql).fetchone()[0]
        active = CONN.execute("SELECT name FROM personas WHERE is_active=1").fetchone()
        pending = one("SELECT COUNT(*) FROM messages WHERE direction='in' AND processed=0")
        status = {
            "version": VERSION, "data_dir": DATA_DIR, "db": DB_PATH,
            "active_persona": active["name"] if active else None,
            "counts": {"personas": one("SELECT COUNT(*) FROM personas"),
                       "pending_messages": pending, "memories": one("SELECT COUNT(*) FROM memories"),
                       "profiles": one("SELECT COUNT(*) FROM profiles"),
                       "rules": one("SELECT COUNT(*) FROM rules"),
                       "stickers": one("SELECT COUNT(*) FROM stickers"),
                       "outbox": one("SELECT COUNT(*) FROM outbox")},
            "intro_shown": bool(load_config().get("intro", {}).get("shown")),
            "qq": {"enabled": cfg["qq"]["enabled"], "napcat_base_url": cfg["qq"]["napcat_base_url"],
                   "token_set": bool(cfg["qq"]["napcat_token"])},
            "wechat": {"enabled": cfg["wechat"]["enabled"], "adb_path": cfg["wechat"]["adb_path"],
                       "adb_found": bool(shutil.which(cfg["wechat"]["adb_path"])) or os.path.sep in cfg["wechat"]["adb_path"],
                       "device_serial": cfg["wechat"]["device_serial"] or "(自动检测)"},
            "memory_embed": bool(load_config().get("memory", {}).get("embed_base_url")),
            "ffmpeg": bool(shutil.which(load_config().get("video", {}).get("ffmpeg_path", "ffmpeg"))),
            "collector": {"port": cfg["collector"]["port"], "running": collector_running(),
                          "url": f"http://{cfg['collector']['host']}:{cfg['collector']['port']}/onebot/event"},
        }
    return status


def t_configure_channel(channel, enabled=None, napcat_base_url=None, napcat_token=None,
                        adb_path=None, device_serial=None, collector_port=None, **_):
    if channel not in ("qq", "wechat"):
        raise ValueError("channel 必须是 qq 或 wechat")
    cfg = load_config()
    if channel == "qq":
        if enabled is not None:
            cfg["qq"]["enabled"] = bool(enabled)
        if napcat_base_url:
            cfg["qq"]["napcat_base_url"] = napcat_base_url
        if napcat_token is not None:
            cfg["qq"]["napcat_token"] = napcat_token
    else:
        if enabled is not None:
            cfg["wechat"]["enabled"] = bool(enabled)
        if adb_path:
            cfg["wechat"]["adb_path"] = adb_path
        if device_serial is not None:
            cfg["wechat"]["device_serial"] = device_serial
    if collector_port:
        cfg["collector"]["port"] = int(collector_port)
    save_config(cfg)
    out = dict(cfg)
    out["_note"] = "token 已脱敏；修改渠道配置后若 collector 端口变化需重启 MCP/collector"
    if channel == "qq":
        out["qq"] = {**cfg["qq"], "napcat_token": "***" if cfg["qq"]["napcat_token"] else ""}
    return out


def t_get_config(**_):
    cfg = load_config()
    cfg["qq"]["napcat_token"] = "***" if cfg["qq"]["napcat_token"] else ""
    cfg["_data_dir"] = DATA_DIR
    return cfg


def t_set_config(key, value, **_):
    if not re.fullmatch(r"[a-zA-Z0-9_.]+", key or ""):
        raise ValueError("key 只能是字母数字下划线点，如 humanize.reply_delay_min_ms")
    if isinstance(value, str):
        low = value.lower()
        if low in ("true", "false"):
            value = low == "true"
        else:
            try:
                value = int(value)
            except ValueError:
                try:
                    value = float(value)
                except ValueError:
                    pass
    cfg = load_config()
    node = cfg
    parts = key.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
    save_config(cfg)
    return {"ok": True, "key": key, "value": value}


def t_get_pending_messages(channel=None, chat_id=None, limit=20, **_):
    q = ("SELECT m.*, p.impression AS profile_impression, p.affinity AS profile_affinity,"
         " p.style_notes AS profile_style_notes FROM messages m"
         " LEFT JOIN profiles p ON p.channel=m.channel AND p.contact_id=m.contact_id"
         " WHERE m.direction='in' AND m.processed=0")
    args = []
    if channel:
        q += " AND m.channel=?"
        args.append(channel)
    if chat_id:
        q += " AND m.chat_id=?"
        args.append(str(chat_id))
    q += " ORDER BY m.id LIMIT ?"
    args.append(max(1, min(int(limit), 100)))
    with LOCK:
        rows = [dict(r) for r in CONN.execute(q, args).fetchall()]
    return {"count": len(rows), "messages": rows}


def t_mark_processed(message_ids, **_):
    ids = [int(i) for i in message_ids]
    with LOCK:
        CONN.executemany("UPDATE messages SET processed=1 WHERE id=?", [(i,) for i in ids])
        CONN.commit()
    return {"ok": True, "marked": ids}


def t_simulate_inbound(channel, chat_type, chat_id, contact_id, display_name="", text="", media_url="", **_):
    if channel not in ("qq", "wechat"):
        raise ValueError("channel 必须是 qq 或 wechat")
    msg_type = "image" if media_url and not text else "text"
    with LOCK:
        cur = CONN.execute(
            "INSERT INTO messages(direction, channel, chat_type, chat_id, contact_id, display_name,"
            " msg_type, content, media_url, processed, created_at)"
            " VALUES('in',?,?,?,?,?,?,?,?,0,?)",
            (channel, chat_type, str(chat_id), str(contact_id), display_name, msg_type, text, media_url, now_str()))
        CONN.commit()
        return {"ok": True, "message_id": cur.lastrowid}


def t_check_access(channel, chat_type, chat_id, contact_id, **_):
    return access_decision(channel, chat_type, str(chat_id), str(contact_id) if contact_id else None)


def t_send_qq_message(chat_type, chat_id, text="", image_path=None, contact_id=None, out_kind="reply", **_):
    if chat_type not in ("private", "group"):
        raise ValueError("chat_type 必须是 private 或 group")
    if not text and not image_path:
        raise ValueError("text 与 image_path 至少给一个")
    decision = access_decision("qq", chat_type, str(chat_id), str(contact_id) if contact_id else str(chat_id) if chat_type == "private" else None)
    if not decision["allowed"]:
        return {"sent": False, "blocked": True, **decision}
    ok, why = rate_limit_check("qq", chat_id)
    if not ok:
        return {"sent": False, "blocked": True, "reason": why}
    human_delay()
    message = []
    if text:
        message.append({"type": "text", "data": {"text": text}})
    if image_path:
        message.append({"type": "image", "data": {"file": to_file_uri(image_path)}})
    action = "send_private_msg" if chat_type == "private" else "send_group_msg"
    param = {"user_id": int(chat_id)} if chat_type == "private" else {"group_id": int(chat_id)}
    param["message"] = message
    try:
        resp = napcat_post(action, param)
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        with LOCK:
            CONN.execute(
                "INSERT INTO outbox(channel, chat_type, chat_id, contact_id, text, image_path,"
                " out_kind, tries, last_error, created_at) VALUES('qq',?,?,?,?,?,?,1,?,?)",
                (chat_type, str(chat_id), str(contact_id or ""), text, image_path or "",
                 out_kind, str(e)[:200], now_str()))
            CONN.commit()
        return {"sent": False, "queued": True,
                "reason": "NapCat 暂时不可达，已入发送队列（心跳时 flush_outbox 自动补发）",
                "detail": str(e)[:120]}
    with LOCK:
        CONN.execute(
            "INSERT INTO messages(direction, channel, chat_type, chat_id, contact_id, msg_type, content,"
            " media_path, out_kind, processed, created_at) VALUES('out','qq',?,?,?,?,?,?,?,1,?)",
            (chat_type, str(chat_id), str(contact_id or ""), "text" if text else "image",
             text, image_path or "", out_kind, now_str()))
        CONN.commit()
    return {"sent": True, "napcat_status": resp.get("status"), "message": text[:50]}


def t_send_wechat_message(text, **_):
    if not text:
        raise ValueError("text 不能为空")
    serial = resolve_device()
    ok, why = rate_limit_check("wechat", "active")
    if not ok:
        return {"sent": False, "blocked": True, "reason": why}
    human_delay()
    ime = run_adb(["-s", serial, "shell", "ime", "list", "-s"]).stdout.decode("utf-8", "replace")
    if "com.android.adbkeyboard" in ime:
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        run_adb(["-s", serial, "shell", "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", b64])
        time.sleep(0.6)
        run_adb(["-s", serial, "shell", "input", "keyevent", "66"])
    else:
        try:
            text.encode("ascii")
        except UnicodeEncodeError:
            raise RuntimeError("微信中文输入需要 ADBKeyboard 输入法（见 wechat-adb-setup 技能），当前设备未安装")
        run_adb(["-s", serial, "shell", "input", "text", text.replace(" ", "%s")])
        time.sleep(0.4)
        run_adb(["-s", serial, "shell", "input", "keyevent", "66"])
    with LOCK:
        CONN.execute(
            "INSERT INTO messages(direction, channel, chat_type, chat_id, msg_type, content,"
            " out_kind, processed, created_at) VALUES('out','wechat','private','active','text',?,'reply',1,?)",
            (text, now_str()))
        CONN.commit()
    return {"sent": True, "device": serial, "method": "adbkeyboard" if "com.android.adbkeyboard" in ime else "input-text"}


def t_wechat_screenshot(**_):
    serial = resolve_device()
    out = os.path.join(MEDIA_DIR, f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
    r = run_adb(["-s", serial, "exec-out", "screencap", "-p"], timeout=60)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("截图失败: " + r.stderr.decode("utf-8", "replace")[:200])
    with open(out, "wb") as f:
        f.write(r.stdout)
    return {"path": out, "device": serial, "hint": "用 Read 工具查看该 PNG 即可'看见'当前微信屏幕"}


def t_download_media(url, **_):
    if not url or not url.startswith("http"):
        raise ValueError("需要 http(s) 图片/视频 URL")
    ext = ".jpg"
    m = re.search(r"\.(jpe?g|png|gif|mp4)(?:[?#]|$)", url, re.I)
    if m:
        ext = "." + m.group(1).lower()
    out = os.path.join(MEDIA_DIR, f"media_{int(time.time()*1000)}{ext}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(out, "wb") as f:
        f.write(resp.read())
    return {"path": out}


def t_manage_persona(action, name=None, prompt=None, style=None, persona_id=None, file_path=None, **_):
    with LOCK:
        if action == "list":
            rows = [dict(r) for r in CONN.execute("SELECT * FROM personas ORDER BY is_active DESC, id")]
            return {"personas": rows, "active": next((r["name"] for r in rows if r["is_active"]), None)}
        if action == "create":
            if not name or not prompt:
                raise ValueError("create 需要 name 和 prompt")
            cur = CONN.execute(
                "INSERT INTO personas(name, prompt, style, is_active, created_at, updated_at) VALUES(?,?,?,0,?,?)",
                (name, prompt, style or "", now_str(), now_str()))
            CONN.commit()
            return {"ok": True, "persona_id": cur.lastrowid,
                    "hint": "调用 manage_persona action=set_active 启用"}
        if action == "update":
            if not (persona_id or name):
                raise ValueError("update 需要 persona_id 或 name 定位")
            row = CONN.execute("SELECT id FROM personas WHERE id=? OR name=?", (persona_id or -1, name or "")).fetchone()
            if not row:
                raise ValueError("人格不存在")
            sets, args = ["updated_at=?"], [now_str()]
            if prompt is not None:
                sets.append("prompt=?"); args.append(prompt)
            if style is not None:
                sets.append("style=?"); args.append(style)
            args.append(row["id"])
            CONN.execute(f"UPDATE personas SET {', '.join(sets)} WHERE id=?", args)
            CONN.commit()
            return {"ok": True}
        if action == "set_active":
            row = CONN.execute("SELECT id FROM personas WHERE id=? OR name=?", (persona_id or -1, name or "")).fetchone()
            if not row:
                raise ValueError("人格不存在")
            CONN.execute("UPDATE personas SET is_active=0")
            CONN.execute("UPDATE personas SET is_active=1 WHERE id=?", (row["id"],))
            CONN.commit()
            return {"ok": True}
        if action == "delete":
            if not (persona_id or name):
                raise ValueError("delete 需要 persona_id 或 name")
            CONN.execute("DELETE FROM personas WHERE id=? OR name=?", (persona_id or -1, name or ""))
            CONN.commit()
            return {"ok": True}
        if action == "export":
            row = CONN.execute("SELECT * FROM personas WHERE id=? OR name=?",
                               (persona_id or -1, name or "")).fetchone()
            if not row:
                raise ValueError("人格不存在")
            pdir = os.path.join(DATA_DIR, "personas")
            os.makedirs(pdir, exist_ok=True)
            safe = re.sub(r'[\\/:*?"<>|]+', "_", row["name"]).strip("_") or "persona"
            path = os.path.join(pdir, safe + ".json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"name": row["name"], "prompt": row["prompt"], "style": row["style"]},
                          f, ensure_ascii=False, indent=2)
            return {"ok": True, "file": path,
                    "persona": {"name": row["name"], "prompt": row["prompt"], "style": row["style"]}}
        if action == "import":
            if not file_path:
                raise ValueError("import 需要 file_path（export 生成的 JSON 文件）")
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
            pname, pprompt = str(data.get("name") or "imported").strip(), str(data.get("prompt") or "")
            if not pprompt:
                raise ValueError("导入文件缺少 prompt")
            base, n = pname, 1
            while CONN.execute("SELECT 1 FROM personas WHERE name=?", (pname,)).fetchone():
                n += 1
                pname = f"{base}_{n}"
            cur = CONN.execute(
                "INSERT INTO personas(name, prompt, style, is_active, created_at, updated_at) VALUES(?,?,?,0,?,?)",
                (pname, pprompt, str(data.get("style") or ""), now_str(), now_str()))
            CONN.commit()
            return {"ok": True, "persona_id": cur.lastrowid, "name": pname,
                    "hint": "调用 manage_persona action=set_active 启用"}
    raise ValueError(f"未知 action: {action}")


def t_store_memory(content, channel="", chat_id="", contact_id="", kind="event", importance=3, emotion=0, **_):
    if not content:
        raise ValueError("content 不能为空")
    importance = max(1, min(5, int(importance)))
    emo = max(-3, min(3, int(emotion)))
    vec = embed_text(content)
    with LOCK:
        cur = CONN.execute(
            "INSERT INTO memories(channel, chat_id, contact_id, kind, importance, content, embedding, emotion, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (channel or "", str(chat_id or ""), str(contact_id or ""), kind, importance, content,
             json.dumps(vec) if vec else "", emo, now_str()))
        CONN.commit()
        return {"ok": True, "memory_id": cur.lastrowid, "embedded": bool(vec)}


def t_query_memory(keyword=None, contact_id=None, chat_id=None, channel=None, limit=10, semantic=False, **_):
    q, args = "SELECT * FROM memories WHERE 1=1", []
    if keyword and not semantic:
        q += " AND content LIKE ?"
        args.append(f"%{keyword}%")
    if contact_id:
        q += " AND contact_id=?"; args.append(str(contact_id))
    if chat_id:
        q += " AND chat_id=?"; args.append(str(chat_id))
    if channel:
        q += " AND channel=?"; args.append(channel)
    cap = 200 if semantic else max(1, min(int(limit), 50))
    q += " ORDER BY importance DESC, id DESC LIMIT ?"
    args.append(cap)
    with LOCK:
        rows = [dict(r) for r in CONN.execute(q, args).fetchall()]
    now_ts = time.mktime(datetime.now().timetuple())
    half = float(load_config().get("memory", {}).get("half_life_days", 30))
    if semantic and keyword:
        qv = embed_text(keyword)
        if qv:
            scored = []
            for r in rows:
                if not r.get("embedding"):
                    continue
                try:
                    scored.append((r, _cosine(qv, json.loads(r["embedding"]))))
                except Exception:
                    continue
            if scored:
                scored.sort(key=lambda t: -t[1])
                rows = [{**r, "similarity": round(s, 4), "score": round(_memory_score(r, now_ts, half), 4)}
                        for r, s in scored]
    if rows and "score" not in rows[0]:
        for r in rows:
            r["score"] = round(_memory_score(r, now_ts, half), 4)
        rows.sort(key=lambda x: (-x["score"], -int(x["id"])))
    rows = rows[:max(1, min(int(limit), 50))]
    return {"count": len(rows), "memories": rows}


def t_manage_profile(action, channel="", contact_id="", display_name=None, impression=None,
                     style_notes=None, affinity_delta=0, **_):
    with LOCK:
        if action == "list":
            rows = [dict(r) for r in CONN.execute("SELECT * FROM profiles ORDER BY affinity DESC")]
            return {"profiles": rows}
        if action in ("get", "upsert"):
            if not channel or not contact_id:
                raise ValueError("需要 channel 和 contact_id")
            row = CONN.execute("SELECT * FROM profiles WHERE channel=? AND contact_id=?",
                               (channel, str(contact_id))).fetchone()
            if action == "get":
                return {"profile": dict(row) if row else None}
            if row:
                sets, args = ["updated_at=?"], [now_str()]
                if display_name:
                    sets.append("display_name=?"); args.append(display_name)
                if impression is not None:
                    sets.append("impression=?"); args.append(impression)
                if style_notes is not None:
                    sets.append("style_notes=?"); args.append(style_notes)
                if affinity_delta:
                    sets.append("affinity=MAX(-100, MIN(100, affinity+?))"); args.append(int(affinity_delta))
                args.append(row["id"])
                CONN.execute(f"UPDATE profiles SET {', '.join(sets)} WHERE id=?", args)
            else:
                CONN.execute(
                    "INSERT INTO profiles(channel, contact_id, display_name, impression, style_notes, affinity, updated_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (channel, str(contact_id), display_name or "", impression or "", style_notes or "",
                     max(-100, min(100, int(affinity_delta or 0))), now_str()))
            CONN.commit()
            row = CONN.execute("SELECT * FROM profiles WHERE channel=? AND contact_id=?",
                               (channel, str(contact_id))).fetchone()
            return {"ok": True, "profile": dict(row)}
    raise ValueError(f"未知 action: {action}")


def t_manage_rules(action, list_type=None, scope="contact", channel="*", target_id=None, note="", **_):
    if action == "list":
        with LOCK:
            rows = [dict(r) for r in CONN.execute("SELECT * FROM rules ORDER BY list_type, id")]
        return {"rules": rows}
    if action == "add":
        if list_type not in ("blacklist", "whitelist") or not target_id:
            raise ValueError("add 需要 list_type(blacklist/whitelist) 和 target_id")
        if scope not in ("contact", "group", "global"):
            raise ValueError("scope 必须是 contact/group/global")
        if scope == "global":
            target_id, channel = "*", "*"
        with LOCK:
            CONN.execute(
                "INSERT OR IGNORE INTO rules(list_type, channel, scope, target_id, note, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (list_type, channel, scope, str(target_id), note, now_str()))
            CONN.commit()
        return {"ok": True}
    if action == "remove":
        if not target_id:
            raise ValueError("remove 需要 target_id")
        with LOCK:
            cur = CONN.execute(
                "DELETE FROM rules WHERE target_id=? AND list_type IS ? AND scope IS ? AND channel IS ?",
                (str(target_id), list_type, scope, channel))
            CONN.commit()
        return {"ok": True, "removed": cur.rowcount}
    raise ValueError(f"未知 action: {action}")


def t_manage_stickers(action, file_path=None, emotion="", tags="", source="", source_url=None, sticker_id=None, **_):
    with LOCK:
        if action == "add":
            if not file_path or not os.path.exists(file_path):
                raise ValueError(f"file_path 不存在: {file_path}")
            if source_url:
                dup = CONN.execute("SELECT id FROM stickers WHERE source_url=?", (source_url,)).fetchone()
                if dup:
                    return {"ok": True, "deduped": True, "sticker_id": dup["id"]}
            cur = CONN.execute(
                "INSERT INTO stickers(file_path, emotion, tags, source, source_url, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (os.path.abspath(file_path), emotion, tags, source, source_url or "", now_str()))
            CONN.commit()
            return {"ok": True, "sticker_id": cur.lastrowid}
        if action == "list":
            q, args = "SELECT * FROM stickers WHERE 1=1", []
            if emotion:
                q += " AND (emotion LIKE ? OR tags LIKE ?)"; args += [f"%{emotion}%", f"%{emotion}%"]
            if tags:
                q += " AND tags LIKE ?"; args.append(f"%{tags}%")
            rows = [dict(r) for r in CONN.execute(q + " ORDER BY id", args)]
            return {"stickers": rows}
        if action == "delete":
            if not sticker_id:
                raise ValueError("delete 需要 sticker_id")
            CONN.execute("DELETE FROM stickers WHERE id=?", (int(sticker_id),))
            CONN.commit()
            return {"ok": True}
    raise ValueError(f"未知 action: {action}")


def t_pick_sticker(emotion="", **_):
    with LOCK:
        q, args = "SELECT * FROM stickers WHERE 1=1", []
        if emotion:
            q += " AND (emotion LIKE ? OR tags LIKE ?)"
            args += [f"%{emotion}%", f"%{emotion}%"]
        row = CONN.execute(q + " ORDER BY use_count, RANDOM() LIMIT 1", args).fetchone()
        if not row:
            return {"found": False, "hint": "库里没有匹配该情绪的表情包，可以先用 manage_stickers add 收集，或本次只发文字"}
        CONN.execute("UPDATE stickers SET use_count=use_count+1 WHERE id=?", (row["id"],))
        CONN.commit()
    return {"found": True, "sticker": dict(row)}


def t_proactive_due(**_):
    cfg = load_config()["proactive"]
    if not cfg.get("enabled"):
        return {"proactive_allowed": False, "reason": "proactive.enabled=false"}
    if in_quiet_hours(cfg.get("quiet_hours", "")):
        return {"proactive_allowed": False, "reason": f"安静时段 {cfg.get('quiet_hours')}"}
    today = datetime.now().strftime("%Y-%m-%d")
    with LOCK:
        used = CONN.execute(
            "SELECT COUNT(*) FROM messages WHERE direction='out' AND out_kind='proactive' AND created_at LIKE ?",
            (today + "%",)).fetchone()[0]
    mx = int(cfg.get("max_per_day", 5))
    if used >= mx:
        return {"proactive_allowed": False, "reason": f"今日主动消息已达上限 {mx}"}
    return {"proactive_allowed": True, "used_today": used, "max_per_day": mx,
            "group_reply_probability": cfg.get("reply_probability_group", 0.3)}


def t_poke_qq(group_id, user_id, **_):
    decision = access_decision("qq", "group", str(group_id), None)
    if not decision["allowed"]:
        return {"sent": False, "blocked": True, **decision}
    ok, why = rate_limit_check("qq", str(group_id))
    if not ok:
        return {"sent": False, "blocked": True, "reason": why}
    resp = napcat_post("group_poke", {"group_id": int(group_id), "user_id": int(user_id)})
    with LOCK:
        CONN.execute(
            "INSERT INTO messages(direction, channel, chat_type, chat_id, contact_id, msg_type,"
            " content, out_kind, processed, created_at)"
            " VALUES('out','qq','group',?,?,'poke','[戳一戳]','reply',1,?)",
            (str(group_id), str(user_id), now_str()))
        CONN.commit()
    return {"sent": True, "napcat_status": resp.get("status")}


def t_send_wechat_sticker(file_path, **_):
    if not file_path or not os.path.exists(file_path):
        raise ValueError(f"file_path 不存在: {file_path}")
    serial = resolve_device()
    cfgw = load_config()["wechat"]
    taps = [("plus_tap_x", "plus_tap_y", "聊天右下角 ⊕ 按钮"),
            ("album_tap_x", "album_tap_y", "面板中的 相册"),
            ("first_image_tap_x", "first_image_tap_y", "相册列表第一张图"),
            ("send_btn_tap_x", "send_btn_tap_y", "右下角 发送 按钮")]
    missing = [label for kx, ky, label in taps if not cfgw.get(kx) or not cfgw.get(ky)]
    if missing:
        raise ValueError(
            "微信发表情包需先校准 4 组点击坐标（缺：" + "、".join(missing) + "）。"
            "先 wechat_screenshot 截图定坐标，再 set_config 写入 wechat.plus_tap_x/plus_tap_y/"
            "album_tap_x/album_tap_y/first_image_tap_x/first_image_tap_y/"
            "send_btn_tap_x/send_btn_tap_y 共 8 项。")
    ok, why = rate_limit_check("wechat", "active")
    if not ok:
        return {"sent": False, "blocked": True, "reason": why}
    human_delay()
    remote = f"/sdcard/Pictures/glmfox_{int(time.time() * 1000)}.png"
    run_adb(["-s", serial, "push", os.path.abspath(file_path), remote], timeout=60)
    run_adb(["-s", serial, "shell", "am", "broadcast", "-a",
             "android.intent.action.MEDIA_SCANNER_SCAN_FILE", "-d", "file://" + remote])
    time.sleep(1.5)
    for kx, ky, _label in taps:
        run_adb(["-s", serial, "shell", "input", "tap", str(int(cfgw[kx])), str(int(cfgw[ky]))])
        time.sleep(random.uniform(0.8, 1.6))
    with LOCK:
        CONN.execute(
            "INSERT INTO messages(direction, channel, chat_type, chat_id, msg_type, content,"
            " media_path, out_kind, processed, created_at)"
            " VALUES('out','wechat','private','active','sticker','[表情包]',?,'reply',1,?)",
            (os.path.abspath(file_path), now_str()))
        CONN.commit()
    return {"sent": True, "device": serial,
            "note": "已按校准坐标发送；如卡在某一步，wechat_screenshot 检查并重新校准"}


def t_extract_video_frames(video_path, frames=4, **_):
    if not video_path or not os.path.exists(video_path):
        raise ValueError(f"video_path 不存在: {video_path}")
    ffmpeg = load_config().get("video", {}).get("ffmpeg_path", "ffmpeg")
    exe = shutil.which(ffmpeg) or (ffmpeg if os.path.sep in ffmpeg and os.path.exists(ffmpeg) else None)
    if not exe:
        raise RuntimeError("未找到 ffmpeg，无法抽帧。安装：winget install Gyan.FFmpeg，"
                           "或 set_config video.ffmpeg_path 指向 ffmpeg.exe 全路径")
    frames = max(2, min(int(frames), 8))
    r = None
    duration = 0.0
    try:
        r = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", video_path], capture_output=True, timeout=60)
        duration = float(r.stdout.decode("utf-8", "replace").strip() or 0)
    except Exception:
        duration = 0.0
    fdir = os.path.join(MEDIA_DIR, "frames", f"v{int(time.time() * 1000)}")
    os.makedirs(fdir, exist_ok=True)
    if duration > 0:
        cmd = [exe, "-y", "-i", video_path, "-vf", f"fps={frames / duration:.4f}",
               "-frames:v", str(frames + 2), os.path.join(fdir, "f_%02d.jpg")]
    else:
        cmd = [exe, "-y", "-i", video_path, "-vf", "fps=1",
               "-frames:v", str(frames), os.path.join(fdir, "f_%02d.jpg")]
    subprocess.run(cmd, capture_output=True, timeout=120)
    paths = [os.path.join(fdir, f) for f in sorted(os.listdir(fdir))]
    if not paths:
        detail = r.stderr.decode("utf-8", "replace")[:200] if (duration > 0 and r) else "ffmpeg 无输出"
        raise RuntimeError("抽帧失败：" + detail)
    return {"frames": paths, "hint": "用 Read 逐帧查看以理解视频内容"}


def t_prune_memory(keep_score=1.0, dry_run=True, limit=500, **_):
    """遗忘曲线清理：删除得分低于 keep_score 且重要级<=3 的记忆（重要级>=4 永不清理）。"""
    half = float(load_config().get("memory", {}).get("half_life_days", 30))
    now_ts = time.mktime(datetime.now().timetuple())
    with LOCK:
        rows = [dict(r) for r in CONN.execute(
            "SELECT * FROM memories ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 1000)),))]
    victims = []
    for r in rows:
        score = _memory_score(r, now_ts, half)
        if score < float(keep_score) and int(r["importance"]) <= 3:
            victims.append((r["id"], round(score, 3), r["content"][:40]))
    if dry_run:
        return {"would_delete": len(victims), "samples": victims[:10],
                "note": "dry_run=false 才真正删除；importance>=4 永不清理"}
    with LOCK:
        CONN.executemany("DELETE FROM memories WHERE id=?", [(v[0],) for v in victims])
        CONN.commit()
    return {"deleted": len(victims), "samples": victims[:10]}


def t_flush_outbox(max_age_min=30, **_):
    """补发队列：NapCat 恢复后重试；超过 max_age_min 分钟的过期丢弃。"""
    ttl = float(max_age_min)
    now_ts = time.time()
    sent, dropped, failed = 0, 0, []
    with LOCK:
        rows = [dict(r) for r in CONN.execute("SELECT * FROM outbox ORDER BY id")]
    for r in rows:
        age_min = (now_ts - _parse_dt(r["created_at"])) / 60.0
        if age_min > ttl:
            with LOCK:
                CONN.execute("DELETE FROM outbox WHERE id=?", (r["id"],))
                CONN.commit()
            dropped += 1
            continue
        message = []
        if r["text"]:
            message.append({"type": "text", "data": {"text": r["text"]}})
        if r["image_path"]:
            message.append({"type": "image", "data": {"file": to_file_uri(r["image_path"])}})
        if not message:
            with LOCK:
                CONN.execute("DELETE FROM outbox WHERE id=?", (r["id"],))
                CONN.commit()
            dropped += 1
            continue
        action = "send_private_msg" if r["chat_type"] == "private" else "send_group_msg"
        param = ({"user_id": int(r["chat_id"])} if r["chat_type"] == "private"
                 else {"group_id": int(r["chat_id"])})
        param["message"] = message
        try:
            resp = napcat_post(action, param)
        except Exception as e:
            with LOCK:
                CONN.execute("UPDATE outbox SET tries=tries+1, last_error=? WHERE id=?",
                             (str(e)[:200], r["id"]))
                CONN.commit()
            failed.append(r["id"])
            continue
        with LOCK:
            CONN.execute(
                "INSERT INTO messages(direction, channel, chat_type, chat_id, contact_id, msg_type,"
                " content, media_path, out_kind, processed, created_at)"
                " VALUES('out','qq',?,?,?,?,?,?,?,1,?)",
                (r["chat_type"], r["chat_id"], r["contact_id"], "text" if r["text"] else "image",
                 r["text"], r["image_path"], r["out_kind"], now_str()))
            CONN.execute("DELETE FROM outbox WHERE id=?", (r["id"],))
            CONN.commit()
        sent += 1
        time.sleep(0.3)
    with LOCK:
        remaining = CONN.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    return {"sent": sent, "dropped": dropped, "failed": failed, "remaining": remaining}


def _st_card_from_json(obj):
    data = obj.get("data") if isinstance(obj, dict) and isinstance(obj.get("data"), dict) else obj
    name = str(data.get("name") or data.get("char_name") or "").strip()
    desc = str(data.get("description") or data.get("char_persona") or "").strip()
    personality = str(data.get("personality") or "").strip()
    scenario = str(data.get("scenario") or data.get("world_scenario") or "").strip()
    mes = str(data.get("mes_example") or "").strip()
    first = str(data.get("first_mes") or data.get("char_greeting") or "").strip()
    return name, desc, personality, scenario, mes, first


def _st_card_from_png(path):
    with open(path, "rb") as f:
        blob = f.read()
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG 文件")
    pos = 8
    while pos + 8 <= len(blob):
        length = int.from_bytes(blob[pos:pos + 4], "big")
        ctype = blob[pos + 4:pos + 8]
        data = blob[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"tEXt":
            parts = data.split(b"\x00", 1)
            if len(parts) == 2 and parts[0].decode("latin-1", "replace") in ("chara", "ccv3"):
                return json.loads(base64.b64decode(parts[1]).decode("utf-8"))
        if ctype == b"IEND":
            break
    raise ValueError("PNG 里没有 SillyTavern 角色卡数据（tEXt chara 块）")


def t_import_sillytavern(file_path, **_):
    """导入 SillyTavern 角色卡（.json 或带 tEXt chara 块的 .png）为人格。"""
    if not file_path or not os.path.exists(file_path):
        raise ValueError(f"file_path 不存在: {file_path}")
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".png":
        obj = _st_card_from_png(file_path)
    elif ext == ".json":
        with open(file_path, encoding="utf-8") as f:
            obj = json.load(f)
    else:
        raise ValueError("支持 .json 或内嵌角色卡的 .png（SillyTavern 导出格式）")
    name, desc, personality, scenario, mes, first = _st_card_from_json(obj)
    if not name or not (desc or personality or scenario):
        raise ValueError("角色卡缺少 name 或描述字段，无法生成人格")
    prompt = f"你是{name}。"
    if desc:
        prompt += "\n\n" + desc
    if personality:
        prompt += "\n\n性格：" + personality
    if scenario:
        prompt += "\n\n场景设定：" + scenario
    style = ""
    if first or mes:
        style = ("开场示例：" + first[:200] + (("\n对话示例：" + mes[:300]) if mes else "")).strip()
    with LOCK:
        base, n = name, 1
        while CONN.execute("SELECT 1 FROM personas WHERE name=?", (name,)).fetchone():
            n += 1
            name = f"{base}_{n}"
        cur = CONN.execute(
            "INSERT INTO personas(name, prompt, style, is_active, created_at, updated_at)"
            " VALUES(?,?,?,0,?,?)", (name, prompt, style, now_str(), now_str()))
        CONN.commit()
        return {"ok": True, "persona_id": cur.lastrowid, "name": name,
                "hint": "manage_persona set_active 启用；建议人工过一遍 prompt 补充说话风格与雷点"}


PERSONA_CMDS = (
    ("/personahelp", "help"), ("/personas", "list"), ("/botstatus", "status"), ("/persona", "switch"),
    ("切换人格", "switch"), ("人格列表", "list"), ("机器人状态", "status"), ("人格指令", "help"),
)


def t_bot_command(text, channel="", contact_id="", chat_id="", **_):
    """管理指令入口：管理员在 QQ/微信聊天里即可热切换人格。handled=true 时大脑直接发送 reply。"""
    raw = (text or "").strip()
    if not raw:
        return {"handled": False}
    action, arg = None, ""
    for prefix, act in PERSONA_CMDS:
        if raw.startswith(prefix):
            action, arg = act, raw[len(prefix):].strip()
            break
    if action is None:
        return {"handled": False}
    admins_cfg = load_config().get("admin", {}).get(channel) or []
    if isinstance(admins_cfg, str):
        admins_cfg = admins_cfg.split(",")
    if isinstance(admins_cfg, (int, float)):
        admins_cfg = [admins_cfg]
    admins = {str(a).strip() for a in admins_cfg if str(a).strip()}
    if str(contact_id) not in admins:
        return {"handled": True, "allowed": False, "action": action,
                "reply": "这条指令只有管理员能用。"}
    with LOCK:
        if action == "switch":
            prev = CONN.execute("SELECT name FROM personas WHERE is_active=1").fetchone()
            names = [r["name"] for r in CONN.execute("SELECT name FROM personas ORDER BY id")]
            if not arg:
                return {"handled": True, "allowed": True, "action": "switch", "ok": False,
                        "reply": "用法：切换人格 <名称>。现有：" + ("、".join(names) or "（库是空的）")}
            row = CONN.execute("SELECT id, name FROM personas WHERE name=?", (arg,)).fetchone()
            if not row:
                return {"handled": True, "allowed": True, "action": "switch", "ok": False,
                        "reply": f"没有叫「{arg}」的人格。现有：{'、'.join(names) or '（库是空的）'}"}
            CONN.execute("UPDATE personas SET is_active=0")
            CONN.execute("UPDATE personas SET is_active=1 WHERE id=?", (row["id"],))
            CONN.commit()
            return {"handled": True, "allowed": True, "action": "switch", "ok": True,
                    "reply": f"已切换：{prev['name'] if prev else '（无）'} → {row['name']}，接下来的话就换这个人格说啦",
                    "data": {"active": row["name"]}}
        if action == "list":
            rows = [dict(r) for r in CONN.execute(
                "SELECT name, style, is_active FROM personas ORDER BY is_active DESC, id")]
            lines = [f"{'▶ ' if r['is_active'] else '· '}{r['name']}" + (f"（{r['style'][:20]}）" if r["style"] else "")
                     for r in rows]
            return {"handled": True, "allowed": True, "action": "list",
                    "reply": "人格库：\n" + "\n".join(lines) + "\n切换用：切换人格 <名称>"}
    if action == "status":
        st = t_bot_status()
        data = {"active_persona": st.get("active_persona"),
                "pending_messages": st["counts"]["pending_messages"],
                "memories": st["counts"]["memories"], "rules": st["counts"]["rules"],
                "stickers": st["counts"]["stickers"], "collector_running": st["collector"]["running"]}
        return {"handled": True, "allowed": True, "action": "status", "reply": None, "data": data}
    return {"handled": True, "allowed": True, "action": "help",
            "reply": "管理指令：切换人格 <名称> / 人格列表 / 机器人状态 / 人格指令"}


TOOLS = {
    "bot_status": (t_bot_status, "查看机器人整体状态：数据目录、激活人格、待处理消息数、QQ/微信渠道配置、collector 是否在运行。任何会话开始时先调用它。", {"type": "object", "properties": {}}),
    "configure_channel": (t_configure_channel, "配置 QQ(NapCat) 或微信(ADB) 渠道：地址、token、adb 路径、设备序列号、collector 端口。", {"type": "object", "properties": {"channel": {"type": "string", "enum": ["qq", "wechat"]}, "enabled": {"type": "boolean"}, "napcat_base_url": {"type": "string"}, "napcat_token": {"type": "string"}, "adb_path": {"type": "string"}, "device_serial": {"type": "string"}, "collector_port": {"type": "number"}}, "required": ["channel"]}),
    "get_config": (t_get_config, "读取全部行为配置（token 脱敏）：humanize 防风控参数、proactive 主动聊天策略、collector 端口等。", {"type": "object", "properties": {}}),
    "set_config": (t_set_config, "设置任一配置项（点分路径）。例：set_config('proactive.enabled', true)、set_config('humanize.reply_delay_max_ms', 8000)、set_config('proactive.quiet_hours', '23:30-07:30')。", {"type": "object", "properties": {"key": {"type": "string"}, "value": {}}, "required": ["key", "value"]}),
    "get_pending_messages": (t_get_pending_messages, "取所有未处理的上行消息（含发言人昵称/群名片、媒体URL、说话人关系画像）。这是大脑轮询的入口。", {"type": "object", "properties": {"channel": {"type": "string", "enum": ["qq", "wechat"]}, "chat_id": {"type": "string"}, "limit": {"type": "number", "default": 20}}}),
    "mark_processed": (t_mark_processed, "把已回复/决定忽略的消息标记为已处理，避免重复回复。", {"type": "object", "properties": {"message_ids": {"type": "array", "items": {"type": "number"}}}, "required": ["message_ids"]}),
    "simulate_inbound": (t_simulate_inbound, "注入一条假消息用于测试整个大脑链路（不需要真实 QQ/微信）。", {"type": "object", "properties": {"channel": {"type": "string", "enum": ["qq", "wechat"]}, "chat_type": {"type": "string", "enum": ["private", "group"]}, "chat_id": {"type": "string"}, "contact_id": {"type": "string"}, "display_name": {"type": "string"}, "text": {"type": "string"}, "media_url": {"type": "string"}}, "required": ["channel", "chat_type", "chat_id", "contact_id"]}),
    "check_access": (t_check_access, "黑白名单判定：回复前必须调用。黑名单命中或不在白名单时 allowed=false。", {"type": "object", "properties": {"channel": {"type": "string"}, "chat_type": {"type": "string", "enum": ["private", "group"]}, "chat_id": {"type": "string"}, "contact_id": {"type": "string"}}, "required": ["channel", "chat_type", "chat_id", "contact_id"]}),
    "send_qq_message": (t_send_qq_message, "通过 NapCat 发 QQ 私聊/群聊消息，支持 text 与本地 image_path。工具内部会先做黑白名单二次校验、随机拟人延迟、每会话限频，并记录发送日志。", {"type": "object", "properties": {"chat_type": {"type": "string", "enum": ["private", "group"]}, "chat_id": {"type": "string"}, "text": {"type": "string"}, "image_path": {"type": "string"}, "contact_id": {"type": "string"}, "out_kind": {"type": "string", "enum": ["reply", "proactive"]}}, "required": ["chat_type", "chat_id"]}),
    "send_wechat_message": (t_send_wechat_message, "通过 ADB 向当前打开的微信聊天窗口发送文字（需要手机/模拟器停留在目标聊天界面；中文输入需 ADBKeyboard）。内置拟人延迟与限频。", {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}),
    "wechat_screenshot": (t_wechat_screenshot, "对连接的模拟器/真机截屏，返回 PNG 路径；用 Read 工具查看该图即可识别当前微信聊天内容（图片/表情包）。", {"type": "object", "properties": {}}),
    "download_media": (t_download_media, "下载 QQ 消息里的图片/表情包/视频到本地并返回路径，随后可用 Read 工具做视觉识别。", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}),
    "manage_persona": (t_manage_persona, "人格管理：list / create(name,prompt,style) / update / set_active / delete。prompt 是人格核心设定（性格、语气、口头禅、边界）。", {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "create", "update", "set_active", "delete"]}, "name": {"type": "string"}, "prompt": {"type": "string"}, "style": {"type": "string"}, "persona_id": {"type": "number"}}, "required": ["action"]}),
    "store_memory": (t_store_memory, "写入长期记忆（外部 SQLite）。值得记：承诺、对方偏好、重要事件、关系变化。importance 1-5。", {"type": "object", "properties": {"content": {"type": "string"}, "channel": {"type": "string"}, "chat_id": {"type": "string"}, "contact_id": {"type": "string"}, "kind": {"type": "string"}, "importance": {"type": "number", "default": 3}}, "required": ["content"]}),
    "query_memory": (t_query_memory, "按关键词/联系人/会话检索长期记忆，重要级优先。配置 memory.embed_base_url 后可 semantic=true + 关键词做语义排序（此时关键词只当查询语义，不再过滤）。回复前检索相关记忆能显著提升连续性。", {"type": "object", "properties": {"keyword": {"type": "string"}, "contact_id": {"type": "string"}, "chat_id": {"type": "string"}, "channel": {"type": "string"}, "limit": {"type": "number", "default": 10}, "semantic": {"type": "boolean", "default": False}}}),
    "manage_profile": (t_manage_profile, "关系画像：get / upsert(channel,contact_id,impression,style_notes,affinity_delta) / list。每个联系人独立印象档案，affinity -100~100。", {"type": "object", "properties": {"action": {"type": "string", "enum": ["get", "upsert", "list"]}, "channel": {"type": "string"}, "contact_id": {"type": "string"}, "display_name": {"type": "string"}, "impression": {"type": "string"}, "style_notes": {"type": "string"}, "affinity_delta": {"type": "number"}}, "required": ["action"]}),
    "manage_rules": (t_manage_rules, "黑白名单管理：add/remove/list。list_type=blacklist|whitelist，scope=contact|group|global，channel=qq|wechat|*。", {"type": "object", "properties": {"action": {"type": "string", "enum": ["add", "remove", "list"]}, "list_type": {"type": "string", "enum": ["blacklist", "whitelist"]}, "scope": {"type": "string", "enum": ["contact", "group", "global"]}, "channel": {"type": "string"}, "target_id": {"type": "string"}, "note": {"type": "string"}}, "required": ["action"]}),
    "manage_stickers": (t_manage_stickers, "表情包库：add(本地图片路径,emotion,tags,source_url) / list / delete。emotion 建议用 happy/sad/angry/love/awkward/food 等标签；带 source_url 时自动按 URL 去重。", {"type": "object", "properties": {"action": {"type": "string", "enum": ["add", "list", "delete"]}, "file_path": {"type": "string"}, "emotion": {"type": "string"}, "tags": {"type": "string"}, "source": {"type": "string"}, "source_url": {"type": "string"}, "sticker_id": {"type": "number"}}, "required": ["action"]}),
    "pick_sticker": (t_pick_sticker, "按情绪挑选表情包返回本地路径（自动避免连用同一张），配合 send_qq_message 的 image_path 发送。", {"type": "object", "properties": {"emotion": {"type": "string"}}}),
    "proactive_due": (t_proactive_due, "判断现在是否允许主动发起聊天（enabled、安静时段、每日上限）。主动发言前必须调用。", {"type": "object", "properties": {}}),
    "bot_command": (t_bot_command, "管理指令入口（管理员在 QQ/微信聊天里热切换人格）。对每条入站消息先调用：handled=true 时发送 reply 并结束该条处理；handled=false 继续正常人格流程。指令：切换人格 <名称>、人格列表、机器人状态、人格指令。管理员配置在 admin.qq / admin.wechat。", {"type": "object", "properties": {"text": {"type": "string"}, "channel": {"type": "string"}, "contact_id": {"type": "string"}, "chat_id": {"type": "string"}}, "required": ["text"]}),
    "poke_qq": (t_poke_qq, "在 QQ 群里戳一戳某人（拟人化互动或回应被戳）。内置黑白名单与限频。", {"type": "object", "properties": {"group_id": {"type": "string"}, "user_id": {"type": "string"}}, "required": ["group_id", "user_id"]}),
    "send_wechat_sticker": (t_send_wechat_sticker, "微信发送本地表情包图片（需先校准 4 组点击坐标，见报错提示与 wechat-adb-setup 技能）。内置拟人延迟与限频。", {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}),
    "extract_video_frames": (t_extract_video_frames, "视频抽帧：对已下载到本地的视频均匀抽取若干帧返回图片路径，再用 Read 逐帧理解内容。需要 ffmpeg（winget install Gyan.FFmpeg）。", {"type": "object", "properties": {"video_path": {"type": "string"}, "frames": {"type": "number", "default": 4}}, "required": ["video_path"]}),
    "prune_memory": (t_prune_memory, "遗忘曲线清理：删除遗忘得分低于 keep_score 且重要级<=3 的旧记忆（重要级>=4 永不清理）。先 dry_run=true 预览。", {"type": "object", "properties": {"keep_score": {"type": "number", "default": 1.0}, "dry_run": {"type": "boolean", "default": True}, "limit": {"type": "number", "default": 500}}}),
    "import_sillytavern": (t_import_sillytavern, "导入 SillyTavern 角色卡为人格：.json 或带 tEXt chara 数据的 .png。重名自动加后缀。", {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}),
    "flush_outbox": (t_flush_outbox, "补发队列：NapCat 掉线期间排队的消息在恢复后重发，超过 max_age_min 分钟的过期丢弃。每轮心跳结束时应调用一次。", {"type": "object", "properties": {"max_age_min": {"type": "number", "default": 30}}}),
}

HANDLERS = {name: fn for name, (fn, _, _) in TOOLS.items()}


# ---------------------------------------------------------------- MCP stdio loop

def handle_call(method, params):
    if method == "initialize":
        return {"protocolVersion": (params or {}).get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "glm-foxgirl", "version": VERSION}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": s} for n, (_, d, s) in TOOLS.items()]}
    if method == "tools/call":
        name = (params or {}).get("name")
        args = (params or {}).get("arguments") or {}
        handler = HANDLERS.get(name)
        if not handler:
            return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
        try:
            data = handler(**args)
        except TypeError as e:
            data = {"error": f"参数错误: {e}"}
            return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": True}
        except Exception as e:
            return {"content": [{"type": "text", "text": json.dumps({"error": str(e)}, ensure_ascii=False)}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
    raise KeyError(method)


def main_mcp():
    init_db()
    start_collector_thread()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        mid = msg.get("id")
        if mid is None:
            continue
        try:
            result = handle_call(msg.get("method", ""), msg.get("params") or {})
        except KeyError:
            out = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
        else:
            out = {"jsonrpc": "2.0", "id": mid, "result": result}
        sys.stdout.write(json.dumps(out, ensure_ascii=True) + "\n")
        sys.stdout.flush()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    init_db()
    if "--collector" in sys.argv:
        cfg = load_config()["collector"]
        srv = ThreadingHTTPServer((cfg.get("host", "127.0.0.1"), int(cfg.get("port", 3001))), EventHandler)
        print(f"[glm-foxgirl] collector daemon on http://{cfg['host']}:{cfg['port']}/onebot/event (Ctrl+C 退出)", file=sys.stderr)
        srv.serve_forever()
    else:
        main_mcp()


if __name__ == "__main__":
    main()
