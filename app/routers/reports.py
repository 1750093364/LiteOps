# -*- coding: utf-8 -*-
"""报告工作台模块路由（卡09）。

- 报告 CRUD：GET /api/reports（列表）、POST（新建，状态草稿）、GET/PUT/DELETE /api/reports/{id}
  （PUT 保存 content_html + refs_json + ticket_id，刷新 updated_at）；
- 框架模板：GET /api/report-templates（7 个内置模板，sections_json 解析为章节数组）；
- 数据引用计算：POST /api/reports/calc（file_id + field + agg，pandas 分块聚合，
  支持 count/sum/mean/max/min/count_nonnull，千分位文本自动清洗后转数值）；
- 埋点：report_framework_insert（前端插入模板章节时上报，同时回写 template_type）。
"""
import json
import math
import re

import pandas as pd
from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.config import BASE_DIR
from app.db import get_conn
from app.services import exporter, file_service as fs

router = APIRouter(prefix="/api/reports", tags=["reports"])
tpl_router = APIRouter(prefix="/api/report-templates", tags=["report-templates"])

AGGS = {
    "count": "计数",
    "sum": "合计",
    "mean": "平均值",
    "max": "最大值",
    "min": "最小值",
    "count_nonnull": "非空计数",
}


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


def _unsourced_guard(content_html: str):
    """卡11 防幻觉硬约束：正文存在无来源数字（span.unsourced）时禁止导出。

    前端按钮已拦截，此处为后端兜底，防止直连导出 URL 绕过。
    """
    n = len(re.findall(r'class="unsourced"', content_html or ""))
    if n > 0:
        return _fail(f"报告中存在 {n} 处无数据来源的数字（红色标注），请核对或删除后再导出")
    return None


def _serialize(row) -> dict:
    """reports 行 → 前端字典：refs_json 解析为数组。"""
    d = dict(row)
    try:
        d["refs"] = json.loads(d.pop("refs_json") or "[]")
        if not isinstance(d["refs"], list):
            d["refs"] = []
    except (json.JSONDecodeError, TypeError):
        d["refs"] = []
    return d


# ==================== 请求体 ====================

class ReportCreateBody(BaseModel):
    title: str
    template_type: str | None = None
    ticket_id: int | None = None


class ReportSaveBody(BaseModel):
    title: str | None = None
    content_html: str | None = None
    refs_json: str | None = None
    ticket_id: int | None = None
    template_type: str | None = None


class CalcBody(BaseModel):
    file_id: int
    field: str
    agg: str


class TemplateInsertedBody(BaseModel):
    report_id: int
    template_type: str


# ==================== 框架模板 ====================

