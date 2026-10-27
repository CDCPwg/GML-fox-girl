"""身份与画像：账号关联器、身份组展开、画像读写与调和。"""
import json
import random
import re
import time

from . import config, db
from .embeddings import embed_text, _cosine


def _norm_pair(ca, ia, cb, ib):
    side = sorted([(str(ca), str(ia)), (str(cb), str(ib))])
    return side[0][0], side[0][1], side[1][0], side[1][1]


def _linked_ids(channel, contact_id):
    with db.LOCK:
        rows = db.CONN.execute(
            "SELECT left_c, left_i, right_c, right_i FROM account_links"
            " WHERE (left_c=? AND left_i=?) OR (right_c=? AND right_i=?)",
            (channel, str(contact_id), channel, str(contact_id))).fetchall()
    out = [(channel, str(contact_id))]
    for r in rows:
        for pair in ((r["left_c"], r["left_i"]), (r["right_c"], r["right_i"])):
            if pair not in out:
                out.append(pair)
    return out


def _profile_get(channel, contact_id):
    row = db.CONN.execute("SELECT * FROM profiles WHERE channel=? AND contact_id=?",
                          (channel, str(contact_id))).fetchone()
    return dict(row) if row else None


def _merge_text(a, b):
    def parts(s):
        return [p.strip() for p in re.split(r"[。！？!?\n;；]+", s or "") if p.strip()]
    seen, out = set(), []
    for p in parts(a) + parts(b):
        k = p.lower()
        if k not in seen:
            seen.add(k)
            out.append(p)
    return "。".join(out) + ("。" if out else "")


def _text_similarity(a, b):
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.3
    va, vb = embed_text(a), embed_text(b)
    if va and vb:
        return _cosine(va, vb)

    def grams(s):
        s = re.sub(r"\s+", "", s)
        return {s[i:i + 2] for i in range(max(0, len(s) - 1))} or {s}
    ga, gb = grams(a), grams(b)
    return len(ga & gb) / len(ga | gb)


def _apply_profile(channel, contact_id, impression, style_notes, affinity, display_name=None):
    row = _profile_get(channel, contact_id)
    if row:
        sets, args = ["impression=?", "style_notes=?", "updated_at=?"], [impression, style_notes, db.now_str()]
        if affinity is not None:
            sets.append("affinity=?")
            args.append(int(affinity))
        args.append(row["id"])
        db.CONN.execute(f"UPDATE profiles SET {', '.join(sets)} WHERE id=?", args)
    else:
        db.CONN.execute(
            "INSERT INTO profiles(channel, contact_id, display_name, impression, style_notes,"
            " affinity, updated_at) VALUES(?,?,?,?,?,?,?)",
            (channel, str(contact_id), display_name or "", impression, style_notes,
             int(affinity or 0), db.now_str()))


