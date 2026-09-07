# -*- coding: utf-8 -*-
"""卡05 自测脚本：清洗预设 CRUD / 套用 / 变更统计 / 回滚 / 埋点。

前置：服务已启动（http://127.0.0.1:8000），库中已有 sales_orders_50k.csv 与
activity_small.xlsx（卡03/04 自测遗留数据）。仅用标准库 urllib + sqlite3。
"""
import json
import sqlite3
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"
DB = "data/liteops.db"

passed = 0
failed = []


def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed.append(name)
        print(f"[FAIL] {name}  {detail}")


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"code": e.code, "msg": f"HTTP {e.code}"}


def db_rows(sql, args=()):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ---------- 0. 健康检查 ----------
r = call("GET", "/api/health")
check("健康检查", r.get("code") == 0, str(r))

# ---------- 1. 内置预设 ----------
r = call("GET", "/api/clean/presets")
check("预设列表 code=0", r.get("code") == 0, str(r))
presets = r["data"]["presets"]
builtin = [p for p in presets if p["is_builtin"] == 1]
check("内置预设共 2 条", len(builtin) == 2, str([p["name"] for p in presets]))
names = {p["name"] for p in builtin}
check("内置预设名称正确",
      "活动订单数据标准清洗" in names and "用户数据基础清洗" in names, str(names))
p_order = next((p for p in builtin if p["name"] == "活动订单数据标准清洗"), None)
p_user = next((p for p in builtin if p["name"] == "用户数据基础清洗"), None)
if p_order and p_user:
    check("订单预设 5 个动作", p_order["actions_cnt"] == 5, str(p_order["actions_cnt"]))
    check("订单预设动作链正确",
          [a["type"] for a in p_order["actions"]] ==
          ["drop_duplicates", "normalize_date", "convert_type", "fill_missing", "handle_anomaly"],
          str(p_order["actions"]))
    check("用户预设 3 个动作", p_user["actions_cnt"] == 3, str(p_user["actions_cnt"]))
    check("用户预设含 __all_text__ 符号",
          p_user["actions"][0]["params"].get("field") == "__all_text__", str(p_user["actions"][0]))
else:
    check("内置预设存在", False, "缺少内置预设")

# ---------- 2. 自建预设 CRUD ----------
r = call("POST", "/api/clean/presets", {
    "name": "卡05测试预设",
    "scenario": "自测用：修剪全部文本字段后全列去重",
    "actions": [
        {"type": "strip_text", "params": {"field": "__all_text__"}},
        {"type": "drop_duplicates", "params": {"columns": None}},
    ],
})
check("新建自建预设 code=0", r.get("code") == 0, str(r))
custom_id = r["data"]["preset_id"] if r.get("code") == 0 else None

r = call("POST", "/api/clean/presets", {"name": "卡05测试预设", "scenario": "", "actions": [
    {"type": "strip_text", "params": {"field": "name"}}]})
check("同名自建预设被拒绝", r.get("code") != 0, str(r))

r = call("POST", "/api/clean/presets", {"name": "空动作预设", "scenario": "", "actions": []})
check("空动作预设被拒绝", r.get("code") != 0, str(r))

r = call("GET", "/api/clean/presets")
got = next((p for p in r["data"]["presets"] if p["id"] == custom_id), None)
check("自建预设出现在列表", got is not None)
check("自建预设 actions_cnt=2", got and got["actions_cnt"] == 2, str(got))

r = call("PUT", f"/api/clean/presets/{custom_id}", {
    "name": "卡05测试预设v2", "scenario": "改名后场景"})
check("PUT 更新自建预设 code=0", r.get("code") == 0, str(r))

r = call("PUT", f"/api/clean/presets/{p_order['id']}", {"name": "试图改内置"})
check("内置预设不可修改", r.get("code") != 0, str(r))

r = call("DELETE", f"/api/clean/presets/{p_order['id']}")
check("内置预设不可删除", r.get("code") != 0, str(r))

# ---------- 3. 套用预设（符号展开 + 埋点） ----------
# 文件 23 = sales_orders_50k.csv（全列：order_id/user_id/order_date/amount/age/city/channel）
r = call("POST", f"/api/clean/presets/{p_order['id']}/apply", {"file_id": 23})
check("套用订单预设 code=0", r.get("code") == 0, str(r))
check("订单预设套用后 5 个动作",
      r.get("code") == 0 and len(r["data"]["actions"]) == 5, str(r.get("data", {}).get("actions")))
check("订单预设 is_builtin=1 返回", r.get("code") == 0 and r["data"]["is_builtin"] == 1)
order_actions = r["data"]["actions"] if r.get("code") == 0 else []

r = call("POST", f"/api/clean/presets/{custom_id}/apply", {"file_id": 23})
check("套用自建预设 code=0", r.get("code") == 0, str(r))
if r.get("code") == 0:
    strip_fields = [a["params"]["field"] for a in r["data"]["actions"] if a["type"] == "strip_text"]
    check("__all_text__ 展开为文本字段（city/channel 等）",
          len(strip_fields) >= 2 and "__all_text__" not in strip_fields, str(strip_fields))
    check("展开后动作数 = 文本字段数 + 1", len(r["data"]["actions"]) == len(strip_fields) + 1,
          str(len(r["data"]["actions"])))

