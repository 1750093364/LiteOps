# -*- coding: utf-8 -*-
"""SQL 片段库路由（卡06）。

- GET    /api/snippets              关键字检索（title/sql_body/note）+ 分类/方言筛选 + 分页
- GET    /api/snippets/categories   分类树计数（含总数）
- POST   /api/snippets              新建片段（source="user"，use_count 默认为 0）
- PUT    /api/snippets/{id}         更新片段（内置/用户片段均可改）
- DELETE /api/snippets/{id}         删除片段（内置/用户片段均可删，二次确认在前端）
- POST   /api/snippets/{id}/copy    复制：use_count +1，回传 SQL 正文，写 snippet_copy 埋点
- 埋点：snippet_search（keyword, result_cnt）、snippet_copy（snippet_id, dialect）、
        snippet_save（category, source）
"""
import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.db import get_conn

router = APIRouter(prefix="/api/snippets", tags=["snippets"])

# 固定分类（与前端分类树一致）
CATEGORIES = ["留存", "漏斗", "TopN", "同环比", "去重", "汇总", "其他"]


def _ok(data=None):
    return {"code": 0, "data": data}


def _fail(msg: str, code: int = 1):
    """业务失败：HTTP 200 + code!=0，避免前端拿到 500。"""
    return JSONResponse(status_code=200, content={"code": code, "msg": msg})


def _log_event(conn, event: str, params: dict) -> None:
    conn.execute(
        "INSERT INTO event_logs (event, params_json) VALUES (?, ?)",
        (event, json.dumps(params, ensure_ascii=False)),
    )


# ==================== 检索 / 筛选 / 分页 ====================

