# GLM狐狸娘 驾驶舱

这是 GLM狐狸娘 机器人的**专用工作区**：日常聊天、收发 QQ/微信消息、人格与记忆管理都在这里进行。机器人状态全部外置于 `~/.persona-bot/`（备份该目录即备份一切），对话可随时交接换页，不丢任何记忆。

## 开机自恢复（每次对话开始必做）

1. 调 persona-bot MCP 的 `bot_status` 快检，并按结果分支：
   - `setup_completed=false` → 本页是**配置页**：按 SessionStart 提示逐项完成配置（渠道就绪后重点核对 `admin.qq` 管理员名单与知情开启项），完成后执行 `/persona-finish-setup`，再引导用户新开对话作为聊天区。
   - `setup_completed=true` → 本页是**聊天区**：继续第 2 步。
2. 调 `session_note(action=list, limit=3)` 读取最近接力笔记，再调 `session_progress()` 复位轮数计数——按笔记接上之前的脉络（未完成事项、待跟进话题）。
3. 有待处理消息且用户已开启心跳/明确要求处理：按 persona-brain 技能的心跳循环处理。
4. 用两三句向用户报到：激活人格、待处理消息数、collector 是否在运行、上次接力留下的要点。

## 值机守则

- 消息心跳循环（取消息→黑白名单→回复必要性→人格化生成→拟人发送→记忆沉淀）、人格纪律、防风控、账号关联、反思与记忆图——一切按 **persona-brain 技能**执行，本文件不重复其内容。
- 管理权限硬约束：所有配置/功能修改先 `check_admin`；非管理员拒绝并 `report_guard`（超 10 次自动拉黑）。
- 心跳定时任务默认不存在：用户明确说"开启15分钟心跳"才创建（间隔 1~15 分钟任选），"关闭心跳"即删除，绝不代开。
- 每轮交互结束调 `session_progress()`；返回 `warn` 提醒用户接近上限，返回 `handover` 立即执行交接：总结本会话 → `session_note(action=save)` → 提醒新开对话页。用户说"交接"时手动走 `/persona-handover` 流程。
- collector 未运行时提醒用户：`python <glm-foxgirl插件目录>\server\persona_mcp.py --collector`（任一 ZCode 会话的 MCP 服务启动时也会自动拉起；端口被占则自动复用已有实例，v1.2 起单例化不重复绑定）。

## 快速索引

| 需求 | 入口 |
|---|---|
| 查状态 | `/persona-bot` 或 `bot_status` |
| 后台切换人格 | `/persona-switch <名称>` |
| 新建人格 | `/persona-create`（问答式 / 人设卡一键导入） |
| 聊天内管理指令 | 管理员发"切换人格 <名称> / 人格列表 / 机器人状态 / 交接" |
| 统计报表 | `get_stats` |
| 会话交接 | 说"交接" → `/persona-handover` |
| 追溯与查找 | `search_messages` / `export_transcript` / WebUI"记录"页 |
| 本地控制台 | `python server/persona_mcp.py --webui`（启动日志含带 token 的访问地址） |

## 页面分工

- **配置页**（安装后的第一次对话）：只做功能设置与开关（intro 速览里的事），不聊天。配置完成后执行 `/persona-finish-setup` 封存。
- **聊天区**（新开的对话）：日常使用。开机按"开机必做"恢复值机；每轮交互调用 `session_progress()`，达到阈值（默认 40 轮）触发**自动交接**：总结存档并提醒用户开新聊天区。
- 判别：`bot_status.setup_completed=false` 且用户想聊天 → 引导先完成配置再新开聊天区；`=true` → 本页即聊天区，正常服务。

## 路径速查

- 数据（人格/记忆/画像/记录/笔记）：`~/.persona-bot/`（备份即备份一切）
- 插件源码：本仓库 `glm-foxgirl/`（即 AGENTS.md 所在仓库的子目录）
- 测试：`python glm-foxgirl/tests/test_smoke.py`（当前应 147/147；未装 ffmpeg 时自动跳过视频抽帧项）
