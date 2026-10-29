"""独立大脑守护进程（可选）：实时轮询收件箱，规则预筛后仅对需要人格的消息调用 LLM。

规则预筛（零成本）：黑白名单 → 聊天内管理指令 → 群聊回复必要性门控。
人格上下文：人格表（现有人格）+ 关系画像（身份组）+ 相关记忆 + 记忆图 + 最近对话。
与心跳自动化互斥：二选一，避免双脑重复回复。
LLM 走 OpenAI 兼容 /chat/completions（brain.api_base/api_key/model，默认关闭）。
"""
import json
import random
import re
import sys
import time
import urllib.request

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
        sent = _send_reply(msg, reply)
        if sent.get("sent"):
            stats["replied"] += 1
            done()
        elif sent.get("blocked"):
            # 拦截类失败（限频/泄密防火墙）为终态：标记处理，避免守护进程无限重试
            stats["skipped"] += 1
            done()
        else:
            # 网络类失败（NapCat 不可达等）：保留未处理，下轮重试
            stats["errors"] += 1
    return stats


def main_brain(once=False):
    cfg = config.load_config()["brain"]
    if not (cfg.get("api_base") and cfg.get("model")):
        print("[glm-foxgirl] 独立大脑未配置：需 set_config brain.api_base / brain.api_key / brain.model "
              "（OpenAI 兼容接口）。注意：与心跳自动化互斥，二选一。", file=sys.stderr)
        sys.exit(2)
    db.init_db()
    qq.start_collector_thread()
    print(f"[glm-foxgirl] 独立大脑运行中：model={cfg['model']} poll={cfg.get('poll_seconds', 5)}s"
          f"（实时人格回复；与心跳自动化互斥，勿同时开启）", file=sys.stderr)
    if once:
        print(json.dumps(brain_process_once(), ensure_ascii=False), file=sys.stderr)
        return
    while True:
        try:
            brain_process_once()
        except Exception as e:
            print(f"[glm-foxgirl] brain loop error: {e}", file=sys.stderr)
        time.sleep(max(2, int(cfg.get("poll_seconds", 5))))
