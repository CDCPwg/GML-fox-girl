"""会话与状态：bot_status、配置、收件箱、统计、追溯、接力、自动交接、主动聊天判定。"""
import os
import re
import shutil
from datetime import datetime, timedelta

from . import access, config, db, qq
from .memory import _memory_score


def t_bot_status(**_):
    cfg = config.load_config()
    with db.LOCK:
        def one(sql):
            return db.CONN.execute(sql).fetchone()[0]
        active = db.CONN.execute("SELECT name FROM personas WHERE is_active=1").fetchone()
        pending = one("SELECT COUNT(*) FROM messages WHERE direction='in' AND processed=0")
        status = {
            "version": config.VERSION, "data_dir": config.DATA_DIR, "db": config.DB_PATH,
            "active_persona": active["name"] if active else None,
            "counts": {"personas": one("SELECT COUNT(*) FROM personas"),
                       "pending_messages": pending, "memories": one("SELECT COUNT(*) FROM memories"),
                       "profiles": one("SELECT COUNT(*) FROM profiles"),
                       "rules": one("SELECT COUNT(*) FROM rules"),
                       "stickers": one("SELECT COUNT(*) FROM stickers"),
                       "outbox": one("SELECT COUNT(*) FROM outbox"),
                       "graph_nodes": one("SELECT COUNT(*) FROM graph_nodes"),
                       "graph_edges": one("SELECT COUNT(*) FROM graph_edges"),
                       "notes": one("SELECT COUNT(*) FROM session_notes"),
                       "guard": one("SELECT COUNT(*) FROM guard")},
            "intro_shown": bool(load_intro().get("shown")),
            "setup_completed": bool(cfg.get("setup", {}).get("completed_at")),
            "session": dict(cfg.get("session", {})),
            "qq": {"enabled": cfg["qq"]["enabled"], "napcat_base_url": cfg["qq"]["napcat_base_url"],
                   "token_set": bool(cfg["qq"]["napcat_token"])},
            "wechat": {"enabled": cfg["wechat"]["enabled"], "adb_path": cfg["wechat"]["adb_path"],
                       "adb_found": bool(shutil.which(cfg["wechat"]["adb_path"])) or os.path.sep in cfg["wechat"]["adb_path"],
                       "device_serial": cfg["wechat"]["device_serial"] or "(自动检测)"},
            "memory_embed": bool(cfg.get("memory", {}).get("embed_base_url")),
            "ffmpeg": bool(shutil.which(cfg.get("video", {}).get("ffmpeg_path", "ffmpeg"))),
            "collector": {"port": cfg["collector"]["port"], "running": qq.collector_running(),
                          "url": f"http://{cfg['collector']['host']}:{cfg['collector']['port']}/onebot/event"},
        }
    return status


def load_intro():
    return config.load_config().get("intro", {})


def t_configure_channel(channel, enabled=None, napcat_base_url=None, napcat_token=None,
                        adb_path=None, device_serial=None, collector_port=None, **_):
    if channel not in ("qq", "wechat"):
        raise ValueError("channel 必须是 qq 或 wechat")
    cfg = config.load_config()
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
    config.save_config(cfg)
    out = dict(cfg)
    out["_note"] = "token 已脱敏；修改渠道配置后若 collector 端口变化需重启 MCP/collector"
    if channel == "qq":
        out["qq"] = {**cfg["qq"], "napcat_token": "***" if cfg["qq"]["napcat_token"] else ""}
    return out


def t_get_config(**_):
    cfg = config.load_config()
    cfg["qq"]["napcat_token"] = "***" if cfg["qq"]["napcat_token"] else ""
    cfg["_data_dir"] = config.DATA_DIR
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
    cfg = config.load_config()
    node = cfg
    parts = key.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
    config.save_config(cfg)
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
    with db.LOCK:
        rows = [dict(r) for r in db.CONN.execute(q, args).fetchall()]
    return {"count": len(rows), "messages": rows}


def t_mark_processed(message_ids, **_):
    ids = [int(i) for i in message_ids]
    with db.LOCK:
        db.CONN.executemany("UPDATE messages SET processed=1 WHERE id=?", [(i,) for i in ids])
        db.CONN.commit()
    return {"ok": True, "marked": ids}


