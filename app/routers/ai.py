# -*- coding: utf-8 -*-
"""AI 构架 / 精炼路由（卡11）。

- 设置：GET/PUT /api/settings/ai（Key 读取脱敏）、POST /api/settings/ai/test（连通性测试）；
- 构架：POST /api/ai/framework（主题 + 模板类型 + 字段名清单 → 6 章节框架 JSON → 标红 HTML）；
- 精炼：POST /api/ai/polish（选段/全文 + 模式 → 保留全部数字的改写 → 标红 HTML）；
- 防幻觉：白名单外数字在返回 HTML 中以 span.unsourced 包裹，返回 unsourced_numbers_cnt；
- 失败安全：离线/未配置/超时/报错一律 HTTP 200 + code!=0 + 中文 msg，前端保留原文可重试；
- 埋点：ai_framework_gen / ai_polish（duration_s, result, unsourced_numbers_cnt）。
"""
import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.db import get_conn
from app.services import ai_client as ai
from app.config import env_ai_config_present

router = APIRouter(prefix="/api/ai", tags=["ai"])
ai_settings_router = APIRouter(prefix="/api/settings", tags=["ai-settings"])


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


# ==================== 请求体 ====================

class AiConfigBody(BaseModel):
    ai_base_url: str | None = None
    ai_api_key: str | None = None   # None=不修改；''=清除；非空=更新
    ai_model: str | None = None
    offline_mode: bool | None = None


class FrameworkBody(BaseModel):
    topic: str = ""
    template_type: str = ""
    fields: list[str] = []           # 仅字段名，不含数据行
    masked_fields: list[str] = []    # 需脱敏的字段名
    refs: list[dict] = []            # 已引用聚合值 [{field, value, agg_label}]


class PolishBody(BaseModel):
    text: str = ""
    mode: str = "professional"       # professional 专业化 / structured 结构化
    fields: list[str] = []
    masked_fields: list[str] = []
    refs: list[dict] = []


# ==================== AI 配置（/api/settings/ai） ====================

@ai_settings_router.get("/ai")
def get_ai_settings():
    """读取 AI 配置（Key 脱敏，绝不明文返回）。"""
    with get_conn() as conn:
        return _ok(ai.public_config(conn))


@ai_settings_router.put("/ai")
def update_ai_settings(body: AiConfigBody):
    """保存 AI 配置；api_key 留空（None）表示不修改，空串表示清除。"""
    patch = body.model_dump(exclude_none=True)
    with get_conn() as conn:
        ai.save_config(conn, patch)
        out = ai.public_config(conn)
    return _ok(out)


@ai_settings_router.post("/ai/test")
def test_ai_settings(body: AiConfigBody | None = None):
    """用当前配置（可被请求体临时覆盖，便于先测后存）发最简 chat 请求，返回成败与具体原因。

    当 AI 配置被服务端环境变量锁定（演示模式）时，不允许临时覆盖 Key/地址/模型，
    仅离线模式可被请求体切换。
    """
    with get_conn() as conn:
        cfg = ai.load_config(conn)
    locked = env_ai_config_present()
    if body is not None:
        patch = body.model_dump(exclude_none=True)
        # 测试页面临时值：Key 传空串视为“不修改”（输入框留空=用已保存的 Key）
        if patch.get("ai_api_key") == "":
            patch.pop("ai_api_key")
        for k, v in patch.items():
            if k in cfg:
                # 锁定模式下 Key/地址/模型不可被请求体覆盖
                if locked and k in ("ai_api_key", "ai_base_url", "ai_model"):
                    continue
                cfg[k] = v
    if cfg.get("offline_mode"):
        return _ok({"ok": False, "msg": "当前为离线模式，AI 功能已关闭；请先关闭离线模式再测试",
                    "kind": "offline"})
    if not cfg.get("ai_api_key"):
        return _ok({"ok": False, "msg": "尚未配置 API Key，请先填写 Key", "kind": "config"})
    if not cfg.get("ai_base_url"):
        return _ok({"ok": False, "msg": "尚未配置 AI 服务地址（Base URL）", "kind": "config"})
    if not cfg.get("ai_model"):
        return _ok({"ok": False, "msg": "尚未配置模型名称", "kind": "config"})
    return _ok(ai.test_connection(cfg))


# ==================== AI 构架 ====================

@router.post("/framework")
def ai_framework(body: FrameworkBody):
    """根据主题 + 模板 + 字段名清单生成 6 章节框架；白名单外数字标红返回。"""
    topic = (body.topic or "").strip()
    if not topic:
        return _fail("请填写报告主题/需求描述后再生成框架")
    try:
        cfg = ai.get_ready_config()
        result = ai.build_framework(
            cfg, topic, (body.template_type or "").strip(),
            body.fields or [], body.masked_fields or [], body.refs or [])
    except ai.AICallError as e:
        with get_conn() as conn:
            _log_event(conn, "ai_framework_gen", {
                "result": "failed", "fail_reason": e.kind,
                "unsourced_numbers_cnt": 0, "duration_s": 0,
                "template_type": body.template_type or "",
            })
        return _fail(e.msg)
    with get_conn() as conn:
        _log_event(conn, "ai_framework_gen", {
            "result": "success",
            "duration_s": result["duration_s"],
            "unsourced_numbers_cnt": result["unsourced_numbers_cnt"],
            "template_type": body.template_type or "",
            "fields_cnt": len(body.fields or []),
            "masked_fields_cnt": len(body.masked_fields or []),
        })
    return _ok(result)


# ==================== AI 精炼 ====================

@router.post("/polish")
def ai_polish(body: PolishBody):
    """对选段/全文做专业化/结构化改写；原文数字白名单校验，外造数字标红、丢失数字告警。"""
    text = (body.text or "").strip()
    if not text:
        return _fail("待精炼的文本为空，请先选中段落或撰写正文")
    if body.mode not in ai.MODE_DESC:
        return _fail(f"不支持的精炼模式「{body.mode}」，可选：专业化 / 结构化")
    try:
        cfg = ai.get_ready_config()
        result = ai.polish_text(
            cfg, text, body.mode,
            body.fields or [], body.masked_fields or [], body.refs or [])
    except ai.AICallError as e:
        with get_conn() as conn:
            _log_event(conn, "ai_polish", {
                "result": "failed", "fail_reason": e.kind,
                "unsourced_numbers_cnt": 0, "duration_s": 0, "mode": body.mode,
            })
        return _fail(e.msg)
    with get_conn() as conn:
        _log_event(conn, "ai_polish", {
            "result": "success",
            "duration_s": result["duration_s"],
            "unsourced_numbers_cnt": result["unsourced_numbers_cnt"],
            "dropped_numbers_cnt": len(result["dropped_numbers"]),
            "mode": body.mode,
            "chars": len(text),
        })
    return _ok(result)
