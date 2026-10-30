"""QQ 渠道：NapCat(OneBot 11) 收发、事件收集器、戳一戳、发送队列补发。"""
import base64
import json
import hashlib
import os
import re
import socket
import subprocess
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


def _collector_port_occupied(host, port):
    """探测端口是否已有收集器在监听——避免 Windows 下 SO_REUSEADDR 双绑互抢。"""
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def start_collector_thread():
    global COLLECTOR_SERVER
    cfg = config.load_config()["collector"]
    host, port = cfg.get("host", "127.0.0.1"), int(cfg.get("port", 3001))
    if _collector_port_occupied(host, port):
        # 单例化：已有收集器实例时本进程不重复绑定（事件由在跑实例接收入库）
        print(f"[glm-foxgirl] 端口 {port} 已有收集器运行，本进程不重复启动", file=sys.stderr)
        return False
    try:
        COLLECTOR_SERVER = ThreadingHTTPServer((host, port), EventHandler)
        t = threading.Thread(target=COLLECTOR_SERVER.serve_forever, daemon=True)
        t.start()
        w = threading.Thread(target=_watchdog_loop, daemon=True)
        w.start()
        s = threading.Thread(target=_alert_sender_loop, daemon=True)
        s.start()
        e = threading.Thread(target=_ensure_napcat_loop, daemon=True)
        e.start()
        print(f"[glm-foxgirl] collector listening on http://{host}:{port}/onebot/event", file=sys.stderr)
        return True
    except OSError:
        print(f"[glm-foxgirl] port {port} busy, assuming an external collector is running", file=sys.stderr)
        return False


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
                "reason": "NapCat 暂时不可达，已入发送队列（下轮消息处理时 flush_outbox 自动补发）",
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
    # NapCat WebUI 认证需要 sha256(token + ".napcat") 哈希；原始 token 会被拒绝
    resp = _webui_request("/api/auth/login",
                          {"hash": hashlib.sha256((token + ".napcat").encode()).hexdigest(),
                           "totpCode": ""})
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


def _notify_admins(text):
    """即时系统告警：罐装文本写入持久化队列，由后台发送线程异步直发全部管理员
    （不经大脑、无 LLM 调用、绕过拟人延迟、不阻塞上报处理线程；进程重启不丢）。
    alerts.enabled=false 时不发。待发超限丢弃并留痕。"""
    if not config.load_config().get("alerts", {}).get("enabled"):
        return
    with db.LOCK:
        pending = db.CONN.execute("SELECT COUNT(*) FROM alert_queue WHERE sent=0").fetchone()[0]
        if pending >= 100:
            log_error("alert queue overflow", f"drop: {text[:60]}")
            return
        db.CONN.execute("INSERT INTO alert_queue(text, created_at) VALUES(?,?)",
                        (text, db.now_str()))
        db.CONN.commit()


def _drain_alerts_once():
    """发送线程的一次巡检：发达到期告警（TTL 过期的直接弃置）。"""
    ttl = float(config.load_config().get("alerts", {}).get("ttl_min", 60))
    now_ts = time.time()
    with db.LOCK:
        rows = [dict(r) for r in db.CONN.execute(
            "SELECT id, text, created_at FROM alert_queue WHERE sent=0 ORDER BY id LIMIT 10")]
    for r in rows:
        if (now_ts - db._parse_dt(r["created_at"])) / 60.0 > ttl:
            with db.LOCK:
                db.CONN.execute("UPDATE alert_queue SET sent=2 WHERE id=?", (r["id"],))
                db.CONN.commit()
            continue
        for admin in access._admin_set("qq"):
            try:
                napcat_post("send_private_msg", {
                    "user_id": int(admin),
                    "message": [{"type": "text", "data": {"text": r["text"]}}]})
                with db.LOCK:
                    db.CONN.execute(
                        "INSERT INTO messages(direction, channel, chat_type, chat_id, contact_id, msg_type,"
                        " content, out_kind, processed, created_at)"
                        " VALUES('out','qq','private',?,?,'alert',?,'alert',1,?)",
                        (str(admin), str(admin), r["text"], db.now_str()))
                    db.CONN.commit()
            except Exception as e:
                log_error("alert notify failed", e)
        with db.LOCK:
            db.CONN.execute("UPDATE alert_queue SET sent=1 WHERE id=?", (r["id"],))
            db.CONN.commit()
        time.sleep(0.2)


def _alert_sender_loop():
    while True:
        try:
            _drain_alerts_once()
        except Exception as e:
            log_error("alert sender error", e)
        time.sleep(0.5)


