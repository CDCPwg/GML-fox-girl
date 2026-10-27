# pebanchajian · GLM狐狸娘（开发主仓库）

本仓库是插件 **glm-foxgirl**（GLM狐狸娘，v0.1.0 起）的开发与迭代主仓库，同时充当 ZCode 本地测试市场的**市场根目录**。

## 结构

```text
marketplace.json          ZCode 本地测试市场清单（市场根目录 = 本仓库根目录）
glm-foxgirl/              插件源码：GLM狐狸娘（结构见其 README.md）
server/persona_mcp.py     核心服务：MCP 服务器（37 工具）+ 收集器 + --webui 控制台
hooks/                    首次对话功能提示（SessionStart）
skills/                   persona-brain 大脑手册 / qq-napcat-setup / wechat-adb-setup
commands/                 /persona-bot、/persona-switch（热切换）、/persona-create（问答生成人设）
assets/personas/          GLM娘初始人设（scripts/install_default_persona.py 安装进人格库）
tests/test_smoke.py       端到端烟雾测试（82 项，纯标准库）
workspace/                专用驾驶舱工作区（AGENTS.md：配置页/聊天区分工 + 开机恢复值机）
```

## 日常迭代流程

1. 直接修改 `glm-foxgirl/` 下的源码。
2. 跑测试确认：`python glm-foxgirl\tests\test_smoke.py`（应 82/82）。
3. 在 `glm-foxgirl/.zcode-plugin/plugin.json` 里**递增语义化版本**，并同步 `marketplace.json` 里该插件的 `version`。
4. 提交：`git add -A && git commit -m "feat: ..."`（便携 git 在 `D:\z code shuju\tools\MinGit\cmd\git.exe`）。
5. 在 ZCode 里：**插件市场 → 齿轮 → 市场源 → 刷新该市场**，再到 **个人 → 插件 → 更新**。

## 安装/试用

- 市场根目录（在"插件市场 → 添加 → 添加插件市场"里粘贴）：`D:\z code shuju\chajianku\pebanchajian`
- 市场名：`dev-persona-bot-local`；插件：`glm-foxgirl@dev-persona-bot-local`（GLM狐狸娘）
- 详细功能与架构见 [glm-foxgirl/README.md](glm-foxgirl/README.md)。

## 数据与备份

运行时数据（人格/记忆/画像/黑白名单/表情包/配置）在 `~/.persona-bot/`，不进本仓库；备份该目录即备份一切。
