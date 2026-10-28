"""微信渠道：ADB 设备、拟人轨迹、截图/读屏/通知解析、文字与表情包发送。"""
import base64
import os
import random
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

from . import access, config, db, security


def run_adb(args, timeout=30):
    adb = config.load_config()["wechat"].get("adb_path", "adb")
    if not shutil.which(adb) and os.path.sep not in adb:
        raise RuntimeError(f"找不到 adb（{adb}）。请安装 platform-tools 并在 configure_channel 里填 adb_path")
    exe = adb if os.path.isabs(adb) or os.path.sep in adb else shutil.which(adb)
    return subprocess.run([exe] + args, capture_output=True, timeout=timeout)


def resolve_device():
    serial = config.load_config()["wechat"].get("device_serial", "")
    if serial:
        return serial
    r = run_adb(["devices"])
    for line in r.stdout.decode("utf-8", "replace").splitlines()[1:]:
        line = line.strip()
        if line.endswith("\tdevice"):
            return line.split("\t")[0]
    raise RuntimeError("没有已连接的 ADB 设备。请先连接模拟器或 USB 手机并运行 adb devices 确认")


def _human_trace(serial):
    """ADB 轨迹随机化：偶尔来一下无意义小滑动，降低自动化特征。失败静默忽略。"""
    if not config.load_config()["wechat"].get("human_trace", True):
        return
    try:
        if random.random() < 0.35:
            run_adb(["-s", serial, "shell", "input", "swipe",
                     str(random.randint(80, 160)), str(random.randint(700, 1100)),
                     str(random.randint(80, 160)), str(random.randint(650, 1050)),
                     str(random.randint(150, 350))])
            time.sleep(random.uniform(0.3, 0.8))
    except Exception:
        pass


def t_send_wechat_message(text, **_):
    if not text:
        raise ValueError("text 不能为空")
    if security.secret_leak(text):
        return {"sent": False, "blocked": True,
                "reason": "内容疑似包含本机敏感信息（token/密钥/凭据），已被内容防火墙拦截"}
    serial = resolve_device()
    ok, why = access.rate_limit_check("wechat", "active")
    if not ok:
        return {"sent": False, "blocked": True, "reason": why}
    access.human_delay()
    _human_trace(serial)
    ime = run_adb(["-s", serial, "shell", "ime", "list", "-s"]).stdout.decode("utf-8", "replace")
    if "com.android.adbkeyboard" in ime:
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        run_adb(["-s", serial, "shell", "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", b64])
        time.sleep(0.6)
        run_adb(["-s", serial, "shell", "input", "keyevent", "66"])
    else:
        try:
            text.encode("ascii")
        except UnicodeEncodeError:
            raise RuntimeError("微信中文输入需要 ADBKeyboard 输入法（见 wechat-adb-setup 技能），当前设备未安装")
        run_adb(["-s", serial, "shell", "input", "text", text.replace(" ", "%s")])
        time.sleep(0.4)
        run_adb(["-s", serial, "shell", "input", "keyevent", "66"])
    with db.LOCK:
        db.CONN.execute(
            "INSERT INTO messages(direction, channel, chat_type, chat_id, msg_type, content,"
            " out_kind, processed, created_at) VALUES('out','wechat','private','active','text',?,'reply',1,?)",
            (text, db.now_str()))
        db.CONN.commit()
    return {"sent": True, "device": serial, "method": "adbkeyboard" if "com.android.adbkeyboard" in ime else "input-text"}


def t_wechat_screenshot(**_):
    serial = resolve_device()
    out = os.path.join(config.MEDIA_DIR, f"screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png")
    r = run_adb(["-s", serial, "exec-out", "screencap", "-p"], timeout=60)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("截图失败: " + r.stderr.decode("utf-8", "replace")[:200])
    with open(out, "wb") as f:
        f.write(r.stdout)
    return {"path": out, "device": serial, "hint": "用 Read 工具查看该 PNG 即可'看见'当前微信屏幕"}


def t_send_wechat_sticker(file_path, **_):
    if not file_path or not os.path.exists(file_path):
        raise ValueError(f"file_path 不存在: {file_path}")
    serial = resolve_device()
    cfgw = config.load_config()["wechat"]
    taps = [("plus_tap_x", "plus_tap_y", "聊天右下角 ⊕ 按钮"),
            ("album_tap_x", "album_tap_y", "面板中的 相册"),
            ("first_image_tap_x", "first_image_tap_y", "相册列表第一张图"),
            ("send_btn_tap_x", "send_btn_tap_y", "右下角 发送 按钮")]
    missing = [label for kx, ky, label in taps if not cfgw.get(kx) or not cfgw.get(ky)]
    if missing:
        raise ValueError(
            "微信发表情包需先校准 4 组点击坐标（缺：" + "、".join(missing) + "）。"
            "先 wechat_screenshot 截图定坐标，再 set_config 写入 wechat.plus_tap_x/plus_tap_y/"
            "album_tap_x/album_tap_y/first_image_tap_x/first_image_tap_y/"
            "send_btn_tap_x/send_btn_tap_y 共 8 项。")
    ok, why = access.rate_limit_check("wechat", "active")
    if not ok:
        return {"sent": False, "blocked": True, "reason": why}
    access.human_delay()
    _human_trace(serial)
    remote = f"/sdcard/Pictures/glmfox_{int(time.time() * 1000)}.png"
    run_adb(["-s", serial, "push", os.path.abspath(file_path), remote], timeout=60)
    run_adb(["-s", serial, "shell", "am", "broadcast", "-a",
             "android.intent.action.MEDIA_SCANNER_SCAN_FILE", "-d", "file://" + remote])
    time.sleep(1.5)
    for kx, ky, _label in taps:
        run_adb(["-s", serial, "shell", "input", "tap", str(int(cfgw[kx])), str(int(cfgw[ky]))])
        time.sleep(random.uniform(0.8, 1.6))
    with db.LOCK:
        db.CONN.execute(
            "INSERT INTO messages(direction, channel, chat_type, chat_id, msg_type, content,"
            " media_path, out_kind, processed, created_at)"
            " VALUES('out','wechat','private','active','sticker','[表情包]',?,'reply',1,?)",
            (os.path.abspath(file_path), db.now_str()))
        db.CONN.commit()
    return {"sent": True, "device": serial,
            "note": "已按校准坐标发送；如卡在某一步，wechat_screenshot 检查并重新校准"}


