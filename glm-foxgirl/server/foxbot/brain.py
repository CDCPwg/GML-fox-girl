"""独立大脑守护进程（可选）：实时轮询收件箱，规则预筛后仅对需要人格的消息调用 LLM。

规则预筛（零成本）：黑白名单 → 聊天内管理指令 → 群聊回复必要性门控。
人格上下文：人格表（现有人格）+ 关系画像（身份组）+ 相关记忆 + 记忆图 + 最近对话。
与事件驱动看门互斥：二选一，避免双脑重复回复；可选值班表（brain.duty_windows）启用后自动换班。
LLM 走 OpenAI 兼容 /chat/completions（brain.api_base/api_key/model，默认关闭）。
"""
import json
import random
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta

from . import access, config, db, graph, identity, qq
from .memory import t_query_memory
from .persona import t_bot_command


def _llm_chat(system, user):
    cfg = config.load_config()["brain"]
    base = (cfg.get("api_base") or "").strip()
    if not base:
        raise RuntimeError("brain.api_base 未配置")
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    # temperature/max_tokens 可经 brain.temperature / brain.max_tokens 覆盖；
    # 思考型模型（如 kimi-k2.6）强制 temperature=1，reasoning 计入 max_tokens，需留足余量
    body = {"model": cfg.get("model", ""),
            "temperature": float(cfg.get("temperature", 1.0)),
            "max_tokens": int(cfg.get("max_tokens", 2000)),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data["choices"][0]["message"]["content"] or "").strip()


def _active_persona():
    with db.LOCK:
        row = db.CONN.execute("SELECT name, prompt, style FROM personas WHERE is_active=1").fetchone()
    return dict(row) if row else None


def _recent_chat(channel, chat_id, exclude_id=None, limit=10):
    with db.LOCK:
        rows = [dict(r) for r in db.CONN.execute(
            "SELECT id, direction, display_name, content, created_at FROM messages"
            " WHERE channel=? AND chat_id=? AND msg_type!='blocked' AND id!=?"
            " ORDER BY id DESC LIMIT ?",
            (channel, str(chat_id), exclude_id if exclude_id else -1,
             max(2, min(int(limit), 30))))]
    return list(reversed(rows))


def _mentioned(media_json, persona_name, text):
    try:
        for seg in json.loads(media_json or "[]"):
            if seg.get("type") == "at":
                return True
    except Exception:
        pass
    return bool(persona_name and persona_name in (text or ""))


def _build_context(msg, persona):
    channel, contact_id, chat_id = msg["channel"], msg["contact_id"], msg["chat_id"]
    prof = None
    try:
        ids = [i for _, i in identity._linked_ids(channel, contact_id)]
        marks = ",".join("?" * len(ids))
        with db.LOCK:
            prows = [dict(r) for r in db.CONN.execute(
                f"SELECT * FROM profiles WHERE contact_id IN ({marks})", ids)]
        prof = prows[0] if prows else None
    except Exception:
        prof = None
    mem = t_query_memory(contact_id=contact_id, channel=channel, limit=5).get("memories", [])
    graph_hint = ""
    try:
        gq = graph.t_graph_query(entity=(msg.get("display_name") or ""), depth=1, limit=8)
        if gq.get("found"):
            graph_hint = "；".join(f"{e['src_name']}-{e['relation']}-{e['dst_name']}"
                                   for e in gq["edges"][:6])
    except Exception:
        graph_hint = ""
    recent = _recent_chat(channel, chat_id, exclude_id=msg.get("id"))
    sys_prompt = (persona["prompt"] + ("\n\n说话风格：" + persona["style"] if persona["style"] else "")
                  + "\n\n【你正在 QQ 上与人聊天】直接输出要发送的回复文本：一条、短句、口语、"
                    "完全符合人格；不要解释、不要前缀引号、不要列表。如果这条消息不值得回复，只输出 [SKIP]。")
    user_parts = [f"对方：{msg.get('display_name') or contact_id}（{chat_id}）"]
    if prof:
        user_parts.append(f"好感：{prof.get('affinity', 0)}｜印象：{prof.get('impression') or '（无）'}"
                          f"｜相处方式：{prof.get('style_notes') or '（无）'}")
    if mem:
        user_parts.append("相关记忆：" + "；".join(m["content"][:60] for m in mem[:5]))
    if graph_hint:
        user_parts.append("关联事实：" + graph_hint)
    if recent:
        user_parts.append("最近对话：\n" + "\n".join(
            f"{'我' if r['direction'] == 'out' else (r['display_name'] or '对方')}：{r['content'][:80]}"
            for r in recent[-8:]))
    user_parts.append(f"这条新消息：{msg.get('content') or '（媒体消息）'}")
    return sys_prompt, "\n".join(user_parts)


