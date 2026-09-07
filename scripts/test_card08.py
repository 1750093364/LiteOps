# -*- coding: utf-8 -*-
"""卡08 需求 List 与排序 —— 端到端自测脚本（urllib + sqlite3 标准库）。

用法：.venv\\Scripts\\python.exe scripts\\test_card08.py [base_url]
默认打 http://127.0.0.1:8010（自测实例，勿扰 8000 用户进程）。
测试结束清理：删除本脚本创建的需求单与相关埋点，权重恢复默认。
"""
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
DB = Path(__file__).resolve().parent.parent / "data" / "liteops.db"

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
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


now = datetime.now()

print("== 1. 健康检查与默认权重 ==")
h = call("GET", "/api/health")
check("health code=0", h["code"] == 0)
w = call("GET", "/api/settings/sort-weights")
check("默认权重 0.5/0.3/0.2",
      w["code"] == 0 and w["data"]["weights"] == {"w_urgency": 0.5, "w_deadline": 0.3, "w_requester": 0.2},
      w)
r = call("PUT", "/api/settings/sort-weights", {"w_urgency": 1.5, "w_deadline": 0.3, "w_requester": 0.2})
check("权重 1.5 越界拒绝", r["code"] != 0)
r = call("PUT", "/api/settings/sort-weights", {"w_urgency": -0.1, "w_deadline": 0.3, "w_requester": 0.2})
check("权重 -0.1 越界拒绝", r["code"] != 0)
r = call("GET", "/api/settings")
check("GET /api/settings 返回 sort_weights", r["code"] == 0 and "sort_weights" in r["data"])

print("== 2. 创建 5 条需求（紧急性/截止时间受控） ==")
specs = [
    # (标题, 紧急性, 截止偏移小时, 是否有截止)
    ("T1-高紧急24h内截止", "高", 10, True),    # u=3 d=3
    ("T2-低紧急已逾期", "低", -48, True),       # u=1 d=4
    ("T3-中紧急3天内截止", "中", 50, True),     # u=2 d=2
    ("T4-高紧急无截止", "高", None, False),     # u=3 d=1
    ("T5-中紧急10天后", "中", 240, True),       # u=2 d=1
]
ids = {}
for title, urg, off, _ in specs:
    body = {"title": title, "requester": "测试业务方", "urgency": urg,
            "description": "卡08自测数据", "est_hours": 8, "analysis_type": "活动复盘"}
    if off is not None:
        body["deadline"] = iso(now + timedelta(hours=off))
    r = call("POST", "/api/tickets", body)
    check(f"创建 {title}", r["code"] == 0 and r["data"]["status"] == "pending", r)
    if r["code"] == 0:
        ids[title[:2]] = r["data"]["id"]

r = call("POST", "/api/tickets", {"title": "  ", "urgency": "高"})
check("空标题拒绝", r["code"] != 0)
r = call("POST", "/api/tickets", {"title": "坏紧急性", "urgency": "特急"})
check("非法紧急性拒绝", r["code"] != 0)
r = call("POST", "/api/tickets", {"title": "负工时", "urgency": "中", "est_hours": -2})
check("负预计工时拒绝", r["code"] != 0)

t1, t2, t3, t4, t5 = (ids["T1"], ids["T2"], ids["T3"], ids["T4"], ids["T5"])

print("== 3. 默认权重排序与手算公式核对 ==")
# score = u*(0.5) + d*(0.3) + 1.5*(0.2)
expected = {
    t1: ("T1", 3, 3, 3 * 0.5 + 3 * 0.3 + 1.5 * 0.2),  # 2.7
    t4: ("T4", 3, 1, 3 * 0.5 + 1 * 0.3 + 1.5 * 0.2),  # 2.1
    t2: ("T2", 1, 4, 1 * 0.5 + 4 * 0.3 + 1.5 * 0.2),  # 2.0
    t3: ("T3", 2, 2, 2 * 0.5 + 2 * 0.3 + 1.5 * 0.2),  # 1.9
    t5: ("T5", 2, 1, 2 * 0.5 + 1 * 0.3 + 1.5 * 0.2),  # 1.6
}
r = call("GET", "/api/tickets/ranked")
check("ranked code=0 且 manual=false", r["code"] == 0 and r["data"]["manual"] is False, r)
order = [t["id"] for t in r["data"]["list"]]
my = [i for i in (t1, t4, t2, t3, t5) if i in order]
check("排序 T1>T4>T2>T3>T5", order[:5] == my, f"实际 {order[:5]}")
by_id = {t["id"]: t for t in r["data"]["list"]}
for tid, (name, u, d, sc) in expected.items():
    t = by_id.get(tid, {})
    check(f"{name} 因子分 u={u}/d={d}", t.get("u_score") == u and t.get("d_score") == d, t)
    check(f"{name} 总分 {round(sc, 4)}", abs(t.get("score", -1) - round(sc, 4)) < 0.01,
          f"实际 {t.get('score')}")
