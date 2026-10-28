---
name: qq-napcat-setup
description: 引导用户安装配置 NapCat，把 QQ 接入 persona-bot（OneBot 11 HTTP + HTTP 上报）。当用户要接入 QQ、配置 NapCat、QQ 机器人收不到消息或发不出消息时使用。
---

# QQ 渠道接入（NapCat / OneBot 11）

NapCat 是基于 NTQQ 的协议端，在电脑上运行、无需手机或模拟器，把 QQ 收发转成标准 HTTP 接口。**强烈建议使用专用 QQ 小号**：第三方协议端有封号风险。

## 步骤

1. **安装 NapCat**（Windows）：从 NapCat 官方文档 https://napneko.github.io/guide/boot-shell 获取 NapCat.Shell 一键启动包，解压到任意目录。首次启动按提示安装/启动 QQ 并**扫码登录小号**。
2. **配置网络**（NapCat WebUI，默认 http://127.0.0.1:6099 ）：
   - 新增 **HTTP 服务器**：端口 `3000`（插件调它发消息）。可设 token。
   - 新增 **HTTP 上报（POST）**：URL 填 `http://127.0.0.1:3001/onebot/event`（NapCat 推消息给插件）。token 与上面一致（可选）。
   - 消息格式选 **array**。
3. **告诉插件**（在 ZCode 里说或直接调用工具）：
   `configure_channel(channel="qq", enabled=true, napcat_base_url="http://127.0.0.1:3000", napcat_token="<如果设了>")`
4. **验证**：
   - `bot_status` → `collector.running` 必须为 `true`（false 则让用户运行 `python <插件目录>/server/persona_mcp.py --collector` 或重启 MCP）。
   - 用另一个 QQ 给小号发一条私聊 → `get_pending_messages` 应出现该消息（含发言人名片/昵称、群名片）。
   - 回一条测试 → 对方 QQ 收到即链路通。

## 常见故障

| 现象 | 处理 |
|---|---|
| 收不到消息 | NapCat 上报 URL/token 是否与 `get_config` 里 collector 一致；`bot_status` 看 collector.running |
| 发送报错连接失败 | NapCat HTTP 服务器端口 3000 是否启用、登录是否在线 |
| 发图失败 | image_path 必须是本机存在的文件（表情包库里的都是本地路径，直接可用） |
| 想换端口 | `configure_channel(collector_port=...)` 并同步改 NapCat 上报 URL；重启 MCP |

## 掉线自动恢复与登录托管（v0.15+）

NapCat 重启可能丢失登录态。三层恢复策略，建议全配：

1. **autoLoginAccount**：编辑 NapCat 的 `webui.json`，设 `autoLoginAccount: "<机器人QQ号>"`，重启后自动快速登录。OneKey 启动脚本里 `NapCatWinBootMain.exe <QQ号>` 的参数必须是**真实QQ号**（占位符 10086 会导致卡扫码页）。
2. **WebUI 自动快速登录**：`set_config("qq.webui_url", "http://127.0.0.1:6099")` 与 `set_config("qq.webui_token", "<WebUI密码>")`——掉线时 `qq_relogin` 自动尝试快速登录。注意 WebUI 的 Bearer 凭证必须**登录后立即使用**，陈旧凭证一律被拒（工具内部已按此处理）。
3. **账号密码托管（可选）**：说"托管QQ登录凭证 <QQ号> <密码>"——密码经 Windows DPAPI 加密落盘（本机本用户才可解密，永不回显）。NTQQ 不支持远程密码登录，托管用于快速登录失败时的提醒上下文与本地保管；手动登录时在 NapCat 窗口输入。
4. 心跳自动探测：`qq_login_status` → offline 就 `qq_relogin`；仍失败会明确提醒用户。

注意：NapCat 的 HTTP 上报使用 chunked 编码（无 Content-Length）——collector 已兼容；上报配置**只在 NapCat 进程启动时读取**，改完要重启 NapCat。自测时自发消息是 `message_sent` 类型，collector 只收 `post_type=="message"`。

## 风险提示（要主动告知用户）

- 协议端非官方，存在封号可能，建议小号 + 不要高频群发。
- 插件已内置拟人延迟与每小时限频（`get_config` → `humanize`），不要调得激进。
