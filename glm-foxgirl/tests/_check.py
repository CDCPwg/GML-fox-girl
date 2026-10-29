import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
checks = {
    "stickers 下载上限": ("server/foxbot/stickers.py", "max_download_mb"),
    "session 配置收紧": ("server/foxbot/session.py", "仅布尔串"),
    "brain 退避函数": ("server/foxbot/brain.py", "_llm_backoff_active"),
    "brain 退避门控": ("server/foxbot/brain.py", 'stats["backoff"]'),
    "identity BFS": ("server/foxbot/identity.py", "while frontier"),
    "memory M修复": ("server/foxbot/memory.py", "now_ts - db._parse_dt"),
    "tests v1.1": ("tests/test_smoke.py", "退避窗口内跳过"),
    "qq 异步告警": ("server/foxbot/qq.py", "_alert_sender_loop"),
    "qq 泄密扫描": ("server/foxbot/qq.py", "security.secret_leak"),
    "config brain退避": ("server/foxbot/config.py", "backoff_max_seconds"),
    "config 下载上限": ("server/foxbot/config.py", "max_download_mb"),
}
for name, (rel, needle) in checks.items():
    p = os.path.join(BASE, rel)
    ok = needle in open(p, encoding="utf-8").read()
    print(("OK  " if ok else "MISS") + " " + name + " -> " + rel)
