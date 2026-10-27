"""长期记忆：写入（含冲突取代）、检索（衰减评分+语义+身份组互联）、遗忘清理、反思机制。"""
import json
import time
from datetime import datetime, timedelta

from . import config, db, identity
from .embeddings import embed_text, _cosine


def _memory_score(row, now_ts, half_life_days):
    """重要级+情绪强度，随时间指数衰减（遗忘曲线）：强情绪记忆衰减更慢。"""
    age_days = max(0.0, (db._parse_dt(row["created_at"])) / 86400.0)
    decay = 0.5 ** (age_days / max(1.0, float(half_life_days)))
    return (int(row["importance"]) + abs(int(row.get("emotion") or 0))) * decay


def t_store_memory(content, channel="", chat_id="", contact_id="", kind="event",
                   importance=3, emotion=0, supersedes=None, **_):
    """写入长期记忆。supersedes=[旧记忆id]：标记旧记忆被本条取代（事实更新，旧条不再出现在检索中但保留可追溯）。"""
    if not content:
        raise ValueError("content 不能为空")
    importance = max(1, min(5, int(importance)))
    emo = max(-3, min(3, int(emotion)))
    vec = embed_text(content)
    with db.LOCK:
        cur = db.CONN.execute(
            "INSERT INTO memories(channel, chat_id, contact_id, kind, importance, content, embedding, emotion, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (channel or "", str(chat_id or ""), str(contact_id or ""), kind, importance, content,
             json.dumps(vec) if vec else "", emo, db.now_str()))
        new_id = cur.lastrowid
        superseded = []
        for old in (supersedes or []):
            try:
                old_id = int(old)
            except (TypeError, ValueError):
                continue
            r = db.CONN.execute("SELECT id FROM memories WHERE id=?", (old_id,)).fetchone()
            if r:
                db.CONN.execute("UPDATE memories SET superseded_by=? WHERE id=?", (new_id, old_id))
                superseded.append(old_id)
        db.CONN.commit()
        return {"ok": True, "memory_id": new_id, "embedded": bool(vec), "superseded": superseded}


def t_query_memory(keyword=None, contact_id=None, chat_id=None, channel=None, limit=10,
                   semantic=False, include_superseded=False, **_):
    q, args = "SELECT * FROM memories WHERE 1=1", []
    if not include_superseded:
        q += " AND superseded_by=0"
    linked = None
    if contact_id and channel:
        linked = [i for _, i in identity._linked_ids(channel, str(contact_id))]
    if channel and not (linked and len(linked) > 1):
        q += " AND channel=?"
        args.append(channel)
    if keyword and not semantic:
        q += " AND content LIKE ?"
        args.append(f"%{keyword}%")
    if contact_id:
        if linked and len(linked) > 1:
            q += f" AND contact_id IN ({','.join('?' * len(linked))})"
            args.extend(linked)
        else:
            q += " AND contact_id=?"
            args.append(str(contact_id))
    if chat_id:
        q += " AND chat_id=?"; args.append(str(chat_id))
    cap = 200 if semantic else max(1, min(int(limit), 50))
    q += " ORDER BY importance DESC, id DESC LIMIT ?"
    args.append(cap)
    with db.LOCK:
        rows = [dict(r) for r in db.CONN.execute(q, args).fetchall()]
    now_ts = time.mktime(datetime.now().timetuple())
    half = float(config.load_config().get("memory", {}).get("half_life_days", 30))
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


def t_memory_conflicts(content, contact_id="", channel="", limit=5, **_):
    """存储事实性记忆前调用：找出可能与新内容矛盾/重叠的旧记忆候选，
    由大脑语义判断是否矛盾——矛盾则 store_memory(supersedes=[旧id])，不矛盾正常存。"""
    if not content:
        raise ValueError("content 不能为空")
    data = t_query_memory(contact_id=contact_id, channel=channel, limit=50)
    vec = embed_text(content)
    candidates = []
    for r in data["memories"]:
        if r["kind"] == "reflection":
            continue
        sim = 0.0
        if vec and r.get("embedding"):
            try:
                sim = _cosine(vec, json.loads(r["embedding"]))
            except Exception:
                sim = 0.0
        if not sim:
            sim = _bigram_sim(content, r["content"])
        if sim >= 0.35:
            candidates.append({"id": r["id"], "content": r["content"], "created_at": r["created_at"],
                               "similarity": round(sim, 3), "importance": r["importance"]})
    candidates.sort(key=lambda x: -x["similarity"])
    return {"count": len(candidates[:int(limit)]),
            "candidates": candidates[:max(1, min(int(limit), 20))],
            "hint": "语义判断：矛盾→store_memory(supersedes=[旧id])；只是相关→正常存，不传 supersedes"}


