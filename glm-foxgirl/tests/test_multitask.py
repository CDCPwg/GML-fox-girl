#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""glm-foxgirl v1.6.0 多任务大脑测试：会话分组并发 / 渠道车道 / 微信守卫 / 跨端接续。

Run: python tests/test_multitask.py
（进程内直测：PERSONA_BOT_DATA_DIR 指向临时目录后 import foxbot；不依赖 ADB/真实 NapCat）
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

TMP = tempfile.mkdtemp(prefix="fox160_")
os.environ["PERSONA_BOT_DATA_DIR"] = TMP
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))


# ---------- 假 LLM（ThreadingHTTPServer：并发必需）与假 NapCat ----------
LLM_LOG = []          # {"start","end","body"}
LLM_SLEEP = 0.55
LLM_PORT = 3987
NAP_PORT = 3989
NAP_LOG = []


class FakeLLM(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace")
        rec = {"start": time.time(), "end": None, "body": body}
        LLM_LOG.append(rec)
        time.sleep(LLM_SLEEP)
        rec["end"] = time.time()
        if "记忆提取器" in body:
            payload = {"memory": "阿明在玩星穹铁道", "importance": 4, "affinity_delta": 2,
                       "impression_add": "", "graph": []}
            out = json.dumps({"choices": [{"message": {
                "content": json.dumps(payload, ensure_ascii=False)}}]}).encode("utf-8")
        else:
            out = '{"choices":[{"message":{"content":"在的呀"}}]}'.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, fmt, *args):
        pass


class FakeNap(BaseHTTPRequestHandler):
    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        NAP_LOG.append({"path": self.path, "body": data.decode("utf-8", "replace")})
        out = b'{"status":"ok","retcode":0,"data":null}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, fmt, *args):
        pass


