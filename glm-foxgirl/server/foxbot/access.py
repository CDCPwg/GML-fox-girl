"""黑白名单判定、管理守卫、限频与拟人延迟。"""
import random
import time
from datetime import datetime

from . import config, db


def access_decision(channel, chat_type, chat_id, contact_id):
    """Blacklist denies; if any whitelist entry exists for the channel, only
    whitelisted targets pass. Returns {"allowed": bool, "reason": str}."""
    with db.LOCK:
        def hit(list_type, scope, target):
            if target is None:
                return None
            return db.CONN.execute(
                "SELECT note FROM rules WHERE list_type=? AND channel IN (?, '*') AND scope=? AND target_id=?",
                (list_type, channel, scope, str(target)),
            ).fetchone()

        for scope, target in (("global", "*"), ("contact", contact_id),
                              ("group", chat_id if chat_type == "group" else None)):
            row = hit("blacklist", scope, target)
            if row:
                return {"allowed": False, "reason": f"黑名单拦截({scope})", "note": row["note"] or ""}
        wl = db.CONN.execute(
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


def rate_limit_check(channel, chat_id):
    cfg = config.load_config()["humanize"]
    mx = int(cfg.get("max_per_chat_per_hour", 12))
    key = f"{channel}:{chat_id}"
    with db.LOCK:
        row = db.CONN.execute("SELECT window_start, count FROM counters WHERE chat_key=?", (key,)).fetchone()
        t = time.time()
        if not row or t - float(row["window_start"]) > 3600:
            db.CONN.execute("INSERT OR REPLACE INTO counters VALUES(?,?,1)", (key, t))
        elif int(row["count"]) >= mx:
            db.CONN.commit()
            return False, f"该会话每小时限频 {mx} 条已满，本次发送被拦截"
        else:
            db.CONN.execute("UPDATE counters SET count=count+1 WHERE chat_key=?", (key,))
        db.CONN.commit()
    return True, ""


def human_delay():
    cfg = config.load_config()["humanize"]
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


def _admin_set(channel):
    admins_cfg = config.load_config().get("admin", {}).get(channel) or []
    if isinstance(admins_cfg, str):
        admins_cfg = admins_cfg.split(",")
    if isinstance(admins_cfg, (int, float)):
        admins_cfg = [admins_cfg]
    return {str(a).strip() for a in admins_cfg if str(a).strip()}


def _guard_hit(channel, contact_id, action):
    """非管理员尝试修改配置/功能：计数；单账号超过 10 次自动加入黑名单。"""
    with db.LOCK:
        db.CONN.execute(
            "INSERT INTO guard(channel, contact_id, count, last_action, last_at) VALUES(?,?,1,?,?)"
            " ON CONFLICT(channel, contact_id) DO UPDATE SET count=count+1,"
            " last_action=excluded.last_action, last_at=excluded.last_at",
            (channel, str(contact_id), action[:60], db.now_str()))
        row = db.CONN.execute("SELECT count FROM guard WHERE channel=? AND contact_id=?",
                              (channel, str(contact_id))).fetchone()
        count = int(row["count"])
        blacklisted = False
        if count > 10:
            db.CONN.execute(
                "INSERT OR IGNORE INTO rules(list_type, channel, scope, target_id, note, created_at)"
                " VALUES('blacklist', ?, 'contact', ?, ?, ?)",
                (channel, str(contact_id), f"非管理员尝试修改配置 {count} 次，自动拉黑", db.now_str()))
            blacklisted = True
        db.CONN.commit()
    return count, blacklisted


def t_check_admin(channel, contact_id, **_):
    return {"is_admin": str(contact_id) in _admin_set(channel),
            "admin_count": len(_admin_set(channel))}


def t_report_guard(channel, contact_id, action="", **_):
    """记录一次非管理员的配置/功能修改尝试；单账号超过 10 次自动拉黑不再回复。"""
    if not channel or contact_id in (None, ""):
        raise ValueError("需要 channel 和 contact_id")
    count, blacklisted = _guard_hit(channel, contact_id, action or "unknown")
    return {"count": count, "blacklisted": blacklisted,
            **({"note": "已自动加入黑名单，之后其消息不再回复"} if blacklisted else {})}