check("业务方因子分固定 1.5", all(by_id[i]["r_score"] == 1.5 for i in expected))

r = call("GET", "/api/tickets?status=pending")
check("状态筛选 pending=5", r["code"] == 0 and r["data"]["total"] == 5, r)
r = call("GET", "/api/tickets?status=bad")
check("非法状态筛选拒绝", r["code"] != 0)

print("== 4. 埋点 ticket_create ==")
conn = db()
ev = conn.execute("SELECT params_json FROM event_logs WHERE event='ticket_create'").fetchall()
create_logs = [json.loads(x["params_json"]) for x in ev]
mine = [e for e in create_logs if e.get("ticket_id") in ids.values()]
check("5 条 ticket_create 埋点", len(mine) == 5, f"实际 {len(mine)}")
check("has_deadline 标记正确",
      any(e["ticket_id"] == t4 and e["has_deadline"] is False for e in mine)
      and all(e["has_deadline"] for e in mine if e["ticket_id"] != t4))

print("== 5. 改权重实时重排 ==")
call("PUT", "/api/settings/sort-weights", {"w_urgency": 1.0, "w_deadline": 0.0, "w_requester": 0.0})
r = call("GET", "/api/tickets/ranked")
order = [t["id"] for t in r["data"]["list"]][:5]
check("权重=紧急性主导：T1,T4(高) > T3,T5(中) > T2(低)",
      order == [t1, t4, t3, t5, t2], f"实际 {order}")
call("PUT", "/api/settings/sort-weights", {"w_urgency": 0.0, "w_deadline": 1.0, "w_requester": 0.0})
r = call("GET", "/api/tickets/ranked")
order = [t["id"] for t in r["data"]["list"]][:5]
check("权重=截止主导：T2(逾期) > T1(24h) > T3(3天) > T4,T5",
      order == [t2, t1, t3, t4, t5], f"实际 {order}")
call("PUT", "/api/settings/sort-weights", {"w_urgency": 0.5, "w_deadline": 0.3, "w_requester": 0.2})
r = call("GET", "/api/tickets/ranked")
order = [t["id"] for t in r["data"]["list"]][:5]
check("恢复默认权重后顺序复原", order == [t1, t4, t2, t3, t5], f"实际 {order}")

print("== 6. 拖拽手动排序持久化 ==")
weird = [t5, t1, t3, t4, t2]
r = call("PUT", "/api/tickets/sort-order", {"ids": weird})
check("sort-order 落库成功", r["code"] == 0 and r["data"]["manual"] is True, r)
r = call("GET", "/api/tickets/ranked")
check("ranked 返回 manual=true", r["data"]["manual"] is True)
check("手动顺序生效（刷新后保持）", [t["id"] for t in r["data"]["list"]][:5] == weird,
      f"实际 {[t['id'] for t in r['data']['list']][:5]}")
r = call("PUT", "/api/tickets/sort-order", {"ids": [999999]})
check("不存在的 id 拒绝", r["code"] != 0)
r = call("PUT", "/api/tickets/sort-order", {"ids": [t1, t1]})
check("重复 id 拒绝", r["code"] != 0)
r = call("PUT", "/api/tickets/sort-order", {"ids": []})
check("空数组恢复自动排序", r["code"] == 0 and r["data"]["manual"] is False)
r = call("GET", "/api/tickets/ranked")
check("恢复后按得分排序", [t["id"] for t in r["data"]["list"]][:5] == [t1, t4, t2, t3, t5])

