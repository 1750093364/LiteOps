# -*- coding: utf-8 -*-
"""设置模块路由（卡08：需求排序权重；卡13：埋点日志导出）。

权重存 settings 表 key='sort_weights'（JSON）：
  w_urgency 紧急性权重（高3/中2/低1）、w_deadline 截止权重（逾期4/24h内3/3天内2/其他1）、
  w_requester 业务方权重（固定 1.5 分），各权重取值 0~1。
埋点导出：GET /events/export 输出 event_logs 全量 JSON（本地行为日志，PRD 第 7 章）。
"""
import json
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.config import DEMO_MODE
from app.db import get_conn
from app.services import ticket_service as ts

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _ok(data=None):
    return {"code": 0, "data": data}


def _fail(msg: str, code: int = 1):
    return JSONResponse(status_code=200, content={"code": code, "msg": msg})


class WeightsBody(BaseModel):
    w_urgency: float
    w_deadline: float
    w_requester: float


@router.get("")
def get_all_settings():
    """全部设置（排序权重 + 演示模式标识）。"""
    with get_conn() as conn:
        weights = ts.load_weights(conn)
    return _ok({"sort_weights": weights, "demo_mode": DEMO_MODE})


@router.get("/sort-weights")
def get_sort_weights():
    with get_conn() as conn:
        weights = ts.load_weights(conn)
    return _ok({"weights": weights, "defaults": dict(ts.DEFAULT_WEIGHTS)})


@router.put("/sort-weights")
def update_sort_weights(body: WeightsBody):
    """修改排序权重（每项 0~1）；保存后看板按新权重实时重排。"""
    try:
        with get_conn() as conn:
            weights = ts.save_weights(conn, body.model_dump())
    except ValueError as e:
        return _fail(str(e))
    return _ok({"weights": weights})


@router.get("/events/export")
def export_events():
    """导出埋点日志（event_logs 全量 JSON 下载）：本地行为日志，含事件汇总。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, event, params_json, created_at FROM event_logs ORDER BY id"
        ).fetchall()
        counts = conn.execute(
            "SELECT event, COUNT(*) AS cnt FROM event_logs GROUP BY event ORDER BY cnt DESC"
        ).fetchall()

    events = [
        {
            "id": r["id"],
            "event": r["event"],
            "params": json.loads(r["params_json"]) if r["params_json"] else {},
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    payload = {
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "product": "轻析 LiteOps v1.0",
        "total_cnt": len(events),
        "event_summary": {r["event"]: r["cnt"] for r in counts},
        "events": events,
    }
    filename = f"liteops_events_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{filename}"},
    )
