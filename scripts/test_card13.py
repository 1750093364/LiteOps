# -*- coding: utf-8 -*-
"""卡13 端到端联调自测（PRD 4.1 主链路 HTTP 断言 + 埋点核对）。

用法：.venv\\Scripts\\python.exe scripts\\test_card13.py [BASE_URL]
默认 BASE=http://127.0.0.1:8000（建议指向全新目录 run.bat 启动的空库实例）。
覆盖：导入→体检→套预设清洗（产物回写F1）→片段检索/复制/沉淀→需求单→
框架确认→报告模板/引用卡片→AI无Key禁用→核对清单→导出Word/PDF→埋点16事件导出。
"""
import json
import sys
import time
import uuid
from pathlib import Path

from urllib import request as urlreq
from urllib.error import HTTPError, URLError

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
ROOT = Path(__file__).resolve().parent.parent
CSV_50K = ROOT / "data" / "test" / "sales_orders_50k.csv"
XLSX = ROOT / "data" / "test" / "activity_small.xlsx"

REQUIRED_EVENTS = [
    "file_import", "data_profile_view", "clean_preset_apply", "clean_execute",
    "clean_preset_save", "report_framework_insert", "ai_framework_gen", "ai_polish",
    "report_export", "snippet_search", "snippet_copy", "snippet_save",
    "ticket_create", "ticket_confirm", "doc_upload", "doc_search",
]

ok_cnt = 0
fail_cnt = 0
failures = []


def check(name, cond, detail=""):
    global ok_cnt, fail_cnt
    if cond:
        ok_cnt += 1
        print(f"[PASS] {name} {detail}")
    else:
        fail_cnt += 1
        failures.append(name)
        print(f"[FAIL] {name} {detail}")


def http(method, path, body=None, raw=False, headers=None, timeout=180, data_bytes=None):
    """返回 (status, headers, body_bytes|dict)。"""
    url = BASE + path
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif data_bytes is not None:
        data = data_bytes
    req = urlreq.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urlreq.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            sc, hd = resp.status, dict(resp.headers)
    except HTTPError as e:
        payload = e.read()
        sc, hd = e.code, dict(e.headers)
    if raw:
        return sc, hd, payload
    try:
        return sc, hd, json.loads(payload.decode("utf-8"))
    except Exception:
        return sc, hd, {"_raw": payload[:200].decode("utf-8", "ignore")}


def multipart(path_field, file_path, extra=None):
    """构造 multipart/form-data 请求体。"""
    boundary = "----liteops" + uuid.uuid4().hex
    buf = []
    for k, v in (extra or {}).items():
        buf.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    data = file_path.read_bytes()
    fname = file_path.name
    mime = "text/csv" if fname.endswith(".csv") else (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if fname.endswith(".xlsx") else "text/markdown")
    buf.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{path_field}\"; "
        f"filename=\"{fname}\"\r\nContent-Type: {mime}\r\n\r\n".encode() + data + b"\r\n")
    buf.append(f"--{boundary}--\r\n".encode())
    body = b"".join(buf)
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


# ==================== 0. 健康检查 ====================
sc, _, body = http("GET", "/api/health")
check("健康检查", sc == 200 and body.get("code") == 0)

# ==================== 1. 文件导入（F1） ====================
md_body, md_hdr = multipart("file", CSV_50K)
sc, _, raw_payload = http("POST", "/api/files/upload", raw=True, headers=md_hdr,
                          data_bytes=md_body)
body = json.loads(raw_payload.decode("utf-8"))
check("导入 5万行 CSV", sc == 200 and body.get("code") == 0, str(body.get("data", body))[:120])
fid_csv = body["data"]["id"]
check("CSV 行数=50000", body["data"]["row_count"] == 50000)

md_body, md_hdr = multipart("file", XLSX)
sc, _, raw_payload = http("POST", "/api/files/upload", raw=True, headers=md_hdr,
                          data_bytes=md_body)
body = json.loads(raw_payload.decode("utf-8"))
check("导入 xlsx", sc == 200 and body.get("code") == 0)
fid_xlsx = body["data"]["id"]

sc, _, body = http("GET", "/api/files?page=1&page_size=20")
ids = {it["id"] for it in body["data"]["list"]}
check("F1 列表含两个新文件", fid_csv in ids and fid_xlsx in ids)

# ==================== 2. 数据体检（F2） ====================
sc, _, body = http("POST", f"/api/files/{fid_csv}/profile", body={"columns": None})
check("体检扫描", sc == 200 and body.get("code") == 0,
      str(body.get("data", {}).get("report", {}).get("summary", body.get("msg")))[:120])
rep = body["data"]["report"]
check("体检重复行=200", rep["summary"]["dup_rows"] == 200)
check("体检异常捕获", rep["summary"]["anomaly_cnt"] >= 1000)
sc, _, body = http("GET", f"/api/files/{fid_csv}/profile")
check("体检报告可回看", sc == 200 and body.get("code") == 0 and body["data"].get("exists"))

