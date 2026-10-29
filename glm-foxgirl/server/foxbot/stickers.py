"""表情包库与媒体处理（下载、视频抽帧）。"""
import os
import re
import shutil
import subprocess
import time
import urllib.request

from . import config, db


def t_manage_stickers(action, file_path=None, emotion="", tags="", source="", source_url=None, sticker_id=None, **_):
    with db.LOCK:
        if action == "add":
            if not file_path or not os.path.exists(file_path):
                raise ValueError(f"file_path 不存在: {file_path}")
            if source_url:
                dup = db.CONN.execute("SELECT id FROM stickers WHERE source_url=?", (source_url,)).fetchone()
                if dup:
                    return {"ok": True, "deduped": True, "sticker_id": dup["id"]}
            cur = db.CONN.execute(
                "INSERT INTO stickers(file_path, emotion, tags, source, source_url, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (os.path.abspath(file_path), emotion, tags, source, source_url or "", db.now_str()))
            db.CONN.commit()
            return {"ok": True, "sticker_id": cur.lastrowid}
        if action == "list":
            q, args = "SELECT * FROM stickers WHERE 1=1", []
            if emotion:
                q += " AND (emotion LIKE ? OR tags LIKE ?)"; args += [f"%{emotion}%", f"%{emotion}%"]
            if tags:
                q += " AND tags LIKE ?"; args.append(f"%{tags}%")
            rows = [dict(r) for r in db.CONN.execute(q + " ORDER BY id", args)]
            return {"stickers": rows}
        if action == "delete":
            if not sticker_id:
                raise ValueError("delete 需要 sticker_id")
            db.CONN.execute("DELETE FROM stickers WHERE id=?", (int(sticker_id),))
            db.CONN.commit()
            return {"ok": True}
    raise ValueError(f"未知 action: {action}")


def t_pick_sticker(emotion="", **_):
    with db.LOCK:
        q, args = "SELECT * FROM stickers WHERE 1=1", []
        if emotion:
            q += " AND (emotion LIKE ? OR tags LIKE ?)"
            args += [f"%{emotion}%", f"%{emotion}%"]
        row = db.CONN.execute(q + " ORDER BY use_count, RANDOM() LIMIT 1", args).fetchone()
        if not row:
            return {"found": False, "hint": "库里没有匹配该情绪的表情包，可以先用 manage_stickers add 收集，或本次只发文字"}
        db.CONN.execute("UPDATE stickers SET use_count=use_count+1 WHERE id=?", (row["id"],))
        db.CONN.commit()
    return {"found": True, "sticker": dict(row)}


def t_download_media(url, **_):
    if not url or not url.startswith("http"):
        raise ValueError("需要 http(s) 图片/视频 URL")
    cap_mb = float(config.load_config().get("media", {}).get("max_download_mb", 50))
    cap = cap_mb * 1024 * 1024
    ext = ".jpg"
    m = re.search(r"\.(jpe?g|png|gif|mp4)(?:[?#]|$)", url, re.I)
    if m:
        ext = "." + m.group(1).lower()
    out = os.path.join(config.MEDIA_DIR, f"media_{int(time.time()*1000)}{ext}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = b""
    with urllib.request.urlopen(req, timeout=60) as resp:
        cl = resp.headers.get("Content-Length")
        if cl and int(cl) > cap:
            raise ValueError(f"文件超过下载上限 {cap_mb:g}MB（Content-Length {cl}），已中止")
        while True:
            chunk = resp.read(524288)
            if not chunk:
                break
            data += chunk
            if len(data) > cap:
                raise ValueError(f"文件超过下载上限 {cap_mb:g}MB，已中止下载")
    with open(out, "wb") as f:
        f.write(data)
    return {"path": out}


def t_extract_video_frames(video_path, frames=4, **_):
    if not video_path or not os.path.exists(video_path):
        raise ValueError(f"video_path 不存在: {video_path}")
    ffmpeg = config.load_config().get("video", {}).get("ffmpeg_path", "ffmpeg")
    exe = shutil.which(ffmpeg) or (ffmpeg if os.path.sep in ffmpeg and os.path.exists(ffmpeg) else None)
    if not exe:
        raise RuntimeError("未找到 ffmpeg，无法抽帧。安装：winget install Gyan.FFmpeg，"
                           "或 set_config video.ffmpeg_path 指向 ffmpeg.exe 全路径")
    frames = max(2, min(int(frames), 8))
    r = None
    duration = 0.0
    try:
        r = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", video_path], capture_output=True, timeout=60)
        duration = float(r.stdout.decode("utf-8", "replace").strip() or 0)
    except Exception:
        duration = 0.0
    fdir = os.path.join(config.MEDIA_DIR, "frames", f"v{int(time.time() * 1000)}")
    os.makedirs(fdir, exist_ok=True)
    if duration > 0:
        cmd = [exe, "-y", "-i", video_path, "-vf", f"fps={frames / duration:.4f}",
               "-frames:v", str(frames + 2), os.path.join(fdir, "f_%02d.jpg")]
    else:
        cmd = [exe, "-y", "-i", video_path, "-vf", "fps=1",
               "-frames:v", str(frames), os.path.join(fdir, "f_%02d.jpg")]
    subprocess.run(cmd, capture_output=True, timeout=120)
    paths = [os.path.join(fdir, f) for f in sorted(os.listdir(fdir))]
    if not paths:
        detail = r.stderr.decode("utf-8", "replace")[:200] if (duration > 0 and r) else "ffmpeg 无输出"
        raise RuntimeError("抽帧失败：" + detail)
    return {"frames": paths, "hint": "用 Read 逐帧查看以理解视频内容"}
