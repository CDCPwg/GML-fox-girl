---
name: wechat-adb-setup
description: 引导用户通过 ADB 连接安卓模拟器或 USB 真机，把微信接入 persona-bot。当用户要接入微信、配置模拟器/真机、安装 ADB、微信发不出消息或截图失败时使用。
---

# 微信渠道接入（ADB：模拟器 / USB 真机）

微信没有可用的第三方协议（网页协议已被封），本插件用 **ADB 模拟点击**方案：不注入微信进程，只模拟真人操作，属灰度方案，但仍有风控风险。**强烈建议专用小号。**

v1 能力边界（要如实告知）：可以**读**截图里的文字/图片/表情包（靠 ZCode 视觉识别），发送支持**文字**；图片/表情包发送、视频识别属于后续迭代。

## 步骤

1. **装 ADB**（已装可跳过）：
   - 下载 Google Platform Tools：https://developer.android.com/tools/releases/platform-tools ，解压到如 `D:\platform-tools`。
   - `configure_channel(channel="wechat", adb_path="D:\\platform-tools\\adb.exe", enabled=true)`
2. **连接设备**（二选一）：
   - **模拟器**：装好微信并登录后，按模拟器查端口连接：
     - MuMu 12：`adb connect 127.0.0.1:16384`
     - 雷电：`adb connect 127.0.0.1:5555`
     - 其他模拟器看其文档"adb 调试端口"。多开实例端口不同，用哪个填哪个。
   - **USB 真机**：开发者选项 → 打开 USB 调试 → 插线 → 手机上点"允许调试"。
   - 验证：`adb devices` 列出 `device` 状态。多设备时把序列号填进 `configure_channel(device_serial=...)`。
3. **中文输入（必须）**：安装 ADBKeyboard https://github.com/senzhk/ADBKeyBoard （Releases 里有 apk），然后：
   - `adb install ADBKeyboard.apk`
   - `adb shell ime set com.android.adbkeyboard/.AdbIME`
   未安装时插件只能发英文，中文会报明确错误。
4. **验证链路**：
   - 手机/模拟器停在某个微信聊天界面 → `wechat_screenshot` → 用 Read 读返回的 PNG，确认能看到聊天内容。
   - `send_wechat_message(text="测试")` → 微信里发出即通。
5. **测试全链路**（不真发）：`simulate_inbound(channel="wechat", ...)` 走一遍大脑决策。

## 使用约定

- 发送前设备必须**停留在目标聊天窗口**（v1 不自动导航聊天列表）。
- 微信图片/表情包的"识别"流程：`wechat_screenshot` → Read 识图 → 决策 → 文字回复。

## 表情包发送坐标校准（v0.5+）

`send_wechat_sticker` 依赖 4 组点击坐标（⊕ 按钮 → 相册 → 第一张图 → 发送按钮）：

1. 手机停在目标聊天，`wechat_screenshot` 截图并用 Read 查看，记下右下角 ⊕ 的中心坐标。
2. 依次走到每一步各截一次图，记录 相册 / 第一张图 / 发送按钮 的坐标。
3. `set_config` 写入 8 项：`wechat.plus_tap_x/y`、`wechat.album_tap_x/y`、`wechat.first_image_tap_x/y`、`wechat.send_btn_tap_x/y`。
4. `send_wechat_sticker(file_path=表情包库路径)` 试发一次，卡住就截图微调。

注意：相册按时间倒序，刚 push 的图通常在第一张；换设备/换分辨率需重新校准。

## 风控提示（要主动告知用户）

- 模拟器有设备指纹风险；保持默认拟人延迟，不要手动高频调用 send。
- 新号、新设备、夜间高频操作最容易触发风控。
- 安卓 10+ 真机截图可能需要"允许 USB 调试（安全设置）"（小米等品牌）。
