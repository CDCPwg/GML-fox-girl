"""记忆图：实体-关系三元组（人物-事件-偏好关联）。"""
from . import db


def _graph_node(name, kind):
    row = db.CONN.execute("SELECT id FROM graph_nodes WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if row:
        db.CONN.execute("UPDATE graph_nodes SET updated_at=? WHERE id=?", (db.now_str(), row["id"]))
        return row["id"]
    return db.CONN.execute("INSERT INTO graph_nodes(name, kind, created_at, updated_at) VALUES(?,?,?,?)",
                           (name, kind or "other", db.now_str(), db.now_str())).lastrowid


def t_graph_add(subject, relation, target, subject_kind="other", target_kind="other", note="", **_):
    """向记忆图添加/加权一条三元组：谁 -关系-> 什么（同一三元组重复添加只加权重）。"""
    if not subject or not relation or not target:
        raise ValueError("subject/relation/target 都不能为空")
    with db.LOCK:
        sid = _graph_node(subject.strip(), subject_kind)
        tid = _graph_node(target.strip(), target_kind)
        db.CONN.execute(
            "INSERT INTO graph_edges(src_id, dst_id, relation, weight, note, last_seen)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(src_id, dst_id, relation) DO UPDATE SET weight=weight+1,"
            " last_seen=excluded.last_seen,"
            " note=CASE WHEN excluded.note!='' THEN excluded.note ELSE graph_edges.note END",
            (sid, tid, relation.strip(), 1, note or "", db.now_str()))
        db.CONN.commit()
        return {"ok": True}


def t_graph_query(entity="", depth=1, limit=60, **_):
    """记忆图查询：entity 留空返回总览（按关联度排序）；否则从该实体 BFS 扩散 depth 层。"""
    with db.LOCK:
        if not str(entity).strip():
            rows = db.CONN.execute(
                "SELECT n.id, n.name, n.kind, n.summary,"
                " COALESCE((SELECT SUM(weight) FROM graph_edges e WHERE e.src_id=n.id OR e.dst_id=n.id),0) AS degree"
                " FROM graph_nodes n ORDER BY degree DESC, n.id DESC LIMIT ?",
                (max(1, min(int(limit), 100)),)).fetchall()
            return {"overview": True, "nodes": [dict(r) for r in rows]}
        root = db.CONN.execute("SELECT id, name, kind FROM graph_nodes WHERE name=? COLLATE NOCASE",
                               (str(entity).strip(),)).fetchone()
        if not root:
            return {"found": False, "hint": "图里还没有这个实体；可先 graph_add 添加"}
        seen = {root["id"]: {"name": root["name"], "kind": root["kind"]}}
        frontier = [root["id"]]
        edges_out = []
        for _ in range(max(1, min(int(depth), 3))):
            nxt = []
            for nid in frontier:
                for e in db.CONN.execute(
                        "SELECT e.*, sn.name AS src_name, dn.name AS dst_name FROM graph_edges e"
                        " JOIN graph_nodes sn ON sn.id=e.src_id JOIN graph_nodes dn ON dn.id=e.dst_id"
                        " WHERE e.src_id=? OR e.dst_id=?", (nid, nid)).fetchall():
                    edges_out.append(dict(e))
                    other = e["dst_id"] if e["src_id"] == nid else e["src_id"]
                    if other not in seen:
                        row = db.CONN.execute("SELECT id, name, kind FROM graph_nodes WHERE id=?",
                                              (other,)).fetchone()
                        seen[other] = {"name": row["name"], "kind": row["kind"]}
                        nxt.append(other)
            frontier = nxt
        uniq = {(e["src_id"], e["dst_id"], e["relation"]): e for e in edges_out}
        return {"found": True, "root": root["name"], "nodes": list(seen.values()),
                "edges": list(uniq.values())[:max(1, min(int(limit), 100))]}