def _bigram_sim(a, b):
    def grams(s):
        s = s.replace(" ", "")
        return {s[i:i + 2] for i in range(max(0, len(s) - 1))} or {s}
    ga, gb = grams(a or ""), grams(b or "")
    return len(ga & gb) / len(ga | gb) if ga and gb else 0.0


def t_prune_memory(keep_score=1.0, dry_run=True, limit=500, **_):
    """遗忘曲线清理：删除得分低于 keep_score 且重要级<=3 的记忆（重要级>=4 永不清理）。"""
    half = float(config.load_config().get("memory", {}).get("half_life_days", 30))
    now_ts = time.mktime(datetime.now().timetuple())
    with db.LOCK:
        rows = [dict(r) for r in db.CONN.execute(
            "SELECT * FROM memories ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 1000)),))]
    victims = []
    for r in rows:
        score = _memory_score(r, now_ts, half)
        if score < float(keep_score) and int(r["importance"]) <= 3:
            victims.append((r["id"], round(score, 3), r["content"][:40]))
    if dry_run:
        return {"would_delete": len(victims), "samples": victims[:10],
                "note": "dry_run=false 才真正删除；importance>=4 永不清理"}
    with db.LOCK:
        db.CONN.executemany("DELETE FROM memories WHERE id=?", [(v[0],) for v in victims])
        db.CONN.commit()
    return {"deleted": len(victims), "samples": victims[:10]}


def t_reflect_due(limit=10, **_):
    """反思到期检查：某联系人在上次反思之后新增了足够多的记忆（默认 8 条），
    或首批评论满 3 条且从未反思，即视为到期。大脑据此执行反思（reflect_material → 合成洞察）。"""
    mem_cfg = config.load_config().get("memory", {})
    min_new = max(1, int(mem_cfg.get("reflect_min_new", 8)))
    days = max(1, int(mem_cfg.get("reflect_days", 7)))
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with db.LOCK:
        pairs = [dict(r) for r in db.CONN.execute(
            "SELECT DISTINCT channel, contact_id FROM memories WHERE contact_id!=''")]
        due = []
        for p in pairs:
            if not p["contact_id"]:
                continue
            last = db.CONN.execute(
                "SELECT id, created_at FROM memories WHERE channel=? AND contact_id=? AND kind='reflection'"
                " ORDER BY id DESC LIMIT 1", (p["channel"], p["contact_id"])).fetchone()
            last_id = last["id"] if last else 0
            new_count = db.CONN.execute(
                "SELECT COUNT(*) FROM memories WHERE channel=? AND contact_id=? AND id>? AND kind!='reflection'",
                (p["channel"], p["contact_id"], last_id)).fetchone()[0]
            stale = (not last) or (last["created_at"] < cutoff)
            if new_count >= min_new or (new_count >= 3 and stale):
                due.append({"channel": p["channel"], "contact_id": p["contact_id"],
                            "new_count": new_count,
                            "last_reflection": last["created_at"] if last else None})
    return {"count": len(due[:max(1, min(int(limit), 50))]), "due": due[:max(1, min(int(limit), 50))],
            "hint": "对每个到期对象：reflect_material 取素材 → 合成 2-4 条高层洞察 → "
                    "store_memory(kind='reflection', importance=4) + manage_profile(upsert) 更新印象"}


def t_reflect_material(channel, contact_id, limit=40, **_):
    """反思素材：该身份组的近期记忆（排除已被取代）、画像、近两条反思。"""
    if not channel or not contact_id:
        raise ValueError("需要 channel 和 contact_id")
    pairs = identity._linked_ids(channel, contact_id)
    cond = " OR ".join(["(channel=? AND contact_id=?)"] * len(pairs))
    pargs = [v for p in pairs for v in p]
    with db.LOCK:
        ph = [dict(r) for r in db.CONN.execute(
            f"SELECT * FROM profiles WHERE {cond}", pargs)]
        rows = [dict(r) for r in db.CONN.execute(
            f"SELECT id, kind, importance, emotion, content, created_at FROM memories"
            f" WHERE ({cond}) AND superseded_by=0 AND kind!='reflection'"
            f" ORDER BY id DESC LIMIT ?", pargs + [max(5, min(int(limit), 100))])]
        reflections = [dict(r) for r in db.CONN.execute(
            f"SELECT content, created_at FROM memories WHERE ({cond}) AND kind='reflection'"
            f" ORDER BY id DESC LIMIT 2", pargs)]
    return {"channel": channel, "contact_id": str(contact_id),
            "identities": [list(p) for p in pairs],
            "profile": ph, "memories": rows, "previous_reflections": reflections,
            "hint": ("合成 2-4 条高层洞察（关系趋势/偏好/性格观察），不要复述原句；然后 "
                     "store_memory(kind='reflection', importance=4, content='洞察：…')，"
                     "并用 manage_profile(upsert) 把洞察融入 impression（增量补充）")}
