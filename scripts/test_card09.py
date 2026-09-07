# -*- coding: utf-8 -*-
"""卡09 报告编辑器与框架模板 —— 端到端自测脚本（urllib + sqlite3 标准库）。

用法：.venv\\Scripts\\python.exe scripts\\test_card09.py [base_url]
默认打 http://127.0.0.1:8010（自测实例，勿扰 8000 用户进程）。
测试结束清理：删除本脚本创建的报告/需求单/临时数据文件与相关埋点。
"""
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "liteops.db"
TMP_CSV = ROOT / "data" / "files" / "test_card09_calc.csv"

PASS, FAIL = 0, 0


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
        return resp.status, json.loads(resp.read().decode("utf-8"))


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


print("== 0. 健康检查（HTTP 200 + code=0 统一响应） ==")
status, h = call("GET", "/api/health")
check("health HTTP 200", status == 200)
check("health code=0", h["code"] == 0)

print("== 1. 框架模板：7 个内置模板灌种与章节骨架 ==")
status, r = call("GET", "/api/report-templates")
check("GET /api/report-templates HTTP 200", status == 200)
check("code=0", r["code"] == 0)
tpls = r["data"]["list"]
check("模板共 7 个", r["data"]["total"] == 7 and len(tpls) == 7, r["data"]["total"])
check("type 集合 = 复盘/漏斗/留存/渠道/异常/ab/专题",
      sorted(t["type"] for t in tpls) == sorted(["复盘", "漏斗", "留存", "渠道", "异常", "ab", "专题"]),
      [t["type"] for t in tpls])
SKELETON = ["背景与目标", "指标口径说明", "数据表现", "归因分析", "结论与建议", "附录"]
for t in tpls:
    secs = t["sections"]
    ok_title = [s["title"] for s in secs] == SKELETON
    ok_hint = all(isinstance(s.get("hint"), str) and len(s["hint"]) >= 10 for s in secs)
    check(f"模板「{t['name']}」6 章节且骨架顺序正确", len(secs) == 6 and ok_title,
          [s["title"] for s in secs])
    check(f"模板「{t['name']}」提示语齐全（每章 ≥10 字）", ok_hint)
with db() as conn:
    n = conn.execute("SELECT COUNT(*) AS c FROM report_templates WHERE is_builtin=1").fetchone()["c"]
check("DB report_templates 内置模板 7 行", n == 7, n)

print("== 2. 报告 CRUD ==")
status, r = call("POST", "/api/reports",
                 {"title": "卡09自测-漏斗报告", "template_type": "漏斗"})
check("新建报告（带模板类型）", status == 200 and r["code"] == 0 and r["data"]["status"] == "草稿", r)
rid = r["data"]["id"]
check("新建后正文为空", r["data"]["content_html"] == "")
check("refs 初始为空数组", r["data"]["refs"] == [])

status, r = call("POST", "/api/reports", {"title": "   "})
check("空标题拒绝", status == 200 and r["code"] != 0, r)

status, r = call("GET", "/api/reports")
check("报告列表含新报告", r["code"] == 0 and any(x["id"] == rid for x in r["data"]["list"]))
card = next(x for x in r["data"]["list"] if x["id"] == rid)
check("列表项不带正文但带引用计数", card["content_html"] is None and card["refs_cnt"] == 0)

status, r = call("GET", f"/api/reports/{rid}")
check("报告详情可取", r["code"] == 0 and r["data"]["template_type"] == "漏斗")
created_at_1 = r["data"]["updated_at"]

refs = [{"file_id": 999, "file_name": "demo.csv", "field": "amount",
         "agg": "sum", "agg_label": "合计", "value": 8700.5, "ts": "12:00:00"}]
content = ('<h2>数据表现</h2><p class="template-hint">先总体后细分</p>'
           '<p>GMV 达到 <span class="ref-card" contenteditable="false">8,700.5'
           '（来源：demo.csv · amount 合计）</span></p>')
status, r = call("PUT", f"/api/reports/{rid}",
                 {"content_html": content,
                  "refs_json": json.dumps(refs, ensure_ascii=False)})
check("保存正文与引用 JSON", status == 200 and r["code"] == 0, r)
check("保存后 updated_at 刷新", r["data"]["updated_at"] >= created_at_1)
status, r = call("GET", f"/api/reports/{rid}")
check("刷新后草稿恢复（正文 round-trip 一致）", r["data"]["content_html"] == content)
check("refs_json 解析为完整来源数组",
      r["data"]["refs"] == refs, r["data"]["refs"])

status, r = call("PUT", f"/api/reports/{rid}", {"refs_json": "{bad json"})
check("非法 refs_json 拒绝", r["code"] != 0)
status, r = call("PUT", f"/api/reports/{rid}", {"ticket_id": 999999})
check("关联不存在需求单拒绝", r["code"] != 0)

print("== 3. 模板插入埋点 ==")
status, r = call("POST", "/api/reports/template-inserted",
                 {"report_id": rid, "template_type": "漏斗"})
check("template-inserted 返回成功", status == 200 and r["code"] == 0, r)
with db() as conn:
    ev = conn.execute(
        "SELECT params_json FROM event_logs WHERE event='report_framework_insert' "
        "ORDER BY id DESC LIMIT 1").fetchone()
check("report_framework_insert 埋点已记录",
      ev is not None and json.loads(ev["params_json"])["template_type"] == "漏斗", ev)
