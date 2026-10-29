"""QQ 渠道：NapCat(OneBot 11) 收发、事件收集器、戳一戳、发送队列补发。"""
import base64
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import access, config, db, security
from .httpio import log_error, read_body


def napcat_post(action, params):
    cfg = config.load_config()["qq"]
    if not cfg.get("enabled"):
        raise RuntimeError("QQ(NapCat) 渠道未启用，请先调用 configure_channel 并在 NapCat 配置 HTTP 服务")
    url = cfg["napcat_base_url"].rstrip("/") + "/" + action
    headers = {"Content-Type": "application/json"}
    if cfg.get("napcat_token"):
        headers["Authorization"] = "Bearer " + cfg["napcat_token"]
    req = urllib.request.Request(url, data=json.dumps(params, ensure_ascii=False).encode("utf-8"),
                                 headers=headers, method="POST")
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
    # 内容防火墙：恶意内容直接拦截为 blocked（永不进入大脑上下文），并计入守卫
    if config.load_config().get("security", {}).get("content_filter", True):
        scan = security.scan_content(text)
        if scan["malicious"]:
            with db.LOCK:
                db.CONN.execute(
                    "INSERT INTO messages(direction, channel, chat_type, chat_id, contact_id, display_name,"
                    " msg_type, content, media_url, media_json, processed, created_at)"
                    " VALUES('in','qq',?,?,?,?,'blocked',?,'','',1,?)",
                    (chat_type, chat_id, contact_id, display, text, db.now_str()))
                db.CONN.commit()
            access._guard_hit("qq", contact_id, "malicious:" + "/".join(scan["categories"]))
            count = db.CONN.execute("SELECT count FROM guard WHERE channel='qq' AND contact_id=?",
                                    (contact_id,)).fetchone()
            if config.load_config().get("alerts", {}).get("enabled"):
                _notify_admins(f"[防火墙拦截] {display or contact_id} 触发 "
                               f"{'/'.join(scan['categories'])}（第 {count[0] if count else '?'} 次）"
                               + ("，已自动拉黑" if (count and int(count[0]) > 10) else ""))
            return
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
        body = read_body(self)  # NapCat 上报走 chunked（无 Content-Length），必须分块读取
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
        except Exception as e:
            log_error("collector event parse/store error", e)
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
        w = threading.Thread(target=_watchdog_loop, daemon=True)
        w.start()
        s = threading.Thread(target=_alert_sender_loop, daemon=True)
        s.start()
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
    if text and security.secret_leak(text):
        from .httpio import log_error as _le
        _le("outgoing blocked (secret leak guard)", f"chat={chat_id}")
        return {"sent": False, "blocked": True,
                "reason": "内容疑似包含本机敏感信息（token/密钥/凭据），已被内容防火墙拦截"}
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


# ---------------------------------------------------------------- 登录托管与备用登录

def _webui_request(path, payload=None, credential=None, timeout=8):
    """NapCat WebUI API（尽力而为）：未配置返回 None，任何失败返回 None。"""
    base = (config.load_config()["qq"].get("webui_url") or "").rstrip("/")
    if not base:
        return None
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = "Bearer " + credential
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _webui_credential():
    token = config.load_config()["qq"].get("webui_token", "")
    if not token:
        return None
    resp = _webui_request("/api/auth/login", {"token": token})
    if not resp:
        return None
    data = resp.get("data") if isinstance(resp, dict) else {}
    if isinstance(data, dict):
        for k, v in data.items():
            if "credential" in k.lower() and isinstance(v, str):
                return v
    return None


def _dig(obj, *keys):
    node = obj
    for k in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(k)
    return node


def _webui_login_state(credential):
    resp = _webui_request("/api/QQLogin/CheckLoginStatus", credential=credential)
    if resp is None:
        resp = _webui_request("/api/auth/login-info", credential=credential)
    if resp is None:
        return None
    login = _dig(resp, "data", "login") or _dig(resp, "data", "online") or resp.get("login")
    uin = _dig(resp, "data", "uin") or resp.get("uin")
    return bool(login), uin


ALERT_Q = queue.Queue()


def _notify_admins(text):
    """即时系统告警：罐装文本入队，由后台发送线程异步直发全部管理员
    （不经大脑、无 LLM 调用、绕过拟人延迟、不阻塞上报处理线程）。
    alerts.enabled=false 时不发。队列超限丢弃并留痕。"""
    if not config.load_config().get("alerts", {}).get("enabled"):
        return
    if ALERT_Q.qsize() >= 100:
        log_error("alert queue overflow", f"drop: {text[:60]}")
        return
    ALERT_Q.put(text)


def _alert_sender_loop():
    while True:
        try:
            text = ALERT_Q.get(timeout=0.5)
        except Exception:
            continue
        for admin in access._admin_set("qq"):
            try:
                napcat_post("send_private_msg", {
                    "user_id": int(admin),
                    "message": [{"type": "text", "data": {"text": text}}]})
                with db.LOCK:
                    db.CONN.execute(
                        "INSERT INTO messages(direction, channel, chat_type, chat_id, contact_id, msg_type,"
                        " content, out_kind, processed, created_at)"
                        " VALUES('out','qq','private',?,?,'alert',?,'alert',1,?)",
                        (str(admin), str(admin), text, db.now_str()))
                    db.CONN.commit()
            except Exception as e:
                log_error("alert notify failed", e)
        time.sleep(0.2)


