"""独立大脑守护进程（可选）：实时轮询收件箱，规则预筛后仅对需要人格的消息调用 LLM。

多任务版（v1.6）：待处理消息按 (渠道, 会话) 分组，每个会话组一个独立任务——
组内同一人连发的多条新消息合并成一次 LLM 调用一并回应，组与组之间在线程池里
并发处理（brain.max_concurrency，默认 3），多人同时说话互不阻塞、回复不掉线。
QQ/微信是两条独立车道（brain.channels，默认仅 qq；"qq,wechat" 开启微信车道），
上下文互不掺和、提示词标明所在平台；微信发送默认 guard 模式——先读屏确认当前
聊天窗就是对方才发，防串台错发（brain.wechat_send_mode: guard/on/off）。
人格上下文：人格表 + 关系画像（身份组）+ 相关记忆 + 记忆图 + 最近对话
（私聊按身份组跨渠道取——QQ/微信双端关联后，换端能接上另一端的对话）。
规则预筛（零成本）：黑白名单 → 聊天内管理指令 → 群聊回复必要性门控。
与事件驱动看门互斥：二选一，避免双脑重复回复；可选值班表（brain.duty_windows）
启用后自动换班。LLM 走 OpenAI 兼容 /chat/completions（brain.api_base/api_key/model）。
"""
import concurrent.futures
import json
import random
import re
import sys
import threading
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


def _fmt_chat_line(row, current_channel):
    """最近对话的一行：跨渠道的往来打上（QQ）/（微信）标记，让换端接续可见。"""
    who = "我" if row["direction"] == "out" else (row.get("display_name") or "对方")
    if row.get("channel") and row["channel"] != current_channel:
        who += "（QQ）" if row["channel"] == "qq" else "（微信）"
    return f"{who}：{(row.get('content') or '')[:80]}"


def _recent_context(channel, chat_id, contact_id, chat_type, exclude_ids=(), limit=10):
    """最近对话（私聊按身份组跨渠道）：同一人的 QQ/微信两端往来合并成一条时间线，
    换端无缝接续；群聊仍按本群取。返回时间正序。"""
    exclude = [int(i) for i in exclude_ids if i is not None]
    where = ["msg_type!='blocked'"]
    args = []
    if exclude:
        where.append("id NOT IN (%s)" % ",".join("?" * len(exclude)))
        args += exclude
    if chat_type == "group":
        where.append("(channel=? AND chat_id=?)")
        args += [channel, str(chat_id)]
    else:
        try:
            pairs = identity._linked_ids(channel, contact_id)
        except Exception:
            pairs = [(channel, str(contact_id))]
        conds = ["(channel=? AND chat_id=?)"]
        args += [channel, str(chat_id)]
        for c, i in pairs:
            conds.append("(chat_type='private' AND channel=? AND contact_id=?)")
            args += [c, str(i)]
        where.append("(" + " OR ".join(conds) + ")")
    with db.LOCK:
        rows = [dict(r) for r in db.CONN.execute(
            "SELECT id, direction, channel, display_name, content, created_at FROM messages"
            " WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT ?",
            args + [max(2, min(int(limit), 30))])]
    seen_ids, uniq = set(), []
    for r in rows:  # 身份组条件可能与 chat_id 条件命中同一行，去重
        if r["id"] not in seen_ids:
            seen_ids.add(r["id"])
            uniq.append(r)
    return list(reversed(uniq))


def _mentioned(media_json, persona_name, text):
    try:
        for seg in json.loads(media_json or "[]"):
            if seg.get("type") == "at":
                return True
    except Exception:
        pass
    return bool(persona_name and persona_name in (text or ""))