r = call("POST", f"/api/clean/presets/{p_order['id']}/apply", {"file_id": 99999})
check("套用到不存在文件被拒绝", r.get("code") != 0, str(r))
r = call("POST", "/api/clean/presets/99999/apply", {"file_id": 23})
check("套用不存在预设被拒绝", r.get("code") != 0, str(r))

# ---------- 4. 执行订单预设 → 变更统计 ----------
fields = db_rows("SELECT field_name FROM data_fields WHERE file_id = 23 ORDER BY id")
col_names = [f["field_name"] for f in fields]
check("文件 23 字段含 order_date/amount/channel/age",
      all(c in col_names for c in ("order_date", "amount", "channel", "age")), str(col_names))

r = call("POST", "/api/clean/execute", {
    "file_id": 23, "preset_id": p_order["id"], "actions": order_actions})
check("执行订单预设 code=0", r.get("code") == 0, str(r))
stats = r.get("data", {}).get("stats", {}) if r.get("code") == 0 else {}
print("       stats =", json.dumps(stats, ensure_ascii=False))
check("去重约 200 行（180~220）", 180 <= stats.get("rows_dropped_dedup", -1) <= 220,
      str(stats.get("rows_dropped_dedup")))
check("千分位转换为 1 个字段 amount",
      stats.get("fields_converted") == ["amount"] and stats.get("fields_converted_cnt") == 1,
      str(stats.get("fields_converted")))
check("channel 缺失填充约 2500 处（2000~3000）",
      2000 <= stats.get("missing_filled", -1) <= 3000, str(stats.get("missing_filled")))
check("age 异常剔除 >400 行", stats.get("rows_dropped_anomaly", 0) > 400,
      str(stats.get("rows_dropped_anomaly")))
check("总行数 50000 → 约 49295",
      stats.get("total_rows_before") == 50000 and 49200 <= stats.get("total_rows_after", 0) <= 49400,
      f"{stats.get('total_rows_before')}→{stats.get('total_rows_after')}")
check("strip_count=0（本流程无修剪）", stats.get("strip_count") == 0, str(stats.get("strip_count")))
record_id = r.get("data", {}).get("record_id") if r.get("code") == 0 else None
out_file_id = r.get("data", {}).get("output_file_id") if r.get("code") == 0 else None

# ---------- 5. 产物内容抽查（channel 已填充、amount 无千分位） ----------
conn = sqlite3.connect(DB)
out_path = conn.execute("SELECT path FROM data_files WHERE id = ?", (out_file_id,)).fetchone()[0]
conn.close()
import csv as _csv
with open(out_path, encoding="utf-8-sig", newline="") as f:
    rows = list(_csv.DictReader(f))
    header = list(rows[0].keys())
empty_channel = sum(1 for row in rows if row["channel"].strip() in ("", "NA", "na", "null", "--", "-"))
check("产物中 channel 无缺失", empty_channel == 0, str(empty_channel))
check("产物中 amount 无千分位", all("," not in row["amount"] for row in rows[:2000]))
check("产物行数与统计一致", len(rows) == stats.get("total_rows_after"),
      f"{len(rows)} vs {stats.get('total_rows_after')}")

# ---------- 6. 执行用户预设（xlsx：strip 展开 + phone 标记 + name 缺失删行） ----------
fields25 = [f["field_name"] for f in db_rows(
    "SELECT field_name FROM data_fields WHERE file_id = 25 ORDER BY id")]
r = call("POST", f"/api/clean/presets/{p_user['id']}/apply", {"file_id": 25})
check("套用用户预设 code=0", r.get("code") == 0, str(r))
user_actions = r["data"]["actions"] if r.get("code") == 0 else []
r2 = call("POST", "/api/clean/execute", {
    "file_id": 25, "preset_id": p_user["id"], "actions": user_actions})
check("执行用户预设 code=0", r2.get("code") == 0, str(r2))
st2 = r2.get("data", {}).get("stats", {}) if r2.get("code") == 0 else {}
print("       stats2 =", json.dumps(st2, ensure_ascii=False))
check("用户预设 strip_count>0（city 首尾空格）", st2.get("strip_count", 0) > 0, str(st2.get("strip_count")))
check("用户预设 mark 模式不删行（rows_dropped_anomaly=0）",
      st2.get("rows_dropped_anomaly") == 0, str(st2.get("rows_dropped_anomaly")))
check("用户预设新增 is_anomaly 列（cols_after=cols_before+1）",
      st2.get("cols_after") == st2.get("cols_before") + 1,
      f"{st2.get('cols_before')}→{st2.get('cols_after')}")
user_record_id = r2.get("data", {}).get("record_id") if r2.get("code") == 0 else None