def t_link_accounts(action, channel="", contact_id="", peer_channel="", peer_contact_id="",
                    code="", request_id=None, choice="", **_):
    """QQ/WX 账号关联器：request 发起（生成确认码）→ 对方在自己平台发确认码 confirm；
    画像自动调和（单侧继承/相似合并/不相似冲突由账号选 a/b/merge）；记忆按身份组互联。
    另有 status / ids（展开身份组）/ unlink。"""
    if action == "request":
        if not channel or not contact_id or not peer_channel or not peer_contact_id:
            raise ValueError("request 需要 channel/contact_id/peer_channel/peer_contact_id")
        if channel == peer_channel and str(contact_id) == str(peer_contact_id):
            raise ValueError("不能和自己关联")
        with db.LOCK:
            lc, li, rc, ri = _norm_pair(channel, contact_id, peer_channel, peer_contact_id)
            if db.CONN.execute("SELECT 1 FROM account_links WHERE left_c=? AND left_i=? AND right_c=? AND right_i=?",
                               (lc, li, rc, ri)).fetchone():
                return {"ok": True, "already_linked": True}
            pending = db.CONN.execute(
                "SELECT COUNT(*) FROM link_requests WHERE status='pending' AND"
                " ((channel_a=? AND id_a=?) OR (channel_b=? AND id_b=?))",
                (channel, str(contact_id), channel, str(contact_id))).fetchone()[0]
            if pending >= 3:
                raise ValueError("待确认的关联请求过多（≤3），等对方确认或过期后再试")
            code = f"{random.randint(0, 999999):06d}"
            cur = db.CONN.execute(
                "INSERT INTO link_requests(channel_a, id_a, channel_b, id_b, code, status,"
                " created_at, updated_at) VALUES(?,?,?,?,?,'pending',?,?)",
                (channel, str(contact_id), peer_channel, str(peer_contact_id), code, db.now_str(), db.now_str()))
            db.CONN.commit()
        return {"ok": True, "request_id": cur.lastrowid, "code": code,
                "hint": (f"请把确认码 {code} 私下转达对方本人（这是账号所有权证明，勿发他人）；"
                         f"对方须在 {peer_channel} 上用该账号发送：关联确认 {code}（"
                         f"{config.load_config().get('link', {}).get('expire_min', 15)} 分钟内有效）")}
    if action == "confirm":
        if not channel or not contact_id or not code:
            raise ValueError("confirm 需要 channel/contact_id/code")
        with db.LOCK:
            row = db.CONN.execute(
                "SELECT * FROM link_requests WHERE status='pending' AND channel_b=? AND id_b=? AND code=?"
                " ORDER BY id DESC LIMIT 1", (channel, str(contact_id), code.strip())).fetchone()
            if not row:
                wrong = db.CONN.execute(
                    "SELECT id FROM link_requests WHERE status='pending' AND channel_a=? AND id_a=? AND code=?",
                    (channel, str(contact_id), code.strip())).fetchone()
                if wrong:
                    raise ValueError("确认码只能由对方账号在其平台发送，发起方不能自我确认")
                raise ValueError("没有匹配的待确认请求（检查确认码/平台/账号）")
            age_min = (time.time() - db._parse_dt(row["created_at"])) / 60.0
            if age_min > float(config.load_config().get("link", {}).get("expire_min", 15)):
                db.CONN.execute("UPDATE link_requests SET status='expired', updated_at=? WHERE id=?",
                                (db.now_str(), row["id"]))
                db.CONN.commit()
                raise ValueError("确认码已过期，请让对方重新发起")
            lc, li, rc, ri = _norm_pair(row["channel_a"], row["id_a"], row["channel_b"], row["id_b"])
            db.CONN.execute(
                "INSERT OR IGNORE INTO account_links(left_c, left_i, right_c, right_i, created_at)"
                " VALUES(?,?,?,?,?)", (lc, li, rc, ri, db.now_str()))
            pa = _profile_get(row["channel_a"], row["id_a"])
            pb = _profile_get(row["channel_b"], row["id_b"])
            threshold = float(config.load_config().get("link", {}).get("similar_threshold", 0.6))
            if pa and pb and ((pa["impression"] and pb["impression"]
                               and _text_similarity(pa["impression"], pb["impression"]) < threshold)
                              or (pa["style_notes"] and pb["style_notes"]
                                  and _text_similarity(pa["style_notes"], pb["style_notes"]) < threshold)):
                conflict = {"impression": {"a": pa["impression"], "b": pb["impression"]},
                            "style_notes": {"a": pa["style_notes"], "b": pb["style_notes"]}}
                db.CONN.execute("UPDATE link_requests SET status='conflict', payload=?, updated_at=? WHERE id=?",
                                (json.dumps({"channel_a": row["channel_a"], "id_a": row["id_a"],
                                             "channel_b": row["channel_b"], "id_b": row["id_b"],
                                             "aff_a": pa["affinity"], "aff_b": pb["affinity"],
                                             **conflict}, ensure_ascii=False), db.now_str(), row["id"]))
                db.CONN.commit()
                return {"ok": True, "status": "conflict", "request_id": row["id"],
                        "conflict": conflict,
                        "hint": ("两侧画像不相似：请该账号选择 a=保留发起方版 / b=保留对方版 / merge=融合，"
                                 "然后 link_accounts(action=resolve, request_id, choice)")}
            inherited = False
            if pa and not pb:
                _apply_profile(row["channel_b"], row["id_b"], pa["impression"], pa["style_notes"],
                               pa["affinity"], display_name=pa["display_name"])
                inherited = True
            elif pb and not pa:
                _apply_profile(row["channel_a"], row["id_a"], pb["impression"], pb["style_notes"],
                               pb["affinity"], display_name=pb["display_name"])
                inherited = True
            elif pa and pb:
                imp = _merge_text(pa["impression"], pb["impression"])
                sty = _merge_text(pa["style_notes"], pb["style_notes"])
                aff = max((pa["affinity"], pb["affinity"]), key=lambda v: abs(v))
                _apply_profile(row["channel_a"], row["id_a"], imp, sty, aff)
                _apply_profile(row["channel_b"], row["id_b"], imp, sty, aff)
            db.CONN.execute("UPDATE link_requests SET status='linked', updated_at=? WHERE id=?",
                            (db.now_str(), row["id"]))
            db.CONN.commit()
        return {"ok": True, "status": "linked", "request_id": row["id"], "profile_inherited": inherited,
                "hint": "记忆按身份组互联（不复制）；画像查询请先用 action=ids 展开身份组"}
    if action == "resolve":
        if not request_id or choice not in ("a", "b", "merge"):
            raise ValueError("resolve 需要 request_id 和 choice(a/b/merge)")
        with db.LOCK:
            row = db.CONN.execute("SELECT * FROM link_requests WHERE id=? AND status='conflict'",
                                  (int(request_id),)).fetchone()
            if not row:
                raise ValueError("找不到该冲突请求")
            payload = json.loads(row["payload"] or "{}")
            ca, ia = payload.get("channel_a"), payload.get("id_a")
            cb, ib = payload.get("channel_b"), payload.get("id_b")
            pa, pb = _profile_get(ca, ia) or {}, _profile_get(cb, ib) or {}
            aff = max((pa.get("affinity", 0), pb.get("affinity", 0)), key=lambda v: abs(v))
            if choice == "a":
                _apply_profile(cb, ib, payload["impression"]["a"], payload["style_notes"]["a"], aff)
            elif choice == "b":
                _apply_profile(ca, ia, payload["impression"]["b"], payload["style_notes"]["b"], aff)
            else:
                _apply_profile(ca, ia, _merge_text(payload["impression"]["a"], payload["impression"]["b"]),
                               _merge_text(payload["style_notes"]["a"], payload["style_notes"]["b"]), aff)
                _apply_profile(cb, ib, _merge_text(payload["impression"]["a"], payload["impression"]["b"]),
                               _merge_text(payload["style_notes"]["a"], payload["style_notes"]["b"]), aff)
            db.CONN.execute("UPDATE link_requests SET status='linked', updated_at=? WHERE id=?",
                            (db.now_str(), int(request_id)))
            db.CONN.commit()
        return {"ok": True, "status": "linked", "choice": choice}
    if action == "status":
        with db.LOCK:
            if channel and contact_id:
                links = [dict(r) for r in db.CONN.execute(
                    "SELECT * FROM account_links WHERE (left_c=? AND left_i=?) OR (right_c=? AND right_i=?)",
                    (channel, str(contact_id), channel, str(contact_id)))]
                reqs = [dict(r) for r in db.CONN.execute(
                    "SELECT id, channel_a, id_a, channel_b, id_b, status, created_at FROM link_requests"
                    " WHERE (channel_a=? AND id_a=?) OR (channel_b=? AND id_b=?) ORDER BY id DESC LIMIT 10",
                    (channel, str(contact_id), channel, str(contact_id)))]
            else:
                links = [dict(r) for r in db.CONN.execute("SELECT * FROM account_links")]
                reqs = [dict(r) for r in db.CONN.execute(
                    "SELECT id, channel_a, id_a, channel_b, id_b, status, created_at FROM link_requests"
                    " ORDER BY id DESC LIMIT 10")]
        return {"links": links, "requests": reqs}
    if action == "ids":
        if not channel or not contact_id:
            raise ValueError("ids 需要 channel/contact_id")
        return {"ok": True, "identities": [list(p) for p in _linked_ids(channel, contact_id)]}
    if action == "unlink":
        if not channel or not contact_id:
            raise ValueError("unlink 需要 channel/contact_id")
        with db.LOCK:
            if peer_channel and peer_contact_id:
                lc, li, rc, ri = _norm_pair(channel, contact_id, peer_channel, peer_contact_id)
                cur = db.CONN.execute(
                    "DELETE FROM account_links WHERE left_c=? AND left_i=? AND right_c=? AND right_i=?",
                    (lc, li, rc, ri))
            else:
                cur = db.CONN.execute(
                    "DELETE FROM account_links WHERE (left_c=? AND left_i=?) OR (right_c=? AND right_i=?)",
                    (channel, str(contact_id), channel, str(contact_id)))
            db.CONN.commit()
        return {"ok": True, "removed": cur.rowcount}
    raise ValueError(f"未知 action: {action}")


