"""QQ 渠道：NapCat(OneBot 11) 收发、事件收集器、戳一戳、发送队列补发。"""
import base64
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import access, config, db


def napcat_post(action, params):
    cfg = config.load_config()["qq"]
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
    with db.LOCK:
        db.CONN.execute(
            "INSERT INTO messages(direction, channel, chat_type, chat_id, contact_id, display_name,"
            " msg_type, content, media_url, media_json, processed, created_at)"
            " VALUES('in','qq',?,?,?,?,?,?,?,?,0,?)",
            (chat_type, chat_id, contact_id, display, msg_type, text, "|".join(urls),
             json.dumps(segments, ensure_ascii=False), db.now_str()))
        if display:
            db.CONN.execute(
                "INSERT INTO profiles(channel, contact_id, display_name, updated_at) VALUES('qq',?,?,?)"
                " ON CONFLICT(channel, contact_id) DO UPDATE SET display_name=excluded.display_name,"
                " updated_at=excluded.updated_at WHERE profiles.display_name='' OR profiles.display_name IS NULL",
                (contact_id, display, db.now_str()))
        db.CONN.commit()


def store_poke(ev):
    operator = str(ev.get("user_id") or "")
    if not operator:
        return
    target = str(ev.get("target_id") or "")
    chat_type = "group" if ev.get("group_id") else "private"
    chat_id = str(ev.get("group_id") or operator)
    with db.LOCK:
        db.CONN.execute(
            "INSERT INTO messages(direction, channel, chat_type, chat_id, contact_id, msg_type,"
            " content, media_json, processed, created_at)"
            " VALUES('in','qq',?,?,'','poke',?,'[]',0,?)",
            (chat_type, chat_id, f"[戳一戳] {operator} 戳了 {target or '我'}", db.now_str()))
        db.CONN.commit()


class EventHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/onebot/event":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        token = config.load_config()["collector"].get("token", "")
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
    cfg = config.load_config()["collector"]
    host, port = cfg.get("host", "127.0.0.1"), int(cfg.get("port", 3001))
    try:
        COLLECTOR_SERVER = ThreadingHTTPServer((host, port), EventHandler)
        t = threading.Thread(target=COLLECTOR_SERVER.serve_forever, daemon=True)
        t.start()
        print(f"[glm-foxgirl] collector listening on http://{host}:{port}/onebot/event", file=sys.stderr)
    except OSError:
        print(f"[glm-foxgirl] port {port} busy, assuming an external collector is running", file=sys.stderr)


def collector_running():
    cfg = config.load_config()["collector"]
    try:
        with socket.create_connection((cfg.get("host", "127.0.0.1"), int(cfg.get("port", 3001))), timeout=0.5):
            return True
    except OSError:
        return False


def t_send_qq_message(chat_type, chat_id, text="", image_path=None, contact_id=None, out_kind="reply", **_):
    if chat_type not in ("private", "group"):
        raise ValueError("chat_type 必须是 private 或 group")
    if not text and not image_path:
        raise ValueError("text 与 image_path 至少给一个")
    decision = access.access_decision("qq", chat_type, str(chat_id),
                                      str(contact_id) if contact_id else str(chat_id) if chat_type == "private" else None)
    if not decision["allowed"]:
        return {"sent": False, "blocked": True, **decision}
    ok, why = access.rate_limit_check("qq", chat_id)
    if not ok:
        return {"sent": False, "blocked": True, "reason": why}
    access.human_delay()
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
        with db.LOCK:
            db.CONN.execute(
                "INSERT INTO outbox(channel, chat_type, chat_id, contact_id, text, image_path,"
                " out_kind, tries, last_error, created_at) VALUES('qq',?,?,?,?,?,?,1,?,?)",
                (chat_type, str(chat_id), str(contact_id or ""), text, image_path or "",
                 out_kind, str(e)[:200], db.now_str()))
            db.CONN.commit()
        return {"sent": False, "queued": True,
                "reason": "NapCat 暂时不可达，已入发送队列（心跳时 flush_outbox 自动补发）",
                "detail": str(e)[:120]}
    with db.LOCK:
        db.CONN.execute(
            "INSERT INTO messages(direction, channel, chat_type, chat_id, contact_id, msg_type, content,"
            " media_path, out_kind, processed, created_at) VALUES('out','qq',?,?,?,?,?,?,?,1,?)",
            (chat_type, str(chat_id), str(contact_id or ""), "text" if text else "image",
             text, image_path or "", out_kind, db.now_str()))
        db.CONN.commit()
    return {"sent": True, "napcat_status": resp.get("status"), "message": text[:50]}