def serve(handler, port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


serve(FakeLLM, LLM_PORT)
serve(FakeNap, NAP_PORT)

# ---------- 环境就位后再 import foxbot ----------
from foxbot import brain, config, db, identity  # noqa: E402
from foxbot import session as fox_session  # noqa: E402
from foxbot import wechat as wechat_mod  # noqa: E402
from foxbot.persona import t_manage_persona  # noqa: E402

db.init_db()
t_manage_persona("create", name="小婉", prompt="你是小婉，测试人格。", style="短句口语")
t_manage_persona("set_active", name="小婉")

cfg = config.load_config()
cfg["brain"].update({"api_base": f"http://127.0.0.1:{LLM_PORT}", "api_key": "k",
                     "model": "test-model", "max_concurrency": 3,
                     "channels": "qq", "wechat_send_mode": "guard"})
cfg["qq"].update({"enabled": True, "napcat_base_url": f"http://127.0.0.1:{NAP_PORT}",
                  "napcat_token": "t"})
cfg["humanize"].update({"reply_delay_min_ms": 1, "reply_delay_max_ms": 5})
cfg["proactive"]["reply_probability_group"] = 0
config.save_config(cfg)


def reply_calls():
    return [r for r in LLM_LOG if "这条新消息" in r["body"] or "这几条新消息" in r["body"]]


def processed(content):
    conn = sqlite3.connect(os.path.join(TMP, "persona.db"))
    row = conn.execute("SELECT processed FROM messages WHERE content=?", (content,)).fetchone()
    conn.close()
    return bool(row) and row[0] == 1


def sim(**kw):
    fox_session.t_simulate_inbound(**kw)


def max_overlap(calls):
    events = [(c["start"], 1) for c in calls] + [(c["end"], -1) for c in calls]
    cur = peak = 0
    for _, d in sorted(events):
        cur += d
        peak = max(peak, cur)
    return peak


print("[1] 同组合并：同一人连发多条 → 一次 LLM 一并回应")
sim(channel="qq", chat_type="private", chat_id="10001", contact_id="10001",
    display_name="阿明", text="多任务同组第一句")
sim(channel="qq", chat_type="private", chat_id="10001", contact_id="10001",
    display_name="阿明", text="多任务同组第二句")
st = brain.brain_process_once()
rc = reply_calls()
check("同组两条合并一次 LLM", len(rc) == 1 and "多任务同组第一句" in rc[0]["body"]
      and "多任务同组第二句" in rc[0]["body"], (len(rc), st))
check("组内批量回复后全部标记处理",
      processed("多任务同组第一句") and processed("多任务同组第二句"), st)
check("合并回复已发出", any("在的呀" in r["body"] for r in NAP_LOG), NAP_LOG[:3])

print("[2] 多任务并发：不同的人同时回复，互不阻塞")
LLM_SLEEP = 0.55
for cid, name in (("20001", "小北"), ("20002", "小南"), ("20003", "小东")):
    sim(channel="qq", chat_type="private", chat_id=cid, contact_id=cid,
        display_name=name, text=f"{name}的私聊消息")
t0 = time.time()
st = brain.brain_process_once()
wall = time.time() - t0
rc = reply_calls()[-3:]
check("三会话各自一次 LLM", len(rc) == 3 and st.get("replied") == 3, (len(rc), st))
check("并发重叠（max_concurrency=3）", max_overlap(rc) >= 2, max_overlap(rc))
check("并发显著快于串行", wall < 3 * LLM_SLEEP, f"{wall:.2f}s")
for cid, name in (("20001", "小北"), ("20002", "小南"), ("20003", "小东")):
    check(f"{name} 已回复", any("/send_private_msg" in r["path"] and cid in r["body"]
                                for r in NAP_LOG), None)

print("[3] 串行模式（max_concurrency=1）退回原行为")
cfg["brain"]["max_concurrency"] = 1
config.save_config(cfg)
for cid, name in (("30001", "甲"), ("30002", "乙")):
    sim(channel="qq", chat_type="private", chat_id=cid, contact_id=cid,
        display_name=name, text=f"{name}串行测试")
st = brain.brain_process_once()
rc = reply_calls()[-2:]
check("max_concurrency=1 无重叠", max_overlap(rc) == 1, max_overlap(rc))
check("串行仍全部回复", st.get("replied") == 2, st)

print("[4] 渠道车道：默认仅 QQ；开启后微信独立处理")
sim(channel="wechat", chat_type="private", chat_id="wxA", contact_id="wxA",
    display_name="微信甲", text="微信车道关闭时的消息")
before = len(reply_calls())
st = brain.brain_process_once()
check("车道关闭：微信不进 LLM 且保留给工作台", len(reply_calls()) == before
      and not processed("微信车道关闭时的消息"), st)
n = len(NAP_LOG)
check("车道关闭：不触发微信发送", len(NAP_LOG) == n, None)

cfg["brain"]["channels"] = "qq,wechat"
config.save_config(cfg)
brain._wx_screen_has = lambda name: True           # 守卫读屏打桩：窗口匹配
wechat_mod.t_send_wechat_message = lambda text, **k: {"sent": True, "stub": True}
sim(channel="wechat", chat_type="private", chat_id="wxA", contact_id="wxA",
    display_name="微信甲", text="微信车道开启后的消息")
before = len(reply_calls())
st = brain.brain_process_once()
check("微信车道：LLM 上下文标明微信平台", "你正在微信上与人聊天" in reply_calls()[-1]["body"],
      reply_calls()[-1]["body"][-200:])
check("微信车道：守卫通过后发出并标记处理", processed("微信车道开启后的消息")
      and st.get("replied") >= 1, st)

print("[5] 微信守卫：聊天窗不匹配 → 持等（不重烧 LLM）→ 超限放弃")
# 隔离：把 [4] 遗留的待处理消息标记处理（工作台接手语义），清空持等计数
from foxbot.session import t_get_pending_messages, t_mark_processed  # noqa: E402
leftover = [m["id"] for m in t_get_pending_messages(limit=50)["messages"]]
t_mark_processed(leftover)
brain._wx_holds.clear()
brain._wx_pending.clear()
brain._wx_screen_has = lambda name: False
cfg["brain"]["wechat_hold_max"] = 2  # 第1次失配持等，第2次超限放弃
config.save_config(cfg)
sim(channel="wechat", chat_type="private", chat_id="wxB", contact_id="wxB",
    display_name="微信乙", text="守卫持等测试消息")


def wx_marker_calls():
    return [r for r in LLM_LOG if "守卫持等测试消息" in r["body"]]


before = len(wx_marker_calls())
st = brain.brain_process_once()
check("守卫失配：回复持等，消息保留", not processed("守卫持等测试消息")
      and st.get("held", 0) >= 1 and len(wx_marker_calls()) == before + 1, (st, len(wx_marker_calls())))
st = brain.brain_process_once()
check("持等重试不重烧 LLM", len(wx_marker_calls()) == before + 1, len(wx_marker_calls()))
check("超限放弃：清计数并标记处理", processed("守卫持等测试消息"), st)

print("[6] 关联身份跨端接续：QQ 端上下文能看到微信端往来")
link = identity.t_link_accounts("request", channel="qq", contact_id="10001",
                                peer_channel="wechat", peer_contact_id="wxA")
identity.t_link_accounts("confirm", channel="wechat", contact_id="wxA", code=link["code"])
msg = {"id": 99999999, "channel": "qq", "chat_type": "private", "chat_id": "10001",
       "contact_id": "10001", "display_name": "阿明", "content": "换到QQ来找你啦"}
persona = brain._active_persona()
system, user = brain._build_context("qq", "private", "10001", [msg], persona)
check("微信端消息进入 QQ 端上下文", "（微信）" in user and "微信车道开启后的消息" in user,
      user[-300:])
check("平台提示随车道切换", "你正在QQ上与人聊天" in system, system[-120:])

print("[7] config 原子落盘：4 线程并发保存不损坏")
import glob as _glob  # noqa: E402
from foxbot.config import CONFIG_PATH as _CP, load_config as _lc, save_config as _sc  # noqa: E402


def _hammer():
    for i in range(40):
        c = _lc()
        c["brain"]["fail_streak"] = i % 5
        _sc(c)


hs = [threading.Thread(target=_hammer) for _ in range(4)]
[t.start() for t in hs]
[t.join() for t in hs]
try:
    _ok = isinstance(_lc(), dict)
except Exception:
    _ok = False
check("并发保存后 config 仍合法且无临时文件残留", _ok and not _glob.glob(_CP + ".tmp*"), None)

print("\nRESULT: PASS=%d FAIL=%d" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