def _sediment(msg, reply):
    """实时沉淀（实测区验证）：每条回复成功后一次轻量提取——事实记忆/印象增量/好感/记忆图。
    过短闲聊跳过；提取结果宁缺毋滥；任何异常静默跳过，绝不影响回复主链路。"""
    text = (msg.get("content") or "").strip()
    if len(text) < 5:
        return
    channel, contact_id, chat_id = msg["channel"], str(msg["contact_id"] or ""), msg["chat_id"]
    sys_prompt = (
        "你是记忆提取器。根据对话片段输出严格 JSON（无 markdown、无解释、无多余键），字段："
        '{"memory":"值得长期记住的事实（对方偏好/约定/重要事件/关系变化；纯闲聊则为空字符串）",'
        '"importance":1到5,"affinity_delta":-5到5,'
        '"impression_add":"值得追加到印象的一句话（没有则为空字符串）",'
        '"graph":[{"s":"主体","r":"关系(喜欢/讨厌/养了/在玩/擅长/提到/约定等)","t":"对象"}]}'
        "。宁缺毋滥：没有值得记的就留空、0、空数组。")
    user = (f"对方：{msg.get('display_name') or contact_id}\n"
            f"对方说：{text[:300]}\n我回复：{reply[:200]}")
    try:
        raw = _llm_chat(sys_prompt, user)
    except Exception:
        return
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return
    try:
        data = json.loads(m.group(0))
    except Exception:
        return
    try:
        from .identity import t_manage_profile
        from .memory import t_store_memory
        mem = str(data.get("memory") or "").strip()
        if mem:
            t_store_memory(mem[:200], channel=channel, chat_id=str(chat_id),
                           contact_id=contact_id, kind="fact",
                           importance=max(1, min(5, int(data.get("importance") or 3))))
        delta = max(-5, min(5, int(data.get("affinity_delta") or 0)))
        add = str(data.get("impression_add") or "").strip()
        if add or delta:
            with db.LOCK:
                row = db.CONN.execute(
                    "SELECT impression FROM profiles WHERE channel=? AND contact_id=?",
                    (channel, contact_id)).fetchone()
            base = dict(row)["impression"] if row else ""
            merged = ((base + ("；" if base else "") + add)[:400]) if add else base
            t_manage_profile("upsert", channel=channel, contact_id=contact_id,
                             impression=merged or None, affinity_delta=delta)
        for g in (data.get("graph") or [])[:3]:
            s = str(g.get("s") or "").strip()[:30]
            r = str(g.get("r") or "").strip()[:10]
            t = str(g.get("t") or "").strip()[:30]
            if s and r and t:
                graph.t_graph_add(s, r, t)
    except Exception as e:
        print(f"[glm-foxgirl] brain sediment error: {e}", file=sys.stderr)


def _send_reply(msg, text):
    if msg["channel"] == "qq":
        return qq.t_send_qq_message(chat_type=msg["chat_type"], chat_id=msg["chat_id"],
                                    text=text, contact_id=msg["contact_id"])
    return {"sent": False, "reason": "独立大脑 v1 仅处理 QQ 渠道；微信消息留给工作台处理"}