def _ensure_napcat_loop():
    """插件启动时的 NapCat 自动拉起（qq.autostart=true 且 qq.enabled 时）：
    探测 OneBot；不可达则发现并拉起 NapCat，一次性尝试后退出。"""
    time.sleep(2)
    try:
        cfg = config.load_config()
        if not (cfg.get("qq", {}).get("enabled") and cfg.get("qq", {}).get("autostart", True)):
            return
        try:
            napcat_post("get_login_info", {})
            return  # 本就在线
        except Exception:
            pass
        res = t_start_napcat(wait_seconds=45)
        if res.get("state") == "logged_in":
            _notify_admins("[恢复] NapCat 已自动启动并上线。")
        else:
            _notify_admins("[提醒] NapCat 已自动启动，但登录未完成（需扫码或手动验证）。")
    except Exception as e:
        log_error("ensure napcat failed", e)


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


# ---------------------------------------------------------------- NapCat 发现与拉起

NAPCAT_BOOT_EXE = "NapCatWinBootMain.exe"
NAPCAT_SCAN_SKIP = {"windows", "windowsapps", "$recycle.bin", "$winreagent", "perflogs",
                    "node_modules", "appdata", "recovery", "dumpstacklog.tmp"}


def _napcat_uin():
    creds = config.load_config().get("qq", {}).get("credentials", {})
    u = str(creds.get("uin", "") or "")
    return u or None


def _qq_registry_dirs():
    dirs = []
    if sys.platform != "win32":
        return dirs
    import winreg
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            for sub in (r"SOFTWARE\Tencent\QQNT", r"SOFTWARE\WOW6432Node\Tencent\QQNT"):
                try:
                    with winreg.OpenKey(hive, sub, 0, view | winreg.KEY_READ) as k:
                        for name in ("Install", "InstallPath", "UninstallString"):
                            try:
                                v, _ = winreg.QueryValueEx(k, name)
                                if v:
                                    d = v if os.path.isdir(v) else os.path.dirname(v)
                                    if os.path.isdir(d):
                                        dirs.append(d)
                            except OSError:
                                pass
                except OSError:
                    pass
    return dirs


def _scan_for_napcat(roots, max_depth=4, budget_seconds=12):
    """限时扫描候选根目录，找含 NapCatWinBootMain.exe 的框架目录。"""
    deadline = time.time() + budget_seconds
    found = []

    def walk(d, depth):
        if len(found) >= 5 or time.time() > deadline or depth > max_depth:
            return
        try:
            entries = set(os.listdir(d))
        except OSError:
            return
        if NAPCAT_BOOT_EXE in entries:
            found.append(d)
            return
        for e in sorted(entries):
            if e.lower() in NAPCAT_SCAN_SKIP or e.startswith((".", "$")):
                continue
            p = os.path.join(d, e)
            if os.path.isdir(p):
                walk(p, depth + 1)

    for r in roots:
        if os.path.isdir(r):
            walk(r, 0)
    return found


def _running_qq_dirs():
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='QQ.exe'\" | "
             "Select-Object -ExpandProperty ExecutablePath"],
            capture_output=True, timeout=20)
        out = []
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if line and os.path.isfile(line):
                out.append(os.path.dirname(line))
        return out
    except Exception:
        return []


def t_find_napcat(roots=None, **_):
    """查找 NapCat 框架目录（含 NapCatWinBootMain.exe）。
    给定 roots 时只扫描这些根目录（可预测，调试/测试用）；不给则走完整发现：
    手动路径（qq.napcat_path）→ 正在运行的 QQ 进程 → 注册表安装目录 → 常见根目录限时扫描。"""
    cfg = config.load_config().get("qq", {})
    if roots:
        found = _scan_for_napcat([r for r in roots if os.path.isdir(r)])
        return {"found": found, "count": len(found),
                "hint": ("启动：start_napcat()；固定路径：set_config qq.napcat_path"
                         if found else "给定根目录下未找到 NapCat 框架目录")}
    candidates = []
    manual = (cfg.get("napcat_path") or "").strip()
    if manual:
        d = manual if os.path.isdir(manual) else os.path.dirname(manual)
        if os.path.isfile(os.path.join(d, NAPCAT_BOOT_EXE)):
            candidates.append(d)
    for d in _running_qq_dirs():
        if d not in candidates:
            candidates.append(d)
    for d in _qq_registry_dirs():
        if d not in candidates and os.path.isfile(os.path.join(d, NAPCAT_BOOT_EXE)):
            candidates.append(d)
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    scan_roots = [pf, os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                  os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
                  os.path.expanduser("~")]
    for d in _scan_for_napcat([r for r in scan_roots if os.path.isdir(r)]):
        if d not in candidates:
            candidates.append(d)
    valid = [c for c in candidates if os.path.isfile(os.path.join(c, NAPCAT_BOOT_EXE))]
    return {"found": valid, "count": len(valid),
            "hint": ("启动：start_napcat()；固定路径：set_config qq.napcat_path"
                     if valid else
                     "未找到 NapCat 框架目录（需含 NapCatWinBootMain.exe）。"
                     "请确认 NapCat.Shell 已安装，或 set_config qq.napcat_path 手动指定")}


