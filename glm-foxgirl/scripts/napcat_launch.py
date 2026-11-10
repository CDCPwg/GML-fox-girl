"""NapCat 启动器（密码兜底版）：读取托管凭据（DPAPI 加密），设置 ACCOUNT +
NAPCAT_QUICK_PASSWORD 环境变量后拉起 NapCat——快速登录票据失效时自动走密码登录，
无需扫码。任何情况下不打印明文密码。

前置：set_config qq.napcat_path=<NapCat.Shell 框架目录>（含 NapCatWinBootMain.exe）；
账密经 save_qq_credentials 托管。
用法：python scripts/napcat_launch.py
"""
import os
import subprocess
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, "server"))
from foxbot import config, qq  # noqa: E402

cfg = config.load_config()["qq"]
creds = cfg.get("credentials") or {}
uin = str(creds.get("uin", ""))
shell_dir = (cfg.get("napcat_path") or "").strip()

if not shell_dir:
    print("napcat_path 未配置：set_config qq.napcat_path=<NapCat.Shell 框架目录>")
    sys.exit(2)
if shell_dir.lower().endswith(".exe"):
    shell_dir = os.path.dirname(shell_dir)

# 环境武装与插件 t_start_napcat 同源：密码兜底 + WebUI token 固定（_napcat_launch_env）
env = qq._napcat_launch_env(cfg)
armed = [k for k in ("ACCOUNT", "NAPCAT_QUICK_PASSWORD", "NAPCAT_WEBUI_SECRET_KEY")
         if env.get(k) and env[k] != os.environ.get(k, "")]
print("env armed:", ",".join(armed) if armed else "(无托管凭据/token，走原登录路径)")

exe = os.path.join(shell_dir, "NapCatWinBootMain.exe")
if not os.path.isfile(exe):
    print(f"NapCatWinBootMain.exe 不存在：{shell_dir}")
    sys.exit(2)
flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
         if os.name == "nt" else 0)
subprocess.Popen([exe] + ([uin] if uin else []), cwd=shell_dir, env=env,
                 creationflags=flags)
print("napcat launched", f"with uin={uin}" if uin else "")