def _build_context(channel, chat_type, chat_id, batch, persona):
    """一个会话组的 LLM 上下文：画像/记忆/图取身份组，最近对话跨渠道，新消息批量给。"""
    msg = batch[0]
    contact_id = str(msg.get("contact_id") or "")
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
    recent = _recent_context(channel, chat_id, contact_id, chat_type,
                             exclude_ids=[m["id"] for m in batch])
    platform = "QQ" if channel == "qq" else "微信"
    sys_prompt = (persona["prompt"] + ("\n\n说话风格：" + persona["style"] if persona["style"] else "")
                  + f"\n\n【你正在{platform}上与人聊天】直接输出要发送的回复文本：一条、短句、口语、"
                    "完全符合人格；不要解释、不要前缀引号、不要列表。如果这些消息不值得回复，只输出 [SKIP]。")
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
            _fmt_chat_line(r, channel) for r in recent[-8:]))
    if len(batch) == 1:
        user_parts.append(f"这条新消息：{batch[0].get('content') or '（媒体消息）'}")
    else:
        user_parts.append("这几条新消息（同一人连发，一并回应）：\n" + "\n".join(
            f"{i}. {m.get('content') or '（媒体消息）'}" for i, m in enumerate(batch, 1)))
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


# ---------- 发送与渠道车道 ----------

_WX_SEND_LOCK = threading.Lock()   # 一台手机一条 ADB 通道，微信发送必须串行
_wx_holds = {}                     # chat_id -> 连续持有次数（聊天窗不匹配）
_wx_pending = {}                   # group_key -> {"ids": [...], "reply": ...}（已生成待发送）


def _wx_screen_has(name):
    """guard 模式的防串台检查：当前微信聊天窗里能看到对方昵称才允许发送。
    读屏失败按不匹配处理（宁可持等，不可错发）。测试可打桩。"""
    try:
        from . import wechat as wechat_mod
        data = wechat_mod.t_wechat_read_screen()
        texts = " ".join(t.get("text", "") for t in data.get("texts", []))
        return bool(name and name in texts)
    except Exception:
        return False


def _send_wechat(msg, text, cfg):
    """微信车道发送：guard（默认）读屏确认聊天窗是对方；on 直发活动窗口；off 不发送。"""
    mode = str(cfg.get("brain", {}).get("wechat_send_mode", "guard"))
    if mode == "off":
        return {"sent": False, "blocked": True,
                "reason": "brain.wechat_send_mode=off（独立大脑不发微信，消息留给工作台）"}
    from . import wechat as wechat_mod
    with _WX_SEND_LOCK:
        if mode == "guard":
            name = msg.get("display_name") or ""
            if not _wx_screen_has(name):
                holds = _wx_holds.get(str(msg["chat_id"]), 0) + 1
                _wx_holds[str(msg["chat_id"])] = holds
                hold_max = max(1, int(cfg.get("brain", {}).get("wechat_hold_max", 10)))
                if holds >= hold_max:
                    return {"sent": False, "blocked": True,
                            "reason": f"微信聊天窗与 {name or '对方'} 持续不匹配（{holds} 次），"
                                      "放弃发送并把消息交还工作台"}
                return {"sent": False, "hold": True,
                        "reason": f"微信当前聊天窗不是 {name or '对方'}，回复持等窗口就位"}
            _wx_holds.pop(str(msg["chat_id"]), None)
        return wechat_mod.t_send_wechat_message(text=text)


def _send_reply(channel, chat_type, chat_id, msg, text, cfg):
    if channel == "qq":
        return qq.t_send_qq_message(chat_type=chat_type, chat_id=chat_id,
                                    text=text, contact_id=msg.get("contact_id"))
    return _send_wechat(msg, text, cfg)


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


def _group_key(msg):
    return (msg["channel"], msg["chat_type"], str(msg["chat_id"]))


def _lane_channels(cfg):
    """车道配置：brain.channels，"qq"（默认）/"qq,wechat"/"all"。"""
    raw = str(cfg.get("brain", {}).get("channels", "qq") or "qq").strip().lower()
    if raw in ("all", "*"):
        return {"qq", "wechat"}
    return {p.strip() for p in raw.split(",") if p.strip() in ("qq", "wechat")} or {"qq"}


def _mark_processed(ids):
    from .session import t_mark_processed
    if ids:
        t_mark_processed(ids)


