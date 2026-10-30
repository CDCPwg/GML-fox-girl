"""独立大脑手动开关（旗标控制，配合 scripts/brain_supervisor.py 使用）：
  python scripts/brain_switch.py on   → 清除停止旗标；若无大脑且无守护进程则直接拉起大脑
  python scripts/brain_switch.py off  → 创建停止旗标并杀掉大脑进程（守护 10 秒内检测旗标不再重启）

大脑内置值班表（brain.duty_start_hour/duty_end_hour，默认 09:00~23:00 值班，
其余长睡歇班），任何时刻拉起都安全；日常启停由 brain_supervisor.py 守护，本脚本用于手动检修。
"""
import os
import subprocess
import sys
import time

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(PLUGIN_DIR, "server")
DATA_DIR = os.environ.get("PERSONA_BOT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".persona-bot")
FLAG = os.path.join(DATA_DIR, "brain.off")
LOG = os.path.join(DATA_DIR, "brain_daemon.log")
MARKER = "persona_mcp.py --brain"

CREATE_FLAGS = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                if os.name == "nt" else 0)


def find_processes(marker):
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
         "Where-Object { $_.CommandLine -like '*" + marker + "*' } | "
         "Select-Object -ExpandProperty ProcessId"],
        capture_output=True, text=True)
    return [int(x) for x in out.stdout.split() if x.strip().isdigit()]


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "on":
        if os.path.exists(FLAG):
            os.remove(FLAG)
            print("flag removed")
        if find_processes(MARKER) or find_processes("brain_supervisor.py"):
            print("brain or supervisor already running")
            return
        time.sleep(2)
        log = open(LOG, "ab")
        subprocess.Popen([sys.executable, "persona_mcp.py", "--brain"], cwd=SERVER,
                         stdout=log, stderr=log, creationflags=CREATE_FLAGS)
        print("brain started (no supervisor running; start brain_supervisor.py for auto-heal)")
    elif action == "off":
        open(FLAG, "w").close()
        pids = find_processes(MARKER)
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            print("killed", pid)
        if not pids:
            print("brain not running")
    else:
        print("usage: brain_switch.py on|off")


if __name__ == "__main__":
    main()
