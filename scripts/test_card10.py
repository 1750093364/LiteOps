# -*- coding: utf-8 -*-
"""卡10 报告导出 Word/PDF —— 端到端自测脚本（urllib + sqlite3 + python-docx）。

用法：.venv\\Scripts\\python.exe scripts\\test_card10.py [base_url]
默认打 http://127.0.0.1:8010（自测实例，勿扰 8000 用户进程）。
测试结束清理：删除本脚本创建的报告与相关埋点。
"""
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

from docx import Document

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "liteops.db"
TMP_DOCX = ROOT / "data" / "exports" / "test_card10_tmp.docx"

PASS, FAIL = 0, 0

# 与卡09 编辑器产出一致的结构：标题/提示语/段落加粗/引用卡片/列表/表格/代码块/base64 图片
PNG_1PX = ("data:image/png;base64,"
           "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
HINT_TEXT = "本章节请填写核心指标的整体表现，如 GMV、转化率等"
CONTENT = (
    "<h1>数据表现总览</h1>"
    f'<p class="template-hint">{HINT_TEXT}</p>'
    "<p>双11 期间 GMV 达 <b>2,100 万</b>，环比 <b>提升 36%</b>。</p>"
    '<span class="ref-card" contenteditable="false" '
    'data-ref=\'{"file_id":1,"file_name":"sales_orders_50k.csv","field":"amount",'
    '"agg":"sum","agg_label":"合计","value":8700.5}\'>8,700.5（来源：sales_orders_50k.csv · amount 合计）</span>'
    "<h2>渠道拆分</h2>"
    "<ul><li>直营渠道贡献 60%</li><li>分销渠道贡献 40%</li></ul>"
    "<h3>明细数据表</h3>"
    '<table border="1"><tbody>'
    "<tr><th>渠道</th><th>GMV（万元）</th></tr>"
    "<tr><td>直营</td><td>5,220</td></tr>"
    "<tr><td>分销</td><td>3,480</td></tr>"
    "</tbody></table>"
    "<ol><li>核对指标口径</li><li>复核数据来源</li></ol>"
    "<pre class=\"snippet-block\">SELECT channel, SUM(amount) FROM orders GROUP BY channel;</pre>"
    f'<p><img src="{PNG_1PX}" style="max-width:100%;"></p>'
    "<p>结尾段落</p>"
)
REFS = [
    {"file_id": 1, "file_name": "sales_orders_50k.csv", "field": "amount",
     "agg": "sum", "agg_label": "合计", "value": 8700.5},
    {"file_id": 2, "file_name": "sales_orders_50k_cleaned.csv", "field": "order_id",
     "agg": "count", "agg_label": "计数", "value": 49295},
]
TITLE_A = "卡10自测-Word导出报告"
TITLE_B = "卡10自测-AI状态报告"


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")


def call(method, path, payload=None):
    url = BASE + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
        headers = {k.lower(): v for k, v in resp.headers.items()}
        body = None
        if "json" in headers.get("content-type", ""):
            body = json.loads(raw.decode("utf-8"))
        return resp.status, body, headers, raw


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


print("== 0. 健康检查 ==")
status, h, _, _ = call("GET", "/api/health")
check("health HTTP 200 + code=0", status == 200 and h["code"] == 0)

print("== 1. 造数：报告 A（草稿，富文本内容 + 2 条引用） ==")
status, r, _, _ = call("POST", "/api/reports", {"title": TITLE_A})
check("新建报告 A", status == 200 and r["code"] == 0, r)
rid_a = r["data"]["id"]
status, r, _, _ = call("PUT", f"/api/reports/{rid_a}", {
    "content_html": CONTENT,
    "refs_json": json.dumps(REFS, ensure_ascii=False),
})
check("保存正文与引用", status == 200 and r["code"] == 0 and len(r["data"]["refs"]) == 2, r)

print("== 2. POST /api/reports/{id}/export/word（报告 A） ==")
status, body, headers, raw = call("POST", f"/api/reports/{rid_a}/export/word")
check("HTTP 200", status == 200)
check("Content-Type 为 docx MIME",
      "wordprocessingml.document" in headers.get("content-type", ""), headers.get("content-type"))
cd = headers.get("content-disposition", "")
check("Content-Disposition 带 filename*（中文文件名）", "filename*=utf-8''" in cd, cd)
if "filename*=utf-8''" in cd:
    import urllib.parse
    fname = urllib.parse.unquote(cd.split("filename*=utf-8''")[-1])
    check("下载文件名 = 报告标题.docx", fname == TITLE_A + ".docx", fname)
TMP_DOCX.write_bytes(raw)
check("产物为有效 docx（可被 python-docx 打开）", len(raw) > 4000 and raw[:2] == b"PK", len(raw))

doc = Document(str(TMP_DOCX))
texts = [p.text for p in doc.paragraphs]
all_text = "\n".join(texts)
today = datetime.now().strftime("%Y 年 %m 月 %d 日")

print("-- 封面页 --")
check("封面含报告标题（大字号居中）", TITLE_A in texts)
tp = next(p for p in doc.paragraphs if p.text == TITLE_A)
check("标题字号 26pt 加粗", tp.runs and tp.runs[0].font.size.pt == 26 and tp.runs[0].bold)
check("封面含日期", today in texts, today)
check("草稿报告：含人工复核声明（无 AI 也保留）",
      "本报告所有数字结论需人工复核后方可对外交付" in texts)
check("草稿报告：不含 AI 声明", "本报告由 AI 辅助生成" not in all_text)

print("-- 目录页 --")
check("含「目 录」标题", "目  录" in texts)
check("目录收录 H1「数据表现总览」", "数据表现总览" in texts)
check("目录收录 H2「渠道拆分」", "渠道拆分" in texts)
toc_items = [t for t in texts if t == "渠道拆分"]
check("目录条目与正文标题均在（渠道拆分出现 ≥2 次）", len(toc_items) >= 2, len(toc_items))

print("-- 正文与提示语剔除 --")
check("无 template-hint 提示语残留", HINT_TEXT not in all_text)
hs = {(p.style.name, p.text) for p in doc.paragraphs if p.style.name.startswith("Heading")}
check("H1 → Heading 1", ("Heading 1", "数据表现总览") in hs, hs)
check("H2 → Heading 2", ("Heading 2", "渠道拆分") in hs)
check("H3 → Heading 3", ("Heading 3", "明细数据表") in hs)
check("段落加粗保留（b 标签 → bold run）",
      any(r.bold and "2,100 万" in r.text for p in doc.paragraphs for r in p.runs))
check("无序列表 → List Bullet",
      sum(1 for p in doc.paragraphs if p.style.name == "List Bullet") == 2)
check("有序列表手动编号", "1. 核对指标口径" in texts and "2. 复核数据来源" in texts)
check("结尾段落完整", "结尾段落" in texts)

print("-- 表格 --")
check("HTML table → docx table（1 张）", len(doc.tables) == 1, len(doc.tables))
t = doc.tables[0]
check("表格 3 行 2 列", len(t.rows) == 3 and len(t.columns) == 2)
check("表头加粗", t.cell(0, 0).paragraphs[0].runs[0].bold is True)
check("表体数据正确", t.cell(1, 0).text == "直营" and t.cell(2, 1).text == "3,480")
check("表格带边框（Table Grid 样式）", "Grid" in t.style.name, t.style.name)

print("-- 图片 / 引用 / 代码块 --")
check("base64 图片已插入", len(doc.inline_shapes) >= 1, len(doc.inline_shapes))
check("引用卡片转纯文本：数值", "8,700.5" in all_text)
check("引用卡片转纯文本：来源",
      "（来源：sales_orders_50k.csv · amount 合计）" in all_text)
code_par = next((p for p in doc.paragraphs if "SELECT channel" in p.text), None)
check("SQL 代码块保留", code_par is not None)
if code_par:
    check("SQL 等宽字体 Consolas", code_par.runs[0].font.name == "Consolas",
          code_par.runs[0].font.name)

print("-- 页眉页脚 --")
sec = doc.sections[0]
check("页眉为报告名", sec.header.paragraphs[0].text == TITLE_A,
      sec.header.paragraphs[0].text)
foot_xml = sec.footer.paragraphs[0]._p.xml
check("页脚含 PAGE 页码域", "PAGE" in foot_xml)
check("页脚含「第…页」样式", sec.footer.paragraphs[0].text.startswith("第"))

print("== 3. AI 状态报告 B：封面 AI 声明 ==")
status, r, _, _ = call("POST", "/api/reports", {"title": TITLE_B})
rid_b = r["data"]["id"]
with db() as conn:
    conn.execute("UPDATE reports SET status = 'AI 已生成/待校验' WHERE id = ?", (rid_b,))
    conn.commit()
status, body, headers, raw = call("POST", f"/api/reports/{rid_b}/export/word")
TMP_DOCX.write_bytes(raw)
doc_b = Document(str(TMP_DOCX))
texts_b = [p.text for p in doc_b.paragraphs]
check("status 含 ai：封面含 AI 辅助声明",
      "本报告由 AI 辅助生成，所有数字结论需人工复核" in texts_b)

print("== 4. GET /api/reports/{id}/print-view（报告 A） ==")
status, body, headers, raw = call("GET", f"/api/reports/{rid_a}/print-view")
html_doc = raw.decode("utf-8")
check("HTTP 200", status == 200)
check("Content-Type text/html", "text/html" in headers.get("content-type", ""))
check("独立 HTML（DOCTYPE + 内联 CSS）",
      html_doc.startswith("<!DOCTYPE html>") and "<style>" in html_doc)
check("封面标题", TITLE_A in html_doc)
check("封面复核声明", "所有数字结论需人工复核" in html_doc)
check("目录页含 H1/H2", "数据表现总览" in html_doc and "渠道拆分" in html_doc)
check("正文含引用来源", "（来源：sales_orders_50k.csv" in html_doc)
check("正文含 SQL 代码块", "SELECT channel" in html_doc)
check("template-hint 段落已剔除", HINT_TEXT not in html_doc)
check("打印按钮 + window.print()", "window.print()" in html_doc)
check("@media print 隐藏工具栏 + A4", "@media print" in html_doc and "size: A4" in html_doc)

print("== 5. report_export 埋点 ==")
with db() as conn:
    rows = conn.execute(
        "SELECT event, params_json FROM event_logs WHERE event = 'report_export' "
        "AND params_json LIKE ? ORDER BY id", (f'%"report_id": {rid_a},%',)).fetchall()
    evs = {json.loads(r["params_json"])["format"]: json.loads(r["params_json"]) for r in rows}
check("word 与 pdf 两条埋点", "word" in evs and "pdf" in evs, list(evs))
if "word" in evs:
    check("word 埋点 doc_length = 正文字符数", evs["word"]["doc_length"] == len(CONTENT),
          evs["word"])
    check("word 埋点 cited_data_cnt = 2", evs["word"]["cited_data_cnt"] == 2)
if "pdf" in evs:
    check("pdf 埋点 doc_length/cited_data_cnt", 
          evs["pdf"]["doc_length"] == len(CONTENT) and evs["pdf"]["cited_data_cnt"] == 2)

print("== 6. 异常路径 ==")
status, r, _, _ = call("POST", "/api/reports/999999/export/word")
check("导出不存在的报告：HTTP 200 + code!=0", status == 200 and r["code"] != 0, r)
status, r, _, _ = call("GET", "/api/reports/999999/print-view")
check("打印视图不存在：HTTP 200 + code!=0", status == 200 and r["code"] != 0, r)

print("== 清理 ==")
call("DELETE", f"/api/reports/{rid_a}")
call("DELETE", f"/api/reports/{rid_b}")
with db() as conn:
    conn.execute("DELETE FROM event_logs WHERE event = 'report_export' AND params_json LIKE ?",
                 (f'%"report_id": {rid_a},%',))
    conn.execute("DELETE FROM event_logs WHERE event = 'report_export' AND params_json LIKE ?",
                 (f'%"report_id": {rid_b},%',))
    conn.commit()
TMP_DOCX.unlink(missing_ok=True)
print(f"已删除测试报告 {rid_a}/{rid_b} 与埋点")

print(f"\n===== 卡10 自测结果：PASS {PASS} / FAIL {FAIL} =====")
sys.exit(1 if FAIL else 0)
