# 会话交接文档 — 2026-10-30

> 本文档供下一个 ZCode 会话接续工作。读完即可无缝接手。

## 一、项目是什么

**GLM狐狸娘（glm-foxgirl）**：让 ZCode 作为"大脑"，以自定义人格在 QQ 和微信里与真人互动的完整系统。不是群聊玩具，是有独立人格、长期记忆、会反思、有情绪的"人"。

## 二、仓库与远端

| 项 | 值 |
|---|---|
| 本地 | `D:\z code shuju\chajianku\pebanchajian` |
| 远端 | `https://github.com/CDCPwg/GML-fox-girl`（公开） |
| 分支 | main（最新 `f690805` 之后若干提交） |
| 标签 | v1.0 / v1.2 / v1.3 / v1.4 / v1.5（均已推送远端） |
| 版本 | 1.5.0（config.py / plugin.json / marketplace.json 三处同步） |
| 测试 | 168 项端到端烟雾测试（`python glm-foxgirl\tests\test_smoke.py`，全部通过） |
| 工具 | 49 个 MCP 工具 |

命令行 git 用完整版：`D:\Git\cmd\git.exe`（2.56，GCM 凭据静默通过）。

## 三、架构一句话

ZCode 会话 = 大脑（persona-brain 技能驱动），`server/foxbot/` 包 = 18 个领域模块（纯 Python 标准库），NapCat = QQ 渠道，ADB = 微信渠道，`~/.persona-bot/` = 全部外部存储（备份即备份一切）。

## 四、运行模式（二选一/可组合）

| 模式 | 启动命令 | 用途 |
|---|---|---|
| MCP stdio | ZCode 插件自动启动 | 49 个工具供大脑调用 |
| 事件驱动看门 | `server/inbox_watch.py`（ZCode 后台任务） | 消息到达即唤醒，零模型成本 |
| 独立大脑 | `python server/persona_mcp.py --brain` | 秒级轮询 + LLM 人格回复 |
| 独立收集器 | `python server/persona_mcp.py --collector` | 24/7 接收 NapCat 上报 |
| 本地 WebUI | `python server/persona_mcp.py --webui --token <令牌>` | 可视化管理 |

**互斥**：事件驱动看门、独立大脑——二选一（定时心跳轮询功能已移除：看门与独立大脑足以覆盖其职责）。

## 五、当前状态与已知问题

- **无致命已知 bug**
- 原低危候选三项均已完成（有测试覆盖：LLM 失败计数持久化、WebUI 页面/鉴权/API、下载大小上限拦截）
- 用户实测区：`D:\zcode\xiangmuwenjainjia\GLM NIANG`（NapCat 3000 已就绪，chunked 修复已验证）
- 用户真实 NapCat 路径：`D:\bot\NapCat.Shell.Windows.OneKey\NapCat.52230.Shell`

## 六、用户关键决策（必须遵守）

1. **独立人格**：不做签到/词云/点歌等群聊玩具
2. **知情开启**：一切定时/主动调用模型的能力默认关闭
3. **配置页/聊天区分工**：首次对话只设置，聊天另起新页
4. **事件驱动取代心跳轮询**（用户自建 `_inbox_watch.py` 并要求正式化）
5. **人格内容锁定**：聊天渠道只可切换不可改内容
6. **不包含 M 分支**：v1.0 封包不含 mstars-fix 分支（M = M-stars，已 cherry-pick 保留署名）

## 七、下一步建议

1. **实测**：回到聊天区（`D:\zcode\xiangmuwenjainjia\GLM NIANG`），接上真实 NapCat，让狐狸娘开始积累记忆
2. **V1.3 候选**：语音消息、记忆图可视化、微信无障碍深度集成、多实例管理
3. 任何实测 bug：写成报告放实测区，或直接描述现象

## 八、关键路径速查

| 内容 | 路径 |
|---|---|
| 数据（人格/记忆/记录/笔记） | `~/.persona-bot/` |
| 插件源码 | `D:\z code shuju\chajianku\pebanchajian\glm-foxgirl\` |
| 测试 | `python glm-foxgirl\tests\test_smoke.py`（应 168/168） |
| 驾驶舱工作区 | `D:\z code shuju\chajianku\pebanchajian\workspace\` |
| Git | `D:\Git\cmd\git.exe`（完整版 2.56，GCM 静默） |
| 实测区 | `D:\zcode\xiangmuwenjainjia\GLM NIANG` |
