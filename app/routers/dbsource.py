# -*- coding: utf-8 -*-
"""数据库连接与取数路由（卡12）。

路由：
- GET    /api/db/connections          连接列表（密码返回 null）
- POST   /api/db/connections          新增连接（密码 Fernet 加密落库）
- DELETE /api/db/connections/{cid}    删除连接
- POST   /api/db/test                 测试连接（请求体传连接配置，不依赖已保存）
- GET    /api/db/{cid}/tables         表名列表
- GET    /api/db/{cid}/preview?table= 表前 100 行预览
- POST   /api/db/{cid}/query          执行 SELECT 取数并落地为数据文件

业务错误统一 HTTP 200 + code!=0 + 中文 msg；连接失败不影响文件功能。
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.db import get_conn
from app.services import db_client as db

router = APIRouter(prefix="/api/db", tags=["db"])


def _ok(data=None):
    return {"code": 0, "data": data}


def _fail(msg: str, code: int = 1):
    return JSONResponse(status_code=200, content={"code": code, "msg": msg})


# ---------- 请求体 ----------

class ConnBody(BaseModel):
    type: str = "mysql"        # mysql / postgresql
    host: str = ""
    port: int = 3306
    user: str = ""
    password: str = ""
    db_name: str = ""
    note: str = ""


class QueryBody(BaseModel):
    sql: str = ""


# ---------- 连接 CRUD ----------

@router.get("/connections")
def list_connections():
    with get_conn() as conn:
        return _ok(db.public_list(conn))


@router.post("/connections")
def create_connection(body: ConnBody):
    data = body.model_dump()
    if not data.get("host"):
        return _fail("请填写数据库主机地址（host）")
    if not data.get("user"):
        return _fail("请填写数据库用户名")
    if not data.get("db_name"):
        return _fail("请填写数据库名")
    ctype = (data.get("type") or "").lower()
    if ctype not in ("mysql", "postgresql"):
        return _fail("数据库类型仅支持 mysql / postgresql")
    port = data.get("port") or 0
    if not (1 <= port <= 65535):
        return _fail("端口号不合法（1~65535）")
    with get_conn() as conn:
        rec = db.create_connection_record(conn, data)
    return _ok(rec)


@router.delete("/connections/{cid}")
def delete_connection(cid: int):
    with get_conn() as conn:
        ok = db.delete_connection_record(conn, cid)
    if not ok:
        return _fail(f"连接不存在（id={cid}）")
    return _ok({"id": cid})


# ---------- 测试连接 ----------

@router.post("/test")
def test_connection(body: ConnBody):
    """测试连接：直接用请求体配置尝试连接，分类返回错误。"""
    data = body.model_dump()
    if not data.get("host"):
        return _fail("请填写数据库主机地址（host）")
    result = db.test_connection(data)
    return _ok(result)


# ---------- 取数 ----------

def _get_cfg(cid: int):
    """取已保存连接配置（含解密密码），不存在返回 None。"""
    with get_conn() as conn:
        return db.get_connection(conn, cid)


@router.get("/{cid}/tables")
def get_tables(cid: int):
    cfg = _get_cfg(cid)
    if not cfg:
        return _fail(f"连接不存在（id={cid}）")
    try:
        tables = db.list_tables(cfg)
    except Exception as e:
        _, msg = db.classify_connect_error(e)
        return _fail(msg)
    return _ok({"tables": tables})


@router.get("/{cid}/preview")
def preview_table(cid: int, table: str = ""):
    if not table:
        return _fail("请指定表名（table 参数）")
    cfg = _get_cfg(cid)
    if not cfg:
        return _fail(f"连接不存在（id={cid}）")
    try:
        columns, rows = db.preview_table(cfg, table)
    except Exception as e:
        _, msg = db.classify_connect_error(e)
        return _fail(msg)
    return _ok({"table": table, "columns": columns, "rows": rows})


@router.post("/{cid}/query")
def query_import(cid: int, body: QueryBody):
    """执行 SELECT 取数并落地为数据文件，返回 file_id 供前端跳转预览。"""
    sql = (body.sql or "").strip()
    if not sql:
        return _fail("SQL 不能为空")
    cfg = _get_cfg(cid)
    if not cfg:
        return _fail(f"连接不存在（id={cid}）")
    try:
        result = db.import_query_result(cfg, sql)
    except ValueError as e:
        return _fail(str(e))
    except Exception as e:
        _, msg = db.classify_connect_error(e)
        return _fail(msg)
    return _ok(result)
