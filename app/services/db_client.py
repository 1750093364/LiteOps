# -*- coding: utf-8 -*-
"""数据库只读连接取数服务（卡12）。

职责：
- 连接配置 CRUD（存 settings 表 key='db_connections'，JSON 数组），密码 Fernet 加密；
- 连接测试（网络不通 / 凭据错误 / 权限不足 / 成功 四类分类）；
- 表列表、表预览（LIMIT 100）、SELECT 查询取数（安全校验 + 自动 LIMIT 10000）；
- 取数产物落地为 CSV 并写 data_files / data_fields，与卡01 导入产物完全同构。

依赖：pymysql（MySQL）+ psycopg2-binary（PostgreSQL）+ cryptography（Fernet）+ pandas.read_sql。
密钥首次启动生成于 data/secret.key，仅本机可读；密码绝不明文落库、绝不返回前端。
"""
import json
import re
import socket
import time
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
from cryptography.fernet import Fernet, InvalidToken

from app.config import BASE_DIR, DATA_DIR, FILES_DIR
from app.db import get_conn
from app.services import file_service as fs

DB_CONN_KEY = "db_connections"
SECRET_KEY_PATH = DATA_DIR / "secret.key"

CONNECT_TIMEOUT = 5           # 连接超时（秒），避免网络不通时卡死
PREVIEW_LIMIT = 100           # 表预览行数
QUERY_LIMIT = 10000           # 查询自动 LIMIT 上限
MAX_QUERY_BYTES = 200_000     # SQL 文本长度上限（防超长注入）

# 安全：禁止的写操作关键字（大小写不敏感，按单词边界匹配）
_FORBIDDEN_KW = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER",
                 "CREATE", "TRUNCATE", "GRANT", "REVOKE"]

# ---------- Fernet 密钥 ----------

def get_fernet() -> Fernet:
    """读取或生成 Fernet 密钥（data/secret.key）。"""
    if SECRET_KEY_PATH.is_file():
        key = SECRET_KEY_PATH.read_bytes()
    else:
        key = Fernet.generate_key()
        SECRET_KEY_PATH.write_bytes(key)
        try:
            SECRET_KEY_PATH.chmod(0o600)  # 仅所有者读写（类 Unix 生效，Windows 忽略）
        except OSError:
            pass
    return Fernet(key)


def encrypt_password(plain: str) -> str:
    if plain is None:
        return None
    return get_fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_password(token: str) -> str | None:
    if not token:
        return None
    try:
        return get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


# ---------- 连接配置 CRUD（settings 表） ----------

def load_connections(conn) -> list[dict]:
    """读取全部连接配置（含解密后的明文密码，仅服务端内部使用）。"""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (DB_CONN_KEY,)
    ).fetchone()
    if row is None:
        return []
    try:
        raw = json.loads(row["value"])
    except (ValueError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        out.append({
            "id": it.get("id"),
            "type": it.get("type") or "mysql",
            "host": it.get("host") or "",
            "port": it.get("port") or 3306,
            "user": it.get("user") or "",
            "password": decrypt_password(it.get("password_enc")),
            "db_name": it.get("db_name") or "",
            "note": it.get("note") or "",
            "created_at": it.get("created_at") or "",
        })
    return out


def save_connections(conn, conns: list[dict]) -> None:
    """写回全部连接配置（密码加密后落库）。"""
    payload = []
    for c in conns:
        payload.append({
            "id": c.get("id"),
            "type": c.get("type") or "mysql",
            "host": c.get("host") or "",
            "port": c.get("port") or 3306,
            "user": c.get("user") or "",
            "password_enc": encrypt_password(c.get("password")),
            "db_name": c.get("db_name") or "",
            "note": c.get("note") or "",
            "created_at": c.get("created_at") or "",
        })
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (DB_CONN_KEY, json.dumps(payload, ensure_ascii=False)),
    )


def public_list(conn) -> list[dict]:
    """返回给前端的连接列表：密码字段为 null，绝不泄露。"""
    out = []
    for c in load_connections(conn):
        out.append({
            "id": c["id"],
            "type": c["type"],
            "host": c["host"],
            "port": c["port"],
            "user": c["user"],
            "password": None,
            "db_name": c["db_name"],
            "note": c["note"],
            "created_at": c["created_at"],
        })
    return out


def get_connection(conn, cid: int) -> dict | None:
    for c in load_connections(conn):
        if c.get("id") == cid:
            return c
    return None


def create_connection_record(conn, data: dict) -> dict:
    """新增连接配置。data 含明文 password。"""
    conns = load_connections(conn)
    new_id = max((c["id"] for c in conns if isinstance(c.get("id"), int)), default=0) + 1
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rec = {
        "id": new_id,
        "type": (data.get("type") or "mysql").strip().lower(),
        "host": (data.get("host") or "").strip(),
        "port": int(data.get("port") or 0),
        "user": (data.get("user") or "").strip(),
        "password": data.get("password") or "",
        "db_name": (data.get("db_name") or "").strip(),
        "note": (data.get("note") or "").strip(),
        "created_at": now,
    }
    conns.append(rec)
    save_connections(conn, conns)
    pub = dict(rec)
    pub["password"] = None
    return pub


