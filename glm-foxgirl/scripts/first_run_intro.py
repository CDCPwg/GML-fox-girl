#!/usr/bin/env python3
"""首次会话功能提示（SessionStart hook）：列出所有可由用户修改的功能与修改方法。

只在第一次输出（之后 config.json 里 intro.shown=true 静默退出）。
会定时/主动调用模型的默认关闭项（心跳、主动聊天、语义记忆）必须由用户知情开启，绝不代开。
"""
import json
import os
import sys
from datetime import datetime

DATA = os.environ.get("PERSONA_BOT_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".persona-bot")
CFG = os.path.join(DATA, "config.json")

GUIDE_VERSION = 3

GUIDE = """【GLM狐狸娘 · 功能速览】本页是【配置页】——只用来完成初始设置与功能开关，不在这里聊天。配好后说"完成配置"，然后【新开一个对话】作为日常聊天区（那边开机自动恢复值机）。

以下是你可以修改的功能与修改方法（直接对 ZCode 说即可）：

1. 人格库：/persona-switch <名称> 热切换；/persona-create 问答式生成人设，或直接丢人设卡/SillyTavern角色卡文件一键导入；QQ里管理员发"切换人格 <名称> / 人格列表"。
2. 管理员名单：说"把我的QQ号 xxx 设为管理员"（写入 admin.qq / admin.wechat）。
3. 防风控参数：回复延迟 humanize.reply_delay_min_ms / humanize.reply_delay_max_ms、每小时限频 humanize.max_per_chat_per_hour、安静时段 proactive.quiet_hours。
4. 【默认关闭，需你知情开启】15分钟消息心跳：会定时自动调用模型收发消息。想开就说"开启15分钟心跳"（可指定其他间隔）；关闭说"关闭心跳"。
5. 【默认关闭，需你知情开启】主动聊天：机器人自己找人搭话（有每日上限与安静时段保护）。开启：set_config proactive.enabled true。
6. 【默认关闭，需你知情开启】语义向量记忆：会调用 embedding API 处理记忆。提供地址与密钥后我帮你配 memory.embed_base_url / memory.embed_api_key。
7. 表情包自动收集（默认开，处理消息时顺带入库、按URL去重）：set_config stickers.auto_collect false 关闭。
8. 黑白名单：说"把 xx 拉黑"或"只允许 xx 聊"，支持联系人/群/全局粒度。
9. 心情状态：说"你现在心情低落一点"即可调整说话温度（manage_mood）。
10. 会话接力与【自动交接】：聊天区对话过长会自动交接——每轮交互自动计数，达到阈值（默认 40 轮，limits.auto_handover_turns 可改；limits.auto_handover_enabled 可关）就自动总结存档并提醒你开新对话页（开新页这一步需你手动点，插件无法代开）；也可随时手动说"交接"（/persona-handover）。新对话说"恢复值机"接上；查记录 search_messages / export_transcript；推荐专用驾驶舱工作区 workspace/（AGENTS.md 开机自动恢复值机）。
11. 本地控制台：python server/persona_mcp.py --webui（仅本机）可视化管理人格/记忆/黑白名单/查记录。

人格、记忆、关系画像、黑白名单、表情包、接力笔记全部存在 ~/.persona-bot/，备份该目录即备份一切。
（重大功能更新会再提示一次，平时不重复。）"""


def main():
    try:
        os.makedirs(DATA, exist_ok=True)
        cfg = {}
        if os.path.exists(CFG):
            try:
                with open(CFG, encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
        intro = cfg.get("intro")
        stale = not (isinstance(intro, dict) and intro.get("shown")
                     and int(intro.get("version", 1)) >= GUIDE_VERSION)
        if stale:
            print(GUIDE)
        cfg["intro"] = {"shown": True, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "version": GUIDE_VERSION}
        cfg["session"] = {"started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "turns": 0}
        with open(CFG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        return


if __name__ == "__main__":
    main()
