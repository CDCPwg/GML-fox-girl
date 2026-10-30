"""收件箱看门脚本（零模型成本，双班制）：

- 夜班（大脑歇班时段，默认 23:00~次日 09:00）：每秒检查待处理入站消息，
  出现即打印 PENDING_MESSAGES 并退出，唤醒 ZCode 会话处理消息。
- 日班（brain.duty_start_hour~duty_end_hour，默认 09:00~23:00）：独立大脑值班，
  本脚本长睡到交班时刻，零查询零空转（班次与 brain.py 值班表共用同一配置）。

部署方式二选一：
- ZCode 后台任务：进程退出即触发任务完成通知，会话处理完消息后重新挂回；
  无消息超过 INBOX_WATCH_MAX_SECONDS 秒（默认 4 小时）也退出重挂防会话陈旧，设 0 关闭。
- 计划任务/守护进程常驻：以 24 小时方式运行，同一脚本无需改动。

只读连接 SQLite（mode=ro），避免与 collector/brain 写入冲突。
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta

DATA_DIR = os.environ.get("PERSONA_BOT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".persona-bot")
DB = os.path.join(DATA_DIR, "persona.db")
MAX_BLOCK_SECONDS = float(os.environ.get("INBOX_WATCH_MAX_SECONDS", 4 * 3600) or 0)
CHECK_SQL = ("SELECT COUNT(*) FROM messages"
             " WHERE processed=0 AND direction='in' AND msg_type!='blocked'")


def duty_hours():
    """班次读 config.json 的 brain.duty_start_hour / duty_end_hour（与 brain.py 同源）。"""
    start_h, end_h = 9, 23
    try:
        with open(os.path.join(DATA_DIR, "config.json"), encoding="utf-8") as f:
            b = (json.load(f).get("brain") or {})
        start_h = int(b.get("duty_start_hour", 9))
        end_h = int(b.get("duty_end_hour", 23))
    except Exception:
        pass
    return start_h, end_h


def in_brain_duty(hour, start_h, end_h):
    if start_h == end_h:
        return False  # 全天看门
    if start_h < end_h:
        return start_h <= hour < end_h
    return hour >= start_h or hour < end_h  # 跨午夜班（如 22~06）


if not os.path.exists(DB):
    print(f"DB_NOT_FOUND={DB}")
    sys.exit(1)

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=3)
deadline = time.time() + MAX_BLOCK_SECONDS if MAX_BLOCK_SECONDS > 0 else None
while True:
    start_h, end_h = duty_hours()
    if in_brain_duty(datetime.now().hour, start_h, end_h):
        # 大脑值班：长睡到交班（end_h）时刻，期间零查询
        now = datetime.now()
        end = now.replace(hour=end_h, minute=0, second=0, microsecond=0)
        if end <= now:
            end += timedelta(days=1)
        time.sleep(max(30, (end - now).total_seconds()))
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
