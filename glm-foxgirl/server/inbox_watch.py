"""收件箱看门脚本（零模型成本）：

- 未启用值班表（默认）：24 小时每秒检查待处理入站消息，出现即打印 PENDING_MESSAGES
  并退出，唤醒 ZCode 会话处理。
- 启用值班表（brain.duty_windows，多段 "HH:MM-HH:MM"，支持跨午夜）：大脑值班时段本脚本
  长睡到交班时刻，其余时段秒查——换班自动生效（换班内置于值班表，不单设开关）。

部署方式二选一：
- ZCode 后台任务：进程退出即触发任务完成通知，会话处理完消息后重新挂回；
  无消息超过 INBOX_WATCH_MAX_SECONDS 秒（默认 4 小时）也退出重挂防会话陈旧，设 0 关闭。
- 计划任务/守护进程常驻：以 24 小时方式运行，同一脚本无需改动。

只读连接 SQLite（mode=ro），避免与 collector/brain 写入冲突。
"""
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

DATA_DIR = os.environ.get("PERSONA_BOT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".persona-bot")
DB = os.path.join(DATA_DIR, "persona.db")
MAX_BLOCK_SECONDS = float(os.environ.get("INBOX_WATCH_MAX_SECONDS", 4 * 3600) or 0)
CHECK_SQL = ("SELECT COUNT(*) FROM messages"
             " WHERE processed=0 AND direction='in' AND msg_type!='blocked'")
DUTY_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


def duty_windows():
    """值班表（与 brain.py 同源同格式）：brain.duty_windows 多段 "HH:MM-HH:MM"，
    兼容旧键 duty_start_hour/duty_end_hour（视为单段 "HH:00-HH:00"）。
    未设置 → None（全天看门）。返回 [(start_min, end_min), ...]。"""
    try:
        with open(os.path.join(DATA_DIR, "config.json"), encoding="utf-8") as f:
            b = (json.load(f).get("brain") or {})
    except Exception:
        return None
    raw = str(b.get("duty_windows") or "").strip()
    if not raw:
        sh, eh = b.get("duty_start_hour"), b.get("duty_end_hour")
        if sh is None or eh is None:
            return None
        try:
            raw = f"{int(sh):02d}:00-{int(eh):02d}:00"
        except (TypeError, ValueError):
            return None
    segs = []
    for part in raw.split(","):
        m = DUTY_RE.match(part.strip())
        if not m:
            continue
        s = int(m.group(1)) * 60 + int(m.group(2))
        e = int(m.group(3)) * 60 + int(m.group(4))
        if s <= 1440 and e <= 1440 and s != e:
            segs.append((s, e))
    return segs or None


def in_brain_duty(cur_min, segs):
    """cur_min（当日分钟数）是否落在任一值班段内（支持跨午夜段）。"""
    for s, e in segs:
        if s < e:
            if s <= cur_min < e:
                return True
        elif cur_min >= s or cur_min < e:
            return True
    return False


def current_segment_end(cur_min, segs):
    """大脑当前所在值班段的结束分钟数（不在此值班则返回 None）。"""
    for s, e in segs:
        if s < e and s <= cur_min < e:
            return e
        if s > e and (cur_min >= s or cur_min < e):
            return e
    return None


if not os.path.exists(DB):
    print(f"DB_NOT_FOUND={DB}")
    sys.exit(1)

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=3)
deadline = time.time() + MAX_BLOCK_SECONDS if MAX_BLOCK_SECONDS > 0 else None
while True:
    now = datetime.now()
    cur_min = now.hour * 60 + now.minute
    segs = duty_windows()
    end_min = current_segment_end(cur_min, segs) if segs else None
    if end_min is not None:
        # 大脑值班中：长睡到本班结束（看门接班点），期间零查询
        delta = (end_min - cur_min) % 1440 or 1440
        time.sleep(max(30, delta * 60 - now.second))
        if MAX_BLOCK_SECONDS > 0:
            deadline = time.time() + MAX_BLOCK_SECONDS
        continue
    try:
        (n,) = conn.execute(CHECK_SQL).fetchone()
    except sqlite3.OperationalError:
        n = 0
    if n > 0:
        print(f"PENDING_MESSAGES={n}")
        sys.exit(0)
    if deadline is not None and time.time() > deadline:
        print("REARM_TIMEOUT")
        sys.exit(0)
    time.sleep(1)