def _canned_command_reply(cmd):
    """守护进程场景下为无 reply 的指令生成罐装回执（ZCode 大脑场景由技能负责）。"""
    d = cmd.get("data") or {}
    if cmd.get("action") == "status":
        return (f"[状态] 激活人格：{d.get('active_persona') or '（无）'}｜待处理消息：{d.get('pending_messages')}"
                f"｜记忆：{d.get('memories')}｜collector：{'运行中' if d.get('collector_running') else '未运行'}")
    if cmd.get("action") == "handover":
        return "[交接] 会话总结与存档需在工作台（ZCode 聊天区）执行，这里无法代劳。"
    return None


def _llm_backoff_active(cfg):
    """LLM 连续失败退避：streak>0 且距上次失败不足退避时长（5s×2^n，封顶
    brain.backoff_max_seconds）时跳过本轮 LLM 调用，避免守护进程对宕机 API 空转。"""
    streak = int(cfg.get("brain", {}).get("fail_streak", 0))
    if streak <= 0:
        return False
    last = cfg.get("brain", {}).get("last_fail", "")
    elapsed = time.time() - db._parse_dt(last) if last else 1e18
    delay = min(float(cfg.get("brain", {}).get("backoff_max_seconds", 300)),
                5.0 * (2 ** (streak - 1)))
    return elapsed < delay


def _llm_ok():
    with db.LOCK:
        cfg = config.load_config()
        cfg.setdefault("brain", {})["fail_streak"] = 0
        cfg["brain"]["last_fail"] = ""
        config.save_config(cfg)


def _llm_fail():
    with db.LOCK:
        cfg = config.load_config()
        cfg.setdefault("brain", {})
        cfg["brain"]["fail_streak"] = int(cfg["brain"].get("fail_streak", 0)) + 1
        cfg["brain"]["last_fail"] = db.now_str()
        config.save_config(cfg)


def brain_process_once(stats=None):
    """单次处理轮：规则预筛 → LLM 人格回复 → 发送 → 收尾。返回统计。"""
    stats = stats or {"seen": 0, "replied": 0, "skipped": 0, "blocked": 0, "commands": 0,
                      "errors": 0, "backoff": 0}
    cfg = config.load_config()
    from .session import t_get_pending_messages, t_mark_processed
    data = t_get_pending_messages(limit=30)
    persona = _active_persona()
    group_p = float(cfg.get("proactive", {}).get("reply_probability_group", 0.3))
    for msg in data.get("messages", []):
        stats["seen"] += 1
        mid = msg["id"]
        channel, chat_type, chat_id = msg["channel"], msg["chat_type"], msg["chat_id"]
        contact_id = str(msg["contact_id"] or "")

        def done():
            t_mark_processed([mid])

        decision = access.access_decision(channel, chat_type, chat_id, contact_id)
        if not decision["allowed"]:
            stats["blocked"] += 1
            done()
            continue
        cmd = t_bot_command(text=msg.get("content") or "", channel=channel,
                            contact_id=contact_id, chat_id=chat_id)
        if cmd.get("handled"):
            stats["commands"] += 1
            if cmd.get("allowed") and cmd.get("reply"):
                _send_reply(msg, cmd["reply"])
            elif cmd.get("allowed"):
                canned = _canned_command_reply(cmd)
                if canned:
                    _send_reply(msg, canned)
            done()
            continue
        if channel != "qq":
            stats["skipped"] += 1
            continue  # 独立大脑 v1 只处理 QQ；微信留给工作台
        if not persona:
            stats["skipped"] += 1
            done()
            continue
        if chat_type == "group":
            mentioned = _mentioned(msg.get("media_json"), persona["name"], msg.get("content"))
            if not mentioned and random.random() >= group_p:
                stats["skipped"] += 1
                done()
                continue
        if not (msg.get("content") or "").strip():
            stats["skipped"] += 1
            done()
            continue
        if _llm_backoff_active(cfg):
            # LLM 不可用退避中：消息保留未处理，退避结束后自动补上
            stats["backoff"] = stats.get("backoff", 0) + 1
            continue
        try:
            system, user = _build_context(msg, persona)
        except Exception as e:
            stats["errors"] += 1
            print(f"[glm-foxgirl] brain context error: {e}", file=sys.stderr)
            done()
            continue
        try:
            reply = _llm_chat(system, user)
            _llm_ok()
        except Exception as e:
            _llm_fail()
            stats["errors"] += 1
            print(f"[glm-foxgirl] brain llm error: {e}", file=sys.stderr)
            continue
        reply = re.sub(r"^['\"“”]+|['\"“”]+$", "", reply or "").strip()
        if not reply or "[SKIP]" in reply:
            stats["skipped"] += 1
            done()
            continue
        # 换班边界防重：发送前复查处理状态——若消息已被看门/夜班处理（processed=1）
        # 则撤销本次回复（边界前后的消息不无视：未被回复的照常回复，已回复的不重复回）
        with db.LOCK:
            row = db.CONN.execute("SELECT processed FROM messages WHERE id=?",
                                  (mid,)).fetchone()
        if row and row[0]:
            stats["skipped"] += 1
            continue
        sent = _send_reply(msg, reply)
        if sent.get("sent"):
            stats["replied"] += 1
            done()
            _sediment(msg, reply)
        elif sent.get("blocked"):
            # 拦截类失败（限频/泄密防火墙）为终态：标记处理，避免守护进程无限重试
            stats["skipped"] += 1
            done()
        else:
            # 网络类失败（NapCat 不可达等）：保留未处理，下轮重试
            stats["errors"] += 1
    return stats


