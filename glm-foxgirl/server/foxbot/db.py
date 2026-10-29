"""SQLite：连接、表结构、迁移、时间工具。CONN 会被 init_db 重赋值，跨模块一律 db.CONN。"""
import os
import sqlite3
import threading
import time
from datetime import datetime

from . import config

LOCK = threading.RLock()
CONN = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS personas(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
  prompt TEXT NOT NULL, style TEXT DEFAULT '', is_active INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS memories(
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT DEFAULT '', chat_id TEXT DEFAULT '',
  contact_id TEXT DEFAULT '', kind TEXT DEFAULT 'event', importance INTEGER DEFAULT 3,
  content TEXT NOT NULL, embedding TEXT DEFAULT '', emotion INTEGER DEFAULT 0,
  superseded_by INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS profiles(
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, contact_id TEXT NOT NULL,
  display_name TEXT DEFAULT '', impression TEXT DEFAULT '', style_notes TEXT DEFAULT '',
  affinity INTEGER DEFAULT 0, updated_at TEXT, UNIQUE(channel, contact_id));
CREATE TABLE IF NOT EXISTS rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT, list_type TEXT NOT NULL, channel TEXT DEFAULT '*',
  scope TEXT DEFAULT 'contact', target_id TEXT NOT NULL, note TEXT DEFAULT '',
  created_at TEXT, UNIQUE(list_type, channel, scope, target_id));
CREATE TABLE IF NOT EXISTS stickers(
  id INTEGER PRIMARY KEY AUTOINCREMENT, file_path TEXT NOT NULL, emotion TEXT DEFAULT '',
  tags TEXT DEFAULT '', source TEXT DEFAULT '', source_url TEXT DEFAULT '',
  use_count INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, direction TEXT DEFAULT 'in', channel TEXT DEFAULT '',
  chat_type TEXT DEFAULT '', chat_id TEXT DEFAULT '', contact_id TEXT DEFAULT '',
  display_name TEXT DEFAULT '', msg_type TEXT DEFAULT 'text', content TEXT DEFAULT '',
  media_url TEXT DEFAULT '', media_path TEXT DEFAULT '', media_json TEXT DEFAULT '[]',
  processed INTEGER DEFAULT 0, out_kind TEXT DEFAULT 'reply', created_at TEXT);
CREATE TABLE IF NOT EXISTS counters(
  chat_key TEXT PRIMARY KEY, window_start REAL, count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT DEFAULT 'qq', chat_type TEXT DEFAULT 'private',
  chat_id TEXT DEFAULT '', contact_id TEXT DEFAULT '', text TEXT DEFAULT '', image_path TEXT DEFAULT '',
  out_kind TEXT DEFAULT 'reply', tries INTEGER DEFAULT 0, last_error TEXT DEFAULT '', created_at TEXT);
CREATE TABLE IF NOT EXISTS graph_nodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT COLLATE NOCASE NOT NULL UNIQUE,
  kind TEXT DEFAULT 'other', summary TEXT DEFAULT '', created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS graph_edges(
  id INTEGER PRIMARY KEY AUTOINCREMENT, src_id INTEGER NOT NULL, dst_id INTEGER NOT NULL,
  relation TEXT NOT NULL, weight INTEGER DEFAULT 1, note TEXT DEFAULT '', last_seen TEXT,
  UNIQUE(src_id, dst_id, relation));
CREATE TABLE IF NOT EXISTS session_notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT DEFAULT '', content TEXT NOT NULL,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS guard(
  channel TEXT NOT NULL, contact_id TEXT NOT NULL, count INTEGER DEFAULT 0,
  last_action TEXT DEFAULT '', last_at TEXT,
  UNIQUE(channel, contact_id));
CREATE TABLE IF NOT EXISTS link_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel_a TEXT, id_a TEXT,
  channel_b TEXT, id_b TEXT, code TEXT, status TEXT DEFAULT 'pending',
  payload TEXT DEFAULT '', created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS account_links(
  id INTEGER PRIMARY KEY AUTOINCREMENT, left_c TEXT, left_i TEXT,
  right_c TEXT, right_i TEXT, created_at TEXT,
  UNIQUE(left_c, left_i, right_c, right_i));
CREATE TABLE IF NOT EXISTS alert_queue(
  id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,
  sent INTEGER DEFAULT 0, created_at TEXT);
"""


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(s):
    try:
        return time.mktime(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timetuple())
    except Exception:
        return time.mktime(datetime.now().timetuple())


def _migrate():
    for table, col, decl in (("memories", "embedding", "TEXT DEFAULT ''"),
                             ("memories", "emotion", "INTEGER DEFAULT 0"),
                             ("memories", "superseded_by", "INTEGER DEFAULT 0"),
                             ("stickers", "source_url", "TEXT DEFAULT ''")):
        cols = {r[1] for r in CONN.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            CONN.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db():
    global CONN
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.MEDIA_DIR, exist_ok=True)
    CONN = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    CONN.row_factory = sqlite3.Row
    CONN.execute("PRAGMA busy_timeout=5000")  # 多 collector 进程并存时避免立即 SQLITE_BUSY
    with LOCK:
        CONN.executescript(SCHEMA)
        _migrate()
        CONN.commit()
