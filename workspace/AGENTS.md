# GLM狐狸娘 驾驶舱

这是 GLM狐狸娘 机器人的**专用工作区**：与机器人相关的对话（看消息、调人格、查记录、配置渠道）都在这里进行，避免和其他任务混在一起。

## 开机必做（每次对话开始）

1. 调 persona-bot MCP 的 `bot_status` 快检：collector 是否在跑、待处理消息数、激活人格、心情。
2. 调 `session_note(action=list, limit=3)` 读取最近的接力笔记——接上上次工作的脉络（用户说"恢复值机"或直接提需求时都要先做这一步）。
3. 若有待处理消息且用户已开启心跳/明确要求处理：按 persona-brain 技能的心跳循环处理。
4. 用一两句话向用户报到：当前人格、待处理数、上次接力留下的要点。

## 本工作区约定

- 会话较长或用户要开新页面时，执行 `/persona-handover` 做接力存档，再让用户开新对话；新对话按"开机必做"恢复。
- 追溯与查找：`search_messages`（关键词/联系人/时间范围）、`export_transcript`（导出 Markdown 存档）、WebUI 的"记录"页（`python server/persona_mcp.py --webui`）。
- 心跳、主动聊天、语义记忆**默认关闭**——必须用户知情后开启，绝不代开。
- 渠道接入引导：QQ 走 qq-napcat-setup 技能，微信走 wechat-adb-setup 技能。

## 页面分工

- **配置页**（安装后的第一次对话）：只做功能设置与开关（intro 速览里的事），不聊天。配置完成后执行 `/persona-finish-setup` 封存。
- **聊天区**（新开的对话）：日常使用。开机按"开机必做"恢复值机；每轮交互调用 `session_progress()`，达到阈值（默认 40 轮）触发**自动交接**：总结存档并提醒用户开新聊天区。
- 判别：`bot_status.setup_completed=false` 且用户想聊天 → 引导先完成配置再新开聊天区；`=true` → 本页即聊天区，正常服务。

## 路径速查

- 数据（人格/记忆/画像/记录/笔记）：`~/.persona-bot/`（备份即备份一切）
- 插件源码：`D:\z code shuju\chajianku\pebanchajian\glm-foxgirl`
- 测试：`python glm-foxgirl\tests\test_smoke.py`（当前应 125/125）
