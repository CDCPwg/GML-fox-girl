# glm-foxgirl · GLM狐狸娘（QQ/微信人格伴侣）

让 ZCode 作为"大脑"，以自定义人格在 QQ 和微信里与真人互动：**配置页/聊天区分工**（首次对话只做设置，聊天另起新页）、**会话接力**（换页记忆不丢、接力笔记可追溯）、记录检索与导出、被动回复（定时心跳，默认关闭知情开启）+ 主动聊天、无障碍读屏精确识别每个发言人（通知补捞）、识别图片/表情包/视频（抽帧）、长期记忆（语义检索 + 遗忘曲线 + 情绪效价 + 记忆图）与关系画像、心情状态层、黑白名单权限、拟人化防风控（含 ADB 轨迹随机化）、**人格库热切换**（后台 `/persona-switch` 或聊天内管理员指令）、SillyTavern 角色卡导入、表情包自动收集、戳一戳、发送队列掉线补发、本地 WebUI 控制台。

## 架构

```
                 ┌──────────────────────────── ZCode（大脑）────────────────────────────┐
                 │  persona-brain 技能：回复必要性 → 人格生成 → 拟人发送 → 记忆沉淀        │
                 └───────────────▲──────────────────────────────────┬───────────────────┘
                                 │ MCP 工具                          │
                   persona_mcp.py（纯 Python 标准库，MCP 服务器 + 事件收集器）
                   存储：~/.persona-bot/  persona.db（SQLite）+ config.json + media/
                     │                                    │
        QQ：NapCat(OneBot11)                     微信：ADB（模拟器/USB 真机）
        HTTP 上报 → collector → SQLite            截图识图 + ADBKeyboard 发送
```

## 组件

| 路径 | 作用 |
|---|---|
| `server/persona_mcp.py` | MCP 服务器（36 个工具）+ NapCat 事件收集器 + `--webui` 本地管理面板 + `--collector` 独立 24/7 运行 |
| `commands/persona-handover` | `/persona-handover` 会话接力：总结存档后安全换新对话页（记忆外置无损） |
| `commands/persona-finish-setup` | `/persona-finish-setup` 封存配置页，引导新开聊天区 |
| `hooks/hooks.json` + `scripts/first_run_intro.py` | 首次对话功能提示（列出可修改功能与知情开启项，只出现一次） |
| `skills/persona-brain` | 大脑运作手册：心跳循环、回复必要性、防风控纪律、记忆规范、人格库指令 |
| `skills/qq-napcat-setup` | QQ 接入引导 |
| `skills/wechat-adb-setup` | 微信（模拟器/USB）接入引导 |
| `commands/persona-bot` | `/persona-bot` 状态总览 |
| `commands/persona-switch` | `/persona-switch` 后台人格热切换 |
| `commands/persona-create` | `/persona-create` 问答式生成人设 / 人设卡一键导入 |
| `assets/personas/` + `scripts/` | GLM娘初始人设及其安装器（写入人格库并自动激活） |

## 数据与备份

全部外部存储在 `~/.persona-bot/`（可用环境变量 `PERSONA_BOT_DATA_DIR` 改）：`persona.db`（人格/记忆/画像/黑白名单/表情包/消息日志）、`config.json`（渠道与行为配置）、`media/`。**备份这个文件夹即备份一切。**

## 快速开始

1. 安装后的第一次对话是**配置页**：自动列出全部可修改功能（SessionStart 速览），在这里完成渠道接入（"接入QQ/接入微信"）、人格（GLM娘已预置）、管理员与知情开启项（心跳/主动聊天/语义记忆）。
2. 配好后说"**完成配置**"（/persona-finish-setup）：校验关键项并封存配置页。
3. **新开一个对话作为聊天区**：开机自动恢复值机（接力笔记；把仓库 workspace/ 设为工作区、AGENTS.md 生效效果最佳）。日常聊天、看消息都在聊天区。
4. 聊天区对话太长说"交接"（/persona-handover）换新页——记忆外置无损；记录随时 `search_messages` / `export_transcript` / WebUI"记录"页追溯。
5. 消息心跳（定时自动收发）**默认关闭**：需要时在聊天区说"开启15分钟心跳"，"关闭心跳"即停。

## 迭代路线（参考开源项目）

- ~~v0.4 定时心跳~~ / ~~v0.5 语义记忆·视频·表情包收集·戳一戳·微信表情包~~ 已完成
- ~~v0.6 记忆遗忘曲线与情绪效价 / SillyTavern 角色卡导入 / 发送队列与掉线重试 / 首次对话功能提示~~ 已完成
- ~~v0.7 心情状态层 / 统计报表 / 本地 WebUI（--webui，仅本机）/ ADB 轨迹随机化 / 联网指引~~ 已完成
- ~~v0.8 记忆图（人物-事件-偏好关联，事实层）~~ / ~~v0.9 无障碍读屏+通知补捞精确发言人~~ 已完成
- 后续候选（按需再排期）：记忆图可视化、语音消息、多实例管理、人格云同步

## 风险声明

QQ（NapCat 协议端）与微信（ADB 自动化）均为非官方接入方式，存在账号风控/封禁可能。请使用专用小号，保持插件默认的拟人延迟与限频设置。
