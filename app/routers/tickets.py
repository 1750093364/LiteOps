# -*- coding: utf-8 -*-
"""需求清单与排序模块路由（卡08）。

- 需求 CRUD：GET /api/tickets（按状态筛选）、POST /api/tickets、PUT /api/tickets/{id}；
- 状态流转：PUT /api/tickets/{id}/status（状态机校验，返工必须填 confirm_note）；
- 实际工时：PUT /api/tickets/{id}/actual-hours；
- 排序：GET /api/tickets/ranked（得分降序，有手动顺序时优先 sort_order）、
  PUT /api/tickets/sort-order（拖拽顺序落库，空数组恢复自动排序）；
- 框架确认页：GET/PUT /api/tickets/{id}/framework（内容存 confirm_note 列 JSON）；
- 埋点：ticket_create（urgency,has_deadline）、ticket_confirm（confirm_result,rework_cnt）。
"""
import json
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.db import get_conn
from app.services import ticket_service as ts

router = APIRouter(prefix="/api/tickets", tags=["tickets"])

URGENCIES = {"高", "中", "低"}
ANALYSIS_TYPES = ["活动复盘", "漏斗/路径", "留存流失", "渠道效果",
                  "异常分析", "A/B 测试", "专题分析"]


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


def _serialize(row, weights: dict) -> dict:
    """行 → 前端字典：解析框架 JSON、附状态中文名与实时得分。"""
    t = dict(row)
    fw = ts.parse_framework(t.pop("confirm_note", None))
    t["framework"] = {k: fw.get(k, "") for k in ts.FRAMEWORK_FIELDS}
    t["latest_note"] = fw.get("note", "")
    t["framework_history"] = fw.get("history", [])
    t["status_label"] = ts.STATUS_LABELS.get(t["status"], t["status"])
    t["has_deadline"] = bool(t.get("deadline"))
    t.update(ts.compute_scores(t, weights))
    return t


# ==================== 请求体 ====================

class TicketCreateBody(BaseModel):
    title: str
    requester: str = ""
    description: str = ""
    deadline: str | None = None
    urgency: str = "中"
    est_hours: float | None = None
    analysis_type: str = ""


class TicketUpdateBody(BaseModel):
    title: str | None = None
    requester: str | None = None
    description: str | None = None
    deadline: str | None = None
    urgency: str | None = None
    est_hours: float | None = None
    analysis_type: str | None = None
    actual_hours: float | None = None


class StatusBody(BaseModel):
    status: str
    confirm_note: str | None = None


class ActualHoursBody(BaseModel):
    actual_hours: float | None = None


class SortOrderBody(BaseModel):
    ids: list[int]


class FrameworkBody(BaseModel):
    objective: str = ""
    caliber: str = ""
    dimensions: str = ""
    daterange: str = ""
    deliverable: str = ""
    confirm_note: str = ""


# ==================== 列表 / 排序 ====================

@router.get("")
def list_tickets(status: str | None = None):
    """需求列表（按录入倒序），可按状态筛选；附实时得分。"""
    if status is not None and status not in ts.STATUS_LABELS:
        return _fail(f"未知状态「{status}」")
    with get_conn() as conn:
        weights = ts.load_weights(conn)
        if status:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tickets ORDER BY id DESC").fetchall()
    return _ok({"list": [_serialize(r, weights) for r in rows],
                "total": len(rows)})


@router.get("/ranked")
def ranked_tickets():
    """看板数据：得分降序；存在手动顺序（sort_order>0）时优先手动顺序。

    已取消需求不参与看板；手动顺序之外的新需求追加在手动列表之后（按得分）。
    """
    with get_conn() as conn:
        weights = ts.load_weights(conn)
        rows = conn.execute(
            "SELECT * FROM tickets WHERE status != 'canceled'"
        ).fetchall()
    items = [_serialize(r, weights) for r in rows]
    manual = any((t.get("sort_order") or 0) > 0 for t in items)
    if manual:
        items.sort(key=lambda t: (0 if (t.get("sort_order") or 0) > 0 else 1,
                                  t.get("sort_order") or 0, -t["score"], t["id"]))
    else:
        items.sort(key=lambda t: (-t["score"], t["id"]))
    return _ok({"list": items, "weights": weights, "manual": manual,
                "analysis_types": ANALYSIS_TYPES})


