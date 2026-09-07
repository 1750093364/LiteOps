# -*- coding: utf-8 -*-
"""数据存储模块路由：文件导入、分页列表、数据预览、删除、编码重解析、
分类目录、标签、全文检索、字段类型修正、备注口径。"""
import json
import time
from pathlib import Path

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import BASE_DIR
from app.db import get_conn
from app.services import file_service as fs

router = APIRouter(prefix="/api/files", tags=["files"])
cat_router = APIRouter(prefix="/api/categories", tags=["categories"])

# 手动切换编码允许的候选
MANUAL_ENCODINGS = {"utf-8", "utf-8-sig", "gbk", "gb2312", "gb18030", "big5"}

SUFFIX_LABEL = {".csv": "CSV", ".xlsx": "Excel", ".xls": "Excel"}

# 字段类型可选值（修正用）
FIELD_TYPES = ["整数", "小数", "日期", "布尔", "文本", "混合"]

# 标签存储：JSON 数组字符串
def _tags_to_str(tags):
    if not tags:
        return None
    if isinstance(tags, str):
        # 兼容逗号分隔输入
        parts = [t.strip() for t in tags.split(",") if t.strip()]
        return json.dumps(parts, ensure_ascii=False) if parts else None
    if isinstance(tags, list):
        parts = [str(t).strip() for t in tags if str(t).strip()]
        return json.dumps(parts, ensure_ascii=False) if parts else None
    return None


def _tags_from_str(s):
    if not s:
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return [t.strip() for t in s.split(",") if t.strip()]


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


def _log_import_event(params: dict) -> None:
    """file_import 埋点（失败路径无打开中的连接，单独取连接写入）。"""
    try:
        with get_conn() as conn:
            _log_event(conn, "file_import", params)
    except Exception:
        pass  # 埋点失败不影响主流程


def _physical_path(relative_path: str) -> Path:
    return BASE_DIR / relative_path


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """导入数据文件：校验后缀与大小 → 落盘 → 解析统计 → 写库。"""
    origin_name = file.filename or "未命名"
    suffix = Path(origin_name).suffix.lower()
    if suffix not in fs.ALLOWED_SUFFIX:
        _log_import_event({"file_type": suffix or "unknown", "result": "fail",
                           "fail_reason": f"不支持的文件类型「{suffix or '无后缀'}」"})
        return _fail(f"不支持的文件类型「{suffix or '无后缀'}」，仅支持 .xlsx / .xls / .csv")

    is_csv = suffix == ".csv"
    max_bytes = fs.MAX_CSV_BYTES if is_csv else fs.MAX_EXCEL_BYTES
    limit_desc = "CSV 500MB" if is_csv else "Excel 50MB"

    # 流式落盘（超限抛 ValueError）
    try:
        path, size = fs.save_upload_to_disk(file.file, suffix, max_bytes, limit_desc)
    except ValueError as e:
        _log_import_event({"file_type": "csv" if is_csv else "excel", "result": "fail",
                           "size_mb": 0, "fail_reason": str(e)})
        return _fail(str(e))
    except OSError:
        _log_import_event({"file_type": "csv" if is_csv else "excel", "result": "fail",
                           "fail_reason": "文件保存失败，请检查 data 目录写入权限"})
        return _fail("文件保存失败，请检查 data 目录写入权限")

    # 解析统计
    started = time.time()
    try:
        if is_csv:
            encoding = fs.resolve_csv_encoding(path)
            result = fs.analyze_csv(path, encoding)
        else:
            encoding = None
            result = fs.analyze_excel(path)
    except ValueError as e:
        fs.remove_physical_file(str(path))
        _log_import_event({"file_type": "csv" if is_csv else "excel", "result": "fail",
                           "size_mb": round(size / 1048576, 2), "fail_reason": str(e)})
        return _fail(str(e))
    except Exception:
        fs.remove_physical_file(str(path))
        _log_import_event({"file_type": "csv" if is_csv else "excel", "result": "fail",
                           "size_mb": round(size / 1048576, 2),
                           "fail_reason": "文件解析失败，请确认文件未损坏且格式正确"})
        return _fail("文件解析失败，请确认文件未损坏且格式正确后重试")

    relative = path.relative_to(BASE_DIR).as_posix()
    try:
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO data_files
                   (origin_name, name, file_type, source, path, encoding,
                    row_count, col_count, size_bytes, status)
                   VALUES (?, ?, ?, '本地导入', ?, ?, ?, ?, ?, '已导入')""",
                (origin_name, origin_name, "csv" if is_csv else "excel", relative, encoding,
                 result["row_count"], result["col_count"], size),
            )
            file_id = cur.lastrowid
            conn.executemany(
                """INSERT INTO data_fields
                   (file_id, field_name, inferred_type, missing_rate, sample_values)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (file_id, f["field_name"], f["inferred_type"],
                     f["missing_rate"], json.dumps(f["sample_values"], ensure_ascii=False))
                    for f in result["fields"]
                ],
            )
            _log_event(conn, "file_import", {"file_id": file_id, "name": origin_name,
                                             "file_type": "csv" if is_csv else "excel",
                                             "rows": result["row_count"],
                                             "cols": result["col_count"],
                                             "size_mb": round(size / 1048576, 2),
                                             "result": "success",
                                             "cost_sec": round(time.time() - started, 2)})
    except Exception:
        fs.remove_physical_file(relative)
        _log_import_event({"file_type": "csv" if is_csv else "excel", "result": "fail",
                           "fail_reason": "导入信息写入数据库失败，已回滚"})
        return _fail("导入信息写入数据库失败，已回滚")

    return _ok({
        "id": file_id,
        "origin_name": origin_name,
        "file_type": "csv" if is_csv else "excel",
        "encoding": encoding,
        "row_count": result["row_count"],
        "col_count": result["col_count"],
        "size_bytes": size,
        "cost_sec": round(time.time() - started, 2),
    })


