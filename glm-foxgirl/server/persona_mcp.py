#!/usr/bin/env python3
"""GLM狐狸娘 MCP 入口：QQ (NapCat/OneBot11) + WeChat (ADB) 人格伴侣。

Pure Python stdlib. Modes:
  python persona_mcp.py              MCP stdio server (也启动本地事件收集线程)
  python persona_mcp.py --collector  独立收集器守护进程（24/7 接收 NapCat 上报）
  python persona_mcp.py --webui [--port N]  本地 WebUI 控制台（仅 127.0.0.1）

存储在 PERSONA_BOT_DATA_DIR 或 ~/.persona-bot（persona.db、config.json、media/）。
实现按领域拆在 foxbot/ 包：config/db/embeddings/access/identity/memory/graph/
stickers/qq/wechat/persona/session/webui。
"""
import json
import sys
import time

from foxbot import config, db, qq, webui
from foxbot.access import (t_check_admin, t_report_guard)
from foxbot.graph import t_graph_add, t_graph_query
from foxbot.identity import t_link_accounts, t_manage_profile
from foxbot.memory import (t_memory_conflicts, t_prune_memory, t_query_memory,
                           t_reflect_due, t_reflect_material, t_store_memory)
from foxbot.persona import (t_bot_command, t_import_sillytavern, t_manage_mood,
                            t_manage_persona, t_manage_rules)
from foxbot.qq import (t_flush_outbox, t_poke_qq, t_qq_login_status, t_qq_relogin,
                       t_save_qq_credentials, t_send_qq_message, t_find_napcat, t_start_napcat)
from foxbot.session import (t_bot_status, t_check_access, t_configure_channel,
                            t_export_transcript, t_get_config, t_get_pending_messages,
                            t_get_stats, t_mark_processed, t_proactive_due,
                            t_search_messages, t_session_note, t_session_progress,
                            t_set_config, t_simulate_inbound)
from foxbot.security import t_recent_blocks
from foxbot.stickers import (t_download_media, t_extract_video_frames,
                             t_manage_stickers, t_pick_sticker)
from foxbot.wechat import (t_send_wechat_message, t_send_wechat_sticker,
                           t_wechat_read_notifications, t_wechat_read_screen,
                           t_wechat_screenshot)