def delete_connection_record(conn, cid: int) -> bool:
    conns = load_connections(conn)
    new_list = [c for c in conns if c.get("id") != cid]
    if len(new_list) == len(conns):
        return False
    save_connections(conn, new_list)
    return True


# ---------- 连接建立与错误分类 ----------

def _build_conn(cfg: dict):
    """根据 type 建立原生连接（pymysql / psycopg2），失败抛对应异常。"""
    ctype = (cfg.get("type") or "mysql").lower()
    host = cfg.get("host") or ""
    port = int(cfg.get("port") or 0)
    user = cfg.get("user") or ""
    password = cfg.get("password") or ""
    db_name = cfg.get("db_name") or ""
    if ctype == "mysql":
        import pymysql
        return pymysql.connect(
            host=host, port=port, user=user, password=password,
            database=db_name or None, charset="utf8mb4",
            connect_timeout=CONNECT_TIMEOUT, read_timeout=30, write_timeout=30,
        )
    elif ctype == "postgresql":
        import psycopg2
        return psycopg2.connect(
            host=host, port=port, user=user, password=password,
            dbname=db_name or "", connect_timeout=CONNECT_TIMEOUT,
        )
    raise ValueError(f"不支持的数据库类型「{ctype}」，可选 mysql / postgresql")


def classify_connect_error(exc: Exception) -> tuple[str, str]:
    """把连接异常分类为 (kind, 中文提示)。

    kind: network / credential / permission / other
    """
    msg = str(exc)
    low = msg.lower()
    # 网络层
    if isinstance(exc, (socket.timeout, TimeoutError, ConnectionRefusedError, OSError)):
        return "network", "网络不通：连接超时或被拒绝，请检查 host/port 与网络可达性"
    cls_name = type(exc).__name__.lower()
    # MySQL（pymysql.err.OperationalError）
    if "operationalerror" in cls_name:
        code = exc.args[0] if exc.args else None
        if code in (2003, 2006, 2013, 2026):
            return "network", "网络不通：无法连接到 MySQL 服务器，请检查 host/port"
        if code in (1045,):
            return "credential", "凭据错误：用户名或密码不正确"
        if code in (1044,):
            return "permission", "权限不足：该用户无权访问此数据库"
        return "other", f"连接失败：{msg}"
    # PostgreSQL（psycopg2.OperationalError）
    if "operationalerror" in cls_name or cls_name.startswith("psycopg2"):
        if any(k in low for k in ("timeout", "refused", "could not connect",
                                   "connection refused", "name or service not known")):
            return "network", "网络不通：无法连接到 PostgreSQL 服务器，请检查 host/port"
        if any(k in low for k in ("authentication failed", "password authentication",
                                   "role", "no password supplied")):
            return "credential", "凭据错误：用户名或密码不正确"
        if "permission" in low or "denied" in low:
            return "permission", "权限不足：该用户无权访问此数据库或表"
        return "other", f"连接失败：{msg}"
    if isinstance(exc, ValueError):
        return "other", str(exc)
    return "other", f"连接失败：{msg}"


def test_connection(cfg: dict) -> dict:
    """测试连接：返回 {ok, kind, msg}。"""
    try:
        c = _build_conn(cfg)
        try:
            # 执行一条最简查询确认库可访问
            cur = c.cursor()
            if cfg.get("type", "").lower() == "postgresql":
                cur.execute("SELECT 1")
            else:
                cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
        finally:
            c.close()
        return {"ok": True, "kind": "success", "msg": "连接成功，数据库可访问"}
    except Exception as e:
        kind, msg = classify_connect_error(e)
        return {"ok": False, "kind": kind, "msg": msg}


# ---------- 表列表 / 表预览 ----------

def list_tables(cfg: dict) -> list[str]:
    """列出数据库中的表名。"""
    c = _build_conn(cfg)
    try:
        cur = c.cursor()
        if cfg.get("type", "").lower() == "postgresql":
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "ORDER BY tablename"
            )
        else:
            cur.execute("SHOW TABLES")
        rows = cur.fetchall()
        cur.close()
        return [r[0] for r in rows]
    finally:
        c.close()


def _quote_ident(cfg: dict, name: str) -> str:
    """标识符引号：MySQL 反引号，PostgreSQL 双引号。"""
    if cfg.get("type", "").lower() == "postgresql":
        return '"' + name.replace('"', '""') + '"'
    return "`" + name.replace("`", "``") + "`"


def preview_table(cfg: dict, table: str) -> tuple[list[str], list[list]]:
    """预览表前 PREVIEW_LIMIT 行，返回 (columns, rows)。"""
    sql = f"SELECT * FROM {_quote_ident(cfg, table)} LIMIT {PREVIEW_LIMIT}"
    c = _build_conn(cfg)
    try:
        df = pd.read_sql(sql, c)
    finally:
        c.close()
    columns = [str(c) for c in df.columns]
    rows = df.astype("object").where(df.notna(), "").astype(str).values.tolist()
    return columns, rows