# ---------- 7. 历史记录列表 ----------
r = call("GET", "/api/clean/records?file_id=23")
check("记录列表 code=0", r.get("code") == 0, str(r))
recs = r["data"]["records"] if r.get("code") == 0 else []
mine = next((x for x in recs if x["id"] == record_id), None)
check("新记录在列表中且带预设名", mine is not None and mine.get("preset_name") == "活动订单数据标准清洗",
      str(mine and mine.get("preset_name")))
check("记录含产物 id 与统计", mine is not None and mine.get("output_file_id") == out_file_id
      and mine.get("stats", {}).get("missing_filled", 0) > 0)
r = call("GET", "/api/clean/records?file_id=25")
recs25 = r["data"]["records"] if r.get("code") == 0 else []
mine25 = next((x for x in recs25 if x["id"] == user_record_id), None)
check("xlsx 记录带用户预设名", mine25 is not None
      and mine25.get("preset_name") == "用户数据基础清洗", str(mine25 and mine25.get("preset_name")))

# ---------- 8. 回滚 ----------
r = call("POST", f"/api/clean/records/{record_id}/rollback")
check("回滚订单预设记录 code=0", r.get("code") == 0, str(r))
r = call("GET", "/api/clean/records?file_id=23")
check("回滚后记录消失", all(x["id"] != record_id for x in r["data"]["records"]))
status23 = db_rows("SELECT status FROM data_files WHERE id = 23")[0]["status"]
check("原文件 23 状态恢复为「体检完成」", status23 == "体检完成", status23)
status_out = db_rows("SELECT count(*) AS n FROM data_files WHERE id = ?", (out_file_id,))[0]["n"]
check("产物 data_files 记录已删除", status_out == 0)

r = call("POST", f"/api/clean/records/{user_record_id}/rollback")
check("回滚用户预设记录 code=0", r.get("code") == 0, str(r))

# ---------- 9. 删除自建预设 ----------
r = call("DELETE", f"/api/clean/presets/{custom_id}")
check("删除自建预设 code=0", r.get("code") == 0, str(r))
r = call("GET", "/api/clean/presets")
check("删除后列表中不存在", all(p["id"] != custom_id for p in r["data"]["presets"]))

# ---------- 10. 埋点核对 ----------
ev = {r["event"]: r for r in db_rows(
    "SELECT event, count(*) AS n FROM event_logs GROUP BY event")}
check("clean_preset_apply 有记录", ev.get("clean_preset_apply", {}).get("n", 0) >= 3, str(ev.get("clean_preset_apply")))
check("clean_preset_save 有记录", ev.get("clean_preset_save", {}).get("n", 0) >= 1, str(ev.get("clean_preset_save")))
check("clean_execute 新增记录", ev.get("clean_execute", {}).get("n", 0) >= 2, str(ev.get("clean_execute")))
apply_rows = db_rows(
    "SELECT params_json FROM event_logs WHERE event='clean_preset_apply' ORDER BY id DESC LIMIT 4")
apply_params = [json.loads(r["params_json"]) for r in apply_rows]
check("clean_preset_apply 参数含 preset_id/is_builtin/actions_cnt",
      all(("preset_id" in p and "is_builtin" in p and "actions_cnt" in p) for p in apply_params),
      str(apply_params[:1]))
save_rows = db_rows(
    "SELECT params_json FROM event_logs WHERE event='clean_preset_save' ORDER BY id DESC LIMIT 3")
save_params = [json.loads(r["params_json"]) for r in save_rows]
check("clean_preset_save 参数含 actions_cnt/scenario_tag",
      all(("actions_cnt" in p and "scenario_tag" in p) for p in save_params), str(save_params))
exe_rows = db_rows(
    "SELECT params_json FROM event_logs WHERE event='clean_execute' ORDER BY id DESC LIMIT 2")
exe_params = [json.loads(r["params_json"]) for r in exe_rows]
check("clean_execute 参数含 rows_removed/fields_converted/missing_filled/duration_s",
      all(all(k in p for k in ("rows_removed", "fields_converted", "missing_filled", "duration_s"))
          for p in exe_params), str(exe_params[:1]))
# 订单预设那条埋点（本轮 5 万行执行：actions_cnt=5 且来自预设）
all_exe = [json.loads(r["params_json"]) for r in db_rows(
    "SELECT params_json FROM event_logs WHERE event='clean_execute' ORDER BY id DESC LIMIT 10")]
order_exe = next((p for p in all_exe
                  if p.get("actions_cnt") == 5 and p.get("preset_id") is not None), None)
check("订单预设埋点 fields_converted=1 且 missing_filled≈2500",
      order_exe is not None and order_exe.get("fields_converted") == 1
      and 2000 <= order_exe.get("missing_filled", 0) <= 3000, str(order_exe))

print()
print(f"===== 自测结果：通过 {passed} 项，失败 {len(failed)} 项 =====")
if failed:
    print("失败项：", failed)
    sys.exit(1)
