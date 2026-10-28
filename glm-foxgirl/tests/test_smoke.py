#!/usr/bin/env python3
"""persona-bot smoke test: MCP handshake + full brain tool chain + collector.

Run: python tests/test_smoke.py
"""
import base64
import json
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zlib
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server", "persona_mcp.py")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))


def make_png(path):
    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def start_embed_server(port=3977):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n))
            inputs = body.get("input")
            if isinstance(inputs, str):
                inputs = [inputs]
            vecs = [[float(t.count("猫")), float(t.count("狗")), float(len(t) % 7) / 7.0]
                    for t in inputs]
            out = json.dumps({"data": [{"embedding": v} for v in vecs]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, fmt, *args):
            pass

    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def start_fake_napcat(port=3999):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            out = b'{"status":"ok","retcode":0,"data":null}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, fmt, *args):
            pass

    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def make_card_png(path, card):
    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    payload = base64.b64encode(json.dumps(card).encode("utf-8"))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                + chunk(b"tEXt", b"chara\x00" + payload)
                + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
                + chunk(b"IEND", b""))


class Client:
    def __init__(self, data_dir):
        env = dict(os.environ, PERSONA_BOT_DATA_DIR=data_dir, PYTHONIOENCODING="utf-8")
        self.p = subprocess.Popen([sys.executable, SERVER], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        self.n = 0

    def call(self, method, params=None):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        self.p.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline().decode("utf-8"))

    def tool(self, name, args=None):
        body = self.call("tools/call", {"name": name, "arguments": args or {}})["result"]
        return json.loads(body["content"][0]["text"]), body.get("isError", False)

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        self.p.terminate()
        try:
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def main():
    tmp = tempfile.mkdtemp(prefix="persona_test_")
    print(f"[data dir] {tmp}")
    c = Client(tmp)

    r = c.call("initialize", {"protocolVersion": "2024-11-05"})
    check("MCP initialize 握手", r["result"]["serverInfo"]["name"] == "glm-foxgirl")

    r = c.call("tools/list")
    tools = [t["name"] for t in r["result"]["tools"]]
    check("tools/list 返回 43 个工具", len(tools) == 43, f"got {len(tools)}: {tools}")

    data, err = c.tool("manage_persona", {"action": "create", "name": "小婉",
                                          "prompt": "你叫小婉，20岁大学生，语气活泼爱用颜文字，游戏话痨。",
                                          "style": "短句，爱打趣"})
    check("创建人格", not err and data.get("ok"), data)
    data, err = c.tool("manage_persona", {"action": "set_active", "name": "小婉"})
    check("激活人格", not err and data.get("ok"), data)
    data, _ = c.tool("manage_persona", {"action": "list"})
    check("list 显示激活人格", data.get("active") == "小婉", data)

    data, _ = c.tool("manage_rules", {"action": "add", "list_type": "blacklist",
                                      "channel": "qq", "scope": "contact", "target_id": "10086", "note": "骚扰"})
    check("加黑名单", data.get("ok"), data)
    data, _ = c.tool("check_access", {"channel": "qq", "chat_type": "private", "chat_id": "10086", "contact_id": "10086"})
    check("黑名单被拒", data.get("allowed") is False, data)
    data, _ = c.tool("check_access", {"channel": "qq", "chat_type": "private", "chat_id": "10001", "contact_id": "10001"})
    check("陌生人放行", data.get("allowed") is True, data)
    data, _ = c.tool("manage_rules", {"action": "add", "list_type": "whitelist", "channel": "qq",
                                      "scope": "contact", "target_id": "10001"})
    c.tool("check_access", {"channel": "qq", "chat_type": "private", "chat_id": "10002", "contact_id": "10002"})
    data, _ = c.tool("check_access", {"channel": "qq", "chat_type": "private", "chat_id": "10002", "contact_id": "10002"})
    check("白名单非空时外人被拒", data.get("allowed") is False, data)
    c.tool("manage_rules", {"action": "remove", "list_type": "whitelist", "channel": "qq",
                            "scope": "contact", "target_id": "10001"})
    data, _ = c.tool("check_access", {"channel": "qq", "chat_type": "private", "chat_id": "10002", "contact_id": "10002"})
    check("清空白名单后恢复放行", data.get("allowed") is True, data)

    c.tool("simulate_inbound", {"channel": "qq", "chat_type": "group", "chat_id": "777",
                                "contact_id": "10001", "display_name": "阿明",
                                "text": "今晚开黑吗？"})
    c.tool("simulate_inbound", {"channel": "qq", "chat_type": "private", "chat_id": "10086",
                                "contact_id": "10086", "display_name": "骚扰者", "text": "你好"})
    data, _ = c.tool("get_pending_messages")
    msgs = data.get("messages", [])
    check("待处理消息 2 条", data.get("count") == 2, data)
    ming = next((m for m in msgs if m["contact_id"] == "10001"), None)
    check("消息带发言人与画像字段", bool(ming) and ming.get("display_name") == "阿明"
          and "profile_impression" in ming, ming)

    c.tool("store_memory", {"channel": "qq", "chat_id": "777", "contact_id": "10001",
                            "content": "阿明周六固定晚上开黑打王者", "importance": 4})
    data, _ = c.tool("query_memory", {"keyword": "开黑", "contact_id": "10001"})
    check("记忆写入并可检索", data.get("count") == 1 and "开黑" in data["memories"][0]["content"], data)

    data, _ = c.tool("manage_profile", {"action": "upsert", "channel": "qq", "contact_id": "10001",
                                        "display_name": "阿明", "impression": "游戏好友，热情",
                                        "affinity_delta": 5})
    check("画像 upsert 且 affinity=5", data.get("profile", {}).get("affinity") == 5, data)

    png = os.path.join(tmp, "s.png")
    make_png(png)
    data, _ = c.tool("manage_stickers", {"action": "add", "file_path": png, "emotion": "happy", "tags": "开黑,狗头"})
    check("表情包入库", data.get("ok"), data)
    data, _ = c.tool("pick_sticker", {"emotion": "happy"})
    check("按情绪挑表情包", data.get("found") and os.path.exists(data["sticker"]["file_path"]), data)
    data, _ = c.tool("pick_sticker", {"emotion": "sad"})
    check("无匹配情绪时明确返回", data.get("found") is False, data)

    data, err = c.tool("send_qq_message", {"chat_type": "private", "chat_id": "10001", "text": "hi"})
    check("渠道未启用时发送报错(而非假成功)", err and "未启用" in data.get("error", ""), (err, data))

    c.tool("set_config", {"key": "proactive.enabled", "value": True})
    c.tool("set_config", {"key": "proactive.quiet_hours", "value": ""})
    data, _ = c.tool("proactive_due")
    check("主动聊天开关生效", data.get("proactive_allowed") is True, data)
    c.tool("set_config", {"key": "proactive.quiet_hours", "value": "00:00-23:59"})
    data, _ = c.tool("proactive_due")
    check("安静时段拦截主动聊天", data.get("proactive_allowed") is False, data)
    c.tool("set_config", {"key": "proactive.quiet_hours", "value": "23:30-07:30"})

    data, _ = c.tool("bot_status")
    check("bot_status 状态齐全", data.get("active_persona") == "小婉"
          and data["counts"]["pending_messages"] == 2
          and data["counts"]["rules"] >= 1 and data["counts"]["stickers"] == 1, data)

    ids = [m["id"] for m in msgs]
    c.tool("mark_processed", {"message_ids": ids})
    data, _ = c.tool("get_pending_messages")
    check("标记已处理后清空收件箱", data.get("count") == 0, data)

    print("[persona library] 人格库与管理指令")
    data, _ = c.tool("manage_persona", {"action": "create", "name": "阿玲",
                                        "prompt": "你叫阿玲，25岁插画师，说话温柔慢热，喜欢猫。",
                                        "style": "轻声细语"})
    check("创建第二个人格", data.get("ok"), data)
    data, _ = c.tool("bot_command", {"text": "今天天气不错", "channel": "qq", "contact_id": "10001"})
    check("普通聊天不触发指令", data.get("handled") is False, data)
    data, _ = c.tool("bot_command", {"text": "人格列表", "channel": "qq", "contact_id": "99999"})
    check("非管理员被拒", data.get("handled") is True and data.get("allowed") is False, data)
    c.tool("set_config", {"key": "admin.qq", "value": "10001"})
    data, _ = c.tool("bot_command", {"text": "人格列表", "channel": "qq", "contact_id": "10001"})
    check("管理员查看人格库", data.get("allowed") is True and "小婉" in data.get("reply", ""), data)
    data, _ = c.tool("bot_command", {"text": "切换人格阿玲", "channel": "qq", "contact_id": "10001"})
    check("管理员热切换人格", data.get("ok") is True and data.get("data", {}).get("active") == "阿玲", data)
    data, _ = c.tool("manage_persona", {"action": "list"})
    check("切换后激活人格生效", data.get("active") == "阿玲", data)
    data, _ = c.tool("bot_command", {"text": "切换人格 幽灵", "channel": "qq", "contact_id": "10001"})
    check("切换不存在人格被拦截", data.get("ok") is False and "现有" in data.get("reply", ""), data)
    data, _ = c.tool("bot_command", {"text": "机器人状态", "channel": "qq", "contact_id": "10001"})
    check("状态指令", data.get("action") == "status"
          and data.get("data", {}).get("pending_messages") == 0, data)
    data, _ = c.tool("manage_persona", {"action": "export", "name": "阿玲"})
    exported = data.get("file", "")
    check("导出人格 JSON", data.get("ok") and os.path.exists(exported), data)
    c.tool("manage_persona", {"action": "delete", "name": "阿玲"})
    data, _ = c.tool("manage_persona", {"action": "import", "file_path": exported})
    check("导入人格(原名恢复)", data.get("name") == "阿玲", data)
    data, _ = c.tool("manage_persona", {"action": "import", "file_path": exported})
    check("重名导入自动唯一化", data.get("name") == "阿玲_2", data)

    print("[v0.5] 向量记忆 / 视频抽帧 / 戳一戳 / 表情包库")
    data, _ = c.tool("store_memory", {"channel": "qq", "contact_id": "10001", "content": "阿明想学吉他"})
    check("未配置 embedding 时记忆照存", data.get("ok") and data.get("embedded") is False, data)
    srv = start_embed_server(3977)
    c.tool("set_config", {"key": "memory.embed_base_url", "value": "http://127.0.0.1:3977"})
    data, _ = c.tool("store_memory", {"channel": "qq", "contact_id": "10001",
                                      "content": "阿明家的猫叫煤球，特别胖"})
    check("配置后记忆自动向量化", data.get("embedded") is True, data)
    c.tool("store_memory", {"channel": "qq", "contact_id": "10001", "content": "阿明想养一只狗"})
    data, _ = c.tool("query_memory", {"contact_id": "10001", "keyword": "他家的猫", "semantic": True})
    top = (data.get("memories") or [{}])[0]
    check("语义检索命中猫且带相似度", "煤球" in top.get("content", "")
          and top.get("similarity", 0) > 0, data)
    srv.shutdown()

    if shutil.which("ffmpeg"):
        ffmpeg_video = os.path.join(tmp, "test.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", ffmpeg_video],
                       capture_output=True, timeout=60)
        data, err = c.tool("extract_video_frames", {"video_path": ffmpeg_video})
        check("视频抽帧(ffmpeg)", not err and len(data.get("frames", [])) >= 2
              and all(os.path.exists(p) for p in data["frames"]), (err, data))
    else:
        print("  SKIP 视频抽帧(ffmpeg)：未安装 ffmpeg，该项跳过，其余检查不受影响")

    data, err = c.tool("poke_qq", {"group_id": "777", "user_id": "10001"})
    check("戳一戳渠道未启用时报错", err and "未启用" in data.get("error", ""), (err, data))
    data, err = c.tool("send_wechat_sticker", {"file_path": png})
    check("微信发表情包无设备时报错", err and "adb" in data.get("error", "").lower(), (err, data))
    data, _ = c.tool("manage_stickers", {"action": "add", "file_path": png, "emotion": "happy",
                                         "source_url": "https://example.com/s1.jpg"})
    data2, _ = c.tool("manage_stickers", {"action": "add", "file_path": png, "emotion": "happy",
                                          "source_url": "https://example.com/s1.jpg"})
    check("表情包按 URL 去重", data.get("ok") and not data.get("deduped")
          and data2.get("deduped") is True, (data, data2))

    print("[v0.6] 遗忘曲线 / SillyTavern / 发送队列")
    c.tool("store_memory", {"channel": "qq", "contact_id": "10001", "content": "顺口聊过的天气",
                            "importance": 2, "emotion": -3})
    data, _ = c.tool("query_memory", {"keyword": "天气"})
    check("情绪效价随记忆存储", data["memories"][0].get("emotion") == -3, data)

    old_ts = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(os.path.join(tmp, "persona.db"))
    conn.execute("UPDATE memories SET created_at=? WHERE content LIKE ?", (old_ts, "%开黑打王者%"))
    conn.commit()
    conn.close()
    data, _ = c.tool("query_memory", {"contact_id": "10001", "limit": 50})
    top = data["memories"][0]
    kaihei = next(m for m in data["memories"] if "开黑" in m["content"])
    check("遗忘曲线：旧记忆衰减不再置顶", "开黑" not in top["content"] and kaihei["score"] < 0.2,
          (top["content"], kaihei["score"]))
    tianqi = next(m for m in data["memories"] if "天气" in m["content"])
    check("遗忘曲线：新记忆得分不被错误衰减(P0回归)", tianqi["score"] >= 4.0, tianqi["score"])
    data, _ = c.tool("prune_memory", {"keep_score": 1.0})
    check("遗忘清理：keep_score=1.0 不误删新记忆(P0回归)", data.get("would_delete") == 0, data)

    data, _ = c.tool("prune_memory", {"keep_score": 3.5})
    check("遗忘清理预览(dry_run)", data.get("would_delete", 0) >= 3
          and all("开黑" not in s[2] for s in data.get("samples", [])), data)
    data, _ = c.tool("prune_memory", {"keep_score": 3.5, "dry_run": False})
    check("遗忘清理执行", data.get("deleted", 0) >= 3, data)
    data, _ = c.tool("query_memory", {"keyword": "开黑"})
    check("重要级4记忆不被清理", data.get("count") == 1, data)

    card = {"spec": "chara_card_v2", "data": {"name": "猫娘雪糕", "description": "一只懒洋洋的猫娘",
                                              "personality": "嗜睡、贪吃", "scenario": "午后的宠物店"}}
    card_path = os.path.join(tmp, "card.json")
    with open(card_path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False)
    data, err = c.tool("import_sillytavern", {"file_path": card_path})
    check("ST 角色卡 JSON 导入", not err and data.get("name") == "猫娘雪糕", (err, data))
    data, _ = c.tool("manage_persona", {"action": "list"})
    xuegao = next((p for p in data["personas"] if p["name"] == "猫娘雪糕"), {})
    check("ST 卡字段映射进 prompt", "懒洋洋的猫娘" in xuegao.get("prompt", "")
          and "嗜睡" in xuegao.get("prompt", "") and "宠物店" in xuegao.get("prompt", ""), xuegao)
    png_card = os.path.join(tmp, "card.png")
    make_card_png(png_card, card)
    data, err = c.tool("import_sillytavern", {"file_path": png_card})
    check("ST 角色卡 PNG 导入(重名唯一化)", not err and data.get("name") == "猫娘雪糕_2", (err, data))

    c.tool("configure_channel", {"channel": "qq", "enabled": True,
                                 "napcat_base_url": "http://127.0.0.1:3998"})
    data, _ = c.tool("send_qq_message", {"chat_type": "private", "chat_id": "10001", "text": "补发测试"})
    check("NapCat 不可达时进入发送队列", data.get("queued") is True, data)
    data, _ = c.tool("bot_status")
    check("状态显示队列与 intro 标记", data["counts"].get("outbox") == 1
          and "intro_shown" in data, data)
    nap = start_fake_napcat(3999)
    c.tool("configure_channel", {"channel": "qq", "napcat_base_url": "http://127.0.0.1:3999"})
    data, _ = c.tool("flush_outbox")
    check("恢复后补发成功", data.get("sent") == 1 and data.get("remaining") == 0, data)
    nap.shutdown()

    print("[v0.7] 心情 / 统计 / WebUI")
    data, _ = c.tool("manage_mood", {"action": "set", "mood": 9, "reason": "被夸爆了"})
    check("心情设置并夹到范围", data.get("mood") == 2, data)
    data, _ = c.tool("manage_mood", {"action": "get"})
    check("心情读取", data.get("mood") == 2 and "被夸爆了" in data.get("reason", ""), data)
    data, _ = c.tool("get_stats")
    check("统计报表可用", data.get("week", {}).get("in", 0) >= 2
          and isinstance(data.get("top_profiles"), list), data)

    env2 = dict(os.environ, PERSONA_BOT_DATA_DIR=tmp, PYTHONIOENCODING="utf-8")
    web = subprocess.Popen([sys.executable, SERVER, "--webui", "--port", "3988"], env=env2,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    up = False
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", 3988), timeout=0.3):
                up = True
                break
        except OSError:
            time.sleep(0.3)
    try:
        page = urllib.request.urlopen("http://127.0.0.1:3988/", timeout=5).read().decode("utf-8")
        st = json.loads(urllib.request.urlopen("http://127.0.0.1:3988/api/state", timeout=5).read().decode("utf-8"))
        req = urllib.request.Request("http://127.0.0.1:3988/api/mood",
                                     data=json.dumps({"mood": 1}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        mr = json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))
        check("WebUI 页面与 API", up and "GLM狐狸娘" in page
              and "personas" in st and mr.get("ok") is True, (up, bool(st), mr))
    finally:
        web.terminate()
        try:
            web.wait(timeout=5)
        except Exception:
            web.kill()

    print("[v0.8] 记忆图")
    c.tool("graph_add", {"subject": "阿明", "relation": "在玩", "target": "王者荣耀",
                         "subject_kind": "person", "target_kind": "topic"})
    c.tool("graph_add", {"subject": "阿明", "relation": "养了", "target": "煤球",
                         "subject_kind": "person", "target_kind": "pet",
                         "note": "一只特别胖的猫"})
    c.tool("graph_add", {"subject": "煤球", "relation": "讨厌", "target": "洗澡",
                         "subject_kind": "pet", "target_kind": "thing"})
    data, _ = c.tool("graph_query", {"entity": "阿明"})
    check("记忆图一跳关联", data.get("found") and len(data.get("edges", [])) == 2
          and any(e["dst_name"] == "煤球" for e in data["edges"]), data)
    data, _ = c.tool("graph_query", {"entity": "煤球", "depth": 2})
    names = {n["name"] for n in data.get("nodes", [])}
    check("记忆图二跳扩散到王者荣耀", data.get("found") and "王者荣耀" in names, data)
    c.tool("graph_add", {"subject": "阿明", "relation": "在玩", "target": "王者荣耀"})
    data, _ = c.tool("graph_query", {"entity": "阿明"})
    e = next(x for x in data["edges"] if x["dst_name"] == "王者荣耀")
    check("重复三元组加权不重复", e["weight"] == 2, e)
    data, _ = c.tool("graph_query", {})
    check("记忆图总览", data.get("overview") and len(data.get("nodes", [])) >= 3, data)
    data, _ = c.tool("bot_status")
    check("状态含记忆图计数", data["counts"].get("graph_nodes") == 4
          and data["counts"].get("graph_edges") == 3, data["counts"])

    print("[v0.9] 微信精确发言人（无障碍读屏 + 通知解析）")
    sample_xml = ('<?xml version="1.0"?><hierarchy rotation="0">'
                  '<node index="0" text="" resource-id="" class="android.widget.FrameLayout">'
                  '<node index="1" text="小明" resource-id="com.tencent.mm:id/name"/>'
                  '<node index="2" text="晚上一起开黑吗？" resource-id="com.tencent.mm:id/bubble"/>'
                  '<node index="3" text="" content-desc="返回" resource-id="com.tencent.mm:id/back"/>'
                  '</node></hierarchy>')
    data, _ = c.tool("wechat_read_screen", {"xml_text": sample_xml})
    texts = [t["text"] for t in data.get("texts", [])]
    check("无障碍读屏精确取字", data.get("count") == 3 and "小明" in texts
          and "晚上一起开黑吗？" in texts, data)
    data, err = c.tool("wechat_read_screen", {"xml_text": "<broken"})
    check("坏 XML 明确报错", err and "解析失败" in data.get("error", ""), (err, data))
    sample_notif = ('NotificationRecord{u=0 pkg=com.tencent.mm}\n'
                    '  android.title=Seq("硬盘坏了群")\n'
                    '  android.text=Seq("硬盘坏了群: 小明: 开黑吗")\n'
                    'NotificationRecord{u=0 pkg=com.tencent.mm}\n'
                    '  android.title=Seq("小明")\n'
                    '  android.text=Seq("私聊内容测试")\n'
                    'NotificationRecord{u=0 pkg=other.app}\n'
                    '  android.text=Seq("不该出现")\n')
    data, _ = c.tool("wechat_read_notifications", {"raw_text": sample_notif})
    msgs = data.get("messages", [])
    check("通知解析群聊三元组", any(m.get("group") == "硬盘坏了群" and m.get("speaker") == "小明"
                                   and m.get("text") == "开黑吗" for m in msgs), data)
    check("通知解析私聊且过滤其他应用", any(m.get("speaker") == "小明"
                                          and m.get("text") == "私聊内容测试" for m in msgs)
          and len(msgs) == 2, data)

    print("[v0.10] 会话接力 / 记录追溯 / 专用工作区")
    data, _ = c.tool("session_note", {"action": "save", "title": "测试接力",
                                      "content": "处理了阿明的开黑邀约；待跟进：猫粮品牌"})
    nid = data.get("note_id")
    check("接力笔记保存", data.get("ok") and nid, data)
    data, _ = c.tool("session_note", {"action": "get", "note_id": nid})
    check("接力笔记读取", "猫粮品牌" in (data.get("note") or {}).get("content", ""), data)
    data, _ = c.tool("session_note", {"action": "list", "limit": 5})
    check("接力笔记列表", any(n["id"] == nid for n in data.get("notes", [])), data)
    data, _ = c.tool("search_messages", {"keyword": "补发"})
    check("记录追溯：关键词命中发送日志", data.get("count", 0) >= 1
          and any(m["direction"] == "out" for m in data["messages"]), data)
    data, _ = c.tool("search_messages", {"keyword": "开黑", "since": "2099-01-01"})
    check("记录追溯：时间过滤", data.get("count") == 0, data)
    data, _ = c.tool("export_transcript")
    check("导出 Markdown 存档", data.get("ok") and os.path.exists(data.get("path", ""))
          and data.get("count", 0) >= 1, data)
    data, _ = c.tool("bot_status")
    check("状态含接力笔记计数", "notes" in data["counts"], data["counts"])
    c.tool("set_config", {"key": "setup.completed_at", "value": "2026-10-28 12:00"})
    data, _ = c.tool("bot_status")
    check("配置页完成标记", data.get("setup_completed") is True, data)

    print("[v0.11] 自动交接")
    c.tool("set_config", {"key": "session.turns", "value": 0})
    data, _ = c.tool("session_progress", {})
    check("会话进度计数", data.get("turns") == 1 and data.get("auto_handover") is False, data)
    c.tool("session_progress", {})
    c.tool("set_config", {"key": "limits.auto_handover_turns", "value": 3})
    data, _ = c.tool("session_progress", {})
    check("达到阈值触发自动交接", data.get("turns") == 3 and data.get("auto_handover") is True, data)
    c.tool("set_config", {"key": "limits.auto_handover_enabled", "value": False})
    data, _ = c.tool("session_progress", {})
    check("自动交接可关闭", data.get("auto_handover") is False and bool(data.get("reason")), data)
    c.tool("set_config", {"key": "limits.auto_handover_enabled", "value": True})
    data, _ = c.tool("bot_status")
    check("状态含会话进度", (data.get("session") or {}).get("turns", 0) >= 3, data)
    hook_env = dict(os.environ, PERSONA_BOT_DATA_DIR=tmp, PYTHONIOENCODING="utf-8")
    script = os.path.join(ROOT, "scripts", "first_run_intro.py")
    r1 = subprocess.run([sys.executable, script], env=hook_env, capture_output=True, timeout=30)
    r2 = subprocess.run([sys.executable, script], env=hook_env, capture_output=True, timeout=30)
    with open(os.path.join(tmp, "config.json"), encoding="utf-8") as f:
        cfgj = json.load(f)
    check("hook 首次输出速览并重置会话", "配置页" in r1.stdout.decode("utf-8", "replace")
          and "自动交接" in r1.stdout.decode("utf-8", "replace")
          and (cfgj.get("session") or {}).get("turns") == 0
          and cfgj.get("intro", {}).get("version") == 6, cfgj.get("session"))
    check("hook 二次静默", r2.stdout.decode("utf-8", "replace").strip() == "", r2.stdout)

    print("[v0.12] 管理守卫 / 95%提醒 / 远程交接")
    data, _ = c.tool("check_admin", {"channel": "qq", "contact_id": "10001"})
    a1 = data.get("is_admin")
    data, _ = c.tool("check_admin", {"channel": "qq", "contact_id": "88888"})
    check("管理员判定", a1 is True and data.get("is_admin") is False, (a1, data))
    data, _ = c.tool("bot_command", {"text": "交接", "channel": "qq", "contact_id": "10001"})
    check("QQ指令远程交接(管理员)", data.get("action") == "handover"
          and data.get("allowed") is True, data)
    data, _ = c.tool("bot_command", {"text": "交接", "channel": "qq", "contact_id": "88888"})
    check("非管理员交接被拒并计警", data.get("allowed") is False
          and data.get("violations") == 1, data)
    for _ in range(10):
        data, _ = c.tool("report_guard", {"channel": "qq", "contact_id": "77777",
                                          "action": "set_config"})
    check("连续尝试计数到10不拉黑", data.get("count") == 10
          and data.get("blacklisted") is False, data)
    data, _ = c.tool("report_guard", {"channel": "qq", "contact_id": "77777",
                                      "action": "set_config"})
    check("超过10次自动拉黑", data.get("count") == 11
          and data.get("blacklisted") is True, data)
    data, _ = c.tool("check_access", {"channel": "qq", "chat_type": "private",
                                      "chat_id": "77777", "contact_id": "77777"})
    check("拉黑后不再回复其消息", data.get("allowed") is False, data)
    c.tool("set_config", {"key": "session.turns", "value": 0})
    c.tool("set_config", {"key": "limits.auto_handover_turns", "value": 20})
    for _ in range(17):
        data, _ = c.tool("session_progress", {})
    check("85%轻提醒级别", data.get("level") == "warn"
          and data.get("estimated_pct") == 85, data)
    data, _ = c.tool("session_progress", {})
    data, _ = c.tool("session_progress", {})
    check("95%触发交接级别", data.get("level") == "handover"
          and data.get("estimated_pct") == 95 and data.get("auto_handover") is True, data)
    data, _ = c.tool("bot_status")
    check("状态含守卫计数", data["counts"].get("guard") == 3, data["counts"])

    print("[v0.13] QQ/WX 账号关联器")
    c.tool("manage_profile", {"action": "upsert", "channel": "wechat", "contact_id": "wx_aming",
                              "display_name": "阿明微信", "impression": "微信端印象：喜欢摄影和做饭",
                              "affinity": 5})
    data, _ = c.tool("link_accounts", {"action": "request", "channel": "qq", "contact_id": "10001",
                                       "peer_channel": "wechat", "peer_contact_id": "wx_aming"})
    check("发起关联请求并生成6位确认码", data.get("ok")
          and re.match(r"^\d{6}$", data.get("code", "")) is not None, data)
    code = data.get("code", "")
    data, err = c.tool("link_accounts", {"action": "confirm", "channel": "qq",
                                         "contact_id": "10001", "code": code})
    check("发起方不能自我确认", err and "对方账号" in data.get("error", ""), (err, data))
    data, _ = c.tool("link_accounts", {"action": "confirm", "channel": "wechat",
                                       "contact_id": "wx_aming", "code": code})
    check("画像不相似走冲突流程", data.get("status") == "conflict" and data.get("request_id"), data)
    rid = data.get("request_id")
    data, _ = c.tool("link_accounts", {"action": "resolve", "request_id": rid, "choice": "a"})
    check("冲突解决：保留发起方版本", data.get("status") == "linked", data)
    data, _ = c.tool("manage_profile", {"action": "get", "channel": "wechat", "contact_id": "wx_aming"})
    check("微信画像已继承QQ版", "游戏好友" in (data.get("profile") or {}).get("impression", ""), data)
    data, _ = c.tool("link_accounts", {"action": "ids", "channel": "qq", "contact_id": "10001"})
    ids = [i for _, i in data.get("identities", [])]
    check("身份组互联", "wx_aming" in ids, data)
    data, _ = c.tool("query_memory", {"channel": "wechat", "contact_id": "wx_aming", "keyword": "开黑"})
    check("记忆按身份组互联检索", data.get("count", 0) >= 1, data)
    data, _ = c.tool("link_accounts", {"action": "unlink", "channel": "qq", "contact_id": "10001",
                                       "peer_channel": "wechat", "peer_contact_id": "wx_aming"})
    check("解除关联", data.get("removed") == 1, data)
    c.tool("manage_profile", {"action": "upsert", "channel": "wechat", "contact_id": "wx_10002",
                              "display_name": "老王微信", "impression": "从微信继承的印象", "affinity": 2})
    data, _ = c.tool("link_accounts", {"action": "request", "channel": "qq", "contact_id": "10002",
                                       "peer_channel": "wechat", "peer_contact_id": "wx_10002"})
    code2 = data.get("code", "")
    data, _ = c.tool("link_accounts", {"action": "confirm", "channel": "wechat",
                                       "contact_id": "wx_10002", "code": code2})
    check("单侧画像直接继承(无冲突)", data.get("status") == "linked"
          and data.get("profile_inherited") is True, data)
    data, _ = c.tool("manage_profile", {"action": "get", "channel": "qq", "contact_id": "10002"})
    check("QQ侧拿到微信画像", "从微信继承" in (data.get("profile") or {}).get("impression", ""), data)

    print("[v0.14] 反思机制 / 记忆冲突消解")
    data, _ = c.tool("reflect_due", {})
    check("反思到期检查可用", data.get("count") is not None, data)
    for i in range(7):
        c.tool("store_memory", {"channel": "qq", "contact_id": "10001",
                                "content": f"阿明聊到第{i}次组队开黑的趣事", "importance": 3})
    data, _ = c.tool("reflect_due", {})
    ming = next((d for d in data["due"] if d["contact_id"] == "10001"), None)
    check("新增记忆达标触发反思", bool(ming) and ming["new_count"] >= 8, data)
    data, _ = c.tool("reflect_material", {"channel": "qq", "contact_id": "10001"})
    check("反思素材齐全", bool(data.get("memories")) and bool(data.get("profile")), data)
    c.tool("store_memory", {"channel": "qq", "contact_id": "10001", "kind": "reflection",
                            "importance": 4, "content": "洞察：阿明热衷组队开黑，是关系的重要纽带"})
    data, _ = c.tool("reflect_due", {})
    ming = next((d for d in data["due"] if d["contact_id"] == "10001"), None)
    check("反思后重置计数", ming is None, data)
    data, _ = c.tool("store_memory", {"channel": "qq", "contact_id": "10001",
                                      "content": "阿明现在在玩王者荣耀", "importance": 3})
    aid = data.get("memory_id")
    data, _ = c.tool("memory_conflicts", {"content": "阿明现在在玩原神", "contact_id": "10001"})
    cand = next((x for x in data.get("candidates", []) if x["id"] == aid), None)
    check("冲突候选识别", bool(cand) and cand.get("similarity", 0) >= 0.35, data)
    data, _ = c.tool("store_memory", {"channel": "qq", "contact_id": "10001",
                                      "content": "阿明现在在玩原神", "importance": 3,
                                      "supersedes": [aid]})
    bid = data.get("memory_id")
    check("事实更新取代旧记忆", data.get("superseded") == [aid], data)
    data, _ = c.tool("query_memory", {"keyword": "王者", "contact_id": "10001"})
    check("被取代记忆默认不出现", all(m["id"] != aid for m in data.get("memories", [])), data)
    data, _ = c.tool("query_memory", {"keyword": "原神", "contact_id": "10001"})
    check("新事实可检索", data.get("count") == 1, data)
    data, _ = c.tool("query_memory", {"keyword": "王者", "contact_id": "10001",
                                      "include_superseded": True})
    old = next((m for m in data.get("memories", []) if m["id"] == aid), None)
    check("旧记忆保留可追溯", bool(old) and old.get("superseded_by") == bid, data)

    c.close()
    time.sleep(1.0)

    print("[collector] 独立进程事件上报测试（独立端口，避开真实部署占用的 3001）")
    tmp2 = tempfile.mkdtemp(prefix="persona_coll_")
    coll_port = 3777
    with open(os.path.join(tmp2, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"collector": {"port": coll_port}}, f)
    env = dict(os.environ, PERSONA_BOT_DATA_DIR=tmp2, PYTHONIOENCODING="utf-8")
    coll = subprocess.Popen([sys.executable, SERVER, "--collector"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", coll_port), timeout=0.3):
                break
        except OSError:
            time.sleep(0.3)
    url = f"http://127.0.0.1:{coll_port}/onebot/event"
    try:
        ev = {"post_type": "message", "message_type": "group", "group_id": 888, "user_id": 2001,
              "sender": {"card": "群友A", "nickname": "a2001"},
              "message": [{"type": "text", "data": {"text": "有人吗"}},
                          {"type": "image", "data": {"url": "https://example.com/x.jpg"}}]}
        req = urllib.request.Request(url,
                                     data=json.dumps(ev).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5).read()
        ev2 = {"post_type": "message", "message_type": "private", "user_id": 2002,
               "sender": {"nickname": "b2002"}, "message": "[CQ:image,file=a.jpg,url=https://example.com/y.jpg]晚上好"}
        req = urllib.request.Request(url,
                                     data=json.dumps(ev2).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5).read()
        ev3 = {"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
               "group_id": 888, "user_id": 2001, "target_id": 999999}
        req = urllib.request.Request(url, data=json.dumps(ev3).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5).read()
        time.sleep(1.0)
        db = sqlite3.connect(os.path.join(tmp2, "persona.db"))
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute("SELECT * FROM messages ORDER BY id")]
        check("collector 收到群消息(array格式)", len(rows) >= 1 and rows[0]["chat_id"] == "888"
              and rows[0]["display_name"] == "群友A" and rows[0]["content"] == "有人吗"
              and rows[0]["msg_type"] == "mixed", rows[:1])
        if len(rows) >= 2:
            check("collector 收到私聊(CQ码格式)", rows[1]["chat_id"] == "2002"
                  and rows[1]["content"] == "晚上好"
                  and rows[1]["media_url"] == "https://example.com/y.jpg", rows[1])
        if len(rows) >= 3:
            check("collector 收到戳一戳并入库", rows[2]["msg_type"] == "poke"
                  and "2001" in rows[2]["content"], rows[2])
        profs = [dict(r) for r in db.execute("SELECT * FROM profiles")]
        check("发言人群名片自动写入画像", any(p["display_name"] == "群友A" for p in profs), profs)
        db.close()
    finally:
        coll.terminate()
        try:
            coll.wait(timeout=5)
        except Exception:
            coll.kill()

    print(f"\n结果: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
