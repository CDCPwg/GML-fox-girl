---
description: 人格库热切换：切换/查看激活人格（后台指令）
---

管理人格库。参数：$ARGUMENTS（人格名称，可留空）

1. 无参数：调用 `manage_persona(action=list)`，用表格展示人格库（名称、是否激活▶、风格摘要），提示"带名称再次使用即可切换"。
2. 有参数：调用 `manage_persona(action=set_active, name=<参数>)`：
   - 成功 → 回复"已热切换到〈名称〉"，附一句该人格 style 摘要，并提醒：QQ/微信的后续回复立即使用新人格；也可由管理员在聊天里发"切换人格〈名称〉"完成同样操作。
   - 失败（不存在）→ 列出人格库现有名称供选择，不要猜名字。
3. 若用户想新建/修改人格，转 `manage_persona(create/update)`；想备份或分享，用 export/import 并给出 JSON 文件路径（在 `~/.persona-bot/personas/`）。