def _parse_ui_xml(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return {"error": f"XML 解析失败: {e}"}
    texts = []
    for el in root.iter("node"):
        t = (el.get("text") or "").strip()
        d = (el.get("content-desc") or "").strip()
        rid = el.get("resource-id") or ""
        if t:
            texts.append({"text": t, "resource_id": rid})
        if d:
            texts.append({"text": d, "resource_id": rid, "desc": True})
    return {"texts": texts}


def t_wechat_read_screen(xml_text=None, **_):
    """无障碍层级读屏：uiautomator dump 拿当前屏幕精确文字（比截图 OCR 准，可含群昵称）。
    传 xml_text 可离线解析（调试/测试用）。"""
    if xml_text:
        parsed = _parse_ui_xml(xml_text)
        if "error" in parsed:
            raise ValueError(parsed["error"])
        return {"source": "provided", "count": len(parsed["texts"]), "texts": parsed["texts"],
                "hint": "按屏幕顺序排列；结合上下文区分气泡/按钮"}
    serial = resolve_device()
    run_adb(["-s", serial, "shell", "uiautomator", "dump", "/sdcard/glmfox_ui.xml"], timeout=60)
    r = run_adb(["-s", serial, "shell", "cat", "/sdcard/glmfox_ui.xml"], timeout=60)
    xml_text = r.stdout.decode("utf-8", "replace")
    if "hierarchy" not in xml_text:
        raise RuntimeError("uiautomator dump 失败：屏幕未就绪或机型限制，稍等重试一次")
    parsed = _parse_ui_xml(xml_text)
    if "error" in parsed:
        raise RuntimeError(parsed["error"])
    out_path = os.path.join(config.MEDIA_DIR, f"ui_{int(time.time() * 1000)}.xml")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml_text)
    return {"source": serial, "count": len(parsed["texts"]), "texts": parsed["texts"],
            "raw_xml": out_path,
            "hint": "文字精确无 OCR 误差；群聊里昵称通常单独成节点。看图片/表情包仍用 wechat_screenshot"}


NOTIF_TEXT_RE = re.compile(r'android\.text=(?:Seq\()?"((?:[^"\\]|\\.)*)"')
NOTIF_TITLE_RE = re.compile(r'android\.title=(?:Seq\()?"((?:[^"\\]|\\.)*)"')


def _parse_notifications(raw):
    out = []
    for block in re.split(r"(?=NotificationRecord)", raw):
        if "com.tencent.mm" not in block:
            continue
        tm = NOTIF_TEXT_RE.search(block)
        tl = NOTIF_TITLE_RE.search(block)
        body = tm.group(1).strip() if tm else ""
        title = tl.group(1).strip() if tl else ""
        if not body and not title:
            continue
        m3 = re.match(r"^(.{1,40}?)[：:]\s*(.{1,40}?)[：:]\s*(.+)$", body)
        m2 = re.match(r"^(.{1,40}?)[：:]\s*(.+)$", body)
        if m3:
            out.append({"group": m3.group(1).strip(), "speaker": m3.group(2).strip(),
                        "text": m3.group(3).strip()})
        elif m2:
            out.append({"speaker": m2.group(1).strip(), "text": m2.group(2).strip()})
        elif title:
            out.append({"speaker": title, "text": body})
        elif body:
            out.append({"speaker": "", "text": body})
    return out


def t_wechat_read_notifications(raw_text=None, **_):
    """从微信通知栏补捞后台错过的消息（私聊=昵称+内容，群聊=群名+昵称+内容）。
    需要微信通知权限开启、微信在后台。传 raw_text 可离线解析（调试/测试用）。"""
    if raw_text is None:
        serial = resolve_device()
        r = run_adb(["-s", serial, "shell", "dumpsys", "notification", "--noredact"], timeout=60)
        raw_text = r.stdout.decode("utf-8", "replace")
        if "Usage:" in raw_text or len(raw_text) < 200:
            r = run_adb(["-s", serial, "shell", "dumpsys", "notification"], timeout=60)
            raw_text = r.stdout.decode("utf-8", "replace")
    msgs = _parse_notifications(raw_text)
    return {"count": len(msgs), "messages": msgs,
            "hint": "捞到的新消息可按 channel=wechat 走心跳循环处理；通知会聚合，同一人连发多句可能合并成一条"}