# ==================== 3. 套预设清洗 + 产物回写 F1 ====================
sc, _, body = http("GET", "/api/clean/presets")
presets = body["data"]["presets"]
builtin = [p for p in presets if p["is_builtin"]]
check("内置预设存在", len(builtin) >= 2, f"builtin={len(builtin)}")
sc, _, body = http("POST", f"/api/clean/presets/{builtin[0]['id']}/apply",
                   body={"file_id": fid_csv})
check("套用内置预设", sc == 200 and body.get("code") == 0,
      f"actions_cnt={len(body['data']['actions']) if body.get('code') == 0 else body.get('msg')}")
actions = body["data"]["actions"]
preset_id = body["data"]["preset_id"]
sc, _, body = http("POST", "/api/clean/execute",
                   body={"file_id": fid_csv, "actions": actions, "preset_id": preset_id})
check("执行清洗", sc == 200 and body.get("code") == 0, str(body)[:150])
out_fid = body["data"]["output_file_id"]
stats = body["data"]["stats"]
check("清洗统计：去重200", stats["rows_dropped_dedup"] == 200)
check("清洗统计：总行数变化", stats["rows_before"] == 50000 and stats["rows_after"] < 50000)
sc, _, body = http("GET", f"/api/files/{out_fid}/preview")
check("产物回写 F1（可预览）", sc == 200 and body.get("code") == 0,
      f"name={body['data'].get('file', {}).get('origin_name') if body.get('code') == 0 else body.get('msg')}")
sc, _, body = http("GET", f"/api/clean/records?file_id={fid_csv}")
check("清洗记录可回看", sc == 200 and body.get("code") == 0
      and any(r.get("output_file_id") == out_fid for r in body["data"]["records"]))

# 保存自建预设（clean_preset_save）
sc, _, body = http("POST", "/api/clean/presets",
                   body={"name": f"卡13联调预设_{uuid.uuid4().hex[:6]}",
                         "scenario": "订单数据标准清洗",
                         "actions": [{"type": "strip_text",
                                      "params": {"field": "city"}}]})
check("保存自建预设", sc == 200 and body.get("code") == 0)

# ==================== 4. 片段库检索 / 复制 / 沉淀（F4） ====================
sc, _, body = http("GET", "/api/snippets?q=%E7%95%99%E5%AD%98")
check("片段检索(留存)", sc == 200 and body.get("code") == 0 and body["data"]["total"] > 0,
      f"total={body['data']['total'] if body.get('code') == 0 else body.get('msg')}")
sid_seed = body["data"]["list"][0]["id"]
sc, _, body = http("POST", f"/api/snippets/{sid_seed}/copy")
check("片段复制(use_count+1)", sc == 200 and body.get("code") == 0)
sc, _, body = http("POST", "/api/snippets",
                   body={"title": "卡13联调片段", "category": "其他", "dialect": "mysql",
                         "sql_body": "SELECT 1;", "note": "E2E", "source": "manual"})
check("沉淀新片段", sc == 200 and body.get("code") == 0)
sid_new = body["data"]["id"] if body.get("code") == 0 else None

# ==================== 5. 需求单 + 框架确认页（F3 需求 List） ====================
sc, _, body = http("POST", "/api/tickets",
                   body={"title": "卡13联调需求", "requester": "市场部",
                         "description": "8月活动效果复盘", "urgency": "高",
                         "deadline": "2026-09-10", "est_hours": 4,
                         "analysis_type": "活动复盘"})
check("新建需求单", sc == 200 and body.get("code") == 0)
tid = body["data"]["id"]
fw_body = {"objective": "评估8月活动ROI", "caliber": "GMV=下单金额去千分位",
           "dimensions": "渠道/城市", "daterange": "2026-08-01 ~ 2026-08-31",
           "deliverable": "复盘报告 Word", "confirm_note": ""}
sc, _, body = http("PUT", f"/api/tickets/{tid}/framework", body=fw_body)
check("保存框架五分区", sc == 200 and body.get("code") == 0)
sc, _, body = http("GET", f"/api/tickets/{tid}/framework")
check("框架确认页数据", sc == 200 and body.get("code") == 0
      and body["data"]["framework"]["objective"] == "评估8月活动ROI")
sc, _, body = http("PUT", f"/api/tickets/{tid}/status", body={"status": "confirmed"})
check("业务方确认框架", sc == 200 and body.get("code") == 0)

# ==================== 6. 报告 + 模板插入 + 引用数据卡片（F3 报告） ====================
sc, _, body = http("GET", "/api/report-templates")
tpls = body["data"]["list"]
check("内置框架模板=7", len(tpls) == 7, f"actual={len(tpls)}")
tpl = tpls[0]
sc, _, body = http("POST", "/api/reports",
                   body={"title": "卡13联调报告", "ticket_id": tid,
                         "template_type": tpl["type"]})