def t_start_napcat(uin=None, root=None, wait_seconds=60, **_):
    """查找并启动 NapCat（NapCatWinBootMain.exe <QQ号>），随后轮询 OneBot 最多 wait_seconds 秒。"""
    cfg = config.load_config()
    qqcfg = cfg.get("qq", {})
    if root:
        fw = [root] if os.path.isfile(os.path.join(root, NAPCAT_BOOT_EXE)) else []
    else:
        fw = t_find_napcat().get("found", [])
    if not fw:
        raise RuntimeError("未找到 NapCat 框架目录（需含 NapCatWinBootMain.exe）。"
                           "确认 NapCat.Shell 已安装，或 set_config qq.napcat_path 手动指定")
    framework = fw[0]
    exe = os.path.join(framework, NAPCAT_BOOT_EXE)
    u = str(uin or _napcat_uin() or "")
    proc = subprocess.Popen([exe, u] if u else [exe], cwd=framework)
    wait = max(0, min(int(wait_seconds), 180))
    deadline = time.time() + wait
    logged_in, uin_seen = None, None
    while time.time() < deadline:
        time.sleep(2)
        try:
            resp = napcat_post("get_login_info", {})
            data = resp.get("data") or {}
            if data.get("user_id"):
                logged_in, uin_seen = True, str(data.get("user_id"))
                break
        except Exception:
            pass
    return {"launched": True, "pid": proc.pid, "framework": framework, "uin_arg": u or None,
            "state": "logged_in" if logged_in else ("booting" if wait else "unknown"),
            "hint": "启动后 NapCat 会按 webui.json 的 autoLoginAccount 自动登录；"
                    "若卡扫码页请确认启动参数带了真实QQ号，或扫码登录一次"}



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
    """备用登录全自动链：探测 → 发现并拉起 NapCat → WebUI 快速登录 → 如实降级提醒。
    qq.autostart=false 时跳过自动拉起；任何情况下不回显已托管的账密。"""
    notes = []
    creds = config.load_config().get("qq", {}).get("credentials", {})
    uin = str(creds.get("uin", "") or "")

    def online():
        try:
            data = (napcat_post("get_login_info", {}) or {}).get("data") or {}
            return bool(data.get("user_id")), str(data.get("user_id") or "")
        except Exception:
            return False, ""

    ok, cur = online()
    if ok:
        return {"ok": True, "state": "logged_in", "uin": cur, "notes": ["本就在线，无需重登"]}

    if config.load_config().get("qq", {}).get("autostart", True):
        try:
            res = t_start_napcat(uin=uin or None, wait_seconds=45)
            notes.append(f"NapCat 已自动拉起（pid {res.get('pid')}，框架 {res.get('framework')}）")
        except Exception as e:
            notes.append("自动拉起失败：" + str(e)[:100])
    else:
        notes.append("qq.autostart=false，跳过自动拉起")

    ok, cur = online()
    if ok:
        _notify_admins("[恢复] NapCat/QQ 已重新在线（uin " + (cur or "?") + "）。")
        return {"ok": True, "state": "logged_in", "uin": cur, "notes": notes}

    credential = _webui_credential()
    if credential:
        try:
            # NapCat 新版 WebUI 只有快速登录接口（SetQuickLogin，免密）；旧 SetQQLogin 已废弃
            _webui_request("/api/QQLogin/SetQuickLogin",
                           {"uin": uin} if uin else {}, credential=credential)
        except Exception as e:
            notes.append("快速登录尝试失败：" + str(e)[:80])
        for _ in range(10):
            time.sleep(3)
            state = _webui_login_state(credential)
            if state and state[0]:
                _notify_admins("[恢复] NapCat/QQ 已重新在线（快速登录）。")
                return {"ok": True, "state": "logged_in", "method": "webui-quick-login",
                        "uin": str(state[1] or cur or uin or ""), "notes": notes}
            ok, cur = online()
            if ok:
                _notify_admins("[恢复] NapCat/QQ 已重新在线（快速登录）。")
                return {"ok": True, "state": "logged_in", "uin": cur, "notes": notes}

    _notify_admins("[告警] NapCat 自动恢复失败，需要人工登录（扫码或输入已托管账密）。")
    return {"ok": False, "state": "need_manual", "notes": notes,
            "credentials_stored": bool(creds.get("enc_password")),
            "hint": ("自动恢复未完成。请在 NapCat 登录窗口手动登录（扫码或输入已托管账密），"
                     "完成后 qq_login_status 会显示在线。已设置 webui.json 的 autoLoginAccount 时"
                     "重启会自动快速登录（NapCatWinBootMain.exe 需传真实QQ号）。")}