@router.put("/sort-order")
def set_sort_order(body: SortOrderBody):
    """拖拽顺序落库：ids 按顺序赋 sort_order=1..n，其余清零；空数组=恢复自动排序。"""
    ids = body.ids or []
    if len(ids) != len(set(ids)):
        return _fail("排序数据存在重复需求 id")
    with get_conn() as conn:
        exist = {r["id"] for r in conn.execute("SELECT id FROM tickets").fetchall()}
        for i in ids:
            if i not in exist:
                return _fail(f"需求不存在（id={i}）")
        conn.execute("UPDATE tickets SET sort_order = 0")
        conn.executemany(
            "UPDATE tickets SET sort_order = ? WHERE id = ?",
            [(idx + 1, tid) for idx, tid in enumerate(ids)],
        )
    return _ok({"manual": len(ids) > 0, "count": len(ids)})


# ==================== 新建 / 编辑 ====================

@router.post("")
def create_ticket(body: TicketCreateBody):
    title = (body.title or "").strip()
    if not title:
        return _fail("需求标题不能为空")
    if body.urgency not in URGENCIES:
        return _fail("紧急性仅支持 高 / 中 / 低")
    if body.est_hours is not None and body.est_hours < 0:
        return _fail("预计工时不能为负数")
    deadline = (body.deadline or "").strip() or None
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO tickets
               (title, requester, description, deadline, urgency,
                est_hours, analysis_type, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (title, (body.requester or "").strip(), (body.description or "").strip(),
             deadline, body.urgency, body.est_hours,
             (body.analysis_type or "").strip()),
        )
        tid = cur.lastrowid
        weights = ts.load_weights(conn)
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (tid,)).fetchone()
        t = _serialize(row, weights)
        conn.execute("UPDATE tickets SET score = ? WHERE id = ?", (t["score"], tid))
        _log_event(conn, "ticket_create",
                   {"ticket_id": tid, "urgency": body.urgency,
                    "has_deadline": bool(deadline)})
    return _ok(t)


