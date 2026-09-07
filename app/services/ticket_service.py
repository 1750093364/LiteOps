# -*- coding: utf-8 -*-
"""需求清单服务（卡08）。

职责：
  - 排序权重读写（settings 表 key='sort_weights'，JSON），启动种子默认 0.5/0.3/0.2；
  - 需求打分：score = 紧急性分(高3/中2/低1) × w_urgency
              + 截止紧迫度分(逾期4/24h内3/3天内2/其他1) × w_deadline
              + 业务方分(固定1.5) × w_requester；
  - 需求单状态机（待确认/已确认/排期中/进行中/已交付/返工中/已取消）；
  - 框架确认页内容存取（复用 tickets.confirm_note 列存 JSON，不改表结构）。
"""
import json
from datetime import datetime, timedelta

from app.db import get_conn

SORT_WEIGHTS_KEY = "sort_weights"
DEFAULT_WEIGHTS = {"w_urgency": 0.5, "w_deadline": 0.3, "w_requester": 0.2}

URGENCY_SCORE = {"高": 3, "中": 2, "低": 1}
REQUESTER_SCORE = 1.5  # 业务方分固定 1.5（V1.0 不区分需求方权重）

# 状态机（PRD 4.3）：待确认 → 已确认/排期中 → 进行中 → 已交付；
# 分支：返工中（退回后回到进行中）、已取消
STATUS_LABELS = {
    "pending": "待确认",
    "confirmed": "已确认",
    "scheduled": "排期中",
    "in_progress": "进行中",
    "delivered": "已交付",
    "rework": "返工中",
    "canceled": "已取消",
}
STATUS_FLOW = {
    "pending": ["confirmed", "rework", "canceled"],
    "confirmed": ["scheduled", "in_progress", "canceled"],
    "scheduled": ["in_progress", "canceled"],
    "in_progress": ["delivered", "rework"],
    "rework": ["in_progress", "canceled"],
    "delivered": ["rework"],
    "canceled": [],
}
# 进入这些状态必须填写意见（返工必填）
NEED_NOTE_TARGETS = {"rework"}

FRAMEWORK_FIELDS = ("objective", "caliber", "dimensions", "daterange", "deliverable")


# ==================== 排序权重 ====================

def ensure_default_weights() -> None:
    """启动时确保 settings 表有 sort_weights（可重复执行，不覆盖用户已改值）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (SORT_WEIGHTS_KEY,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                (SORT_WEIGHTS_KEY, json.dumps(DEFAULT_WEIGHTS, ensure_ascii=False)),
            )


def load_weights(conn) -> dict:
    """读取权重；缺失或损坏时回退默认值，并逐项校验范围。"""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (SORT_WEIGHTS_KEY,)
    ).fetchone()
    if row is None:
        return dict(DEFAULT_WEIGHTS)
    try:
        raw = json.loads(row["value"])
    except (ValueError, TypeError):
        return dict(DEFAULT_WEIGHTS)
    out = dict(DEFAULT_WEIGHTS)
    if isinstance(raw, dict):
        for k in out:
            v = raw.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1:
                out[k] = round(float(v), 4)
    return out


def save_weights(conn, weights: dict) -> dict:
    """校验并保存权重（每项 0~1）；非法抛 ValueError 由路由层转业务失败。"""
    clean = {}
    for k in DEFAULT_WEIGHTS:
        v = weights.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0 <= v <= 1):
            name = {"w_urgency": "紧急性权重", "w_deadline": "截止权重",
                    "w_requester": "业务方权重"}[k]
            raise ValueError(f"{name}必须是 0~1 之间的数字")
        clean[k] = round(float(v), 4)
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (SORT_WEIGHTS_KEY, json.dumps(clean, ensure_ascii=False)),
    )
    return clean


# ==================== 打分 ====================

def _parse_deadline(s: str) -> datetime | None:
    """解析截止时间：YYYY-MM-DD 视为当天 23:59:59 截止；兼容 YYYY/MM/DD 与带时分。"""
    s = (s or "").strip()
    if not s:
        return None
    norm = s.replace("/", "-")
    dt = None
    try:
        dt = datetime.fromisoformat(norm)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(norm, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if len(norm) == 10:  # 仅日期：当天结束才截止
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt


def urgency_score(urgency: str | None) -> int:
    return URGENCY_SCORE.get(urgency or "", 1)


def deadline_score(deadline: str | None, now: datetime | None = None) -> int:
    """逾期 4 / 24h 内 3 / 3 天内 2 / 其他（含无截止、无法解析）1。"""
    dt = _parse_deadline(deadline or "")
    if dt is None:
        return 1
    now = now or datetime.now()
    if dt < now:
        return 4
    if dt <= now + timedelta(hours=24):
        return 3
    if dt <= now + timedelta(days=3):
        return 2
    return 1


def compute_scores(ticket: dict, weights: dict, now: datetime | None = None) -> dict:
    """返回总分与三项因子分（前端透明展示用）。"""
    u = urgency_score(ticket.get("urgency"))
    d = deadline_score(ticket.get("deadline"), now)
    r = REQUESTER_SCORE
    score = (u * weights["w_urgency"]
             + d * weights["w_deadline"]
             + r * weights["w_requester"])
    return {"score": round(score, 4), "u_score": u, "d_score": d, "r_score": r}


# ==================== 框架确认页（confirm_note 列存 JSON） ====================

def parse_framework(raw) -> dict:
    """confirm_note 列存框架确认页 JSON；空值/旧纯文本兼容为空骨架。"""
    base = {"objective": "", "caliber": "", "dimensions": "",
            "daterange": "", "deliverable": "", "note": "", "history": []}
    if not raw:
        return base
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        base["note"] = str(raw)  # 兼容历史纯文本意见
        return base
    if isinstance(data, dict):
        for k in ("objective", "caliber", "dimensions", "daterange",
                  "deliverable", "note"):
            if data.get(k):
                base[k] = str(data[k])
        if isinstance(data.get("history"), list):
            base["history"] = data["history"]
    return base


def dump_framework(fw: dict) -> str:
    return json.dumps(fw, ensure_ascii=False)


def count_rework(conn, ticket_id: int) -> int:
    """统计该需求单历史返工次数（从 ticket_confirm 埋点计数，不改表结构）。"""
    rows = conn.execute(
        "SELECT params_json FROM event_logs WHERE event = 'ticket_confirm'"
    ).fetchall()
    cnt = 0
    for r in rows:
        try:
            p = json.loads(r["params_json"] or "{}")
        except (ValueError, TypeError):
            continue
        if p.get("ticket_id") == ticket_id and p.get("confirm_result") == "rework":
            cnt += 1
    return cnt
