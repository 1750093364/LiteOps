# -*- coding: utf-8 -*-
"""AI 构架 / 精炼客户端（卡11，全产品风险最高模块）。

设计原则（PRD 6.3 / 8 章 / 9.1）：
- 自带 Key：ai_base_url / ai_api_key / ai_model / offline_mode 存 settings 表（key='ai_config'），
  不内置任何默认 Key；读取时 Key 脱敏返回；离线模式下所有 AI 调用被拦截。
- 数据最小化：请求体只含「字段名 / 引用聚合值 / 用户文本」，绝不包含原始数据行；
  masked_fields（字段名）在拼 prompt 前替换为 [已脱敏字段]。
- 防幻觉硬约束：AI 返回文本中的所有数字（整数/小数/百分比/千分位/日期）与白名单比对
  （白名单 = refs 数值 + 用户原文/主题中出现过的数字），不在白名单的数字用
  <span class="unsourced"> 包裹，前端标红并拦截导出。
- 失败安全：超时（30s）/报错一律抛 AICallError（中文 msg），路由层转 HTTP200 + code!=0，
  前端保留原文可重试；埋点 ai_framework_gen / ai_polish 记录 duration_s/result/unsourced_numbers_cnt。
"""
import html
import json
import logging
import os
import re
import time

from app.config import (
    DEMO_MODE, ENV_AI_KEY, ENV_AI_BASE_URL, ENV_AI_MODEL, env_ai_config_present,
)
from app.db import get_conn

logger = logging.getLogger("liteops.ai")

AI_CONFIG_KEY = "ai_config"
DEFAULT_CONFIG = {
    "ai_base_url": "",
    "ai_api_key": "",
    "ai_model": "",
    "offline_mode": False,
}

MAX_POLISH_CHARS = 20000  # PRD：AI 单次处理文本 ≤ 2 万字，超出提示分章节处理

UNSOURCED_SPAN = '<span class="unsourced" title="无数据来源，请核对或删除">'

# ==================== 数字识别（防幻觉核心） ====================
# token 优先级：中文/斜杠日期 → 千分位数字 → 普通数字（可带 % / ％ / 倍 后缀）
_TOKEN_RE = re.compile(
    r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日?"
    r"|\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    r"|\d+(?:\.\d+)?(?:%|％|倍)?"
)


class AICallError(Exception):
    """AI 调用失败：kind=auth/connection/timeout/rate/status/empty/other，msg 为中文可操作提示。"""

    def __init__(self, msg: str, kind: str = "other"):
        super().__init__(msg)
        self.msg = msg
        self.kind = kind


# ==================== 配置读写（settings 表） ====================