@router.put("/{ticket_id}")
def update_ticket(ticket_id: int, body: TicketUpdateBody):
    """编辑需求全字段（标题/需求方/描述/截止/紧急性/工时/分析类型/实际工时）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if not row:
            return _fail(f"需求不存在（id={ticket_id}）")
        sets, params = [], []
        if body.title is not None:
            title = body.title.strip()
            if not title:
                return _fail("需求标题不能为空")
            sets.append("title = ?")
            params.append(title)
        if body.requester is not None:
            sets.append("requester = ?")
            params.append(body.requester.strip())
        if body.description is not None:
            sets.append("description = ?")
            params.append(body.description.strip())
        if body.deadline is not None:
            sets.append("deadline = ?")
            params.append(body.deadline.strip() or None)
        if body.urgency is not None:
            if body.urgency not in URGENCIES:
                return _fail("紧急性仅支持 高 / 中 / 低")
            sets.append("urgency = ?")
            params.append(body.urgency)
        if body.est_hours is not None:
            if body.est_hours < 0:
                return _fail("预计工时不能为负数")
            sets.append("est_hours = ?")
            params.append(body.est_hours)
        if body.analysis_type is not None:
            sets.append("analysis_type = ?")
            params.append(body.analysis_type.strip())
        if body.actual_hours is not None:
            if body.actual_hours < 0:
                return _fail("实际工时不能为负数")
            sets.append("actual_hours = ?")
            params.append(body.actual_hours)
        if sets:
            params.append(ticket_id)
            conn.execute(
                f"UPDATE tickets SET {', '.join(sets)} WHERE id = ?", params
            )
        weights = ts.load_weights(conn)
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    return _ok(_serialize(row, weights))


# ==================== 状态流转 ====================

@router.put("/{ticket_id}/status")
def update_status(ticket_id: int, body: StatusBody):
    """状态机流转；进入返工必须填 confirm_note；确认/返工记 ticket_confirm 埋点。"""
    note = (body.confirm_note or "").strip()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if not row:
            return _fail(f"需求不存在（id={ticket_id}）")
        cur_status = row["status"]
        tgt = body.status
        if tgt not in ts.STATUS_LABELS:
            return _fail(f"未知状态「{tgt}」")
        if tgt == cur_status:
            return _fail(f"需求已处于「{ts.STATUS_LABELS[tgt]}」状态")
        if tgt not in ts.STATUS_FLOW.get(cur_status, []):
            return _fail(
                f"不允许从「{ts.STATUS_LABELS[cur_status]}」"
                f"流转到「{ts.STATUS_LABELS[tgt]}」"
            )
        if tgt in ts.NEED_NOTE_TARGETS and not note:
            return _fail("退回返工必须填写意见说明")

        fw = ts.parse_framework(row["confirm_note"])
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sets, params = ["status = ?"], [tgt]
        if tgt == "confirmed":
            sets.append("framework_confirmed = 1")
            fw["history"].append({"at": now, "action": "confirm", "note": note})
        elif tgt == "rework":
            fw["history"].append({"at": now, "action": "rework", "note": note})
            fw["note"] = note
        elif note:
            fw["note"] = note
        sets.append("confirm_note = ?")
        params.append(ts.dump_framework(fw))
        params.append(ticket_id)
        conn.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE id = ?", params)

        if tgt in ("confirmed", "rework"):
            prior = ts.count_rework(conn, ticket_id)
            rework_cnt = prior + 1 if tgt == "rework" else prior
            _log_event(conn, "ticket_confirm", {
                "ticket_id": ticket_id,
                "confirm_result": "confirmed" if tgt == "confirmed" else "rework",
                "rework_cnt": rework_cnt,
            })

        weights = ts.load_weights(conn)
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    return _ok(_serialize(row, weights))


@router.put("/{ticket_id}/actual-hours")
def update_actual_hours(ticket_id: int, body: ActualHoursBody):
    """记录实际工时（交付后回填）。"""
    if body.actual_hours is None or body.actual_hours < 0:
        return _fail("实际工时必须是不小于 0 的数字")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if not row:
            return _fail(f"需求不存在（id={ticket_id}）")
        conn.execute(
            "UPDATE tickets SET actual_hours = ? WHERE id = ?",
            (body.actual_hours, ticket_id),
        )
    return _ok({"id": ticket_id, "actual_hours": body.actual_hours})


# ==================== 框架确认页 ====================

@router.get("/{ticket_id}/framework")
def get_framework(ticket_id: int):
    """框架确认页数据：需求基本信息 + 框架五分区 + 意见历史。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if not row:
            return _fail(f"需求不存在（id={ticket_id}）")
        weights = ts.load_weights(conn)
        t = _serialize(row, weights)
        fw = ts.parse_framework(row["confirm_note"])
    return _ok({
        "ticket": {
            "id": t["id"], "title": t["title"], "requester": t["requester"],
            "urgency": t["urgency"], "deadline": t["deadline"],
            "analysis_type": t["analysis_type"], "est_hours": t["est_hours"],
            "status": t["status"], "status_label": t["status_label"],
            "framework_confirmed": t["framework_confirmed"],
            "created_at": t["created_at"], "score": t["score"],
        },
        "framework": {k: fw.get(k, "") for k in ts.FRAMEWORK_FIELDS},
        "note": fw.get("note", ""),
        "history": fw.get("history", []),
    })


@router.put("/{ticket_id}/framework")
def update_framework(ticket_id: int, body: FrameworkBody):
    """保存框架确认页五分区内容（confirm_note 列存 JSON，不改表结构）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if not row:
            return _fail(f"需求不存在（id={ticket_id}）")
        fw = ts.parse_framework(row["confirm_note"])
        for k in ts.FRAMEWORK_FIELDS:
            fw[k] = (getattr(body, k) or "").strip()
        note = (body.confirm_note or "").strip()
        if note:
            fw["note"] = note
            fw["history"].append({
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "action": "note", "note": note,
            })
        conn.execute(
            "UPDATE tickets SET confirm_note = ? WHERE id = ?",
            (ts.dump_framework(fw), ticket_id),
        )
    return _ok({"framework": {k: fw[k] for k in ts.FRAMEWORK_FIELDS},
                "note": fw["note"]})
