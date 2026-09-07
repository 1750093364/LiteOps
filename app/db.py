# -*- coding: utf-8 -*-
"""SQLite 连接与建表（13 张表，一次建全，后续任务不改表结构）。"""
import json
import sqlite3
from contextlib import contextmanager

from app.config import BASE_DIR, DB_PATH

SNIPPETS_SEED = BASE_DIR / "app" / "seeds" / "snippets.json"  # 卡06 内置片段种子
REPORT_TEMPLATES_SEED = BASE_DIR / "app" / "seeds" / "report_templates.json"  # 卡09 报告框架模板种子

SCHEMA = """
CREATE TABLE IF NOT EXISTS data_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_name     TEXT,
    name            TEXT,
    file_type       TEXT,
    source          TEXT,
    path            TEXT,
    encoding        TEXT,
    row_count       INTEGER,
    col_count       INTEGER,
    size_bytes      INTEGER,
    category_id     INTEGER,
    tags            TEXT,
    note            TEXT,
    status          TEXT DEFAULT 'active',
    parent_file_id  INTEGER,
    created_at      TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS data_fields (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id       INTEGER,
    field_name    TEXT,
    inferred_type TEXT,
    manual_type   TEXT,
    missing_rate  REAL,
    sample_values TEXT,
    created_at    TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS categories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    scope      TEXT,
    parent_id  INTEGER,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS clean_profiles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     INTEGER,
    report_json TEXT,
    created_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS clean_presets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT,
    scenario     TEXT,
    actions_json TEXT,
    is_builtin   INTEGER DEFAULT 0,
    created_at   TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS clean_records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id        INTEGER,
    preset_id      INTEGER,
    actions_json   TEXT,
    stats_json     TEXT,
    output_file_id INTEGER,
    snapshot_path  TEXT,
    created_at     TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS snippets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT,
    category   TEXT,
    dialect    TEXT,
    sql_body   TEXT,
    note       TEXT,
    source     TEXT,
    use_count  INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    category_id INTEGER,
    path        TEXT,
    doc_type    TEXT,
    summary     TEXT,
    created_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS tickets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    title               TEXT,
    requester           TEXT,
    description         TEXT,
    urgency             TEXT,
    deadline            TEXT,
    est_hours           REAL,
    analysis_type       TEXT,
    status              TEXT DEFAULT 'pending',
    score               REAL,
    sort_order          INTEGER DEFAULT 0,
    framework_confirmed INTEGER DEFAULT 0,
    confirm_note        TEXT,
    actual_hours        REAL,
    created_at          TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT,
    ticket_id     INTEGER,
    template_type TEXT,
    content_html  TEXT,
    refs_json     TEXT,
    status        TEXT,
    created_at    TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at    TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS report_templates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT,
    type          TEXT,
    sections_json TEXT,
    is_builtin    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    "key"   TEXT PRIMARY KEY,
    "value" TEXT
);

CREATE TABLE IF NOT EXISTS event_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event       TEXT,
    params_json TEXT,
    created_at  TEXT DEFAULT (datetime('now', 'localtime'))
);
"""


@contextmanager
def get_conn():
    """SQLite 连接上下文管理器：正常提交，异常回滚，结束关闭。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _seed_builtin_snippets(conn) -> None:
    """卡06：snippets 表为空时灌入内置种子片段（仅首次启动执行，已有数据不覆盖）。"""
    cnt = conn.execute("SELECT COUNT(*) AS c FROM snippets").fetchone()["c"]
    if cnt > 0:
        return
    if not SNIPPETS_SEED.is_file():
        return
    items = json.loads(SNIPPETS_SEED.read_text(encoding="utf-8"))
    conn.executemany(
        """INSERT INTO snippets (title, category, dialect, sql_body, note, source)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [
            (
                it.get("title", "").strip(),
                it.get("category", "其他").strip(),
                (it.get("dialect") or "mysql").strip(),
                it.get("sql_body", ""),
                it.get("note", "").strip(),
                it.get("source", "builtin").strip(),
            )
            for it in items
        ],
    )


def _seed_report_templates(conn) -> None:
    """卡09：report_templates 表为空时灌入 7 个内置报告框架模板（仅首次启动执行）。"""
    cnt = conn.execute("SELECT COUNT(*) AS c FROM report_templates").fetchone()["c"]
    if cnt > 0:
        return
    if not REPORT_TEMPLATES_SEED.is_file():
        return
    items = json.loads(REPORT_TEMPLATES_SEED.read_text(encoding="utf-8"))
    conn.executemany(
        """INSERT INTO report_templates (name, type, sections_json, is_builtin)
           VALUES (?, ?, ?, 1)""",
        [
            (
                it.get("name", "").strip(),
                it.get("type", "").strip(),
                json.dumps(it.get("sections", []), ensure_ascii=False),
            )
            for it in items
        ],
    )


def init_db():
    """初始化数据库：建全部 13 张表（IF NOT EXISTS，可重复执行）+ 内置种子。"""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _seed_builtin_snippets(conn)
        _seed_report_templates(conn)