status, r = call("GET", f"/api/reports/{rid}")
check("模板类型已回写报告", r["data"]["template_type"] == "漏斗")
status, r = call("POST", "/api/reports/template-inserted",
                 {"report_id": 999999, "template_type": "漏斗"})
check("不存在报告拒绝", r["code"] != 0)

print("== 4. 引用数据计算 /api/reports/calc（pandas 真实计算） ==")
TMP_CSV.write_text(
    'amount,city\r\n"1,200",北京\r\n3000,上海\r\n,北京\r\n4500.5,深圳\r\n',
    encoding="utf-8-sig")
conn = db()
conn.execute(
    """INSERT INTO data_files (origin_name, name, file_type, source, path,
       encoding, row_count, col_count, size_bytes, status)
       VALUES ('test_card09_calc.csv', 'test_card09_calc.csv', 'csv', '本地导入',
       'data/files/test_card09_calc.csv', 'utf-8-sig', 4, 2, 100, '已导入')""")
conn.commit()
fid = conn.execute(
    "SELECT id FROM data_files WHERE origin_name='test_card09_calc.csv'"
).fetchone()["id"]
conn.close()

status, r = call("POST", "/api/reports/calc", {"file_id": fid, "field": "amount", "agg": "count"})
check("count = 4（行数）", r["code"] == 0 and r["data"]["value"] == 4, r)
check("返回来源信息", r["data"]["file_name"] == "test_card09_calc.csv"
      and r["data"]["field"] == "amount" and r["data"]["agg_label"] == "计数", r)
status, r = call("POST", "/api/reports/calc", {"file_id": fid, "field": "amount", "agg": "count_nonnull"})
check("count_nonnull = 3（空串算缺失）", r["code"] == 0 and r["data"]["value"] == 3, r)
status, r = call("POST", "/api/reports/calc", {"file_id": fid, "field": "amount", "agg": "sum"})
check("sum = 8700.5（千分位文本 1,200 正确转换）",
      r["code"] == 0 and abs(r["data"]["value"] - 8700.5) < 1e-9, r)
status, r = call("POST", "/api/reports/calc", {"file_id": fid, "field": "amount", "agg": "mean"})
check("mean = 2900.166667", r["code"] == 0 and abs(r["data"]["value"] - 2900.166667) < 1e-4, r)
status, r = call("POST", "/api/reports/calc", {"file_id": fid, "field": "amount", "agg": "max"})
check("max = 4500.5", r["code"] == 0 and r["data"]["value"] == 4500.5, r)
status, r = call("POST", "/api/reports/calc", {"file_id": fid, "field": "amount", "agg": "min"})
check("min = 1200（整数值无浮点尾巴）", r["code"] == 0 and r["data"]["value"] == 1200
      and isinstance(r["data"]["value"], int), r)
status, r = call("POST", "/api/reports/calc", {"file_id": fid, "field": "city", "agg": "count"})
check("文本字段 count 可用", r["code"] == 0 and r["data"]["value"] == 4, r)
status, r = call("POST", "/api/reports/calc", {"file_id": fid, "field": "city", "agg": "sum"})
check("文本字段 sum 报错不返 0", r["code"] != 0, r)
status, r = call("POST", "/api/reports/calc", {"file_id": fid, "field": "city", "agg": "mean"})
check("文本字段 mean 报错", r["code"] != 0)
status, r = call("POST", "/api/reports/calc", {"file_id": fid, "field": "amount", "agg": "median"})
check("不支持 median", r["code"] != 0)
status, r = call("POST", "/api/reports/calc", {"file_id": 999999, "field": "amount", "agg": "sum"})
check("文件不存在拒绝", r["code"] != 0)
status, r = call("POST", "/api/reports/calc", {"file_id": fid, "field": "not_exist", "agg": "count"})
check("字段不存在拒绝", r["code"] != 0)

print("== 5. 关联需求单（创建→关联→清理） ==")
status, r = call("POST", "/api/tickets", {"title": "卡09自测关联需求", "urgency": "低"})
tid = r["data"]["id"]
status, r = call("PUT", f"/api/reports/{rid}", {"ticket_id": tid})
check("报告关联需求单成功", r["code"] == 0 and r["data"]["ticket_id"] == tid, r)
status, r = call("GET", "/api/reports")
card = next(x for x in r["data"]["list"] if x["id"] == rid)
check("列表返回 ticket_id", card["ticket_id"] == tid)

print("== 6. 删除报告与清理 ==")
status, r = call("DELETE", f"/api/reports/{rid}")
check("删除报告", r["code"] == 0)
status, r = call("GET", f"/api/reports/{rid}")
check("删除后详情 404 语义（code!=0）", r["code"] != 0)
status, r = call("DELETE", f"/api/reports/{rid}")
check("重复删除拒绝", r["code"] != 0)

# ---- 清理测试数据 ----
conn = db()
conn.execute("DELETE FROM reports WHERE id = ?", (rid,))
conn.execute("DELETE FROM tickets WHERE id = ?", (tid,))
conn.execute("DELETE FROM data_files WHERE id = ?", (fid,))
conn.execute("DELETE FROM data_fields WHERE file_id = ?", (fid,))
conn.execute("DELETE FROM event_logs WHERE event='report_framework_insert'")
conn.commit()
conn.close()
TMP_CSV.unlink(missing_ok=True)

print(f"\n===== 结果：PASS {PASS} / FAIL {FAIL} =====")
sys.exit(1 if FAIL else 0)