WATCH = {"last_state": None}


def _watchdog_loop():
    """collector 进程内的实时看门狗：探测 NapCat 掉线/恢复并即时告警（规则层，无 LLM）。"""
    while True:
        try:
            cfg = config.load_config()
            if cfg.get("alerts", {}).get("enabled") and cfg.get("qq", {}).get("enabled"):
                try:
                    napcat_post("get_login_info", {})
                    state = "online"
                except Exception:
                    state = "offline"
                last = WATCH.get("last_state")
                if last is not None and state != last:
                    if state == "online":
                        _notify_admins("[系统告警] NapCat/QQ 已恢复在线。")
                    else:
                        log_error("watchdog", "NapCat/QQ 掉线（恢复后将补发告警）")
                WATCH["last_state"] = state
        except Exception:
            pass
        try:
            time.sleep(max(10, int(config.load_config().get("alerts", {}).get("poll_seconds", 30))))
        except Exception:
            time.sleep(30)


def t_save_qq_credentials(uin, password, **_):
    """托管 QQ 账号密码（备用登录用）：密码经 Windows DPAPI 加密后落盘，
    本机本用户之外不可解密，任何接口都不会回显明文。可选功能。"""
    if not uin or not password:
        raise ValueError("uin 与 password 都不能为空")
    from .crypto import dpapi_protect
    enc = dpapi_protect(str(password))
    cfg = config.load_config()
    cfg.setdefault("qq", {})["credentials"] = {
        "uin": str(uin), "enc_password": enc, "saved_at": db.now_str()}
    config.save_config(cfg)
    return {"ok": True, "uin": str(uin),
            "hint": "密码已用 Windows DPAPI 加密托管（本机本用户才可解密，接口永不回显明文）。"
                    "掉线时将自动尝试快速登录；快速登录失败仍需在 NapCat 登录窗口手动验证（如扫码）。"}


def t_qq_login_status(**_):
    """QQ 登录状态探测：渠道未启用→unknown；先打 OneBot get_login_info；
    不可达再用 WebUI 区分 NapCat 宕机与掉线。"""
    cfg = config.load_config()["qq"]
    if not cfg.get("enabled"):
        return {"state": "unknown", "reason": "QQ 渠道未启用",
                "hint": "configure_channel 启用后再查"}
    try:
        resp = napcat_post("get_login_info", {})
        data = resp.get("data") or {}
        return {"state": "logged_in", "uin": str(data.get("user_id") or ""),
                "nickname": data.get("nickname", "")}
    except RuntimeError as e:
        if "未启用" in str(e):
            return {"state": "unknown", "reason": "QQ 渠道未启用",
                    "hint": "configure_channel 启用后再查"}
        credential = _webui_credential()
        webui_resp = _webui_login_state(credential) if credential else None
        if webui_resp is None:
            return {"state": "napcat_down",
                    "reason": "OneBot 不可达且 WebUI 未配置/不可达",
                    "hint": "检查 NapCat 进程；配置 qq.webui_url + qq.webui_token 后可自动区分离线状态"}
        login, uin = webui_resp
        if login:
            return {"state": "logged_in", "uin": str(uin or "")}
        return {"state": "offline", "uin": str(uin or ""),
                "hint": "NapCat 在线但 QQ 未登录：可调 qq_relogin 尝试快速登录"}
    except Exception as e:
        return {"state": "offline", "reason": str(e)[:120],
                "hint": "调用失败，可调 qq_relogin 尝试恢复"}


def t_qq_relogin(**_):
    """备用登录：快速登录优先（无需密码）；失败时提示手动验证。
    账号密码已加密托管时自动尝试快速登录并在提醒中注明；任何情况下不回显密码。"""
    creds = config.load_config()["qq"].get("credentials") or {}
    uin = str(creds.get("uin", ""))
    credential = _webui_credential()
    if credential:
        if uin:
            _webui_request("/api/QQLogin/SetQQLogin", {"uin": uin}, credential=credential)
        else:
            _webui_request("/api/QQLogin/SetQQLogin", {}, credential=credential)
        for _ in range(3):
            time.sleep(2)
            state = _webui_login_state(credential)
            if state and state[0]:
                return {"ok": True, "state": "logged_in", "method": "webui-quick-login",
                        "uin": str(state[1] or uin or "")}
    if not credential:
        return {"ok": False, "state": "need_manual",
                "credentials_stored": bool(creds.get("enc_password")),
                "hint": "WebUI 未配置（qq.webui_url / qq.webui_token），无法自动快速登录。"
                        "请打开 NapCat 登录窗口手动登录（扫码或输入密码），"
                        "或在配置页提供 WebUI 地址与 token 以启用自动快速登录。"}
    return {"ok": False, "state": "need_manual",
            "credentials_stored": bool(creds.get("enc_password")),
            "hint": ("快速登录未成功。账号密码已加密托管，但 NapCat/NTQQ 不提供远程密码登录接口——"
                     "请在 NapCat 登录窗口手动输入（或扫码）。已设置 webui.json 的 autoLoginAccount 时"
                     "重启会自动快速登录（NapCatWinBootMain.exe 需传真实QQ号）。")}
