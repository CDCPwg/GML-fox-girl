"""收件箱看门脚本（零模型成本）：阻塞直到出现待处理入站消息，然后退出以唤醒 ZCode 会话。

由 ZCode 后台任务运行：退出即触发任务完成通知，会话被唤醒处理消息后重新挂回本脚本。
只读连接 SQLite，避免与 collector/brain 写入冲突。
路径动态解析（PERSONA_BOT_DATA_DIR 或 ~/.persona-bot）。
"""
import os
import sqlite3
import sys
import time

DB = os.path.join(
    os.environ.get("PERSONA_BOT_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".persona-bot"),
    "persona.db")
CHECK_SQL = ("SELECT COUNT(*) FROM messages"
             " WHERE processed=0 AND direction='in' AND msg_type!='blocked'")
MAX_BLOCK_SECONDS = 4 * 3600  # 4小时保险超时：无消息也退出重挂，防会话陈旧

if not os.path.exists(DB):
    print(f"DB_NOT_FOUND={DB}")
    sys.exit(1)

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=3)
deadline = time.time() + MAX_BLOCK_SECONDS
while True:
    try:
        (n,) = conn.execute(CHECK_SQL).fetchone()
    except sqlite3.OperationalError:
        time.sleep(1)
        continue
    if n > 0:
        print(f"PENDING_MESSAGES={n}")
        sys.exit(0)
    if time.time() > deadline:
        print("REARM_TIMEOUT")
        sys.exit(0)
    time.sleep(1)