@router.get("")
def list_files(page: int = 1, page_size: int = 10, category_id: int = None,
               tag: str = None, file_type: str = None):
    """分页文件列表：支持按分类/标签/类型筛选。"""
    if page < 1 or page_size < 1 or page_size > 100:
        return _fail("分页参数不合法（page>=1，1<=page_size<=100）")
    where = []
    params = []
    if category_id is not None:
        where.append("category_id = ?")
        params.append(category_id)
    if tag:
        where.append("tags LIKE ?")
        params.append(f'%"{tag}"%')
    if file_type:
        where.append("file_type = ?")
        params.append(file_type)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM data_files {where_sql}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"""SELECT id, origin_name, file_type, source, row_count, col_count,
                      size_bytes, encoding, status, created_at, category_id, tags,
                      parent_file_id
               FROM data_files {where_sql}
               ORDER BY id DESC LIMIT ? OFFSET ?""",
            params + [page_size, (page - 1) * page_size],
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["tags"] = _tags_from_str(d.get("tags"))
        items.append(d)
    return _ok({
        "list": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.get("/{file_id}/preview")
def preview_file(file_id: int):
    """数据预览：元信息 + 字段卡片（含 id 与 manual_type）+ 前 5 行数据。"""
    with get_conn() as conn:
        rec = conn.execute(
            """SELECT id, origin_name, name, file_type, source, path, encoding,
                      row_count, col_count, size_bytes, status, created_at,
                      category_id, tags, note, parent_file_id
               FROM data_files WHERE id = ?""",
            (file_id,),
        ).fetchone()
        if not rec:
            return _fail(f"文件不存在（id={file_id}）")
        fields = conn.execute(
            """SELECT id, field_name, inferred_type, manual_type, missing_rate, sample_values
               FROM data_fields WHERE file_id = ? ORDER BY id""",
            (file_id,),
        ).fetchall()
        # 清洗产物：追溯来源文件名
        parent_name = None
        if rec["parent_file_id"]:
            p = conn.execute(
                "SELECT origin_name FROM data_files WHERE id = ?",
                (rec["parent_file_id"],),
            ).fetchone()
            parent_name = p["origin_name"] if p else None

    path = _physical_path(rec["path"])
    if not path.is_file():
        return _fail(f"物理文件已丢失（{rec['path']}），无法预览")

    try:
        columns, rows = fs.read_head_rows(path, rec["encoding"], n=5)
    except (UnicodeDecodeError, UnicodeError):
        return _fail(f"使用编码 {rec['encoding']} 读取失败，请在顶部切换编码后重新解析")
    except ValueError as e:
        return _fail(str(e))
    except Exception:
        return _fail("文件读取失败，文件可能已损坏")

    field_cards = []
    for f in fields:
        try:
            samples = json.loads(f["sample_values"] or "[]")
        except json.JSONDecodeError:
            samples = []
        field_cards.append({
            "id": f["id"],
            "field_name": f["field_name"],
            "inferred_type": f["inferred_type"],
            "manual_type": f["manual_type"],
            "display_type": f["manual_type"] or f["inferred_type"],
            "missing_rate": f["missing_rate"],
            "sample_values": samples,
        })

    data = dict(rec)
    data["tags"] = _tags_from_str(rec["tags"])
    data["fields"] = field_cards
    data["preview_columns"] = columns
    data["preview_rows"] = rows
    data["parent_name"] = parent_name
    return _ok(data)


@router.delete("/{file_id}")
def delete_file(file_id: int):
    """删除文件：一个事务内先清全部关联子记录，再删主记录；物理文件（含清洗快照）同步删除。

    级联范围（13 张表中引用 data_files.id 的表）：
    - data_fields.file_id                         字段信息
    - clean_profiles.file_id                      体检报告
    - clean_records.file_id / output_file_id      清洗记录（含 data/snapshots 快照物理文件）
    - data_files.parent_file_id                   清洗产物（删源文件时产物一并删除）
    三种场景均允许删除：刚导入未体检、体检过、被清洗记录引用/作为清洗产物输出。
    """
    with get_conn() as conn:
        rec = conn.execute(
            "SELECT id, origin_name, path, parent_file_id FROM data_files WHERE id = ?",
            (file_id,),
        ).fetchone()
        if not rec:
            return _fail(f"文件不存在（id={file_id}）")

        # 1. 待删文件集合：自身 + 其作为源文件产出的全部清洗产物
        file_ids = [file_id]
        if rec["parent_file_id"] is None:
            file_ids.extend(
                r["id"] for r in conn.execute(
                    "SELECT id FROM data_files WHERE parent_file_id = ? ORDER BY id",
                    (file_id,),
                ).fetchall()
            )
        ids_sql = ",".join("?" for _ in file_ids)

        # 2. 关联清洗记录（源或产物任一命中即随删），快照物理文件收集后在提交外删除
        records = conn.execute(
            f"""SELECT id, snapshot_path FROM clean_records
                WHERE file_id IN ({ids_sql}) OR output_file_id IN ({ids_sql})""",
            file_ids + file_ids,
        ).fetchall()
        snapshot_paths = [r["snapshot_path"] for r in records if r["snapshot_path"]]

        # 3. 先清子表：清洗记录 → 体检报告 → 字段信息
        conn.execute(
            f"DELETE FROM clean_records WHERE file_id IN ({ids_sql}) "
            f"OR output_file_id IN ({ids_sql})",
            file_ids + file_ids,
        )
        conn.execute(
            f"DELETE FROM clean_profiles WHERE file_id IN ({ids_sql})", file_ids
        )
        conn.execute(
            f"DELETE FROM data_fields WHERE file_id IN ({ids_sql})", file_ids
        )

        # 4. 收集待删物理文件路径，再删主记录
        file_rows = conn.execute(
            f"SELECT id, path FROM data_files WHERE id IN ({ids_sql})", file_ids
        ).fetchall()
        physical_paths = [r["path"] for r in file_rows if r["path"]]
        conn.execute(
            f"DELETE FROM data_files WHERE id IN ({ids_sql})", file_ids
        )

        _log_event(conn, "delete_file", {
            "file_id": file_id, "name": rec["origin_name"],
            "cascaded_files": len(file_ids) - 1,
            "clean_records_removed": len(records),
        })

    # 5. 事务提交成功后再删物理文件（数据/产物文件 + 清洗快照）
    for rel in physical_paths:
        fs.remove_physical_file(rel)
    for rel in snapshot_paths:
        fs.remove_physical_file(rel)

    return _ok({"id": file_id, "deleted_files": len(file_ids),
                "removed_clean_records": len(records)})


class EncodingBody(BaseModel):
    encoding: str


@router.put("/{file_id}/encoding")
def change_encoding(file_id: int, body: EncodingBody):
    """手动指定编码重解析（仅 CSV）：更新 data_files 与 data_fields。"""
    enc = body.encoding.strip().lower()
    if enc not in MANUAL_ENCODINGS:
        return _fail(f"不支持的编码「{body.encoding}」，可选：UTF-8 / GBK / GB2312")

    with get_conn() as conn:
        rec = conn.execute(
            "SELECT id, path, file_type FROM data_files WHERE id = ?", (file_id,)
        ).fetchone()
        if not rec:
            return _fail(f"文件不存在（id={file_id}）")
        if rec["file_type"] != "csv":
            return _fail("Excel 文件无编码概念，无需手动切换")

        path = _physical_path(rec["path"])
        if not path.is_file():
            return _fail(f"物理文件已丢失（{rec['path']}），无法重解析")

        try:
            result = fs.analyze_csv(path, enc)
        except (UnicodeDecodeError, UnicodeError):
            return _fail(f"使用 {enc} 解码失败，请确认文件编码后重试")
        except ValueError as e:
            return _fail(str(e))
        except Exception:
            return _fail(f"使用 {enc} 解析失败，请确认文件格式正确后重试")

        conn.execute(
            "UPDATE data_files SET encoding = ?, row_count = ? WHERE id = ?",
            (enc, result["row_count"], file_id),
        )
        conn.execute("DELETE FROM data_fields WHERE file_id = ?", (file_id,))
        conn.executemany(
            """INSERT INTO data_fields
               (file_id, field_name, inferred_type, missing_rate, sample_values)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (file_id, f["field_name"], f["inferred_type"],
                 f["missing_rate"], json.dumps(f["sample_values"], ensure_ascii=False))
                for f in result["fields"]
            ],
        )
        _log_event(conn, "reparse_encoding", {"file_id": file_id, "encoding": enc})

    return _ok({"id": file_id, "encoding": enc,
                "row_count": result["row_count"], "col_count": result["col_count"]})


# ==================== 目录树（type / custom） ====================

@router.get("/tree")
def file_tree(view: str = "type"):
    """目录树：type=按文件类型自动分组计数；custom=用户分类树+各分类文件数。"""
    if view not in ("type", "custom"):
        return _fail("view 参数仅支持 type 或 custom")
    with get_conn() as conn:
        if view == "type":
            # 清洗产物优先（parent_file_id 非空），其余按 file_type/source 分组
            cleaned = conn.execute(
                """SELECT f.id, f.origin_name, f.file_type, f.created_at,
                          p.origin_name AS parent_name
                   FROM data_files f
                   LEFT JOIN data_files p ON p.id = f.parent_file_id
                   WHERE f.parent_file_id IS NOT NULL
                   ORDER BY f.id DESC"""
            ).fetchall()
            excel_cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM data_files WHERE file_type='excel' AND parent_file_id IS NULL"
            ).fetchone()["c"]
            csv_cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM data_files WHERE file_type='csv' AND parent_file_id IS NULL"
            ).fetchone()["c"]
            db_cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM data_files WHERE parent_file_id IS NULL AND source LIKE '%数据库%'"
            ).fetchone()["c"]
            groups = [
                {"key": "excel", "label": "Excel", "count": excel_cnt},
                {"key": "csv", "label": "CSV", "count": csv_cnt},
                {"key": "database", "label": "数据库表", "count": db_cnt},
                {
                    "key": "cleaned", "label": "清洗产物", "count": len(cleaned),
                    "items": [
                        {"id": r["id"], "origin_name": r["origin_name"],
                         "parent_name": r["parent_name"], "created_at": r["created_at"]}
                        for r in cleaned
                    ],
                },
            ]
            return _ok({"view": "type", "groups": groups})

        # custom 视图：用户分类树（scope=file，两级）
        cats = conn.execute(
            "SELECT id, name, parent_id FROM categories WHERE scope='file' ORDER BY id"
        ).fetchall()
        # 文件计数按 category_id
        counts = {}
        for r in conn.execute(
            "SELECT category_id, COUNT(*) AS c FROM data_files WHERE category_id IS NOT NULL GROUP BY category_id"
        ):
            counts[r["category_id"]] = r["c"]
        top = [c for c in cats if c["parent_id"] is None]
        tree = []
        for c in top:
            children = [
                {"id": cc["id"], "name": cc["name"], "parent_id": cc["parent_id"],
                 "count": counts.get(cc["id"], 0)}
                for cc in cats if cc["parent_id"] == c["id"]
            ]
            tree.append({
                "id": c["id"], "name": c["name"], "parent_id": None,
                "count": counts.get(c["id"], 0), "children": children,
            })
        uncategorized = conn.execute(
            "SELECT COUNT(*) AS c FROM data_files WHERE category_id IS NULL"
        ).fetchone()["c"]
        return _ok({"view": "custom", "categories": tree, "uncategorized": uncategorized})


# ==================== 全文检索 ====================

@router.get("/search")
def search_files(q: str = ""):
    """全文检索：匹配 origin_name / note / tags / 字段名，返回文件列表+命中字段。"""
    q = (q or "").strip()
    if not q:
        return _ok({"list": [], "total": 0, "q": ""})
    like = f"%{q}%"
    with get_conn() as conn:
        # 命中文件：文件名/备注/标签 或 任一字段名匹配
        rows = conn.execute(
            """SELECT DISTINCT f.id, f.origin_name, f.file_type, f.source,
                      f.row_count, f.col_count, f.size_bytes, f.encoding,
                      f.status, f.created_at, f.category_id, f.tags, f.note,
                      f.parent_file_id
               FROM data_files f
               LEFT JOIN data_fields df ON df.file_id = f.id
               WHERE f.origin_name LIKE ? OR f.note LIKE ? OR f.tags LIKE ?
                  OR df.field_name LIKE ?
               ORDER BY f.id DESC""",
            (like, like, like, like),
        ).fetchall()
        # 每个文件命中的字段名
        hit_map = {}
        for r in conn.execute(
            """SELECT file_id, field_name FROM data_fields
               WHERE field_name LIKE ?""",
            (like,),
        ):
            hit_map.setdefault(r["file_id"], []).append(r["field_name"])
    items = []
    for r in rows:
        d = dict(r)
        d["tags"] = _tags_from_str(d.get("tags"))
        d["hit_fields"] = hit_map.get(r["id"], [])
        items.append(d)
    return _ok({"list": items, "total": len(items), "q": q})


# ==================== 移动文件到分类 ====================

class CategoryBody(BaseModel):
    category_id: int | None = None


@router.put("/{file_id}/category")
def move_file_category(file_id: int, body: CategoryBody):
    """移动文件到指定分类（category_id=null 移出分类）。"""
    with get_conn() as conn:
        rec = conn.execute(
            "SELECT id FROM data_files WHERE id = ?", (file_id,)
        ).fetchone()
        if not rec:
            return _fail(f"文件不存在（id={file_id}）")
        cid = body.category_id
        if cid is not None:
            cat = conn.execute(
                "SELECT id, scope FROM categories WHERE id = ?", (cid,)
            ).fetchone()
            if not cat:
                return _fail(f"分类不存在（id={cid}）")
            if cat["scope"] != "file":
                return _fail("仅可移动到文件分类")
        conn.execute(
            "UPDATE data_files SET category_id = ? WHERE id = ?", (cid, file_id)
        )
    return _ok({"id": file_id, "category_id": cid})


# ==================== 标签更新 ====================

class TagsBody(BaseModel):
    tags: list[str] | str | None = None


@router.put("/{file_id}/tags")
def update_tags(file_id: int, body: TagsBody):
    """更新文件标签（传入数组或逗号分隔字符串，空则清空）。"""
    with get_conn() as conn:
        rec = conn.execute(
            "SELECT id FROM data_files WHERE id = ?", (file_id,)
        ).fetchone()
        if not rec:
            return _fail(f"文件不存在（id={file_id}）")
        tag_str = _tags_to_str(body.tags)
        conn.execute("UPDATE data_files SET tags = ? WHERE id = ?", (tag_str, file_id))
    return _ok({"id": file_id, "tags": _tags_from_str(tag_str)})


# ==================== 备注/口径更新 ====================

class NoteBody(BaseModel):
    note: str | None = None


@router.put("/{file_id}")
def update_note(file_id: int, body: NoteBody):
    """更新文件备注/口径说明。"""
    with get_conn() as conn:
        rec = conn.execute(
            "SELECT id FROM data_files WHERE id = ?", (file_id,)
        ).fetchone()
        if not rec:
            return _fail(f"文件不存在（id={file_id}）")
        note = body.note.strip() if body.note else None
        conn.execute("UPDATE data_files SET note = ? WHERE id = ?", (note, file_id))
    return _ok({"id": file_id, "note": note})


# ==================== 字段类型修正 ====================

class FieldTypeBody(BaseModel):
    manual_type: str | None = None


@router.put("/{file_id}/fields/{field_id}")
def fix_field_type(file_id: int, field_id: int, body: FieldTypeBody):
    """修正字段类型（写 manual_type；传 null 恢复推断类型）。"""
    mt = body.manual_type
    if mt is not None and mt not in FIELD_TYPES:
        return _fail(f"不支持的类型「{mt}」，可选：{'、'.join(FIELD_TYPES)}")
    with get_conn() as conn:
        f = conn.execute(
            "SELECT id, file_id FROM data_fields WHERE id = ? AND file_id = ?",
            (field_id, file_id),
        ).fetchone()
        if not f:
            return _fail(f"字段不存在（file_id={file_id}, field_id={field_id}）")
        conn.execute(
            "UPDATE data_fields SET manual_type = ? WHERE id = ?", (mt, field_id)
        )
    return _ok({"id": field_id, "manual_type": mt})


# ==================== 分类 CRUD（/api/categories） ====================

class CategoryCreate(BaseModel):
    name: str
    parent_id: int | None = None


@cat_router.post("")
def create_category(scope: str = "file", body: CategoryCreate = ...):
    """创建分类（scope=file/doc/snippet/preset，两级父子）。"""
    name = body.name.strip()
    if not name:
        return _fail("分类名称不能为空")
    if scope not in ("file", "doc", "snippet", "preset"):
        return _fail(f"不支持的 scope「{scope}」")
    with get_conn() as conn:
        pid = body.parent_id
        if pid is not None:
            parent = conn.execute(
                "SELECT id, parent_id FROM categories WHERE id = ? AND scope = ?",
                (pid, scope),
            ).fetchone()
            if not parent:
                return _fail(f"父分类不存在（id={pid}）")
            if parent["parent_id"] is not None:
                return _fail("仅支持两级分类，不可在子分类下再建子分类")
        cur = conn.execute(
            "INSERT INTO categories (name, scope, parent_id) VALUES (?, ?, ?)",
            (name, scope, pid),
        )
        cid = cur.lastrowid
    return _ok({"id": cid, "name": name, "scope": scope, "parent_id": pid})


@cat_router.get("")
def list_categories(scope: str = "file"):
    """列出指定 scope 的分类（扁平列表，前端按 parent_id 建树）。"""
    if scope not in ("file", "doc", "snippet", "preset"):
        return _fail(f"不支持的 scope「{scope}」")
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, scope, parent_id, created_at FROM categories WHERE scope = ? ORDER BY id",
            (scope,),
        ).fetchall()
    return _ok([dict(r) for r in rows])


@cat_router.delete("/{cat_id}")
def delete_category(cat_id: int, scope: str = "file"):
    """删除分类：存在子分类或关联文件时拒绝。"""
    with get_conn() as conn:
        cat = conn.execute(
            "SELECT id, scope FROM categories WHERE id = ?", (cat_id,)
        ).fetchone()
        if not cat:
            return _fail(f"分类不存在（id={cat_id}）")
        child = conn.execute(
            "SELECT COUNT(*) AS c FROM categories WHERE parent_id = ?", (cat_id,)
        ).fetchone()["c"]
        if child:
            return _fail("该分类下存在子分类，请先删除子分类")
        # 关联文件检查（scope=file 时检查 data_files）
        linked = conn.execute(
            "SELECT COUNT(*) AS c FROM data_files WHERE category_id = ?", (cat_id,)
        ).fetchone()["c"]
        if linked:
            return _fail("该分类下存在文件，请先移动文件到其他分类")
        conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    return _ok({"id": cat_id})
