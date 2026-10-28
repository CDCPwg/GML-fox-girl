"""内容防火墙：入站恶意内容拦截（本机破坏/信息骗取/提示词注入）与出站泄密反向扫描。

拦截的入站消息永不进入大脑上下文（processed=1 + msg_type='blocked'），
警告由大脑发送罐装文本；重复违规走管理守卫计数（超 10 次自动拉黑）。
"""
import re

from . import config, db

# (正则, 类别)——仅用于 QQ/微信入站文本的硬过滤，误伤率优先于查全率
MALICIOUS_PATTERNS = [
    (r"(rm\s+-rf|del\s+/[sqf]\s|format\s+[a-z]:|mkfs|dd\s+if=|shutdown\s|taskkill|reg\s+add|regedit)",
     "本机破坏命令"),
    (r"(powershell|cmd\.exe|wscript|cscript)\s+[-/\w]", "命令执行"),
    (r"(把|将|读|查看|导出).{0,12}(配置|数据目录|persona\.db|数据库|token|密钥|密码|api[_ ]?key|webui|凭据)"
     r".{0,8}(发|给|传|告诉|看看|导出|打印)", "骗取本机信息"),
    (r"(发|给|告诉我|看看|显示).{0,6}(你的)?(配置|token|密钥|密码|api[_ ]?key|数据目录|系统提示词|人格文件)",
     "骗取本机信息"),
    (r"(~/?\.persona-bot|system32|/persona\.db)", "本机路径探测"),
    (r"(忽略|无视)(之前|上面|以上|所有|先前)?的?(指令|设定|规则|提示词)|ignore\s+(all\s+)?previous|"
     r"system\s+prompt|开发者模式|你现在是(管理员|系统|root)|假装你是管理员", "提示词注入"),
    (r"(帮我|请|给我)?(删除|执行|运行|打开|启动|清理).{0,10}(命令|脚本|程序|\.exe|摄像头|麦克风|任务管理器|注册表)",
     "本机操作请求"),
]
_COMPILED = [(re.compile(p, re.I), cat) for p, cat in MALICIOUS_PATTERNS]


def scan_content(text):
    """入站内容安全扫描：命中返回 {"malicious": True, "categories": [...]}。"""
    hits = []
    text = text or ""
    for pat, cat in _COMPILED:
        if pat.search(text):
            hits.append(cat)
    return {"malicious": bool(hits), "categories": sorted(set(hits))}


def _known_secrets():
    cfg = config.load_config()
    secrets = []
    for path in (("qq", "napcat_token"), ("qq", "webui_token"),
                 ("memory", "embed_api_key"), ("collector", "token")):
        v = cfg.get(path[0], {}).get(path[1], "")
        if isinstance(v, str) and len(v) >= 6:
            secrets.append(v)
    enc = cfg.get("qq", {}).get("credentials", {}).get("enc_password", "")
    if enc:
        try:
            from .crypto import dpapi_unprotect
            secrets.append(dpapi_unprotect(enc))
        except Exception:
            pass
    return [s for s in secrets if len(s) >= 6]


def secret_leak(text):
    """出站泄密反向扫描：待发文本命中任何已托管密钥值 → 拦截。"""
    text = text or ""
    for secret in _known_secrets():
        if secret in text:
            return True
    return False


def t_recent_blocks(limit=20, **_):
    """被内容防火墙拦截的记录（仅元数据，不回传攻击原文，防二次注入）。"""
    with db.LOCK:
        rows = [dict(r) for r in db.CONN.execute(
            "SELECT m.id, m.channel, m.chat_type, m.chat_id, m.contact_id, m.display_name,"
            " m.created_at FROM messages m WHERE m.msg_type='blocked'"
            " ORDER BY m.id DESC LIMIT ?", (max(1, min(int(limit), 100)),))]
        g = db.CONN.execute("SELECT channel, contact_id, count FROM guard").fetchall()
    return {"count": len(rows), "blocked": rows,
            "guard_tracked": [dict(x) for x in g],
            "hint": "拦截消息永不进入大脑上下文；重复违规账号由守卫自动拉黑（>10次）"}
