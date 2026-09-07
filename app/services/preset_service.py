# -*- coding: utf-8 -*-
"""清洗预设服务（卡05）。

职责：
  - 启动时补充 2 条内置预设（按名称判重，可重复执行；内置预设不可删除/修改）；
  - 套用预设时展开符号字段（__all_text__ → 该文件全部文本字段），
    仅在套用环节展开，不修改动作库逻辑本身。
"""
import json

from app.db import get_conn

BUILTIN_PRESETS = [
    {
        "name": "活动订单数据标准清洗",
        "scenario": "适用于活动期订单数据：全列去重 → 订单日期标准化 → 金额转数值（含千分位）"
                    "→ 渠道缺失填充为「未知」→ 年龄异常剔除",
        "actions": [
            {"type": "drop_duplicates", "params": {"columns": None}},
            {"type": "normalize_date", "params": {"field": "order_date", "output_format": "YYYY-MM-DD"}},
            {"type": "convert_type", "params": {"field": "amount", "target": "number"}},
            {"type": "fill_missing", "params": {"field": "channel", "strategy": "fixed", "value": "未知"}},
            {"type": "handle_anomaly", "params": {"field": "age", "mode": "drop", "anomaly_rows": []}},
        ],
    },
    {
        "name": "用户数据基础清洗",
        "scenario": "适用于用户/会员名册：文本字段去首尾空格 → 手机号异常标记（新增 is_anomaly 列）"
                    "→ 姓名缺失行删除",
        "actions": [
            {"type": "strip_text", "params": {"field": "__all_text__"}},
            {"type": "handle_anomaly", "params": {"field": "phone", "mode": "mark", "anomaly_rows": []}},
            {"type": "fill_missing", "params": {"field": "name", "strategy": "drop_row"}},
        ],
    },
]


def ensure_builtin_presets() -> None:
    """启动时按名称补充内置预设（服务可重复启动）。"""
    with get_conn() as conn:
        for p in BUILTIN_PRESETS:
            row = conn.execute(
                "SELECT id FROM clean_presets WHERE name = ? AND is_builtin = 1",
                (p["name"],),
            ).fetchone()
            if row:
                continue
            conn.execute(
                """INSERT INTO clean_presets (name, scenario, actions_json, is_builtin)
                   VALUES (?, ?, ?, 1)""",
                (p["name"], p["scenario"], json.dumps(p["actions"], ensure_ascii=False)),
            )


def expand_text_symbol(conn, file_id: int, actions: list[dict]) -> list[dict]:
    """展开预设中的符号字段：strip_text 的 __all_text__ → 该文件全部文本字段（各生成一个动作）。"""
    out: list[dict] = []
    text_cols: list[str] | None = None
    for a in actions:
        params = a.get("params") or {}
        if a.get("type") == "strip_text" and params.get("field") == "__all_text__":
            if text_cols is None:
                rows = conn.execute(
                    """SELECT field_name FROM data_fields
                       WHERE file_id = ? AND inferred_type LIKE '文本%' ORDER BY id""",
                    (file_id,),
                ).fetchall()
                text_cols = [r["field_name"] for r in rows]
                if not text_cols:
                    # 兜底：无文本类字段时按全部字段展开（strip 无损）
                    rows2 = conn.execute(
                        "SELECT field_name FROM data_fields WHERE file_id = ? ORDER BY id",
                        (file_id,),
                    ).fetchall()
                    text_cols = [r["field_name"] for r in rows2]
            for col in text_cols:
                out.append({"type": "strip_text", "params": {"field": col}})
        else:
            out.append(a)
    return out
