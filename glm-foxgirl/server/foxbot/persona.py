"""人格库（CRUD/导出导入/SillyTavern 角色卡）、黑白名单、心情、聊天内管理指令。"""
import base64
import json
import os
import re

from . import access, config, db


def t_manage_persona(action, name=None, prompt=None, style=None, persona_id=None, file_path=None, **_):
    with db.LOCK:
        if action == "list":
            rows = [dict(r) for r in db.CONN.execute("SELECT * FROM personas ORDER BY is_active DESC, id")]
            return {"personas": rows, "active": next((r["name"] for r in rows if r["is_active"]), None)}
        if action == "create":
            if not name or not prompt:
                raise ValueError("create 需要 name 和 prompt")
            cur = db.CONN.execute(
                "INSERT INTO personas(name, prompt, style, is_active, created_at, updated_at) VALUES(?,?,?,0,?,?)",
                (name, prompt, style or "", db.now_str(), db.now_str()))
            db.CONN.commit()
            return {"ok": True, "persona_id": cur.lastrowid,
                    "hint": "调用 manage_persona action=set_active 启用"}
        if action == "update":
            if not (persona_id or name):
                raise ValueError("update 需要 persona_id 或 name 定位")
            row = db.CONN.execute("SELECT id FROM personas WHERE id=? OR name=?", (persona_id or -1, name or "")).fetchone()
            if not row:
                raise ValueError("人格不存在")
            sets, args = ["updated_at=?"], [db.now_str()]
            if prompt is not None:
                sets.append("prompt=?"); args.append(prompt)
            if style is not None:
                sets.append("style=?"); args.append(style)
            args.append(row["id"])
            db.CONN.execute(f"UPDATE personas SET {', '.join(sets)} WHERE id=?", args)
            db.CONN.commit()
            return {"ok": True}
        if action == "set_active":
            row = db.CONN.execute("SELECT id FROM personas WHERE id=? OR name=?", (persona_id or -1, name or "")).fetchone()
            if not row:
                raise ValueError("人格不存在")
            db.CONN.execute("UPDATE personas SET is_active=0")
            db.CONN.execute("UPDATE personas SET is_active=1 WHERE id=?", (row["id"],))
            db.CONN.commit()
            return {"ok": True}
        if action == "delete":
            if not (persona_id or name):
                raise ValueError("delete 需要 persona_id 或 name")
            db.CONN.execute("DELETE FROM personas WHERE id=? OR name=?", (persona_id or -1, name or ""))
            db.CONN.commit()
            return {"ok": True}
        if action == "export":
            row = db.CONN.execute("SELECT * FROM personas WHERE id=? OR name=?",
                                  (persona_id or -1, name or "")).fetchone()
            if not row:
                raise ValueError("人格不存在")
            pdir = os.path.join(config.DATA_DIR, "personas")
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
            while db.CONN.execute("SELECT 1 FROM personas WHERE name=?", (pname,)).fetchone():
                n += 1
                pname = f"{base}_{n}"
            cur = db.CONN.execute(
                "INSERT INTO personas(name, prompt, style, is_active, created_at, updated_at) VALUES(?,?,?,0,?,?)",
                (pname, pprompt, str(data.get("style") or ""), db.now_str(), db.now_str()))
            db.CONN.commit()
            return {"ok": True, "persona_id": cur.lastrowid, "name": pname,
                    "hint": "调用 manage_persona action=set_active 启用"}
    raise ValueError(f"未知 action: {action}")


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
    with db.LOCK:
        base, n = name, 1
        while db.CONN.execute("SELECT 1 FROM personas WHERE name=?", (name,)).fetchone():
            n += 1
            name = f"{base}_{n}"
        cur = db.CONN.execute(
            "INSERT INTO personas(name, prompt, style, is_active, created_at, updated_at)"
            " VALUES(?,?,?,0,?,?)", (name, prompt, style, db.now_str(), db.now_str()))
        db.CONN.commit()
        return {"ok": True, "persona_id": cur.lastrowid, "name": name,
                "hint": "manage_persona set_active 启用；建议人工过一遍 prompt 补充说话风格与雷点"}


