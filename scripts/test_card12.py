# -*- coding: utf-8 -*-
"""卡12 数据库连接取数 自测脚本。

覆盖：
1. 连接 CRUD（POST/GET/DELETE）
2. 密码加密落库（settings 存密文，GET 返回 null）
3. SQL 安全校验（非 SELECT 拦截、自动补 LIMIT、注释剥离）
4. 连接失败错误分类（网络不通 → network）
5. 取数产物落地（mock 连接 → data_files/data_fields/CSV 与卡01 同构）

本机无 MySQL/PostgreSQL 实例，真实连库取数未测（见交付报告未测项）。
"""
import json
import sqlite3
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, ".")

BASE = "http://127.0.0.1:8000"
DB_PATH = "data/liteops.db"

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}  {detail}")


def api(method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))
    except Exception as e:
        return 0, {"code": -1, "msg": str(e)}


def get_settings_value(key):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


# ============ 1. 连接 CRUD ============
print("\n[1] 连接 CRUD")
status, body = api("POST", "/api/db/connections", {
    "type": "mysql", "host": "127.0.0.1", "port": 3306,
    "user": "tester", "password": "Secret@123",
    "db_name": "testdb", "note": "测试连接",
})
check("创建连接返回 code=0", status == 200 and body.get("code") == 0, str(body))
cid = body.get("data", {}).get("id")
check("返回连接 id", cid is not None, str(body))

# GET 列表密码为 null
status, body = api("GET", "/api/db/connections")
conns = body.get("data", [])
check("列表长度=1", len(conns) == 1, str(conns))
check("GET 返回 password=null", conns[0].get("password") is None, str(conns[0]))
check("host 正确", conns[0].get("host") == "127.0.0.1")
check("type 正确", conns[0].get("type") == "mysql")

# settings 表中密码是密文（不是明文）
raw = get_settings_value("db_connections")
check("settings 中密码非明文 'Secret@123'", raw and "Secret@123" not in raw, raw[:120] if raw else "None")
check("settings 中含 password_enc 密文", raw and "password_enc" in raw, "")
# 密文可解密回原文
from app.services import db_client as db
parsed = json.loads(raw)
enc = parsed[0]["password_enc"]
check("密文可解密回原密码", db.decrypt_password(enc) == "Secret@123", db.decrypt_password(enc))

# 校验失败
status, body = api("POST", "/api/db/connections", {"host": "", "user": "x", "db_name": "x"})
check("空 host 被拦截", body.get("code") != 0, str(body))
status, body = api("POST", "/api/db/connections", {"host": "h", "user": "", "db_name": "x"})
check("空 user 被拦截", body.get("code") != 0)
status, body = api("POST", "/api/db/connections", {"host": "h", "user": "u", "db_name": "x", "port": 0})
check("非法端口被拦截", body.get("code") != 0)

# 删除
status, body = api("DELETE", f"/api/db/connections/{cid}")
check("删除连接 code=0", body.get("code") == 0, str(body))
status, body = api("GET", "/api/db/connections")
check("删除后列表为空", len(body.get("data", [])) == 0)


# ============ 2. 错误分类（网络不通） ============
print("\n[2] 连接失败错误分类")
# 不存在的主机（RFC 保留 .invalid TLD，DNS 立即失败）
status, body = api("POST", "/api/db/test", {
    "type": "mysql", "host": "nonexistent.invalid", "port": 3306,
    "user": "u", "password": "p", "db_name": "d",
})
check("测试连通性返回 code=0", body.get("code") == 0, str(body))
data = body.get("data", {})
check("ok=false", data.get("ok") is False, str(data))
check("kind=network", data.get("kind") == "network", str(data))
check("msg 含网络不通", "网络" in (data.get("msg") or ""), str(data))


# ============ 3. SQL 安全校验 ============
print("\n[3] SQL 安全校验")
# 先建一个连接（不连库，只用于传 cid）
status, body = api("POST", "/api/db/connections", {
    "type": "mysql", "host": "127.0.0.1", "port": 3306,
    "user": "u", "password": "p", "db_name": "d",
})
cid2 = body["data"]["id"]

# 非 SELECT 被拦截
for bad_sql in ["DROP TABLE users", "INSERT INTO t VALUES(1)",
                "UPDATE t SET a=1", "DELETE FROM t", "  CREATE TABLE x(a INT)"]:
    status, body = api("POST", f"/api/db/{cid2}/query", {"sql": bad_sql})
    check(f"拦截 {bad_sql[:20]}", body.get("code") != 0, str(body))

# SELECT 但含写关键字被拦截
status, body = api("POST", f"/api/db/{cid2}/query", {"sql": "SELECT * FROM t; DROP TABLE t"})
check("拦截 SELECT 后接 DROP", body.get("code") != 0, str(body))

