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

GUIDE_VERSION = 7

GUIDE = """【GLM狐狸娘 · 配置页功能指南】

本页是【配置页】：只用来完成初始设置与功能开关，不在这里聊天。
完成下面第一~三步后，说"完成配置"封存本页，然后【新开一个对话】作为日常聊天区——那边开机自动恢复值机（读接力笔记+待处理消息），记忆已外置，换页无损。

━━ 第一步 · 接入渠道（至少一个）━━
- QQ：说"接入QQ"——引导安装 NapCat（电脑上运行、无需手机），扫码登录专用小号
- 微信：说"接入微信"——引导 ADB 连接模拟器或 USB 真机，安装中文输入法
- 随时 /persona-bot 查看渠道状态与 collector 是否在运行

━━ 第二步 · 人格库 ━━
- 已预置"GLM娘"为初始人设；说"切换人格 XX"热切换，聊天里管理员也能切
- 新建：/persona-create 问答式生成，或直接丢人设卡 / SillyTavern 角色卡文件（.json / .png）一键导入
- 备份分享：人格可导出/导入 JSON（存 ~/.persona-bot/personas/）

━━ 第三步 · 管理员与安全 ━━
- 管理员：说"把我的QQ号 xxx 设为管理员"（admin.qq / admin.wechat）——聊天内指令与一切配置修改仅限名单账号
- 管理守卫：非管理员试图修改会被拒绝并警告，单账号超过 10 次自动拉黑、不再回复
- 黑白名单：说"把 xx 拉黑"或"只允许 xx 聊"（联系人/群/全局三种粒度）
- NapCat 掉线自动恢复（可选·强烈建议）：提供机器人小号的账号+密码——密码经 Windows DPAPI 加密托管（本机本用户才可解密，任何接口不回显明文）。掉线时自动尝试快速登录恢复；不提交账密也可以，掉线时会明确提醒你重新登录。说"托管QQ登录凭证 <QQ号> <密码>"即可

━━ 账号关联（可选）━━
- 同一个人的 QQ 和微信可关联成同一身份：画像自动继承/合并（冲突时本人选保留哪版或融合），记忆跨平台互联互通
- 说"关联微信 <微信号>"发起 → 把 6 位确认码私下转给对方 → 对方在微信发"关联确认 <码>"（15 分钟内有效）

━━【默认关闭 · 需你知情开启】（说一句即开，绝不代开）━━
- 15分钟消息心跳：定时自动收发消息 → 说"开启15分钟心跳"，关闭说"关闭心跳"
- 主动聊天：机器人自己找人搭话 → proactive.enabled true（有每日上限与安静时段保护）
- 语义向量记忆：调用 embedding API 处理记忆 → 提供地址与密钥即可配好
- 自动重开新页：自动交接后自动建恢复页定时任务 → limits.auto_reopen true

━━ 日常可调（在聊天区说即可）━━
- 防风控：回复延迟 humanize.reply_delay_min_ms/max_ms、每小时限频 max_per_chat_per_hour、安静时段 proactive.quiet_hours
- 心情温度：说"你现在心情低落一点"（-2~2，影响语气与主动意愿）
- 表情包自动收集（默认开，按URL去重）：stickers.auto_collect false 关闭
- 反思节奏：新记忆满 memory.reflect_min_new（默认8）条自动合成洞察；memory.reflect_days 控制最短间隔
- 记忆清理：prune_memory（遗忘曲线，重要级≥4 的承诺类永不清理）；memory.half_life_days 调衰减速度
- 自动交接：上下文约 85%（warn_pct）轻提醒、约 95%（handover_pct）自动换页；也可随时手动说"交接"
- 记录追溯：search_messages 随时查、export_transcript 导出 Markdown、WebUI 控制台（python server/persona_mcp.py --webui，仅本机）

━━ 数据与备份 ━━
- 人格、记忆、画像、黑白名单、表情包、接力笔记全部在 ~/.persona-bot/ ——备份该目录即备份一切
- 重大功能更新会再提示一次，平时不重复；配置过程中有任何疑问直接问。"""


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