print("== 7. 状态机全流程 + 返工必填 ==")
r = call("PUT", f"/api/tickets/{t1}/status", {"status": "confirmed"})
check("T1 待确认→已确认", r["code"] == 0 and r["data"]["status"] == "confirmed"
      and r["data"]["framework_confirmed"] == 1, r)
r = call("PUT", f"/api/tickets/{t1}/status", {"status": "delivered"})
check("T1 已确认→已交付（跳跃）拒绝", r["code"] != 0)
r = call("PUT", f"/api/tickets/{t1}/status", {"status": "in_progress"})
check("T1 已确认→进行中", r["code"] == 0 and r["data"]["status"] == "in_progress")
r = call("PUT", f"/api/tickets/{t1}/status", {"status": "delivered"})
check("T1 进行中→已交付", r["code"] == 0 and r["data"]["status"] == "delivered")

r = call("PUT", f"/api/tickets/{t2}/status", {"status": "rework"})
check("T2 退回返工不填意见 → 拒绝", r["code"] != 0)
r = call("PUT", f"/api/tickets/{t2}/status", {"status": "rework", "confirm_note": "口径与周报不一致"})
check("T2 待确认→返工中（带意见）", r["code"] == 0 and r["data"]["status"] == "rework", r)
check("返工意见回写 latest_note", r["data"]["latest_note"] == "口径与周报不一致")
r = call("PUT", f"/api/tickets/{t2}/status", {"status": "in_progress"})
check("T2 返工中→进行中", r["code"] == 0 and r["data"]["status"] == "in_progress")
r = call("PUT", f"/api/tickets/{t2}/status", {"status": "delivered"})
check("T2 进行中→已交付", r["code"] == 0 and r["data"]["status"] == "delivered")
r = call("PUT", f"/api/tickets/{t2}/actual-hours", {"actual_hours": 12.5})
check("回填实际工时 12.5", r["code"] == 0 and r["data"]["actual_hours"] == 12.5)
r = call("PUT", f"/api/tickets/{t2}/actual-hours", {"actual_hours": -3})
check("负实际工时拒绝", r["code"] != 0)
r = call("PUT", f"/api/tickets/{t2}/status", {"status": "rework", "confirm_note": "交付后数字有误"})
check("T2 已交付→返工中", r["code"] == 0 and r["data"]["status"] == "rework")
r = call("PUT", f"/api/tickets/{t2}/status", {"status": "in_progress"})
check("T2 再返工→进行中", r["code"] == 0)

r = call("PUT", f"/api/tickets/{t3}/status", {"status": "canceled"})
check("T3 待确认→已取消", r["code"] == 0 and r["data"]["status"] == "canceled")
r = call("GET", "/api/tickets/ranked")
check("已取消不进看板", t3 not in [x["id"] for x in r["data"]["list"]])
r = call("GET", "/api/tickets?status=canceled")
check("状态筛选可查已取消", r["code"] == 0 and any(x["id"] == t3 for x in r["data"]["list"]))
r = call("PUT", f"/api/tickets/{t3}/status", {"status": "in_progress"})
check("已取消为终态拒绝流转", r["code"] != 0)
r = call("PUT", f"/api/tickets/{t1}/status", {"status": "bad_status"})
check("未知状态拒绝", r["code"] != 0)
r = call("PUT", "/api/tickets/999999/status", {"status": "confirmed"})
check("不存在需求单拒绝", r["code"] != 0)

print("== 8. ticket_confirm 埋点（rework_cnt 累计） ==")
ev = conn.execute("SELECT params_json FROM event_logs WHERE event='ticket_confirm'").fetchall()
conf = [json.loads(x["params_json"]) for x in ev]
t1c = [e for e in conf if e.get("ticket_id") == t1]
t2c = [e for e in conf if e.get("ticket_id") == t2]
check("T1 confirmed 埋点 rework_cnt=0",
      len(t1c) == 1 and t1c[0]["confirm_result"] == "confirmed" and t1c[0]["rework_cnt"] == 0, t1c)