def _handle_group(key, msgs, persona, cfg, group_p):
    """处理一个会话组：访问控制/管理指令逐条预筛 → 余下批量一次 LLM → 发送 → 沉淀。
    返回本组统计；LLM 失败/微信持等时消息保留未处理（下轮自动补上）。"""
    channel, chat_type, chat_id = key
    sub = {"replied": 0, "skipped": 0, "blocked": 0, "commands": 0,
           "errors": 0, "backoff": 0, "groups": 1, "held": 0}
    batch = []
    for msg in msgs:
        mid = msg["id"]
        contact_id = str(msg["contact_id"] or "")
        decision = access.access_decision(channel, chat_type, chat_id, contact_id)
        if not decision["allowed"]:
            sub["blocked"] += 1
            _mark_processed([mid])
            continue
        cmd = t_bot_command(text=msg.get("content") or "", channel=channel,
                            contact_id=contact_id, chat_id=chat_id)
        if cmd.get("handled"):
            sub["commands"] += 1
            if cmd.get("allowed") and cmd.get("reply"):
                _send_reply(channel, chat_type, chat_id, msg, cmd["reply"], cfg)
            elif cmd.get("allowed"):
                canned = _canned_command_reply(cmd)
                if canned:
                    _send_reply(channel, chat_type, chat_id, msg, canned, cfg)
            _mark_processed([mid])
            continue
        if not (msg.get("content") or "").strip():
            sub["skipped"] += 1
            _mark_processed([mid])
            continue
        batch.append(msg)
    if not batch:
        return sub
    if not persona:
        sub["skipped"] += len(batch)
        _mark_processed([m["id"] for m in batch])
        return sub
    if chat_type == "group":
        mentioned = any(_mentioned(m.get("media_json"), persona["name"], m.get("content"))
                        for m in batch)
        if not mentioned and random.random() >= group_p:
            sub["skipped"] += len(batch)
            _mark_processed([m["id"] for m in batch])
            return sub
    if _llm_backoff_active(cfg):
        sub["backoff"] += len(batch)  # 消息保留未处理，退避结束后自动补上
        return sub
    # 微信持等中的组：已生成过回复，直接重试发送，不重复消耗 LLM
    cached = _wx_pending.get(key)
    if cached and sorted(cached["ids"]) == sorted(m["id"] for m in batch):
        reply = cached["reply"]
    else:
        _wx_pending.pop(key, None)
        try:
            system, user = _build_context(channel, chat_type, chat_id, batch, persona)
        except Exception as e:
            sub["errors"] += 1
            print(f"[glm-foxgirl] brain context error: {e}", file=sys.stderr)
            _mark_processed([m["id"] for m in batch])
            return sub
        try:
            reply = _llm_chat(system, user)
            _llm_ok()
        except Exception as e:
            _llm_fail()
            sub["errors"] += 1
            print(f"[glm-foxgirl] brain llm error: {e}", file=sys.stderr)
            return sub  # 批量消息保留，下轮重试
    reply = re.sub(r"^['\"“”]+|['\"“”]+$", "", reply or "").strip()
    if not reply or "[SKIP]" in reply:
        sub["skipped"] += len(batch)
        _mark_processed([m["id"] for m in batch])
        _wx_pending.pop(key, None)
        return sub
    # 换班边界防重：发送前复查处理状态——已被看门/夜班处理的消息撤销，其余照发
    with db.LOCK:
        marks = ",".join("?" * len(batch))
        rows = {r["id"]: r["processed"] for r in db.CONN.execute(
            f"SELECT id, processed FROM messages WHERE id IN ({marks})",
            [m["id"] for m in batch]).fetchall()}
    still = [m for m in batch if not rows.get(m["id"])]
    if not still:
        sub["skipped"] += len(batch)
        return sub
    sent = _send_reply(channel, chat_type, chat_id, still[0], reply, cfg)
    if sent.get("sent"):
        sub["replied"] += 1
        _mark_processed([m["id"] for m in still])
        _wx_pending.pop(key, None)
        if len(still) == 1:
            _sediment(still[0], reply)
        else:
            merged = dict(still[0])
            merged["content"] = " / ".join((m.get("content") or "") for m in still)
            _sediment(merged, reply)
    elif sent.get("blocked"):
        # 拦截类失败（限频/泄密防火墙/微信 off/持等超限）为终态：标记处理，避免无限重试
        sub["skipped"] += len(still)
        _mark_processed([m["id"] for m in still])
        _wx_pending.pop(key, None)
    elif sent.get("hold"):
        # 微信聊天窗未就位：回复已生成缓存，下轮直接重发，不重复消耗 LLM
        sub["held"] += 1
        _wx_pending[key] = {"ids": [m["id"] for m in still], "reply": reply}
    else:
        # 网络类失败（NapCat 不可达等）：保留未处理，下轮重试
        sub["errors"] += 1
    return sub


