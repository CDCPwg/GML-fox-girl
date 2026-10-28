"""路径与配置：数据目录、默认配置、config.json 读写。"""
import json
import os

VERSION = "0.14.1"
DATA_DIR = os.environ.get("PERSONA_BOT_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".persona-bot")
DB_PATH = os.path.join(DATA_DIR, "persona.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
MEDIA_DIR = os.path.join(DATA_DIR, "media")

DEFAULT_CONFIG = {
    "qq": {"enabled": False, "napcat_base_url": "http://127.0.0.1:3000", "napcat_token": ""},
    "humanize": {"reply_delay_min_ms": 1200, "reply_delay_max_ms": 6000,
                 "max_per_chat_per_hour": 12, "outbox_ttl_min": 30},
    "proactive": {"enabled": False, "quiet_hours": "23:30-07:30", "max_per_day": 5,
                  "reply_probability_group": 0.3, "topic_reply": False, "topics": []},
    "admin": {"qq": [], "wechat": []},
    "memory": {"embed_base_url": "", "embed_api_key": "", "embed_model": "embedding-3",
               "half_life_days": 30},
    "mood": {"current": 0, "reason": ""},
    "limits": {"auto_handover_turns": 40, "auto_handover_enabled": True,
               "warn_pct": 85, "handover_pct": 95, "auto_reopen": False},
    "video": {"ffmpeg_path": "ffmpeg"},
    "stickers": {"auto_collect": True},
    "wechat": {"enabled": False, "adb_path": "adb", "device_serial": "", "input_tap_x": 0,
               "input_tap_y": 0, "human_trace": True},
    "link": {"similar_threshold": 0.6, "expire_min": 15},
    "collector": {"host": "127.0.0.1", "port": 3001, "token": ""},
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                stored = json.load(f)
            for k, v in stored.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