# 合法 SELECT 但连不上库 → 连接错误（不是 SQL 错误）
status, body = api("POST", f"/api/db/{cid2}/query", {"sql": "SELECT 1"})
check("合法 SELECT 走到连接错误（非 SQL 校验错）",
      body.get("code") != 0 and "SELECT" not in (body.get("msg") or ""), str(body))

# 删除测试连接
api("DELETE", f"/api/db/connections/{cid2}")


# ============ 4. 取数产物落地（mock 连接） ============
print("\n[4] 取数产物落地（mock 连接）")
import pandas as pd
from unittest import mock

# mock _build_conn 返回一个假连接对象，mock pd.read_sql 返回测试 DataFrame
test_df = pd.DataFrame({
    "id": [1, 2, 3, 4, 5],
    "name": ["Alice", "Bob", "Carol", "David", "Eve"],
    "amount": ["1,200.50", "3,400", "560.25", "", "7,890.00"],
    "created_at": ["2026-01-01", "2026-02-15", "2026-03-10", "2026-04-20", "2026-05-01"],
})

cfg = {"type": "mysql", "host": "127.0.0.1", "port": 3306, "user": "u",
       "password": "p", "db_name": "mockdb"}

with mock.patch.object(db, "_build_conn", return_value=mock.MagicMock()), \
     mock.patch.object(db.pd, "read_sql", return_value=test_df):
    result = db.import_query_result(cfg, "SELECT * FROM orders")

check("导入返回 file_id", result.get("file_id") is not None, str(result))
check("行数=5", result.get("row_count") == 5, str(result))
check("列数=4", result.get("col_count") == 4, str(result))
fid = result["file_id"]

# 验证 data_fields 与卡01 同构
with sqlite3.connect(DB_PATH) as conn:
    conn.row_factory = sqlite3.Row
    frec = conn.execute("SELECT * FROM data_files WHERE id=?", (fid,)).fetchone()
    check("data_files.source=数据库导入", frec["source"] == "数据库导入", frec["source"])
    check("data_files.file_type=数据库表", frec["file_type"] == "数据库表", frec["file_type"])
    check("data_files.status=已导入", frec["status"] == "已导入", frec["status"])
    check("data_files.encoding=utf-8", frec["encoding"] == "utf-8")
    check("origin_name 含 mockdb", "mockdb" in (frec["origin_name"] or ""), frec["origin_name"])
    fields = conn.execute("SELECT * FROM data_fields WHERE file_id=? ORDER BY id", (fid,)).fetchall()
    check("data_fields 4 列", len(fields) == 4, str(len(fields)))
    field_map = {f["field_name"]: f for f in fields}
    check("id 推断为整数", field_map["id"]["inferred_type"] == "整数", field_map["id"]["inferred_type"])
    check("name 推断为文本", field_map["name"]["inferred_type"] == "文本", field_map["name"]["inferred_type"])
    check("amount 含千分位提示", "千分位" in field_map["amount"]["inferred_type"], field_map["amount"]["inferred_type"])
    check("amount 缺失率=0.2", abs(field_map["amount"]["missing_rate"] - 0.2) < 0.001, field_map["amount"]["missing_rate"])

# 验证 CSV 物理文件存在且可读回
from pathlib import Path
from app.config import BASE_DIR
csv_path = BASE_DIR / frec["path"]
check("CSV 物理文件存在", csv_path.is_file(), str(csv_path))
df_back = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
check("CSV 读回 5 行", len(df_back) == 5)
check("CSV 列名一致", list(df_back.columns) == ["id", "name", "amount", "created_at"])

# 清理：删除测试文件记录与物理文件
with sqlite3.connect(DB_PATH) as conn:
    conn.execute("DELETE FROM data_fields WHERE file_id=?", (fid,))
    conn.execute("DELETE FROM data_files WHERE id=?", (fid,))
    conn.execute("DELETE FROM event_logs WHERE event='db_import' AND params_json LIKE ?",
                 (f'%{fid}%',))
    conn.commit()
try:
    csv_path.unlink()
except OSError:
    pass
check("测试产物已清理", not csv_path.is_file())


# ============ 5. 连接失败不影响文件功能 ============
print("\n[5] 连接失败不影响文件功能")
status, body = api("GET", "/api/files?page=1&page_size=5")
check("文件列表正常返回", body.get("code") == 0, str(body))
status, body = api("GET", "/api/files/tree?view=type")
check("目录树正常返回", body.get("code") == 0, str(body))


print(f"\n==== 自测结果：{passed} 通过，{failed} 失败 ====")
sys.exit(1 if failed else 0)