TOOLS = {
    "bot_status": (t_bot_status, "查看机器人整体状态：数据目录、激活人格、待处理消息数、QQ/微信渠道配置、collector 是否在运行。任何会话开始时先调用它。", {"type": "object", "properties": {}}),
    "configure_channel": (t_configure_channel, "配置 QQ(NapCat) 或微信(ADB) 渠道：地址、token、adb 路径、设备序列号、collector 端口。", {"type": "object", "properties": {"channel": {"type": "string", "enum": ["qq", "wechat"]}, "enabled": {"type": "boolean"}, "napcat_base_url": {"type": "string"}, "napcat_token": {"type": "string"}, "adb_path": {"type": "string"}, "device_serial": {"type": "string"}, "collector_port": {"type": "number"}}, "required": ["channel"]}),
    "get_config": (t_get_config, "读取全部行为配置（token 脱敏）：humanize 防风控参数、proactive 主动聊天策略、collector 端口等。", {"type": "object", "properties": {}}),
    "set_config": (t_set_config, "设置任一配置项（点分路径）。例：set_config('proactive.enabled', true)、set_config('humanize.reply_delay_max_ms', 8000)。", {"type": "object", "properties": {"key": {"type": "string"}, "value": {}}, "required": ["key", "value"]}),
    "get_pending_messages": (t_get_pending_messages, "取所有未处理的上行消息（含发言人昵称/群名片、媒体URL、说话人关系画像）。这是大脑轮询的入口。", {"type": "object", "properties": {"channel": {"type": "string", "enum": ["qq", "wechat"]}, "chat_id": {"type": "string"}, "limit": {"type": "number", "default": 20}}}),
    "mark_processed": (t_mark_processed, "把已回复/决定忽略的消息标记为已处理，避免重复回复。", {"type": "object", "properties": {"message_ids": {"type": "array", "items": {"type": "number"}}}, "required": ["message_ids"]}),
    "simulate_inbound": (t_simulate_inbound, "注入一条假消息用于测试整个大脑链路（不需要真实 QQ/微信）。", {"type": "object", "properties": {"channel": {"type": "string", "enum": ["qq", "wechat"]}, "chat_type": {"type": "string", "enum": ["private", "group"]}, "chat_id": {"type": "string"}, "contact_id": {"type": "string"}, "display_name": {"type": "string"}, "text": {"type": "string"}, "media_url": {"type": "string"}}, "required": ["channel", "chat_type", "chat_id", "contact_id"]}),
    "check_access": (t_check_access, "黑白名单判定：回复前必须调用。黑名单命中或不在白名单时 allowed=false。", {"type": "object", "properties": {"channel": {"type": "string"}, "chat_type": {"type": "string", "enum": ["private", "group"]}, "chat_id": {"type": "string"}, "contact_id": {"type": "string"}}, "required": ["channel", "chat_type", "chat_id", "contact_id"]}),
    "send_qq_message": (t_send_qq_message, "通过 NapCat 发 QQ 私聊/群聊消息，支持 text 与本地 image_path。工具内部会先做黑白名单二次校验、随机拟人延迟、每会话限频；NapCat 不可达时自动入队待补发。", {"type": "object", "properties": {"chat_type": {"type": "string", "enum": ["private", "group"]}, "chat_id": {"type": "string"}, "text": {"type": "string"}, "image_path": {"type": "string"}, "contact_id": {"type": "string"}, "out_kind": {"type": "string", "enum": ["reply", "proactive"]}}, "required": ["chat_type", "chat_id"]}),
    "send_wechat_message": (t_send_wechat_message, "通过 ADB 向当前打开的微信聊天窗口发送文字（需要手机/模拟器停留在目标聊天界面；中文输入需 ADBKeyboard）。内置拟人延迟与限频。", {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}),
    "wechat_screenshot": (t_wechat_screenshot, "对连接的模拟器/真机截屏，返回 PNG 路径；用 Read 工具查看该图即可识别当前微信聊天内容（图片/表情包）。", {"type": "object", "properties": {}}),
    "download_media": (t_download_media, "下载 QQ 消息里的图片/表情包/视频到本地并返回路径，随后可用 Read 工具做视觉识别。", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}),
    "manage_persona": (t_manage_persona, "人格管理：list / create(name,prompt,style) / update / set_active / delete / export / import。prompt 是人格核心设定（性格、语气、口头禅、边界）。", {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "create", "update", "set_active", "delete", "export", "import"]}, "name": {"type": "string"}, "prompt": {"type": "string"}, "style": {"type": "string"}, "persona_id": {"type": "number"}, "file_path": {"type": "string"}}, "required": ["action"]}),
    "store_memory": (t_store_memory, "写入长期记忆。值得记：承诺、对方偏好、重要事件、关系变化。importance 1-5，emotion -3~3。事实类记忆先调 memory_conflicts 查矛盾：矛盾时传 supersedes=[旧id] 完成事实更新。", {"type": "object", "properties": {"content": {"type": "string"}, "channel": {"type": "string"}, "chat_id": {"type": "string"}, "contact_id": {"type": "string"}, "kind": {"type": "string"}, "importance": {"type": "number", "default": 3}, "emotion": {"type": "number", "default": 0}, "supersedes": {"type": "array", "items": {"type": "number"}}}, "required": ["content"]}),
    "query_memory": (t_query_memory, "按关键词/联系人/会话检索长期记忆（遗忘衰减评分优先；配置 embedding 后 semantic=true 语义排序；channel+contact_id 自动展开账号关联的身份组；已被取代的记忆默认不出现，include_superseded=true 可追溯）。", {"type": "object", "properties": {"keyword": {"type": "string"}, "contact_id": {"type": "string"}, "chat_id": {"type": "string"}, "channel": {"type": "string"}, "limit": {"type": "number", "default": 10}, "semantic": {"type": "boolean", "default": False}, "include_superseded": {"type": "boolean", "default": False}}}),
    "memory_conflicts": (t_memory_conflicts, "冲突消解：存事实前调用，返回与 content 相似度高的旧记忆候选。大脑语义判断：矛盾→store_memory(supersedes=[旧id])；只是相关→正常存。", {"type": "object", "properties": {"content": {"type": "string"}, "contact_id": {"type": "string"}, "channel": {"type": "string"}, "limit": {"type": "number", "default": 5}}, "required": ["content"]}),
    "reflect_due": (t_reflect_due, "反思到期检查：返回新增记忆足够多（默认8条）或长期未反思的联系人。对每个对象执行反思闭环（reflect_material → 合成洞察 → store_memory(kind='reflection') + manage_profile）。", {"type": "object", "properties": {"limit": {"type": "number", "default": 10}}}),
    "reflect_material": (t_reflect_material, "反思素材：某联系人身份组的近期记忆+画像+历史反思。大脑据此合成 2-4 条高层洞察（不复述原句），写入 reflection 记忆并更新画像印象。", {"type": "object", "properties": {"channel": {"type": "string"}, "contact_id": {"type": "string"}, "limit": {"type": "number", "default": 40}}, "required": ["channel", "contact_id"]}),
    "prune_memory": (t_prune_memory, "遗忘曲线清理：删除遗忘得分低于 keep_score 且重要级<=3 的旧记忆（重要级>=4 永不清理）。先 dry_run=true 预览。", {"type": "object", "properties": {"keep_score": {"type": "number", "default": 1.0}, "dry_run": {"type": "boolean", "default": True}, "limit": {"type": "number", "default": 500}}}),
    "manage_profile": (t_manage_profile, "关系画像：get / upsert(channel,contact_id,impression,style_notes,affinity_delta) / list。每个联系人独立印象档案，affinity -100~100。", {"type": "object", "properties": {"action": {"type": "string", "enum": ["get", "upsert", "list"]}, "channel": {"type": "string"}, "contact_id": {"type": "string"}, "display_name": {"type": "string"}, "impression": {"type": "string"}, "style_notes": {"type": "string"}, "affinity_delta": {"type": "number"}}, "required": ["action"]}),
    "manage_rules": (t_manage_rules, "黑白名单管理：add/remove/list。list_type=blacklist|whitelist，scope=contact|group|global，channel=qq|wechat|*。", {"type": "object", "properties": {"action": {"type": "string", "enum": ["add", "remove", "list"]}, "list_type": {"type": "string", "enum": ["blacklist", "whitelist"]}, "scope": {"type": "string", "enum": ["contact", "group", "global"]}, "channel": {"type": "string"}, "target_id": {"type": "string"}, "note": {"type": "string"}}, "required": ["action"]}),
    "manage_stickers": (t_manage_stickers, "表情包库：add(本地图片路径,emotion,tags,source_url) / list / delete。emotion 建议用 happy/sad/angry/love/awkward/food 等标签；带 source_url 时自动按 URL 去重。", {"type": "object", "properties": {"action": {"type": "string", "enum": ["add", "list", "delete"]}, "file_path": {"type": "string"}, "emotion": {"type": "string"}, "tags": {"type": "string"}, "source": {"type": "string"}, "source_url": {"type": "string"}, "sticker_id": {"type": "number"}}, "required": ["action"]}),
    "pick_sticker": (t_pick_sticker, "按情绪挑选表情包返回本地路径（自动避免连用同一张），配合 send_qq_message 的 image_path 发送。", {"type": "object", "properties": {"emotion": {"type": "string"}}}),
    "proactive_due": (t_proactive_due, "判断现在是否允许主动发起聊天（enabled、安静时段、每日上限）。主动发言前必须调用。", {"type": "object", "properties": {}}),
    "bot_command": (t_bot_command, "管理指令入口（管理员在 QQ/微信聊天里操作：切换人格/人格列表/机器人状态/交接远程换页）。对每条入站消息先调用：handled=true 时发送 reply 并结束该条处理；非管理员尝试自动计入管理守卫（超10次拉黑）；handled=false 继续正常人格流程。", {"type": "object", "properties": {"text": {"type": "string"}, "channel": {"type": "string"}, "contact_id": {"type": "string"}, "chat_id": {"type": "string"}}, "required": ["text"]}),
    "poke_qq": (t_poke_qq, "在 QQ 群里戳一戳某人（拟人化互动或回应被戳）。内置黑白名单与限频。", {"type": "object", "properties": {"group_id": {"type": "string"}, "user_id": {"type": "string"}}, "required": ["group_id", "user_id"]}),
    "send_wechat_sticker": (t_send_wechat_sticker, "微信发送本地表情包图片（需先校准 4 组点击坐标，见报错提示与 wechat-adb-setup 技能）。内置拟人延迟与限频。", {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}),
    "extract_video_frames": (t_extract_video_frames, "视频抽帧：对已下载到本地的视频均匀抽取若干帧返回图片路径，再用 Read 逐帧理解内容。需要 ffmpeg（winget install Gyan.FFmpeg）。", {"type": "object", "properties": {"video_path": {"type": "string"}, "frames": {"type": "number", "default": 4}}, "required": ["video_path"]}),
    "import_sillytavern": (t_import_sillytavern, "导入 SillyTavern 角色卡为人格：.json 或带 tEXt chara 数据的 .png。重名自动加后缀。", {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}),
    "flush_outbox": (t_flush_outbox, "补发队列：NapCat 掉线期间排队的消息在恢复后重发，超过 max_age_min 分钟的过期丢弃。每轮心跳结束时应调用一次。", {"type": "object", "properties": {"max_age_min": {"type": "number", "default": 30}}}),
    "manage_mood": (t_manage_mood, "心情状态层：get/set 当前情绪温度（-2~2，set 需 reason）。影响说话温度与主动意愿，随互动由大脑更新。", {"type": "object", "properties": {"action": {"type": "string", "enum": ["get", "set"]}, "mood": {"type": "number"}, "reason": {"type": "string"}}}),
    "get_stats": (t_get_stats, "统计报表：7天收发/回复率/主动次数、今日入站、队列、记忆表情包数量、好感TOP、表情包使用TOP。", {"type": "object", "properties": {}}),
    "graph_add": (t_graph_add, "记忆图写入三元组：谁(subject) -关系-> 什么(target)。关系常用：喜欢/讨厌/养了/参与/提到/约定/擅长/在玩。重复添加同一三元组只加权重。", {"type": "object", "properties": {"subject": {"type": "string"}, "relation": {"type": "string"}, "target": {"type": "string"}, "subject_kind": {"type": "string"}, "target_kind": {"type": "string"}, "note": {"type": "string"}}, "required": ["subject", "relation", "target"]}),
    "graph_query": (t_graph_query, "记忆图查询：entity=实体名 从该实体 BFS 扩散 depth 层（1-3），返回关联的人/事/偏好；entity 留空返回按关联度排序的总览。回复前对消息里出现的具体对象调用它可带出联想。", {"type": "object", "properties": {"entity": {"type": "string"}, "depth": {"type": "number", "default": 1}, "limit": {"type": "number", "default": 60}}}),
    "wechat_read_screen": (t_wechat_read_screen, "无障碍层级读屏：对连接的模拟器/真机执行 uiautomator dump，返回当前屏幕的精确文字节点（无 OCR 误差，群昵称通常单独成节点）。传 xml_text 可离线解析。", {"type": "object", "properties": {"xml_text": {"type": "string"}}}),
    "wechat_read_notifications": (t_wechat_read_notifications, "从微信通知栏补捞后台错过的消息（私聊=昵称+内容，群聊=群名+昵称+内容）。需要微信通知权限开启。传 raw_text 可离线解析。", {"type": "object", "properties": {"raw_text": {"type": "string"}}}),
    "session_note": (t_session_note, "会话接力笔记：换新对话前 save 一份本会话工作总结，新对话开机 list/get 最近几条即可接上脉络（记忆已外置，换页无损）。", {"type": "object", "properties": {"action": {"type": "string", "enum": ["save", "get", "list", "delete"]}, "note_id": {"type": "number"}, "title": {"type": "string"}, "content": {"type": "string"}, "limit": {"type": "number", "default": 5}}}),
    "search_messages": (t_search_messages, "追溯查找全部收发记录：关键词/渠道/联系人/会话/时间范围，记录永久保留。", {"type": "object", "properties": {"keyword": {"type": "string"}, "channel": {"type": "string"}, "contact_id": {"type": "string"}, "chat_id": {"type": "string"}, "since": {"type": "string"}, "until": {"type": "string"}, "limit": {"type": "number", "default": 50}}}),
    "export_transcript": (t_export_transcript, "把一段时间的收发记录导出为 Markdown 存档（存 ~/.persona-bot/transcripts/），返回文件路径。", {"type": "object", "properties": {"since": {"type": "string"}, "until": {"type": "string"}, "channel": {"type": "string"}, "chat_id": {"type": "string"}}}),
    "session_progress": (t_session_progress, "会话进度标记：每轮用户交互/心跳后调用；按轮数折算上下文占用估算百分比，≥warn_pct(85%) 提醒、≥handover_pct(95%) 返回 auto_handover=true 触发自动交接。新会话由 SessionStart hook 自动归零。", {"type": "object", "properties": {"note": {"type": "string"}}}),
    "check_admin": (t_check_admin, "管理员判定：该渠道联系人是否在 admin.qq/admin.wechat 名单。所有配置/功能修改请求前必须调用。", {"type": "object", "properties": {"channel": {"type": "string"}, "contact_id": {"type": "string"}}, "required": ["channel", "contact_id"]}),
    "report_guard": (t_report_guard, "管理守卫：记录一次非管理员的配置/功能修改尝试（拒绝执行后调用）；单账号超过 10 次自动加入黑名单，之后其消息不再回复。bot_command 的指令类尝试已自动计数。", {"type": "object", "properties": {"channel": {"type": "string"}, "contact_id": {"type": "string"}, "action": {"type": "string"}}, "required": ["channel", "contact_id"]}),
    "link_accounts": (t_link_accounts, "QQ/WX 账号关联器：action=request 发起关联（生成6位确认码，私下转达对方）；对方在自己平台用本人账号发'关联确认 <码>'→ action=confirm；画像自动调和（单侧继承/相似合并/不相似返回 conflict 由该账号选 a/b/merge 后 action=resolve）；记忆按身份组互联检索。action=ids 展开身份组（查画像/记忆前先调）；status 查看；unlink 解除。", {"type": "object", "properties": {"action": {"type": "string", "enum": ["request", "confirm", "resolve", "status", "ids", "unlink"]}, "channel": {"type": "string"}, "contact_id": {"type": "string"}, "peer_channel": {"type": "string"}, "peer_contact_id": {"type": "string"}, "code": {"type": "string"}, "request_id": {"type": "number"}, "choice": {"type": "string", "enum": ["a", "b", "merge"]}}, "required": ["action"]}),
    "save_qq_credentials": (t_save_qq_credentials, "托管 QQ 账号密码（可选的备用登录方案）：密码经 Windows DPAPI 加密后落盘（本机本用户才可解密），接口永不回显明文。掉线时自动尝试快速登录；快速登录失败仍需手动验证。", {"type": "object", "properties": {"uin": {"type": "string"}, "password": {"type": "string"}}, "required": ["uin", "password"]}),
    "qq_login_status": (t_qq_login_status, "QQ 登录状态探测：OneBot get_login_info 优先；不可达时用 WebUI（qq.webui_url/token）区分 NapCat 宕机与掉线。状态：logged_in/offline/napcat_down/unknown。", {"type": "object", "properties": {}}),
    "qq_relogin": (t_qq_relogin, "备用登录：尝试 WebUI 快速登录恢复在线；失败返回 need_manual 与提醒话术（账密已加密托管但不回显）。心跳检测到掉线时调用。", {"type": "object", "properties": {}}),
    "recent_blocks": (t_recent_blocks, "内容防火墙拦截记录：被拦截的恶意内容仅元数据（不回传攻击原文，防二次注入）+ 守卫计数。管理员审查用。", {"type": "object", "properties": {"limit": {"type": "number", "default": 20}}}),
    "find_napcat": (t_find_napcat, "查找 NapCat 框架目录（含 NapCatWinBootMain.exe）：手动路径 → 运行中的 QQ 进程 → 注册表 → 常见根目录限时扫描。roots 参数可覆盖扫描根（调试用）。", {"type": "object", "properties": {"roots": {"type": "array", "items": {"type": "string"}}}}),
    "start_napcat": (t_start_napcat, "查找并启动 NapCat（NapCatWinBootMain.exe <QQ号>，QQ 号自动取已托管凭证），启动后轮询 OneBot 确认上线。root 参数可指定框架目录（调试用）。", {"type": "object", "properties": {"uin": {"type": "string"}, "root": {"type": "string"}, "wait_seconds": {"type": "number", "default": 60}}}),
}