def t_manage_profile(action, channel="", contact_id="", display_name=None, impression=None,
                     style_notes=None, affinity_delta=0, **_):
    with db.LOCK:
        if action == "list":
            rows = [dict(r) for r in db.CONN.execute("SELECT * FROM profiles ORDER BY affinity DESC")]
            return {"profiles": rows}
        if action in ("get", "upsert"):
            if not channel or not contact_id:
                raise ValueError("需要 channel 和 contact_id")
            row = db.CONN.execute("SELECT * FROM profiles WHERE channel=? AND contact_id=?",
                                  (channel, str(contact_id))).fetchone()
            if action == "get":
                return {"profile": dict(row) if row else None}
            if row:
                sets, args = ["updated_at=?"], [db.now_str()]
                if display_name:
                    sets.append("display_name=?"); args.append(display_name)
                if impression is not None:
                    sets.append("impression=?"); args.append(impression)
                if style_notes is not None:
                    sets.append("style_notes=?"); args.append(style_notes)
                if affinity_delta:
                    sets.append("affinity=MAX(-100, MIN(100, affinity+?))"); args.append(int(affinity_delta))
                args.append(row["id"])
                db.CONN.execute(f"UPDATE profiles SET {', '.join(sets)} WHERE id=?", args)
            else:
                db.CONN.execute(
                    "INSERT INTO profiles(channel, contact_id, display_name, impression, style_notes, affinity, updated_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (channel, str(contact_id), display_name or "", impression or "", style_notes or "",
                     max(-100, min(100, int(affinity_delta or 0))), db.now_str()))
            db.CONN.commit()
            row = db.CONN.execute("SELECT * FROM profiles WHERE channel=? AND contact_id=?",
                                  (channel, str(contact_id))).fetchone()
            return {"ok": True, "profile": dict(row)}
    raise ValueError(f"未知 action: {action}")