@tpl_router.get("")
def list_templates():
    """7 个内置报告框架模板：sections_json 解析为 [{title, hint}]。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, type, sections_json, is_builtin "
            "FROM report_templates ORDER BY id"
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        try:
            d["sections"] = json.loads(d.pop("sections_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["sections"] = []
        items.append(d)
    return _ok({"list": items, "total": len(items)})


# ==================== 数据引用计算 ====================

def _iter_frames(path, encoding):
    """按文件类型产出 DataFrame 迭代器（CSV 分块，Excel 整读一块）。"""
    if path.suffix.lower() == ".csv":
        enc = encoding or fs.resolve_csv_encoding(path)
        return fs.try_read_csv(path, enc)
    engine = "openpyxl" if path.suffix.lower() == ".xlsx" else "xlrd"
    df = pd.read_excel(path, dtype=str, keep_default_na=False, na_values=[],
                       engine=engine)
    return [df]


def _clean_numeric(s: pd.Series) -> pd.Series:
    """文本列转数值：去千分位逗号与空白后 to_numeric，无法转换的置 NaN。"""
    return pd.to_numeric(
        s.astype(str).str.strip().str.replace(",", "", regex=False),
        errors="coerce",
    )


@router.post("/calc")
def calc_aggregate(body: CalcBody):
    """对指定文件的指定字段做聚合计算，供报告"引用数据"卡片使用。

    count=行数、count_nonnull=非缺失数；sum/mean/max/min 仅对可转数值的字段生效。
    """
    if body.agg not in AGGS:
        return _fail(f"不支持的聚合方式「{body.agg}」，可选：{'/'.join(AGGS)}")
    with get_conn() as conn:
        rec = conn.execute(
            "SELECT id, origin_name, path, encoding FROM data_files WHERE id = ?",
            (body.file_id,),
        ).fetchone()
    if not rec:
        return _fail(f"文件不存在（id={body.file_id}）")
    path = BASE_DIR / rec["path"]
    if not path.is_file():
        return _fail(f"物理文件已丢失（{rec['path']}），无法计算")

    total_rows = 0
    nonnull_rows = 0
    total = 0.0          # sum 累计
    cnt_num = 0          # 可转数值的非缺失个数（mean 分母）
    vmax = None
    vmin = None
    try:
        frames = _iter_frames(path, rec["encoding"])
        for chunk in frames:
            if body.field not in chunk.columns:
                return _fail(f"文件「{rec['origin_name']}」中不存在字段「{body.field}」")
            total_rows += len(chunk)
            s = chunk[body.field].astype("object")
            stripped = s.astype(str).str.strip().str.lower()
            miss = s.isna() | stripped.isin(fs.MISSING_TEXT)
            nonnull_rows += int((~miss).sum())
            if body.agg in ("sum", "mean", "max", "min"):
                num = _clean_numeric(chunk[body.field])
                num = num[~num.isna()]
                if len(num):
                    total += float(num.sum())
                    cnt_num += int(len(num))
                    cmax, cmin = float(num.max()), float(num.min())
                    vmax = cmax if vmax is None else max(vmax, cmax)
                    vmin = cmin if vmin is None else min(vmin, cmin)
        try:
            frames.close()  # CSV 分块读取需关闭句柄（Windows 下否则无法删文件）
        except AttributeError:
            pass
    except (UnicodeDecodeError, UnicodeError):
        return _fail(f"使用编码 {rec['encoding']} 读取失败，请先在数据存储中重新解析编码")
    except ValueError as e:
        return _fail(str(e))
    except Exception:
        return _fail("文件读取失败，请确认文件未损坏")

    if total_rows == 0:
        return _fail("文件内容为空，无法计算")

    if body.agg == "count":
        value = total_rows
    elif body.agg == "count_nonnull":
        value = nonnull_rows
    elif body.agg == "sum":
        if cnt_num == 0:
            return _fail(f"字段「{body.field}」无法转换为数值，不能计算合计")
        value = total
    elif body.agg == "mean":
        if cnt_num == 0:
            return _fail(f"字段「{body.field}」无法转换为数值，不能计算平均值")
        value = total / cnt_num
    elif body.agg == "max":
        if vmax is None:
            return _fail(f"字段「{body.field}」无法转换为数值，不能计算最大值")
        value = vmax
    else:  # min
        if vmin is None:
            return _fail(f"字段「{body.field}」无法转换为数值，不能计算最小值")
        value = vmin

    # 整数值去掉浮点尾巴，均值保留 6 位小数
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value) and abs(value) < 1e15:
            value = int(value)
        else:
            value = round(value, 6)

    return _ok({
        "file_id": body.file_id,
        "file_name": rec["origin_name"],
        "field": body.field,
        "agg": body.agg,
        "agg_label": AGGS[body.agg],
        "value": value,
        "rows_used": total_rows,
    })


# ==================== 报告 CRUD ====================

@router.get("")
def list_reports():
    """报告列表（按更新时间倒序）。"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, title, ticket_id, template_type, content_html,
                      refs_json, status, created_at, updated_at
               FROM reports ORDER BY updated_at DESC, id DESC"""
        ).fetchall()
    items = []
    for r in rows:
        d = _serialize(r)
        d["content_html"] = None  # 列表不带正文，减小响应体
        d["refs_cnt"] = len(d["refs"])
        items.append(d)
    return _ok({"list": items, "total": len(items)})


@router.post("")
def create_report(body: ReportCreateBody):
    """新建报告（草稿状态）；模板类型与关联需求单可空。"""
    title = (body.title or "").strip()
    if not title:
        return _fail("报告标题不能为空")
    with get_conn() as conn:
        if body.ticket_id is not None:
            tk = conn.execute(
                "SELECT id FROM tickets WHERE id = ?", (body.ticket_id,)
            ).fetchone()
            if not tk:
                return _fail(f"关联的需求单不存在（id={body.ticket_id}）")
        cur = conn.execute(
            """INSERT INTO reports (title, ticket_id, template_type,
                                    content_html, refs_json, status)
               VALUES (?, ?, ?, '', '[]', '草稿')""",
            (title, body.ticket_id, (body.template_type or "").strip() or None),
        )
        rid = cur.lastrowid
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (rid,)).fetchone()
    return _ok(_serialize(row))


@router.post("/template-inserted")
def template_inserted(body: TemplateInsertedBody):
    """前端插入框架模板成功后上报：记 report_framework_insert 埋点并回写模板类型。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM reports WHERE id = ?", (body.report_id,)
        ).fetchone()
        if not row:
            return _fail(f"报告不存在（id={body.report_id}）")
        conn.execute(
            "UPDATE reports SET template_type = ? WHERE id = ?",
            (body.template_type, body.report_id),
        )
        _log_event(conn, "report_framework_insert", {
            "report_id": body.report_id,
            "template_type": body.template_type,
        })
    return _ok({"inserted": True})


