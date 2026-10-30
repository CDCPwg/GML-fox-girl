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
from foxbot import config, crypto  # noqa: E402

cfg = config.load_config()["qq"]
creds = cfg.get("credentials") or {}
uin = str(creds.get("uin", ""))
enc = creds.get("enc_password", "")
shell_dir = (cfg.get("napcat_path") or "").strip()

if not shell_dir:
    print("napcat_path 未配置：set_config qq.napcat_path=<NapCat.Shell 框架目录>")
    sys.exit(2)
if shell_dir.lower().endswith(".exe"):
    shell_dir = os.path.dirname(shell_dir)

env = os.environ.copy()
if uin:
    env["ACCOUNT"] = uin
if enc:
    try:
        env["NAPCAT_QUICK_PASSWORD"] = crypto.dpapi_unprotect(enc)
        print("password fallback: armed")
    except Exception as e:
        print("password decrypt failed:", e)

exe = os.path.join(shell_dir, "NapCatWinBootMain.exe")
if not os.path.isfile(exe):
    print(f"NapCatWinBootMain.exe 不存在：{shell_dir}")
    sys.exit(2)
flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
         if os.name == "nt" else 0)
subprocess.Popen([exe] + ([uin] if uin else []), cwd=shell_dir, env=env,
                 creationflags=flags)
print("napcat launched", f"with uin={uin}" if uin else "")