def load_config(conn) -> dict:
    """读取 AI 配置（含明文 Key，仅服务端内部使用）；缺失/损坏回退默认值。

    环境变量优先：当 LITEOPS_AI_KEY/BASE_URL/MODEL 同时存在时（通常由演示模式
    通过 /etc/liteops/liteops.env 注入），Key/地址/模型以环境变量为准，数据库
    中存储的值被忽略；离线模式仍从数据库读取。
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (AI_CONFIG_KEY,)
    ).fetchone()
    out = dict(DEFAULT_CONFIG)
    if row is not None:
        try:
            raw = json.loads(row["value"])
        except (ValueError, TypeError):
            raw = None
        if isinstance(raw, dict):
            if isinstance(raw.get("ai_base_url"), str):
                out["ai_base_url"] = raw["ai_base_url"].strip()
            if isinstance(raw.get("ai_api_key"), str):
                out["ai_api_key"] = raw["ai_api_key"].strip()
            if isinstance(raw.get("ai_model"), str):
                out["ai_model"] = raw["ai_model"].strip()
            if isinstance(raw.get("offline_mode"), bool):
                out["offline_mode"] = raw["offline_mode"]

    # 环境变量优先（覆盖数据库中的 Key/地址/模型）
    if env_ai_config_present():
        out["ai_base_url"] = ENV_AI_BASE_URL
        out["ai_api_key"] = ENV_AI_KEY
        out["ai_model"] = ENV_AI_MODEL
    return out


def save_config(conn, patch: dict) -> dict:
    """合并保存 AI 配置。

    ai_api_key：None=不修改；''=清除 Key；非空=更新为新值。
    返回保存后的完整配置（含明文 Key，仅服务端内部使用）。

    环境变量锁定：当服务端已通过环境变量注入 AI 配置时，Key/地址/模型不允许
    被界面修改（演示模式下前端也会隐藏录入框）；仅离线模式可切换。
    """
    cur = load_config(conn)
    locked = env_ai_config_present()
    if not locked and isinstance(patch.get("ai_base_url"), str):
        cur["ai_base_url"] = patch["ai_base_url"].strip()
    if not locked and isinstance(patch.get("ai_model"), str):
        cur["ai_model"] = patch["ai_model"].strip()
    if isinstance(patch.get("offline_mode"), bool):
        cur["offline_mode"] = patch["offline_mode"]
    if not locked and "ai_api_key" in patch and patch["ai_api_key"] is not None:
        cur["ai_api_key"] = str(patch["ai_api_key"]).strip()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (AI_CONFIG_KEY, json.dumps(cur, ensure_ascii=False)),
    )
    return cur


def mask_key(key: str) -> str:
    """Key 脱敏：保留前 3 位与后 4 位，其余打码；短 Key 全码。"""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:3] + "****" + key[-4:]


def public_config(conn) -> dict:
    """返回给前端的配置：绝不含明文 Key。

    附加 demo_mode（是否演示环境）与 ai_config_locked（AI 配置是否被服务端
    环境变量锁定，锁定时前端隐藏录入框）。
    """
    cfg = load_config(conn)
    locked = env_ai_config_present()
    return {
        "ai_base_url": cfg["ai_base_url"],
        "ai_model": cfg["ai_model"],
        "offline_mode": bool(cfg["offline_mode"]),
        "has_key": bool(cfg["ai_api_key"]),
        "ai_api_key_masked": mask_key(cfg["ai_api_key"]),
        "demo_mode": DEMO_MODE,
        "ai_config_locked": locked,
    }


# ==================== 脱敏（字段名级） ====================

def apply_mask(text: str, masked_fields) -> str:
    """把脱敏字段名替换为 [已脱敏字段]（长名优先，避免短名误替）。"""
    if not text:
        return ""
    for name in sorted({f for f in (masked_fields or []) if f}, key=len, reverse=True):
        text = text.replace(name, "[已脱敏字段]")
    return text


# ==================== 数字白名单与标红 ====================

def _canon_token(tok: str) -> str:
    """数字 token 归一化：日期→纯数字；千分位/百分号/倍→数值串（整数去 .0）。"""
    t = tok.strip()
    if re.search(r"[-/年月]", t) and re.match(r"^\d{4}\b", t):
        return "date:" + re.sub(r"\D", "", t)
    t2 = t.replace(",", "").replace("，", "")
    t2 = re.sub(r"\s*(?:%|％|倍)\s*$", "", t2).strip()
    try:
        f = float(t2)
        return "num:" + (str(int(f)) if f == int(f) else str(round(f, 6)))
    except ValueError:
        return "num:" + t2


def extract_number_set(text: str) -> set:
    """提取文本中全部数字 token 的归一化集合（白名单）。"""
    if not text:
        return set()
    return {_canon_token(m.group(0)) for m in _TOKEN_RE.finditer(text)}


def highlight_unsourced(text: str, whitelist: set) -> str:
    """HTML 转义后，把不在白名单的数字 token 用 span.unsourced 包裹（转义在前，标签在后）。"""
    def repl(m):
        tok = m.group(0)
        if _canon_token(tok) in whitelist:
            return tok
        return UNSOURCED_SPAN + tok + "</span>"
    return _TOKEN_RE.sub(repl, html.escape(text or "", quote=False))


def count_unsourced(rendered_html: str) -> int:
    return rendered_html.count('class="unsourced"')


# ==================== OpenAI 兼容调用 ====================

def _timeout() -> float:
    try:
        return float(os.environ.get("LITEOPS_AI_TIMEOUT", "30"))
    except ValueError:
        return 30.0


def _build_client(cfg: dict):
    """延迟导入 openai，未安装依赖时给出可操作提示。"""
    try:
        from openai import OpenAI
    except ImportError:
        raise AICallError("服务端缺少 openai 依赖，请在运行环境执行 pip install openai", "other")
    return OpenAI(
        base_url=cfg["ai_base_url"].rstrip("/"),
        api_key=cfg["ai_api_key"] or "not-configured",
        timeout=_timeout(),
        max_retries=1,  # 瞬时抖动自动重试一次；硬失败（拒绝/DNS/鉴权）快速反馈，用户可手动重试
    )


def chat(cfg: dict, messages: list, temperature: float = 0.3, max_tokens: int = 2000) -> str:
    """发起最简 chat 请求，返回 assistant 文本；失败统一抛 AICallError（中文提示）。

    请求日志只记录模型与消息体（消息体已做字段脱敏，绝不含原始数据行）。
    """
    logger.info(
        "AI request model=%s timeout=%s messages=%s",
        cfg.get("ai_model"), _timeout(),
        json.dumps(messages, ensure_ascii=False)[:6000],
    )
    client = _build_client(cfg)
    try:
        resp = client.chat.completions.create(
            model=cfg["ai_model"],
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as e:  # openai 异常体系分类映射为中文提示
        ename = type(e).__name__
        if "Timeout" in ename:
            raise AICallError(f"AI 服务响应超时（{_timeout():.0f} 秒），请稍后重试", "timeout")
        if "Authentication" in ename:
            raise AICallError("API Key 无效或未授权（401），请检查 Key 是否正确、是否有该模型权限", "auth")
        if "RateLimit" in ename:
            raise AICallError("触发 AI 服务限流（429），请稍后重试", "rate")
        if "Connection" in ename or "Connect" in ename:
            raise AICallError(f"无法连接 AI 服务地址，请检查 Base URL 与网络：{e}", "connection")
        status = getattr(e, "status_code", None)
        if status:
            raise AICallError(f"AI 服务返回错误（HTTP {status}）：{getattr(e, 'message', e)}", "status")
        raise AICallError(f"AI 调用失败：{e}", "other")
    try:
        content = resp.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        content = None
    if not content or not str(content).strip():
        raise AICallError("AI 返回内容为空，请重试", "empty")
    return str(content).strip()


def test_connection(cfg: dict) -> dict:
    """用当前配置发一条最简请求验证连通性，返回 {ok, msg}。"""
    messages = [
        {"role": "system", "content": "你是连通性测试助手，只需回复 pong。"},
        {"role": "user", "content": "ping"},
    ]
    try:
        chat(cfg, messages, temperature=0, max_tokens=5)
    except AICallError as e:
        return {"ok": False, "msg": e.msg, "kind": e.kind}
    return {"ok": True, "msg": f"连接成功，模型「{cfg['ai_model']}」可用"}


# ==================== JSON 宽容解析（构架输出） ====================

def parse_json_lenient(content: str):
    """LLM JSON 输出多级回退：直解 → 去 ```json 代码块围栏 → 首个 { 到末个 } 截取。"""
    c = (content or "").strip()
    try:
        return json.loads(c)
    except (ValueError, TypeError):
        pass
    c2 = re.sub(r"^```(?:json)?\s*|\s*```$", "", c, flags=re.M).strip()
    try:
        return json.loads(c2)
    except (ValueError, TypeError):
        pass
    start, end = c.find("{"), c.rfind("}")
    if start >= 0 and end > start:
        return json.loads(c[start:end + 1])
    raise ValueError("AI 返回中未找到 JSON 结构")


# ==================== 业务能力 1：构架 ====================

FRAMEWORK_SYS = """你是资深数据分析师的报告框架助手。根据用户给出的报告主题、模板类型与可用字段，生成数据分析报告框架。
严格要求：
1. 输出且仅输出一个 JSON 对象，格式：{"sections":[{"title":"章节标题","points":["写作要点1","写作要点2"]}]}，不要输出任何解释文字、前后缀或 markdown 代码块标记。
2. 章节固定为 6 个，顺序为：背景与目标、指标口径说明、数据表现、归因分析、结论与建议、附录；每章 2~3 条写作要点。
3. 写作要点要结合报告主题与给定字段名，说明该章应分析什么、用哪些字段、建议配什么图表。
4. 严禁编造任何具体数字、百分比、日期、行业均值、排名或外部事实；涉及数字处一律用「【数值】」占位。
5. 标记为 [已脱敏字段] 的字段禁止还原、猜测其真实名称，也不要在要点中提及。
6. 每条写作要点不超过 60 字，语言简洁可执行。"""


def _whitelist_from(topic_or_text: str, fields, refs) -> set:
    """白名单 = 用户原文/主题数字 + 字段名中的数字 + refs 聚合值数字。"""
    wl = extract_number_set(topic_or_text or "")
    for f in (fields or []):
        wl |= extract_number_set(str(f))
    for r in (refs or []):
        wl |= extract_number_set(json.dumps(r, ensure_ascii=False))
        v = r.get("value") if isinstance(r, dict) else None
        if v is not None:
            wl |= extract_number_set(str(v))
    return wl


def build_framework(cfg: dict, topic: str, template_type: str,
                    fields, masked_fields, refs) -> dict:
    """AI 构架：返回 {sections(原文), html(标红后), unsourced_numbers_cnt, duration_s}。"""
    t0 = time.time()
    topic_m = apply_mask(topic, masked_fields)
    fields_m = [apply_mask(str(f), masked_fields) for f in (fields or [])]
    ref_lines = []
    for r in (refs or []):
        if isinstance(r, dict) and r.get("value") is not None:
            ref_lines.append(f"- {r.get('field', '')} {r.get('agg_label', '')} = {r.get('value')}")
    user_msg = (
        f"报告主题：{topic_m}\n"
        f"模板类型：{template_type or '通用数据分析报告'}\n"
        f"可用数据字段（仅字段名，不含任何数据行）：{('、'.join(fields_m)) or '（未提供）'}\n"
        f"已核实的引用数值（可在要点中引用这些数字）：\n{chr(10).join(ref_lines) or '无'}"
    )
    content = chat(cfg, [
        {"role": "system", "content": FRAMEWORK_SYS},
        {"role": "user", "content": user_msg},
    ], temperature=0.4, max_tokens=2000)

    data = parse_json_lenient(content)
    raw_sections = data.get("sections") if isinstance(data, dict) else None
    if not isinstance(raw_sections, list) or not raw_sections:
        raise AICallError("AI 返回的框架结构无法解析，请重试", "empty")

    sections = []
    for s in raw_sections:
        if not isinstance(s, dict):
            continue
        title = str(s.get("title", "")).strip()
        points = [str(p).strip() for p in (s.get("points") or []) if str(p).strip()]
        if title:
            sections.append({"title": title, "points": points})
    if not sections:
        raise AICallError("AI 返回的框架章节为空，请重试", "empty")

    whitelist = _whitelist_from(topic, fields, refs)
    parts = []
    for s in sections:
        parts.append("<h2>" + highlight_unsourced(s["title"], whitelist) + "</h2><ul>")
        for p in s["points"]:
            parts.append("<li>" + highlight_unsourced(p, whitelist) + "</li>")
        parts.append("</ul>")
    rendered = "".join(parts)
    return {
        "sections": sections,
        "html": rendered,
        "unsourced_numbers_cnt": count_unsourced(rendered),
        "duration_s": round(time.time() - t0, 2),
    }


# ==================== 业务能力 2：精炼 ====================

POLISH_SYS = """你是数据分析报告的语言编辑。对用户给出的报告文本进行{mode_desc}改写。
严格要求：
1. 必须完整保留原文中的所有数字、百分比、日期、专有名词、字段名与事实结论，一个都不得遗漏、改写或四舍五入。
2. 严禁新增任何数字、百分比、日期、行业均值、排名或外部事实；只允许优化语言组织与表达结构。
3. 标记为 [已脱敏字段] 的内容保持原样，不得还原或猜测。
4. 直接输出改写后的正文，使用纯文本，段落之间用空行分隔；不要输出 markdown 符号、序号修饰或任何解释说明。"""

MODE_DESC = {
    "professional": "专业化（更书面、更严谨的商务表达）",
    "structured": "结构化（分层分点、逻辑清晰、结论前置）",
}


def polish_text(cfg: dict, text: str, mode: str, fields, masked_fields, refs) -> dict:
    """AI 精炼：返回 {text(原文), html(标红分段), unsourced_numbers_cnt, dropped_numbers, duration_s}。"""
    t0 = time.time()
    text = text or ""
    if len(text) > MAX_POLISH_CHARS:
        raise AICallError(
            f"待精炼文本 {len(text)} 字超出上限 {MAX_POLISH_CHARS} 字，请分章节精炼", "other")
    text_m = apply_mask(text, masked_fields)
    mode_desc = MODE_DESC.get(mode, MODE_DESC["professional"])
    content = chat(cfg, [
        {"role": "system", "content": POLISH_SYS.format(mode_desc=mode_desc)},
        {"role": "user", "content": text_m},
    ], temperature=0.3, max_tokens=4000)

    whitelist = _whitelist_from(text, fields, refs)
    out_set = extract_number_set(content)
    # 原文数字丢失检测：归一化后未在精炼结果中出现的 token
    dropped = []
    seen = set()
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        key = _canon_token(tok)
        if key not in out_set and key not in seen:
            seen.add(key)
            dropped.append(tok.strip())
    paragraphs = [
        "<p>" + highlight_unsourced(line.strip(), whitelist) + "</p>"
        for line in re.split(r"\n+", content) if line.strip()
    ]
    rendered = "".join(paragraphs)
    return {
        "text": content,
        "html": rendered,
        "unsourced_numbers_cnt": count_unsourced(rendered),
        "dropped_numbers": dropped[:20],
        "duration_s": round(time.time() - t0, 2),
    }


# ==================== 离线 / 未配置拦截 ====================

def ensure_ready(cfg: dict):
    """调用前校验：离线模式 / 缺 Key / 缺地址 / 缺模型 → AICallError（前端入口也会禁用）。"""
    if cfg.get("offline_mode"):
        raise AICallError("当前为离线模式，AI 功能已关闭；可到「设置」中关闭离线模式后使用", "offline")
    if not cfg.get("ai_api_key"):
        raise AICallError("尚未配置 API Key，请到「设置 → AI 构架/精炼」填写后使用", "config")
    if not cfg.get("ai_base_url"):
        raise AICallError("尚未配置 AI 服务地址（Base URL），请到「设置」中填写", "config")
    if not cfg.get("ai_model"):
        raise AICallError("尚未配置模型名称，请到「设置」中填写", "config")


def get_ready_config() -> dict:
    """读取配置并做可用性校验，返回可直接调用的 cfg。"""
    with get_conn() as conn:
        cfg = load_config(conn)
    ensure_ready(cfg)
    return cfg