# ==================== 导出（卡10：Word / 打印视图 PDF） ====================

@router.post("/{report_id}/export/word")
def export_word(report_id: int):
    """python-docx 纯库生成 Word：封面页 + 目录页 + 正文 + 页眉页码，返回文件下载。

    埋点 report_export：format=word, doc_length=正文字符数, cited_data_cnt=引用数。
    """
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if not row:
        return _fail(f"报告不存在（id={report_id}）")
    d = _serialize(row)
    blocked = _unsourced_guard(d.get("content_html") or "")
    if blocked is not None:
        return blocked
    out = exporter.EXPORT_DIR / f"report_{report_id}.docx"
    try:
        exporter.export_report_docx(
            d["title"] or "未命名报告", d.get("content_html") or "",
            d.get("status") or "", out)
    except Exception as e:  # 生成失败不让前端拿到 500
        return _fail(f"Word 生成失败：{e}")
    with get_conn() as conn:
        _log_event(conn, "report_export", {
            "report_id": report_id,
            "format": "word",
            "doc_length": len(d.get("content_html") or ""),
            "cited_data_cnt": len(d["refs"]),
        })
    return FileResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=exporter.safe_filename(d["title"] or "报告") + ".docx",
    )


@router.get("/{report_id}/print-view")
def print_view(report_id: int):
    """独立打印 HTML（内联 CSS + @media print）：新窗口打开后浏览器打印另存 PDF。

    埋点 report_export：format=pdf, doc_length, cited_data_cnt。
    """
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if not row:
        return _fail(f"报告不存在（id={report_id}）")
    d = _serialize(row)
    blocked = _unsourced_guard(d.get("content_html") or "")
    if blocked is not None:
        return blocked
    with get_conn() as conn:
        _log_event(conn, "report_export", {
            "report_id": report_id,
            "format": "pdf",
            "doc_length": len(d.get("content_html") or ""),
            "cited_data_cnt": len(d["refs"]),
        })
    return HTMLResponse(exporter.build_print_html(
        d["title"] or "未命名报告", d.get("content_html") or "", d.get("status") or ""))


@router.get("/{report_id}")
def get_report(report_id: int):
    """报告详情：含正文与引用列表（编辑页打开时恢复草稿）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
        if not row:
            return _fail(f"报告不存在（id={report_id}）")
    return _ok(_serialize(row))


@router.put("/{report_id}")
def save_report(report_id: int, body: ReportSaveBody):
    """保存报告：正文 / 引用 JSON / 标题 / 关联需求单 / 模板类型，刷新 updated_at。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
        if not row:
            return _fail(f"报告不存在（id={report_id}）")
        sets, params = [], []
        if body.title is not None:
            title = body.title.strip()
            if not title:
                return _fail("报告标题不能为空")
            sets.append("title = ?")
            params.append(title)
        if body.content_html is not None:
            sets.append("content_html = ?")
            params.append(body.content_html)
        if body.refs_json is not None:
            try:
                refs = json.loads(body.refs_json or "[]")
                if not isinstance(refs, list):
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                return _fail("refs_json 必须是 JSON 数组字符串")
            sets.append("refs_json = ?")
            params.append(json.dumps(refs, ensure_ascii=False))
        if body.ticket_id is not None:
            tk = conn.execute(
                "SELECT id FROM tickets WHERE id = ?", (body.ticket_id,)
            ).fetchone()
            if not tk:
                return _fail(f"关联的需求单不存在（id={body.ticket_id}）")
            sets.append("ticket_id = ?")
            params.append(body.ticket_id)
        if body.template_type is not None:
            sets.append("template_type = ?")
            params.append(body.template_type.strip() or None)
        if sets:
            conn.execute(
                f"UPDATE reports SET {', '.join(sets)}, "
                "updated_at = datetime('now', 'localtime') WHERE id = ?",
                params + [report_id],
            )
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    return _ok(_serialize(row))


@router.delete("/{report_id}")
def delete_report(report_id: int):
    """删除报告（草稿直接删除，不回收站）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
        if not row:
            return _fail(f"报告不存在（id={report_id}）")
        conn.execute("DELETE FROM reports WHERE id = ?", (report_id,))
    return _ok({"deleted": report_id})
