"""大脑进程守护（零模型成本）：作为常驻后台任务运行，保证独立大脑存活。

大脑进程退出（崩溃/意外被杀）→ 10 秒后自动重启。尊重停止旗标：
  <DATA_DIR>/brain.off 存在 → 不启动（brain_switch.py off 创建、on 清除）。
配合 server/inbox_watch.py 的双班制使用：大脑值班表（brain.duty_start_hour /
duty_end_hour）窗口外大脑自行长睡歇班，任何时刻拉起都安全。
"""
import os
import subprocess
import sys
import time

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(PLUGIN_DIR, "server")
DATA_DIR = os.environ.get("PERSONA_BOT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".persona-bot")
LOG = os.path.join(DATA_DIR, "brain_daemon.log")
FLAG = os.path.join(DATA_DIR, "brain.off")
RESTART_DELAY = 10

CREATE_FLAGS = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                if os.name == "nt" else 0)

if not os.path.exists(os.path.join(SERVER, "persona_mcp.py")):
    print(f"SERVER_NOT_FOUND={SERVER}")
    sys.exit(1)

while True:
    if os.path.exists(FLAG):
        time.sleep(5)
        continue
    log = open(LOG, "ab")
    p = subprocess.Popen([sys.executable, "persona_mcp.py", "--brain"], cwd=SERVER,
                         stdout=log, stderr=log, creationflags=CREATE_FLAGS)
    p.wait()
    log.close()
    print(f"[supervisor] brain exited code={p.returncode}, restart in {RESTART_DELAY}s",
          flush=True)
    time.sleep(RESTART_DELAY)