def _merge_stats(stats, sub):
    for k, v in sub.items():
        stats[k] = stats.get(k, 0) + v
    return stats


_inflight = set()
_inflight_lock = threading.Lock()


def brain_process_once(stats=None):
    """单次处理轮：取待处理 → 按会话分组 → 车道过滤 → 线程池并发处理。返回统计。"""
    stats = stats or {"seen": 0, "replied": 0, "skipped": 0, "blocked": 0, "commands": 0,
                      "errors": 0, "backoff": 0, "groups": 0, "held": 0}
    cfg = config.load_config()
    from .session import t_get_pending_messages
    data = t_get_pending_messages(limit=30)
    msgs = data.get("messages", [])
    if not msgs:
        return stats
    persona = _active_persona()
    group_p = float(cfg.get("proactive", {}).get("reply_probability_group", 0.3))
    lanes = _lane_channels(cfg)
    groups = {}
    for m in msgs:
        stats["seen"] += 1
        groups.setdefault(_group_key(m), []).append(m)
    runnable = []
    for key, gmsgs in groups.items():
        if key[0] not in lanes:
            # 车道未开启的渠道：不处理不标记（保留给工作台/其他大脑），仅跳过
            stats["skipped"] += len(gmsgs)
            continue
        runnable.append((key, gmsgs))
    with _inflight_lock:  # 防重入：同一会话组不并发处理两次（轮询重叠时）
        todo = [(k, m) for k, m in runnable if k not in _inflight]
        for k, _ in todo:
            _inflight.add(k)
    try:
        if todo:
            max_w = max(1, int(cfg.get("brain", {}).get("max_concurrency", 3)))
            if max_w == 1 or len(todo) == 1:
                for k, gm in todo:
                    _merge_stats(stats, _handle_group(k, gm, persona, cfg, group_p))
            else:
                with concurrent.futures.ThreadPoolExecutor(
                        max_workers=min(max_w, len(todo))) as pool:
                    futures = [pool.submit(_handle_group, k, gm, persona, cfg, group_p)
                               for k, gm in todo]
                    for f in futures:
                        _merge_stats(stats, f.result())
    finally:
        with _inflight_lock:
            for k, _ in todo:
                _inflight.discard(k)
    return stats


_DUTY_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")
_DUTY_WARNED = [False]


def _warn_bad_duty(raw):
    """值班表配置非法时警告一次（进程内只打一行，防长睡循环刷屏）。"""
    if not _DUTY_WARNED[0]:
        _DUTY_WARNED[0] = True
        print(f"[glm-foxgirl] brain.duty_windows 配置非法（{raw!r}），已按全天值班处理",
              file=sys.stderr)


def _merge_segments(segs):
    """重叠/相邻段合并为并集（跨午夜段先拆尾/头两段再合并、最后还原），
    保证返回的段互不重叠——看门/大脑的"当前所在段"判定因此唯一。"""
    flat = []
    for s, e in segs:
        if s < e:
            flat.append((s, e))
        else:  # 跨午夜：拆成当日尾段 + 次日头段
            if s < 1440:
                flat.append((s, 1440))
            if e > 0:
                flat.append((0, e))
    merged = []
    for s, e in sorted(flat):
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    tail = next((x for x in merged if x[1] == 1440), None)
    head = next((x for x in merged if x[0] == 0 and x[1] != 1440), None)
    if tail and head:
        merged = [x for x in merged if x != tail and x != head]
        merged.append((tail[0], head[1]))
    return sorted(merged)