check("T2 两次 rework 埋点 rework_cnt=1/2",
      len([e for e in t2c if e["confirm_result"] == "rework"]) == 2
      and sorted(e["rework_cnt"] for e in t2c if e["confirm_result"] == "rework") == [1, 2], t2c)

print("== 9. 框架确认页 ==")
fw_payload = {
    "objective": "评估大促期间各渠道转化效果",
    "caliber": "转化率=支付成功UV/访问UV，支付口径",
    "dimensions": "渠道、端（H5/APP）、新老客",
    "daterange": "2026-08-20 ~ 2026-09-05",
    "deliverable": "复盘报告 1 份 + 渠道明细表，9月8日前交付",
}
r = call("PUT", f"/api/tickets/{t4}/framework", fw_payload)
check("保存框架五分区", r["code"] == 0 and r["data"]["framework"]["objective"] == fw_payload["objective"], r)
r = call("GET", f"/api/tickets/{t4}/framework")
check("读取框架内容一致", r["code"] == 0 and all(
    r["data"]["framework"][k] == v for k, v in fw_payload.items()), r)
check("框架页带需求元信息", r["data"]["ticket"]["title"].startswith("T4"))
r = call("PUT", f"/api/tickets/{t4}/framework",
         dict(fw_payload, confirm_note="请业务方周五前确认"))
check("框架确认意见写入", r["code"] == 0 and r["data"]["note"] == "请业务方周五前确认")
r = call("GET", f"/api/tickets/{t4}/framework")
check("意见留痕 history 含 note 记录",
      any(h.get("action") == "note" for h in r["data"]["history"]), r["data"]["history"])
r = call("PUT", f"/api/tickets/{t4}/status", {"status": "confirmed"})
check("T4 框架确认通过 → 已确认", r["code"] == 0 and r["data"]["status"] == "confirmed")
r = call("GET", f"/api/tickets/{t4}/framework")
check("确认后 history 含 confirm 记录",
      any(h.get("action") == "confirm" for h in r["data"]["history"]))
r = call("GET", "/api/tickets/999999/framework")
check("不存在需求框架页拒绝", r["code"] != 0)

print("== 10. 编辑需求 ==")
r = call("PUT", f"/api/tickets/{t5}", {"title": "T5-改名后", "urgency": "高", "actual_hours": 3})
check("编辑标题/紧急性/实际工时", r["code"] == 0 and r["data"]["title"] == "T5-改名后"
      and r["data"]["urgency"] == "高" and r["data"]["actual_hours"] == 3, r)
r = call("PUT", f"/api/tickets/{t5}", {"urgency": "爆炸"})
check("编辑非法紧急性拒绝", r["code"] != 0)
r = call("PUT", f"/api/tickets/{t5}", {"title": "  "})
check("编辑空标题拒绝", r["code"] != 0)

print("== 11. 清理测试数据 ==")
all_ids = [t1, t2, t3, t4, t5]
id_set = set(all_ids)
marks = ",".join("?" for _ in all_ids)
with conn:
    conn.execute(f"DELETE FROM tickets WHERE id IN ({marks})", all_ids)
    # 埋点清理：Python 侧解析 params_json 匹配 ticket_id（不依赖 SQLite JSON1）
    ev_rows = conn.execute(
        "SELECT id, params_json FROM event_logs WHERE event IN ('ticket_create','ticket_confirm')"
    ).fetchall()
    ev_del = [r["id"] for r in ev_rows
              if json.loads(r["params_json"] or "{}").get("ticket_id") in id_set]
    if ev_del:
        emarks = ",".join("?" for _ in ev_del)
        conn.execute(f"DELETE FROM event_logs WHERE id IN ({emarks})", ev_del)
left = conn.execute(f"SELECT COUNT(*) AS c FROM tickets WHERE id IN ({marks})", all_ids).fetchone()["c"]
check("测试需求单已删除", left == 0)
# 权重恢复默认
call("PUT", "/api/settings/sort-weights", {"w_urgency": 0.5, "w_deadline": 0.3, "w_requester": 0.2})
conn.close()

print(f"\n===== 卡08 自测结果：{PASS} 通过 / {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