# ---------- SQL 安全校验 ----------

def _strip_comments(sql: str) -> str:
    """去除 SQL 注释：块注释 /* */ 与行注释 --。"""
    # 块注释（跨行）
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    # 行注释 -- 到行尾
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def validate_select_sql(sql: str) -> tuple[bool, str, str]:
    """校验 SQL 必须是只读 SELECT，返回 (ok, cleaned_sql, 错误提示)。

    规则：去注释后必须以 SELECT 开头；不含写操作关键字（单词边界）；
    无 LIMIT 时自动追加 LIMIT 10000。
    """
    if not sql or not sql.strip():
        return False, "", "SQL 不能为空"
    if len(sql) > MAX_QUERY_BYTES:
        return False, "", f"SQL 过长（超过 {MAX_QUERY_BYTES} 字符）"
    cleaned = _strip_comments(sql).strip()
    if not cleaned:
        return False, "", "SQL 去除注释后为空"
    if not re.match(r"(?i)^SELECT\b", cleaned):
        return False, "", "查询必须以 SELECT 开头，禁止非查询语句"
    for kw in _FORBIDDEN_KW:
        if re.search(r"\b" + kw + r"\b", cleaned, flags=re.IGNORECASE):
            return False, "", f"SQL 包含禁止的写操作关键字「{kw}」，仅允许 SELECT 查询"
    # 自动补 LIMIT
    if not re.search(r"\bLIMIT\b", cleaned, flags=re.IGNORECASE):
        cleaned = cleaned.rstrip().rstrip(";") + f" LIMIT {QUERY_LIMIT}"
    return True, cleaned, ""


# ---------- 查询取数并落地为数据文件 ----------

def import_query_result(cfg: dict, sql: str) -> dict:
    """执行 SELECT 查询，结果落地为 CSV 并写 data_files/data_fields。

    返回 {file_id, origin_name, row_count, col_count, size_bytes, cost_sec}。
    """
    ok, cleaned_sql, err = validate_select_sql(sql)
    if not ok:
        raise ValueError(err)

    started = time.time()
    c = _build_conn(cfg)
    try:
        df = pd.read_sql(cleaned_sql, c)
    finally:
        c.close()

    if df.empty:
        raise ValueError("查询结果为空，无可导入数据")

    # 落地 CSV
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    safe_db = re.sub(r"[^\w\u4e00-\u9fa5]+", "_", cfg.get("db_name") or "db")
    fname = f"{safe_db}_query_{ts}.csv"
    path = FILES_DIR / f"{uuid.uuid4().hex}.csv"
    df.to_csv(path, index=False, encoding="utf-8")
    size = path.stat().st_size
    relative = path.relative_to(BASE_DIR).as_posix()

    # 字段统计（与文件导入同构）
    analysis = fs.analyze_dataframe(df)

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO data_files
               (origin_name, name, file_type, source, path, encoding,
                row_count, col_count, size_bytes, status)
               VALUES (?, ?, ?, '数据库导入', ?, 'utf-8', ?, ?, ?, '已导入')""",
            (fname, fname, "数据库表", relative,
             analysis["row_count"], analysis["col_count"], size),
        )
        file_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO data_fields
               (file_id, field_name, inferred_type, missing_rate, sample_values)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (file_id, f["field_name"], f["inferred_type"],
                 f["missing_rate"], json.dumps(f["sample_values"], ensure_ascii=False))
                for f in analysis["fields"]
            ],
        )
        conn.execute(
            "INSERT INTO event_logs (event, params_json) VALUES (?, ?)",
            ("db_import", json.dumps({
                "file_id": file_id,
                "db_name": cfg.get("db_name"),
                "origin_name": fname,
                "rows": analysis["row_count"],
                "cols": analysis["col_count"],
                "cost_sec": round(time.time() - started, 2),
            }, ensure_ascii=False)),
        )

    return {
        "file_id": file_id,
        "origin_name": fname,
        "row_count": analysis["row_count"],
        "col_count": analysis["col_count"],
        "size_bytes": size,
        "cost_sec": round(time.time() - started, 2),
    }


def import_table(cfg: dict, table: str) -> dict:
    """整表导入（SELECT * FROM table）。"""
    sql = f"SELECT * FROM {_quote_ident(cfg, table)}"
    # 覆写 origin_name 为 库名_表名_时间戳
    result = import_query_result(cfg, sql)
    # result 里的 origin_name 是 query 模式命名，这里重命名为 表名
    with get_conn() as conn:
        fname = f"{cfg.get('db_name')}_{table}_{datetime.now().strftime('%Y%m%d%H%M%S')}.csv"
        conn.execute(
            "UPDATE data_files SET origin_name = ?, name = ? WHERE id = ?",
            (fname, fname, result["file_id"]),
        )
    result["origin_name"] = fname
    return result
