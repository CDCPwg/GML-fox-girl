#!/usr/bin/env python3
"""把 assets/personas/ 下的初始人格安装进人格库（~/.persona-bot/persona.db）。

已存在同名人格则跳过；库为空或没有激活人格时自动激活（初始人设）。
用法：python scripts/install_default_persona.py
"""
import json
import os
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(os.path.dirname(HERE), "assets", "personas")
DATA = os.environ.get("PERSONA_BOT_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".persona-bot")
DB = os.path.join(DATA, "persona.db")


def main():
    if not os.path.exists(DB):
        sys.exit(f"未找到 {DB}：请先安装并启用插件（MCP 启动时会自动建库）")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    installed, activated = [], []
    for fn in sorted(os.listdir(ASSETS)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(ASSETS, fn), encoding="utf-8") as f:
            p = json.load(f)
        name, prompt, style = p["name"], p["prompt"], p.get("style", "")
        row = conn.execute("SELECT id FROM personas WHERE name=?", (name,)).fetchone()
        if row:
            installed.append(f"{name}(已存在,跳过)")
            pid = row["id"]
        else:
            pid = conn.execute(
                "INSERT INTO personas(name, prompt, style, is_active, created_at, updated_at)"
                " VALUES(?,?,?,0,?,?)", (name, prompt, style, now, now)).lastrowid
            installed.append(f"{name}(新增)")
        empty_or_inactive = (conn.execute("SELECT COUNT(*) FROM personas").fetchone()[0] == 1
                             or conn.execute("SELECT COUNT(*) FROM personas WHERE is_active=1").fetchone()[0] == 0)
        if empty_or_inactive:
            conn.execute("UPDATE personas SET is_active=0")
            conn.execute("UPDATE personas SET is_active=1 WHERE id=?", (pid,))
            activated.append(name)
    conn.commit()
    act = conn.execute("SELECT name FROM personas WHERE is_active=1").fetchone()
    print("安装:", ", ".join(installed) or "（assets/personas 为空）")
    print("激活:", ", ".join(activated) if activated
          else f"保持不变（当前激活：{act['name'] if act else '无'}）")
    conn.close()


if __name__ == "__main__":
    main()