check("新建报告", sc == 200 and body.get("code") == 0)
rid = body["data"]["id"]
sc, _, body = http("POST", "/api/reports/template-inserted",
                   body={"report_id": rid, "template_type": tpl["type"]})
check("模板插入上报", sc == 200 and body.get("code") == 0)
sc, _, body = http("POST", "/api/reports/calc",
                   body={"file_id": out_fid, "field": "amount", "agg": "sum"})
check("引用数据计算 sum(amount)", sc == 200 and body.get("code") == 0
      and body["data"]["value"] > 0,
      f"value={body['data']['value'] if body.get('code') == 0 else body.get('msg')}")
calc_val = body["data"]["value"] if body.get("code") == 0 else 0
content_html = (f"<h1>卡13联调报告</h1><p>8月 GMV 合计 "
                f"<span class=\"ref-card\" data-ref=\"{{&quot;file_id&quot;:{out_fid}}}\">{calc_val:.2f}</span> 元。</p>")
refs = [{"file_id": out_fid, "field": "amount", "agg": "sum", "value": calc_val}]
sc, _, body = http("PUT", f"/api/reports/{rid}",
                   body={"title": "卡13联调报告", "content_html": content_html,
                         "refs_json": json.dumps(refs, ensure_ascii=False)})
check("保存报告正文+引用", sc == 200 and body.get("code") == 0)

# ==================== 7. AI 无 Key：验证禁用与中文提示 ====================
sc, _, body = http("GET", "/api/settings/ai")
check("AI 配置默认无 Key", sc == 200 and body.get("code") == 0 and not body["data"]["has_key"])
sc, _, body = http("POST", "/api/ai/framework",
                   body={"topic": "8月活动复盘", "template_type": tpl["type"],
                         "fields": ["amount", "city"], "masked_fields": [], "refs": []})
check("AI 构架无Key被拦", sc == 200 and body.get("code") != 0 and "Key" in (body.get("msg") or ""),
      f"msg={body.get('msg')}")
sc, _, body = http("POST", "/api/ai/polish", body={"text": "这份报告数据不错。", "mode": "professional"})
check("AI 精炼无Key被拦", sc == 200 and body.get("code") != 0 and "Key" in (body.get("msg") or ""),
      f"msg={body.get('msg')}")

# ==================== 8. 导出 Word / PDF 打印视图 ====================
sc, hd, payload = http("POST", f"/api/reports/{rid}/export/word", raw=True)
check("导出 Word 200", sc == 200 and payload[:4] == b"PK\x03\x04",
      f"size={len(payload)}")
check("Word 文件名头", "attachment" in (hd.get("Content-Disposition") or hd.get("content-disposition") or ""))
sc, hd, payload = http("GET", f"/api/reports/{rid}/print-view", raw=True)
check("打印视图(PDF) 200", sc == 200 and "卡13联调报告".encode("utf-8") in payload
      and "text/html" in (hd.get("Content-Type") or hd.get("content-type") or ""))

# ==================== 9. 文档库上传 / 检索（F5） ====================
doc_path = ROOT / "data" / "test" / "_e2e_doc.md"
doc_path.write_text("# 指标口径说明\n\nGMV = 用户下单金额合计，去千分位。\n", encoding="utf-8")
md_body, md_hdr = multipart("file", doc_path)
sc, _, raw_payload = http("POST", "/api/docs/upload", raw=True, headers=md_hdr,
                          data_bytes=md_body)
body = json.loads(raw_payload.decode("utf-8"))
check("上传文档", sc == 200 and body.get("code") == 0, str(body)[:120])
did = body["data"]["id"] if body.get("code") == 0 else None
sc, _, body = http("GET", "/api/docs/search?q=%E6%8C%87%E6%A0%87%E5%8F%A3%E5%BE%84")
check("文档检索", sc == 200 and body.get("code") == 0 and body["data"]["total"] > 0)

# ==================== 10. 埋点核对：16 事件齐全 + 可导出 ====================
sc, hd, payload = http("GET", "/api/settings/events/export", raw=True)
check("埋点导出 200+attachment", sc == 200
      and "attachment" in (hd.get("Content-Disposition") or hd.get("content-disposition") or ""))
events_doc = json.loads(payload.decode("utf-8"))
summary = events_doc.get("event_summary", {})
missing = [e for e in REQUIRED_EVENTS if summary.get(e, 0) < 1]
check("16 个埋点事件齐全", not missing, f"missing={missing}")
check("埋点导出含汇总与明细", events_doc.get("total_cnt", 0) >= len(REQUIRED_EVENTS)
      and len(events_doc.get("events", [])) == events_doc.get("total_cnt"))
print("    事件汇总:", json.dumps(summary, ensure_ascii=False))

# ==================== 汇总 ====================
print(f"\n==== E2E 联调自测完成：{ok_cnt} 通过 / {fail_cnt} 失败 ====")
if failures:
    print("失败项:", failures)
sys.exit(1 if fail_cnt else 0)