_DUTY_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


def _duty_windows():
    """值班表（可选，含自动换班）：brain.duty_windows，逗号分隔多段 "HH:MM-HH:MM"，
    如 "09:00-23:00" 或 "22:00-06:00,09:00-12:00"（支持跨午夜，段数不限）。
    未设置 → 全天值班：LLM 与看门二选一处理消息，无换班；设置后换班自动生效
    （换班内置于值班表，不单设开关）。兼容旧键：仅设 duty_start_hour/duty_end_hour
    时视为单段 "HH:00-HH:00"。返回 [(start_min, end_min), ...] 或 None。"""
    bcfg = config.load_config().get("brain", {})
    raw = str(bcfg.get("duty_windows") or "").strip()
    if not raw:
        sh, eh = bcfg.get("duty_start_hour"), bcfg.get("duty_end_hour")
        if sh is None or eh is None:
            return None
        try:
            raw = f"{int(sh):02d}:00-{int(eh):02d}:00"
        except (TypeError, ValueError):
            return None
    segs = []
    for part in raw.split(","):
        m = _DUTY_RE.match(part.strip())
        if not m:
            continue
        s = int(m.group(1)) * 60 + int(m.group(2))
        e = int(m.group(3)) * 60 + int(m.group(4))
        if s <= 1440 and e <= 1440 and s != e:
            segs.append((s, e))
    return segs or None


def _in_duty_window(now=None):
    """此刻是否处于值班表内：未启用值班表 → 恒 True（全天值班）。"""
    segs = _duty_windows()
    if not segs:
        return True
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    for s, e in segs:
        if s < e:
            if s <= cur < e:
                return True
        elif cur >= s or cur < e:  # 跨午夜段
            return True
    return False


def _next_duty_start(cur_min):
    """下一班开工的分钟数：今天尚未开始的最早段起点，否则明天最早班。"""
    starts = sorted(s for s, _ in (_duty_windows() or []))
    for s in starts:
        if s > cur_min:
            return s
    return starts[0]


_proactive_next = [0.0]