def t_simulate_inbound(channel, chat_type, chat_id, contact_id, display_name="", text="", media_url="", **_):
    if channel not in ("qq", "wechat"):
        raise ValueError("channel 必须是 qq 或 wechat")
    msg_type = "image" if media_url and not text else "text"
    with db.LOCK:
        cur = db.CONN.execute(
            "INSERT INTO messages(direction, channel, chat_type, chat_id, contact_id, display_name,"
            " msg_type, content, media_url, processed, created_at)"
            " VALUES('in',?,?,?,?,?,?,?,?,0,?)",
            (channel, chat_type, str(chat_id), str(contact_id), display_name, msg_type, text, media_url, db.now_str()))
        db.CONN.commit()
        return {"ok": True, "message_id": cur.lastrowid}


def t_check_access(channel, chat_type, chat_id, contact_id, **_):
    return access.access_decision(channel, chat_type, str(chat_id), str(contact_id) if contact_id else None)


def t_proactive_due(**_):
    cfg = config.load_config()["proactive"]
    if not cfg.get("enabled"):
        return {"proactive_allowed": False, "reason": "proactive.enabled=false"}
    if access.in_quiet_hours(cfg.get("quiet_hours", "")):
        return {"proactive_allowed": False, "reason": f"安静时段 {cfg.get('quiet_hours')}"}
    today = datetime.now().strftime("%Y-%m-%d")
    with db.LOCK:
        used = db.CONN.execute(
            "SELECT COUNT(*) FROM messages WHERE direction='out' AND out_kind='proactive' AND created_at LIKE ?",
            (today + "%",)).fetchone()[0]
    mx = int(cfg.get("max_per_day", 5))
    if used >= mx:
        return {"proactive_allowed": False, "reason": f"今日主动消息已达上限 {mx}"}
    return {"proactive_allowed": True, "used_today": used, "max_per_day": mx,
            "group_reply_probability": cfg.get("reply_probability_group", 0.3)}


def t_get_stats(**_):
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.now().strftime("%Y-%m-%d")
    with db.LOCK:
        def one(sql, args=()):
            return db.CONN.execute(sql, args).fetchone()[0]
        in7 = one("SELECT COUNT(*) FROM messages WHERE direction='in' AND created_at>=?", (week_ago,))
        out7 = one("SELECT COUNT(*) FROM messages WHERE direction='out' AND created_at>=?", (week_ago,))
        stats = {
            "week": {"in": in7, "out": out7,
                     "proactive": one("SELECT COUNT(*) FROM messages WHERE direction='out' AND out_kind='proactive' AND created_at>=?", (week_ago,)),
                     "reply_rate": round(out7 / in7, 3) if in7 else 0.0},
            "today_in": one("SELECT COUNT(*) FROM messages WHERE direction='in' AND created_at>=?", (today,)),
            "outbox_pending": one("SELECT COUNT(*) FROM outbox"),
            "memories": one("SELECT COUNT(*) FROM memories"),
            "stickers": one("SELECT COUNT(*) FROM stickers"),
            "mood": int(config.load_config().get("mood", {}).get("current", 0)),
            "top_profiles": [dict(r) for r in db.CONN.execute(
                "SELECT channel, contact_id, display_name, affinity FROM profiles ORDER BY affinity DESC LIMIT 5")],
            "top_stickers": [dict(r) for r in db.CONN.execute(
                "SELECT emotion, tags, use_count FROM stickers ORDER BY use_count DESC LIMIT 5")],
        }
    return stats


def t_search_messages(keyword="", channel="", contact_id="", chat_id="", since="", until="",
                      limit=50, **_):
    """追溯查找全部收发记录（永久保留）：关键词/渠道/联系人/会话/时间范围。"""
    q, args = "SELECT * FROM messages WHERE 1=1", []
    if keyword:
        q += " AND (content LIKE ? OR display_name LIKE ?)"
        args += [f"%{keyword}%", f"%{keyword}%"]
    if channel:
        q += " AND channel=?"
        args.append(channel)
    if contact_id:
        q += " AND contact_id=?"
        args.append(str(contact_id))
    if chat_id:
        q += " AND chat_id=?"
        args.append(str(chat_id))
    if since:
        q += " AND created_at>=?"
        args.append(since)
    if until:
        q += " AND created_at<=?"
        args.append(until + (" 23:59:59" if len(until) == 10 else ""))
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    with db.LOCK:
        rows = [dict(r) for r in db.CONN.execute(q, args).fetchall()]
    return {"count": len(rows), "messages": rows,
            "hint": "收发记录永久保留可追溯；WebUI'记录'页同样可查；export_transcript 可导出 Markdown 存档"}