def _duty_windows():
    """值班表（可选，含自动换班）：brain.duty_windows，逗号分隔多段 "HH:MM-HH:MM"，
    如 "09:00-23:00" 或 "22:00-06:00,09:00-12:00"（支持跨午夜，段数不限，重叠段取并集）。
    未设置 → 全天值班：LLM 与看门二选一处理消息，无换班；设置后换班自动生效
    （换班内置于值班表，不单设开关）。兼容旧键：仅设 duty_start_hour/duty_end_hour
    时视为单段 "HH:00-HH:00"。返回 [(start_min, end_min), ...] 或 None。
    有效性：小时 0~24（24 仅整点，即 24:00）、分钟 0~59；配置非空但全部段非法时
    打一行 stderr 警告并按全天值班处理。
    注意：解析逻辑与 server/inbox_watch.py 的 duty_windows() 为同源副本
    （看门独立运行，故意不 import foxbot），格式演进必须两处同步。"""
    bcfg = config.load_config().get("brain", {})
    raw = str(bcfg.get("duty_windows") or "").strip()
    if not raw:
        sh, eh = bcfg.get("duty_start_hour"), bcfg.get("duty_end_hour")
        if sh is None or eh is None:
            return None
        try:
            raw = f"{int(sh):02d}:00-{int(eh):02d}:00"
        except (TypeError, ValueError):
            _warn_bad_duty(str(sh))
            return None
    segs = []
    for part in raw.split(","):
        m = _DUTY_RE.match(part.strip())
        if not m:
            continue
        hh1, mm1, hh2, mm2 = (int(m.group(i)) for i in range(1, 5))
        if not (0 <= hh1 <= 24 and 0 <= mm1 <= 59 and 0 <= hh2 <= 24 and 0 <= mm2 <= 59):
            continue  # 非法时段（如 09:99、24:30）直接拒绝，绝不让班表静默偏移
        s, e = hh1 * 60 + mm1, hh2 * 60 + mm2
        if s <= 1440 and e <= 1440 and s != e:
            segs.append((s, e))
    if not segs:
        if raw:
            _warn_bad_duty(raw)
        return None
    return _merge_segments(segs)


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
    """下一班开工的分钟数：今天尚未开始的最早段起点，否则明天最早班。
    无班可上（值班表恰被清空等竞态）返回 None，调用方短睡重读配置。"""
    starts = sorted(s for s, _ in (_duty_windows() or []))
    if not starts:
        return None
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
            return  # 主动搭话 v1 仅发 QQ（微信发送需聊天窗前置，不适合无人值守主动消息）
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
        recent = _recent_context(prof["channel"], prof["contact_id"], prof["contact_id"],
                                 "private", limit=6)
        if recent:
            parts.append("最近对话：\n" + "\n".join(
                _fmt_chat_line(r, prof["channel"]) for r in recent[-6:]))
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
    lanes = _lane_channels(config.load_config())
    wx_mode = str(cfg.get("wechat_send_mode", "guard"))
    print(f"[glm-foxgirl] 多任务大脑运行中：model={cfg['model']} poll={cfg.get('poll_seconds', 5)}s"
          f" 并发={cfg.get('max_concurrency', 3)} 车道={','.join(sorted(lanes))}"
          f"（微信发送={wx_mode}；{duty_txt}；与看门脚本互斥，勿同时开启）", file=sys.stderr)
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
                nxt = _next_duty_start(now.hour * 60 + now.minute)
                if nxt is None:
                    time.sleep(30)  # 值班表恰被清空：短睡重读配置
                    continue
                start = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                         + timedelta(minutes=nxt))
                if start <= now:
                    start += timedelta(days=1)
                time.sleep(max(30, (start - now).total_seconds()))
                continue
        except Exception as e:
            print(f"[glm-foxgirl] brain loop error: {e}", file=sys.stderr)
        time.sleep(max(2, int(cfg.get("poll_seconds", 5))))