def _brain_proactive():
    """值班期主动搭话：随机 20~45 分钟一次，额度/话题门控，宁缺毋滥。"""
    if time.time() < _proactive_next[0]:
        return
    _proactive_next[0] = time.time() + random.uniform(20 * 60, 45 * 60)
    try:
        from .session import t_proactive_due
        if not t_proactive_due().get("proactive_allowed"):
            return
        persona = _active_persona()
        if not persona:
            return
        with db.LOCK:
            row = db.CONN.execute(
                "SELECT channel, contact_id, display_name, affinity, impression, style_notes"
                " FROM profiles WHERE affinity > 0 ORDER BY RANDOM() LIMIT 1").fetchone()
        if not row:
            return
        prof = dict(row)
        if prof["channel"] != "qq":
            return  # 独立大脑 v1 只发 QQ
        mem = t_query_memory(contact_id=prof["contact_id"], channel=prof["channel"],
                             limit=5).get("memories", [])
        gq = graph.t_graph_query(entity=(prof.get("display_name") or ""), depth=1, limit=8)
        graph_hint = ""
        if gq.get("found"):
            graph_hint = "；".join(f"{e['src_name']}-{e['relation']}-{e['dst_name']}"
                                   for e in gq["edges"][:6])
        mood = (config.load_config().get("mood", {}) or {}).get("current", 0)
        sys_prompt = (persona["prompt"] + ("\n\n说话风格：" + persona["style"] if persona["style"] else "")
                      + "\n\n【你正在主动找对方搭话】直接输出要发送的一条开场白：短句、口语、自然"
                        "（\"突然想起…\"式），结合下面的话题素材；没有自然的话头就只输出 [SKIP]，绝不硬聊。")
        parts = [f"对方：{prof.get('display_name') or prof['contact_id']}（好感 {prof.get('affinity', 0)}）"]
        if prof.get("impression"):
            parts.append("印象：" + str(prof["impression"])[:200])
        if prof.get("style_notes"):
            parts.append("相处方式：" + str(prof["style_notes"])[:100])
        if mem:
            parts.append("相关记忆：" + "；".join(m["content"][:60] for m in mem[:5]))
        if graph_hint:
            parts.append("关联事实：" + graph_hint)
        if mood:
            parts.append(f"当前心情温度：{mood:+d}")
        recent = _recent_chat(prof["channel"], prof["contact_id"], limit=6)
        if recent:
            parts.append("最近对话：\n" + "\n".join(
                f"{'我' if r['direction'] == 'out' else (r['display_name'] or '对方')}：{r['content'][:60]}"
                for r in recent[-6:]))
        reply = _llm_chat(sys_prompt, "\n".join(parts))
        reply = re.sub(r"^['\"“”]+|['\"“”]+$", "", reply or "").strip()
        if not reply or "[SKIP]" in reply:
            return
        qq.t_send_qq_message(chat_type="private", chat_id=prof["contact_id"],
                             contact_id=prof["contact_id"], text=reply, out_kind="proactive")
    except Exception as e:
        print(f"[glm-foxgirl] brain proactive error: {e}", file=sys.stderr)


def main_brain(once=False):
    cfg = config.load_config()["brain"]
    if not (cfg.get("api_base") and cfg.get("model")):
        print("[glm-foxgirl] 独立大脑未配置：需 set_config brain.api_base / brain.api_key / brain.model "
              "（OpenAI 兼容接口）。注意：与事件驱动看门互斥，二选一。", file=sys.stderr)
        sys.exit(2)
    db.init_db()
    qq.start_collector_thread()
    segs = _duty_windows()
    duty_txt = ("全天值班（未启用值班表）" if not segs
                else "值班表 " + str(config.load_config().get("brain", {}).get("duty_windows")))
    print(f"[glm-foxgirl] 独立大脑运行中：model={cfg['model']} poll={cfg.get('poll_seconds', 5)}s"
          f"（{duty_txt}；与看门脚本互斥，勿同时开启）", file=sys.stderr)
    if once:
        print(json.dumps(brain_process_once(), ensure_ascii=False), file=sys.stderr)
        return
    while True:
        try:
            if _in_duty_window():
                brain_process_once()
                _brain_proactive()
            else:
                # 值班表启用且当前歇班：长睡到下一班开工，期间零查询零调用
                now = datetime.now()
                cur = now.hour * 60 + now.minute
                start = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                         + timedelta(minutes=_next_duty_start(cur)))
                if start <= now:
                    start += timedelta(days=1)
                time.sleep(max(30, (start - now).total_seconds()))
                continue
        except Exception as e:
            print(f"[glm-foxgirl] brain loop error: {e}", file=sys.stderr)
        time.sleep(max(2, int(cfg.get("poll_seconds", 5))))
