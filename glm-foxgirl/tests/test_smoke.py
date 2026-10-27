#!/usr/bin/env python3
"""persona-bot smoke test: MCP handshake + full brain tool chain + collector.

Run: python tests/test_smoke.py
"""
import json
import os
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zlib

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
    check("tools/list 返回 21 个工具", len(tools) == 21, f"got {len(tools)}: {tools}")

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