@router.get("")
def list_snippets(
    q: str = "",
    category: str = "",
    dialect: str = "",
    page: int = 1,
    page_size: int = 10,
):
    """片段列表：关键字匹配标题/SQL 正文/注释；分类、方言精确筛选；分页返回。

    注：分类/方言是独立筛选条件（左侧分类树点击），关键字只搜正文内容，
    因此搜「留存」命中标题/正文含留存的片段，点「留存」分类才按分类过滤。
    """
    kw = (q or "").strip()
    category = (category or "").strip()
    dialect = (dialect or "").strip()
    page = max(1, page)
    page_size = min(max(1, page_size), 100)

    where, params = [], []
    if kw:
        where.append("(title LIKE ? OR sql_body LIKE ? OR note LIKE ?)")
        like = f"%{kw}%"
        params.extend([like, like, like])
    if category:
        where.append("category = ?")
        params.append(category)
    if dialect:
        where.append("dialect = ?")
        params.append(dialect)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM snippets{where_sql}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"""SELECT id, title, category, dialect, sql_body, note, source,
                       use_count, created_at
                FROM snippets{where_sql}
                ORDER BY use_count DESC, id ASC
                LIMIT ? OFFSET ?""",
            params + [page_size, (page - 1) * page_size],
        ).fetchall()
        if kw:
            _log_event(conn, "snippet_search", {
                "keyword": kw,
                "result_cnt": total,
                "category": category or None,
                "dialect": dialect or None,
            })

    return _ok({
        "list": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.get("/categories")
def snippet_categories():
    """分类树计数：总数 + 各分类片段数（含方言筛选时的口径与列表一致）。"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT category, COUNT(*) AS cnt
               FROM snippets GROUP BY category"""
        ).fetchall()
    counts = {r["category"]: r["cnt"] for r in rows}
    total = sum(counts.values())
    return _ok({
        "total": total,
        "categories": [{"name": c, "cnt": counts.get(c, 0)} for c in CATEGORIES],
    })


# ==================== 增 / 改 / 删 ====================

class SnippetBody(BaseModel):
    title: str
    category: str = "其他"
    dialect: str = "mysql"
    sql_body: str
    note: str = ""


class SnippetUpdateBody(BaseModel):
    title: str | None = None
    category: str | None = None
    dialect: str | None = None
    sql_body: str | None = None
    note: str | None = None


def _clean_str(v) -> str:
    return str(v if v is not None else "").strip()


@router.post("")
def create_snippet(body: SnippetBody):
    """新建片段：source='user'，use_count 取表默认 0；写 snippet_save 埋点。"""
    title = _clean_str(body.title)
    sql_body = (body.sql_body or "").strip()
    if not title:
        return _fail("片段标题不能为空")
    if not sql_body:
        return _fail("SQL 正文不能为空")
    if len(title) > 100:
        return _fail("片段标题不能超过 100 字")
    category = _clean_str(body.category) or "其他"
    dialect = _clean_str(body.dialect) or "mysql"
    note = _clean_str(body.note)

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO snippets (title, category, dialect, sql_body, note, source)
               VALUES (?, ?, ?, ?, ?, 'user')""",
            (title, category, dialect, sql_body, note),
        )
        snippet_id = cur.lastrowid
        _log_event(conn, "snippet_save", {
            "snippet_id": snippet_id,
            "category": category,
            "source": "user",
        })
    return _ok({"id": snippet_id, "title": title})


@router.put("/{snippet_id}")
def update_snippet(snippet_id: int, body: SnippetUpdateBody):
    """更新片段字段（内置/用户片段均可改）。

    注：snippets 表为卡00 冻结结构（id/title/category/dialect/sql_body/note/
    source/use_count/created_at），无 updated_at 列且禁止改表，故不刷新更新时间。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM snippets WHERE id = ?", (snippet_id,)
        ).fetchone()
        if not row:
            return _fail(f"片段不存在（id={snippet_id}）")

        sets, vals = [], []
        if body.title is not None:
            title = _clean_str(body.title)
            if not title:
                return _fail("片段标题不能为空")
            sets.append("title = ?")
            vals.append(title)
        if body.category is not None:
            sets.append("category = ?")
            vals.append(_clean_str(body.category) or "其他")
        if body.dialect is not None:
            sets.append("dialect = ?")
            vals.append(_clean_str(body.dialect) or "mysql")
        if body.sql_body is not None:
            sql_body = body.sql_body.strip()
            if not sql_body:
                return _fail("SQL 正文不能为空")
            sets.append("sql_body = ?")
            vals.append(sql_body)
        if body.note is not None:
            sets.append("note = ?")
            vals.append(_clean_str(body.note))
        if not sets:
            return _fail("未提供任何需要修改的内容")
        vals.append(snippet_id)
        conn.execute(
            f"UPDATE snippets SET {', '.join(sets)} WHERE id = ?", vals
        )
    return _ok({"id": snippet_id})


@router.delete("/{snippet_id}")
def delete_snippet(snippet_id: int):
    """删除片段（内置/用户片段均可删；前端二次确认，文案：确定删除片段《标题》？此操作不可恢复）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM snippets WHERE id = ?", (snippet_id,)
        ).fetchone()
        if not row:
            return _fail(f"片段不存在（id={snippet_id}）")
        conn.execute("DELETE FROM snippets WHERE id = ?", (snippet_id,))
    return _ok({"id": snippet_id})


# ==================== 复制计数 ====================

@router.post("/{snippet_id}/copy")
def copy_snippet(snippet_id: int):
    """复制片段：use_count 自增，回传 SQL 正文供前端写剪贴板；写 snippet_copy 埋点。"""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT id, title, category, dialect, sql_body, use_count
               FROM snippets WHERE id = ?""",
            (snippet_id,),
        ).fetchone()
        if not row:
            return _fail(f"片段不存在（id={snippet_id}）")
        conn.execute(
            "UPDATE snippets SET use_count = use_count + 1 WHERE id = ?",
            (snippet_id,),
        )
        _log_event(conn, "snippet_copy", {
            "snippet_id": snippet_id,
            "dialect": row["dialect"],
            "category": row["category"],
        })
        new_count = row["use_count"] + 1

    return _ok({
        "id": snippet_id,
        "title": row["title"],
        "dialect": row["dialect"],
        "category": row["category"],
        "sql_body": row["sql_body"],
        "use_count": new_count,
    })