def t_manage_rules(action, list_type=None, scope="contact", channel="*", target_id=None, note="", **_):
    if action == "list":
        with db.LOCK:
            rows = [dict(r) for r in db.CONN.execute("SELECT * FROM rules ORDER BY list_type, id")]
        return {"rules": rows}
    if action == "add":
        if list_type not in ("blacklist", "whitelist") or not target_id:
            raise ValueError("add 需要 list_type(blacklist/whitelist) 和 target_id")
        if scope not in ("contact", "group", "global"):
            raise ValueError("scope 必须是 contact/group/global")
        if scope == "global":
            target_id, channel = "*", "*"
        with db.LOCK:
            db.CONN.execute(
                "INSERT OR IGNORE INTO rules(list_type, channel, scope, target_id, note, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (list_type, channel, scope, str(target_id), note, db.now_str()))
            db.CONN.commit()
        return {"ok": True}
    if action == "remove":
        if not target_id:
            raise ValueError("remove 需要 target_id")
        with db.LOCK:
            cur = db.CONN.execute(
                "DELETE FROM rules WHERE target_id=? AND list_type IS ? AND scope IS ? AND channel IS ?",
                (str(target_id), list_type, scope, channel))
            db.CONN.commit()
        return {"ok": True, "removed": cur.rowcount}
    raise ValueError(f"未知 action: {action}")


def t_manage_mood(action="get", mood=0, reason="", **_):
    """心情状态层：叠加在人格之上的临时情绪温度（-2 很down ~ +2 很嗨）。"""
    cfg = config.load_config()
    if action == "get":
        return {"mood": int(cfg.get("mood", {}).get("current", 0)),
                "reason": cfg.get("mood", {}).get("reason", ""),
                "scale": "-2 低落 ~ 0 平静 ~ +2 高涨；影响说话温度与主动意愿，由大脑随互动更新"}
    if action == "set":
        m = max(-2, min(2, int(mood)))
        cfg["mood"] = {"current": m, "reason": reason or "", "updated_at": db.now_str()}
        config.save_config(cfg)
        return {"ok": True, "mood": m, "reason": reason or ""}
    raise ValueError(f"未知 action: {action}")


PERSONA_CMDS = (
    ("/personahelp", "help"), ("/personas", "list"), ("/botstatus", "status"), ("/persona", "switch"),
    ("/handover", "handover"),
    ("切换人格", "switch"), ("人格列表", "list"), ("机器人状态", "status"), ("人格指令", "help"),
    ("交接", "handover"),
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
    if str(contact_id) not in access._admin_set(channel):
        count, blacklisted = access._guard_hit(channel, contact_id, "cmd:" + action)
        extra = ("（多次尝试修改，已自动加入黑名单）" if blacklisted
                 else (f"（警告 {count}/10，超过 10 次将自动拉黑）" if count > 3 else ""))
        return {"handled": True, "allowed": False, "action": action,
                "reply": "这条指令只有管理员能用。" + extra,
                "violations": count, "blacklisted": blacklisted}
    with db.LOCK:
        if action == "switch":
            prev = db.CONN.execute("SELECT name FROM personas WHERE is_active=1").fetchone()
            names = [r["name"] for r in db.CONN.execute("SELECT name FROM personas ORDER BY id")]
            if not arg:
                return {"handled": True, "allowed": True, "action": "switch", "ok": False,
                        "reply": "用法：切换人格 <名称>。现有：" + ("、".join(names) or "（库是空的）")}
            row = db.CONN.execute("SELECT id, name FROM personas WHERE name=?", (arg,)).fetchone()
            if not row:
                return {"handled": True, "allowed": True, "action": "switch", "ok": False,
                        "reply": f"没有叫「{arg}」的人格。现有：{'、'.join(names) or '（库是空的）'}"}
            db.CONN.execute("UPDATE personas SET is_active=0")
            db.CONN.execute("UPDATE personas SET is_active=1 WHERE id=?", (row["id"],))
            db.CONN.commit()
            return {"handled": True, "allowed": True, "action": "switch", "ok": True,
                    "reply": f"已切换：{prev['name'] if prev else '（无）'} → {row['name']}，接下来的话就换这个人格说啦",
                    "data": {"active": row["name"]}}
        if action == "list":
            rows = [dict(r) for r in db.CONN.execute(
                "SELECT name, style, is_active FROM personas ORDER BY is_active DESC, id")]
            lines = [f"{'▶ ' if r['is_active'] else '· '}{r['name']}" + (f"（{r['style'][:20]}）" if r["style"] else "")
                     for r in rows]
            return {"handled": True, "allowed": True, "action": "list",
                    "reply": "人格库：\n" + "\n".join(lines) + "\n切换用：切换人格 <名称>"}
        if action == "handover":
            return {"handled": True, "allowed": True, "action": "handover", "reply": None,
                    "data": {"hint": "立即执行会话交接：总结本会话 → session_note(action=save) → 回复确认；"
                                     "limits.auto_reopen=true 时可用 CronCreate 建 1 分钟后的一次性"
                                     "'恢复值机'任务自动开新页"}}
    if action == "status":
        from . import session
        st = session.t_bot_status()
        data = {"active_persona": st.get("active_persona"),
                "pending_messages": st["counts"]["pending_messages"],
                "memories": st["counts"]["memories"], "rules": st["counts"]["rules"],
                "stickers": st["counts"]["stickers"], "collector_running": st["collector"]["running"]}
        return {"handled": True, "allowed": True, "action": "status", "reply": None, "data": data}
    return {"handled": True, "allowed": True, "action": "help",
            "reply": "管理指令：切换人格 <名称> / 人格列表 / 机器人状态 / 人格指令"}
