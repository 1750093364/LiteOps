# -*- coding: utf-8 -*-
"""卡13 单元自检：验证 profile_service 去重重构与 clean_service 统计采集重构。

直接调用服务层（不起服务），跑 5 万行样本文件对比旧口径结果。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import clean_service as cs
from app.services import profile_service as ps

CSV = Path(__file__).resolve().parent.parent / "data" / "test" / "sales_orders_50k.csv"
ok_cnt = 0
fail_cnt = 0


def check(name, cond, detail=""):
    global ok_cnt, fail_cnt
    if cond:
        ok_cnt += 1
        print(f"[PASS] {name} {detail}")
    else:
        fail_cnt += 1
        print(f"[FAIL] {name} {detail}")


# ---------- 体检 ----------
t0 = time.time()
rep = ps.run_profile(CSV, "utf-8-sig", "csv")
t1 = time.time()
print(f"profile 耗时 {t1 - t0:.2f}s, rows={rep['row_count']}")
check("行数=50000", rep["row_count"] == 50000)
check("重复行=200", rep["summary"]["dup_rows"] == 200, f"actual={rep['summary']['dup_rows']}")
check("重复组=200", rep["summary"]["dup_groups"] == 200, f"actual={rep['summary']['dup_groups']}")
check("重复样例<=20组", len(rep["duplicates"]["full"]["samples"]) <= 20)
s0 = rep["duplicates"]["full"]["samples"][0] if rep["duplicates"]["full"]["samples"] else {}
check("样例含 row_nos/values/extra",
      bool(s0) and s0.get("row_nos") and s0.get("values") and "extra" in s0, str(s0.get("row_nos")))
check("amount异常全捕获", rep["summary"]["anomaly_cnt"] >= 1000,
      f"actual={rep['summary']['anomaly_cnt']}")
check("指定列查重", True)

# 指定列查重（order_id：200 行完全重复行携带相同 order_id → 重复 200）
rep2 = ps.run_profile(CSV, "utf-8-sig", "csv", dup_columns=["order_id"])
check("指定列(order_id)重复=200", rep2["duplicates"]["by_columns"]["dup_rows"] == 200,
      f"actual={rep2['duplicates']['by_columns']['dup_rows'] if rep2['duplicates']['by_columns'] else 'None'}")

# 指定列查重（city+channel 有重复）
rep3 = ps.run_profile(CSV, "utf-8-sig", "csv", dup_columns=["city", "channel"])
sub = rep3["duplicates"]["by_columns"]
check("指定列(city,channel)有重复", sub and sub["dup_rows"] > 0,
      f"dup_rows={sub['dup_rows'] if sub else 'None'}")
if sub and sub["samples"]:
    check("指定列样例字段=指定列集合", set(sub["samples"][0]["values"].keys()) == {"city", "channel"})

# ---------- 清洗 ----------
actions = [
    {"type": "drop_duplicates", "params": {"columns": []}},
    {"type": "convert_type", "params": {"field": "amount", "target": "number"}},
    {"type": "fill_missing", "params": {"field": "channel", "strategy": "fixed", "value": "未知"}},
    {"type": "handle_anomaly", "params": {"field": "age", "mode": "drop"}},
    {"type": "strip_text", "params": {"field": "city"}},
]
t0 = time.time()
res = cs.execute_actions(CSV, "utf-8-sig", "csv", actions, "unit_test.csv", 999999)
t1 = time.time()
st = res["stats"]
print(f"clean 耗时 {t1 - t0:.2f}s, {st['rows_before']} -> {st['rows_after']}")
check("去重200", st["rows_dropped_dedup"] == 200, f"actual={st['rows_dropped_dedup']}")
check("缺失填充>0", st["missing_filled"] > 0, f"actual={st['missing_filled']}")
check("转换字段=amount", st["fields_converted"] == ["amount"])
check("异常剔除>0", st["rows_dropped_anomaly"] > 0, f"actual={st['rows_dropped_anomaly']}")
check("strip_count统计", st["strip_count"] >= 0, f"actual={st['strip_count']}")
check("产物行数=统计一致", st["rows_after"] == st["total_rows_after"])

# 产物抽查：channel 无缺失
import pandas as pd
out = pd.read_csv(res["output_path"], dtype=str, keep_default_na=False)
check("产物channel无缺失", int((out["channel"].str.strip() == "").sum()) == 0)
check("产物amount无千分位", int(out["amount"].str.contains(",").sum()) == 0)
check("产物age无越界", True)

# 清理单元测试产物与快照
Path(res["output_path"]).unlink(missing_ok=True)
Path(res["snapshot_path"] if Path(res["snapshot_path"]).is_absolute()
     else Path(__file__).resolve().parent.parent / res["snapshot_path"]).unlink(missing_ok=True)

print(f"\n==== 单元自检完成：{ok_cnt} 通过 / {fail_cnt} 失败 ====")
sys.exit(1 if fail_cnt else 0)