def t_export_transcript(since="", until="", channel="", chat_id="", **_):
    """把一段时间的收发记录导出为 Markdown 存档（时间正序）。"""
    data = t_search_messages(channel=channel, chat_id=chat_id, since=since, until=until, limit=200)
    rows = list(reversed(data["messages"]))
    tdir = os.path.join(config.DATA_DIR, "transcripts")
    os.makedirs(tdir, exist_ok=True)
    path = os.path.join(tdir, f"transcript_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# 对话记录导出（{db.now_str()}）\n\n- 范围：{since or '起'} ~ {until or '今'}"
                f"｜渠道：{channel or '全部'}｜会话：{chat_id or '全部'}\n- 条数：{len(rows)}\n\n")
        for r in rows:
            who = r.get("display_name") or r.get("contact_id") or "?"
            arrow = "→发" if r["direction"] == "out" else "←收"
            f.write(f"- `{r['created_at']}` {arrow} **{who}** ({r['channel']}/{r['chat_type']})："
                    f"{r['content']}\n")
    return {"ok": True, "path": path, "count": len(rows)}


def t_session_note(action="list", note_id=None, title="", content="", limit=5, **_):
    """会话接力笔记：换新对话前由大脑总结本会话存档，新对话开机读取即可接上。"""
    with db.LOCK:
        if action == "save":
            if not content:
                raise ValueError("content 不能为空")
            cur = db.CONN.execute(
                "INSERT INTO session_notes(title, content, created_at, updated_at) VALUES(?,?,?,?)",
                (title or db.now_str()[:10] + " 接力", content, db.now_str(), db.now_str()))
            db.CONN.commit()
            return {"ok": True, "note_id": cur.lastrowid}
        if action == "get":
            if not note_id:
                raise ValueError("get 需要 note_id")
            row = db.CONN.execute("SELECT * FROM session_notes WHERE id=?", (int(note_id),)).fetchone()
            return {"note": dict(row) if row else None}
        if action == "list":
            rows = [dict(r) for r in db.CONN.execute(
                "SELECT id, title, substr(content, 1, 150) AS preview, created_at FROM session_notes"
                " ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 50)),))]
            return {"notes": rows}
        if action == "delete":
            if not note_id:
                raise ValueError("delete 需要 note_id")
            db.CONN.execute("DELETE FROM session_notes WHERE id=?", (int(note_id),))
            db.CONN.commit()
            return {"ok": True}
    raise ValueError(f"未知 action: {action}")


def t_session_progress(note="", **_):
    """会话进度标记：每轮用户交互/心跳处理完后调用。按轮数折算上下文占用估算百分比：
    warn_pct（默认85%）提醒，handover_pct（默认95%）触发自动交接。
    新会话由 SessionStart hook 归零。开新页需用户手动，或 limits.auto_reopen=true 时由大脑建定时任务。"""
    cfg = config.load_config()
    lim = cfg.get("limits", {})
    if not lim.get("auto_handover_enabled", True):
        return {"level": "off", "auto_handover": False, "reason": "limits.auto_handover_enabled=false"}
    turns = int(cfg.get("session", {}).get("turns", 0)) + 1
    full_turns = max(1, int(lim.get("auto_handover_turns", 40)))
    pct = min(100, round(turns / full_turns * 100))
    warn_pct = max(1, min(99, int(lim.get("warn_pct", 85))))
    handover_pct = max(warn_pct, min(100, int(lim.get("handover_pct", 95))))
    cfg.setdefault("session", {})
    cfg["session"]["turns"] = turns
    cfg["session"]["estimated_pct"] = pct
    if note:
        cfg["session"]["last_note"] = note[:200]
    cfg["session"]["updated_at"] = db.now_str()
    config.save_config(cfg)
    level = "handover" if pct >= handover_pct else ("warn" if pct >= warn_pct else "ok")
    resp = {"turns": turns, "estimated_pct": pct, "warn_pct": warn_pct,
            "handover_pct": handover_pct, "level": level,
            "auto_handover": level == "handover",
            "note": "pct 为按轮数折算的估算值（ZCode 未向插件暴露真实上下文百分比）"}
    if level == "warn":
        resp["hint"] = f"上下文约 {pct}%：用一句话提醒用户接近上限，建议说'交接'或开新页"
    if level == "handover":
        resp["hint"] = (f"上下文约 {pct}%：立即自动交接——总结本会话 → session_note(action=save) → "
                        "提醒用户新开对话页；limits.auto_reopen=true 时用 CronCreate 建 1 分钟后"
                        "的一次性'恢复值机'任务自动开新页")
    return resp