def t_flush_outbox(max_age_min=30, **_):
    """补发队列：NapCat 恢复后重试；超过 max_age_min 分钟的过期丢弃。"""
    ttl = float(max_age_min)
    now_ts = time.time()
    sent, dropped, failed = 0, 0, []
    with db.LOCK:
        rows = [dict(r) for r in db.CONN.execute("SELECT * FROM outbox ORDER BY id")]
    for r in rows:
        age_min = (now_ts - db._parse_dt(r["created_at"])) / 60.0
        if age_min > ttl:
            with db.LOCK:
                db.CONN.execute("DELETE FROM outbox WHERE id=?", (r["id"],))
                db.CONN.commit()
            dropped += 1
            continue
        message = []
        if r["text"]:
            message.append({"type": "text", "data": {"text": r["text"]}})
        if r["image_path"]:
            message.append({"type": "image", "data": {"file": to_file_uri(r["image_path"])}})
        if not message:
            with db.LOCK:
                db.CONN.execute("DELETE FROM outbox WHERE id=?", (r["id"],))
                db.CONN.commit()
            dropped += 1
            continue
        action = "send_private_msg" if r["chat_type"] == "private" else "send_group_msg"
        param = ({"user_id": int(r["chat_id"])} if r["chat_type"] == "private"
                 else {"group_id": int(r["chat_id"])})
        param["message"] = message
        try:
            resp = napcat_post(action, param)
        except Exception as e:
            with db.LOCK:
                db.CONN.execute("UPDATE outbox SET tries=tries+1, last_error=? WHERE id=?",
                                (str(e)[:200], r["id"]))
                db.CONN.commit()
            failed.append(r["id"])
            continue
        with db.LOCK:
            db.CONN.execute(
                "INSERT INTO messages(direction, channel, chat_type, chat_id, contact_id, msg_type,"
                " content, media_path, out_kind, processed, created_at)"
                " VALUES('out','qq',?,?,?,?,?,?,?,1,?)",
                (r["chat_type"], r["chat_id"], r["contact_id"], "text" if r["text"] else "image",
                 r["text"], r["image_path"], r["out_kind"], db.now_str()))
            db.CONN.execute("DELETE FROM outbox WHERE id=?", (r["id"],))
            db.CONN.commit()
        sent += 1
        time.sleep(0.3)
    with db.LOCK:
        remaining = db.CONN.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    return {"sent": sent, "dropped": dropped, "failed": failed, "remaining": remaining}


def t_poke_qq(group_id, user_id, **_):
    decision = access.access_decision("qq", "group", str(group_id), None)
    if not decision["allowed"]:
        return {"sent": False, "blocked": True, **decision}
    ok, why = access.rate_limit_check("qq", str(group_id))
    if not ok:
        return {"sent": False, "blocked": True, "reason": why}
    resp = napcat_post("group_poke", {"group_id": int(group_id), "user_id": int(user_id)})
    with db.LOCK:
        db.CONN.execute(
            "INSERT INTO messages(direction, channel, chat_type, chat_id, contact_id, msg_type,"
            " content, out_kind, processed, created_at)"
            " VALUES('out','qq','group',?,?,'poke','[戳一戳]','reply',1,?)",
            (str(group_id), str(user_id), db.now_str()))
        db.CONN.commit()
    return {"sent": True, "napcat_status": resp.get("status")}
