# glm-foxgirl · GLM狐狸娘（QQ/微信人格伴侣）

让 ZCode 作为"大脑"，以自定义人格在 QQ 和微信里与真人互动：被动回复（定时心跳全自动）+ 主动聊天、认识群里每个发言人、识别图片/表情包/视频（抽帧）、长期记忆（可语义向量检索）与关系画像、黑白名单权限、拟人化防风控、**人格库热切换**（后台 `/persona-switch` 或聊天内管理员指令）、表情包自动收集、戳一戳互动。

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
| `server/persona_mcp.py` | MCP 服务器（24 个工具：bot_command 指令、语义记忆、视频抽帧、poke_qq 等）+ NapCat 事件收集器。`--collector` 可独立 24/7 运行 |
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

1. 安装插件后 `/persona-bot` 看状态。
2. 说"接入QQ"或"接入微信"，按技能引导完成配置。
3. 说"帮我创建一个人格"，或用 `/persona-create` 问答式生成（也支持直接丢人设卡文件一键导入）；**GLM娘已预置为初始人设**；建多个人格组成人格库，`/persona-switch` 随时切换。
4. 配置管理员（`admin.qq`）后，在 QQ 里发"切换人格 XX"即可聊天内热切换。
5. 建一个每 15 分钟的工作区定时任务（提示词：执行 GLM狐狸娘 的消息心跳）实现全自动收发；或随时说"看看有没有新消息"。

## 迭代路线（参考开源项目）

- ~~v0.4 定时心跳全自动收发~~ 已完成
- ~~v0.5 语义向量记忆 / 视频抽帧识别 / 表情包自动收集 / 戳一戳 / 微信发表情包~~ 已完成
- v0.6 记忆遗忘曲线与情绪效价（参考 astrbot_plugin_memory_system）
- v0.7 SillyTavern 角色卡导入 + 心情状态层
- v0.8 发送队列与 NapCat 掉线重试、统计报表
- v1.0 Android 无障碍服务读微信精确发言人、WebUI 管理面板

## 风险声明

QQ（NapCat 协议端）与微信（ADB 自动化）均为非官方接入方式，存在账号风控/封禁可能。请使用专用小号，保持插件默认的拟人延迟与限频设置。