HANDLERS = {name: fn for name, (fn, _, _) in TOOLS.items()}


def handle_call(method, params):
    if method == "initialize":
        return {"protocolVersion": (params or {}).get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "glm-foxgirl", "version": config.VERSION}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": s} for n, (_, d, s) in TOOLS.items()]}
    if method == "tools/call":
        name = (params or {}).get("name")
        args = (params or {}).get("arguments") or {}
        handler = HANDLERS.get(name)
        if not handler:
            return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
        try:
            data = handler(**args)
        except TypeError as e:
            return {"content": [{"type": "text", "text": json.dumps({"error": f"参数错误: {e}"}, ensure_ascii=False)}],
                    "isError": True}
        except Exception as e:
            return {"content": [{"type": "text", "text": json.dumps({"error": str(e)}, ensure_ascii=False)}],
                    "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
    raise KeyError(method)


def main_mcp():
    db.init_db()
    qq.start_collector_thread()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        mid = msg.get("id")
        if mid is None:
            continue
        try:
            result = handle_call(msg.get("method", ""), msg.get("params") or {})
        except KeyError:
            out = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
        else:
            out = {"jsonrpc": "2.0", "id": mid, "result": result}
        sys.stdout.write(json.dumps(out, ensure_ascii=True) + "\n")
        sys.stdout.flush()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    db.init_db()
    if "--webui" in sys.argv:
        port = 8765
        if "--port" in sys.argv:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        token = None
        if "--token" in sys.argv:
            token = sys.argv[sys.argv.index("--token") + 1]
        webui.main_webui(port, token)
    elif "--brain" in sys.argv or "--brain-once" in sys.argv:
        from foxbot.brain import main_brain
        main_brain(once="--brain-once" in sys.argv)
    elif "--collector" in sys.argv:
        # 独立收集器：HTTP 接收 + 看门狗 + 告警发送线程全套启动（与 MCP 模式同一入口）
        qq.start_collector_thread()
        print("[glm-foxgirl] collector daemon running (Ctrl+C 退出)", file=sys.stderr)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        main_mcp()


if __name__ == "__main__":
    main()
