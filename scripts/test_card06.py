# -*- coding: utf-8 -*-
"""卡06 自测脚本：SQL 片段库种子 / 检索筛选分页 / 增删改 / 复制计数 / 埋点。

前置：服务已启动（http://127.0.0.1:8000）。仅用标准库 urllib + sqlite3。
自测产生的自建片段会在末尾删除，库内恢复为 10 条内置种子。
"""
import json
import sqlite3
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


EXPECT_TITLES = [
    "次日留存率", "7日留存矩阵", "漏斗转化率", "分组 TopN", "连续 N 天行为",
    "同比环比", "去重 UV 统计", "累计值（累计求和）", "中位数", "复购率",
]

# ---------- 0. 健康检查 ----------
r = call("GET", "/api/health")
check("健康检查", r.get("code") == 0, str(r))

# ---------- 1. 种子入库 ----------
r = call("GET", "/api/snippets?page=1&page_size=100")
check("片段列表 code=0", r.get("code") == 0, str(r))
items = r["data"]["list"]
total = r["data"]["total"]
builtin = [s for s in items if s["source"] == "builtin"]
check("内置种子共 10 条", len(builtin) == 10 and total >= 10,
      f"builtin={len(builtin)} total={total}")
titles = [s["title"] for s in builtin]
check("10 条种子标题齐全", all(t in titles for t in EXPECT_TITLES), str(titles))
check("种子方言统一 mysql", all(s["dialect"] == "mysql" for s in builtin),
      str({s["title"]: s["dialect"] for s in builtin}))
check("种子均有 SQL 正文", all(len(s["sql_body"].strip()) > 30 for s in builtin))
check("种子均含 @param 参数标注", all("@param" in s["sql_body"] for s in builtin),
      str([s["title"] for s in builtin if "@param" not in s["sql_body"]]))
check("种子均含中文注释（--）", all("--" in s["sql_body"] for s in builtin))
check("种子均有 note 说明", all(bool(s.get("note", "").strip()) for s in builtin))
# 记录内置片段 use_count 基线（浏览器人工验证可能已产生复制），收尾时对比未被本脚本污染
builtin_baseline = {s["id"]: s["use_count"] for s in builtin}
check("种子 use_count 字段有效", all(isinstance(s["use_count"], int) for s in builtin))

# 数据库直查
rows = db_rows("SELECT COUNT(*) AS c FROM snippets WHERE source = 'builtin'")
check("DB 中内置片段 10 条", rows[0]["c"] == 10, str(rows))

# ---------- 2. 分类树计数 ----------
r = call("GET", "/api/snippets/categories")
check("分类树 code=0", r.get("code") == 0, str(r))
cats = {c["name"]: c["cnt"] for c in r["data"]["categories"]}
check("分类树总数=10", r["data"]["total"] == 10, str(r["data"]))
check("分类计数正确（留存3/漏斗1/TopN1/同环比1/去重1/汇总2/其他1）",
      cats.get("留存") == 3 and cats.get("漏斗") == 1 and cats.get("TopN") == 1
      and cats.get("同环比") == 1 and cats.get("去重") == 1
      and cats.get("汇总") == 2 and cats.get("其他") == 1, str(cats))

# ---------- 3. 关键字检索 ----------
r = call("GET", "/api/snippets?q=" + urllib.parse.quote("留存"))
hit_titles = [s["title"] for s in r["data"]["list"]]
check("搜「留存」命中 2 条", r["data"]["total"] == 2, str(hit_titles))
check("搜「留存」命中次日留存/7日留存",
      "次日留存率" in hit_titles and "7日留存矩阵" in hit_titles, str(hit_titles))

r = call("GET", "/api/snippets?q=" + urllib.parse.quote("漏斗"))
check("搜「漏斗」命中 1 条", r["data"]["total"] == 1
      and r["data"]["list"][0]["title"] == "漏斗转化率",
      str([s["title"] for s in r["data"]["list"]]))

r = call("GET", "/api/snippets?q=" + urllib.parse.quote("ROW_NUMBER"))
rn_titles = [s["title"] for s in r["data"]["list"]]
check("搜「ROW_NUMBER」命中分组TopN", "分组 TopN" in rn_titles, str(rn_titles))

r = call("GET", "/api/snippets?q=" + urllib.parse.quote("环比增长"))
check("搜「环比增长」命中同比环比",
      r["data"]["total"] == 1 and r["data"]["list"][0]["title"] == "同比环比",
      str([s["title"] for s in r["data"]["list"]]))

r = call("GET", "/api/snippets?q=" + urllib.parse.quote("PERCENTILE_CONT"))
check("搜「PERCENTILE_CONT」命中中位数",
      r["data"]["total"] >= 1 and r["data"]["list"][0]["title"] == "中位数",
      str([s["title"] for s in r["data"]["list"]]))

r = call("GET", "/api/snippets?q=" + urllib.parse.quote("复购"))
check("搜「复购」命中复购率", r["data"]["total"] == 1
      and r["data"]["list"][0]["title"] == "复购率",
      str([s["title"] for s in r["data"]["list"]]))

r = call("GET", "/api/snippets?q=" + urllib.parse.quote("不存在的关键字xyz"))
check("搜无结果关键字 total=0", r["data"]["total"] == 0, str(r["data"]))

# snippet_search 埋点（全量取，避免被后续检索挤出窗口）
logs = db_rows(
    "SELECT params_json FROM event_logs WHERE event = 'snippet_search'")
search_logs = [json.loads(l["params_json"]) for l in logs]
ret_log = next((l for l in search_logs if l.get("keyword") == "留存"), None)
check("snippet_search 埋点（keyword=留存,result_cnt=2）",
      ret_log is not None and ret_log.get("result_cnt") == 2, str(ret_log))

# ---------- 4. 分类 / 方言筛选 ----------
r = call("GET", "/api/snippets?category=" + urllib.parse.quote("留存"))
cat_titles = [s["title"] for s in r["data"]["list"]]
check("分类筛选「留存」3 条（含复购率）",
      r["data"]["total"] == 3 and "复购率" in cat_titles, str(cat_titles))

r = call("GET", "/api/snippets?category=" + urllib.parse.quote("汇总"))
check("分类筛选「汇总」2 条", r["data"]["total"] == 2,
      str([s["title"] for s in r["data"]["list"]]))

r = call("GET", "/api/snippets?dialect=mysql&page_size=100")
check("方言筛选 mysql 命中 10 条", r["data"]["total"] == 10, str(r["data"]["total"]))

r = call("GET", "/api/snippets?dialect=postgresql")
check("方言筛选 postgresql 为 0 条", r["data"]["total"] == 0, str(r["data"]))

# 关键字 + 分类叠加
r = call("GET", "/api/snippets?q=" + urllib.parse.quote("留存")
         + "&category=" + urllib.parse.quote("留存"))
check("搜「留存」+ 分类「留存」= 2 条", r["data"]["total"] == 2,
      str([s["title"] for s in r["data"]["list"]]))

# ---------- 5. 分页 ----------
r1 = call("GET", "/api/snippets?page=1&page_size=3")
r2 = call("GET", "/api/snippets?page=4&page_size=3")
check("分页 page=1 size=3 返回 3 条", len(r1["data"]["list"]) == 3, str(r1["data"]))
check("分页 total=10 且 page=4 返回 1 条",
      r1["data"]["total"] == 10 and len(r2["data"]["list"]) == 1, str(r2["data"]))

# ---------- 6. 新建片段 ----------
r = call("POST", "/api/snippets", {
    "title": "卡06测试片段_近30天加购未支付",
    "category": "漏斗",
    "dialect": "mysql",
    "sql_body": "-- 卡06自测片段\nSELECT 1; -- @param: 表名 t",
    "note": "自测用，结束删除",
})
check("新建片段 code=0", r.get("code") == 0 and r["data"].get("id"), str(r))
new_id = r.get("data", {}).get("id")

r = call("GET", "/api/snippets?q=" + urllib.parse.quote("卡06测试片段"))
check("新片段可检索到（source=user）", r["data"]["total"] == 1
      and r["data"]["list"][0]["source"] == "user"
      and r["data"]["list"][0]["use_count"] == 0, str(r["data"]))

# 校验
r = call("POST", "/api/snippets", {"title": "  ", "sql_body": "SELECT 1"})
check("空标题被拦截", r.get("code") != 0, str(r))
r = call("POST", "/api/snippets", {"title": "无SQL", "sql_body": "   "})
check("空 SQL 被拦截", r.get("code") != 0, str(r))

# snippet_save 埋点
logs = db_rows(
    "SELECT params_json FROM event_logs WHERE event = 'snippet_save' "
    "ORDER BY id DESC LIMIT 3")
save_logs = [json.loads(l["params_json"]) for l in logs]
check("snippet_save 埋点（source=user,category=漏斗）",
      any(l.get("source") == "user" and l.get("category") == "漏斗"
          for l in save_logs), str(save_logs))

# ---------- 7. 修改片段 ----------
r = call("PUT", f"/api/snippets/{new_id}", {"note": "自测修改后的说明", "category": "其他"})
check("修改自建片段 code=0", r.get("code") == 0, str(r))
r = call("GET", "/api/snippets?q=" + urllib.parse.quote("卡06测试片段"))
check("修改内容已回写",
      r["data"]["list"][0]["note"] == "自测修改后的说明"
      and r["data"]["list"][0]["category"] == "其他", str(r["data"]["list"][0]))

# 内置片段同样允许编辑（探测后立即把 note 恢复为原值）
b_edit = next((s for s in builtin if s["title"] == "中位数"), None)
if b_edit:
    orig_note = b_edit["note"]
    r = call("PUT", f"/api/snippets/{b_edit['id']}", {"note": "内置编辑探测_临时内容"})
    check("内置片段允许编辑", r.get("code") == 0, str(r))
    r = call("GET", "/api/snippets?q=" + urllib.parse.quote("中位数"))
    probe_ok = r["data"]["total"] >= 1 and r["data"]["list"][0]["note"] == "内置编辑探测_临时内容"
    check("内置片段编辑内容已生效", probe_ok, str(r["data"])[:200])
    r = call("PUT", f"/api/snippets/{b_edit['id']}", {"note": orig_note})
    check("内置片段探测后恢复 code=0", r.get("code") == 0, str(r))
    r = call("GET", "/api/snippets?q=" + urllib.parse.quote("中位数"))
    check("内置片段 note 已恢复原值",
          r["data"]["list"][0]["note"] == orig_note, str(r["data"])[:200])
else:
    check("内置片段允许编辑", False, "未找到「中位数」种子")
r = call("PUT", "/api/snippets/99999", {"title": "不存在"})
check("修改不存在片段报错", r.get("code") != 0, str(r))

# ---------- 8. 复制计数 ----------
r = call("POST", f"/api/snippets/{new_id}/copy")
check("复制 code=0 且回传 SQL",
      r.get("code") == 0 and "SELECT 1" in r["data"]["sql_body"]
      and r["data"]["use_count"] == 1, str(r.get("data")))
r = call("POST", f"/api/snippets/{new_id}/copy")
check("再次复制 use_count=2", r.get("code") == 0 and r["data"]["use_count"] == 2, str(r))
db_cnt = db_rows("SELECT use_count FROM snippets WHERE id = ?", (new_id,))
check("DB 中 use_count 已自增为 2", db_cnt and db_cnt[0]["use_count"] == 2, str(db_cnt))

logs = db_rows(
    "SELECT params_json FROM event_logs WHERE event = 'snippet_copy' "
    "ORDER BY id DESC LIMIT 3")
copy_logs = [json.loads(l["params_json"]) for l in logs]
check("snippet_copy 埋点（snippet_id,dialect=mysql）",
      any(l.get("snippet_id") == new_id and l.get("dialect") == "mysql"
          for l in copy_logs), str(copy_logs))

r = call("POST", "/api/snippets/99999/copy")
check("复制不存在片段报错", r.get("code") != 0, str(r))

# ---------- 9. 删除片段 ----------
r = call("DELETE", f"/api/snippets/{new_id}")
check("删除自建片段 code=0", r.get("code") == 0, str(r))
r = call("GET", "/api/snippets?q=" + urllib.parse.quote("卡06测试片段"))
check("删除后检索不到", r["data"]["total"] == 0, str(r["data"]))

# 内置片段同样允许删除（探测后用种子 JSON 原样恢复，source 仍为 builtin）
b_del = next((s for s in builtin if s["title"] == "复购率"), None)
if b_del:
    r = call("DELETE", f"/api/snippets/{b_del['id']}")
    check("内置片段允许删除", r.get("code") == 0, str(r))
    rows = db_rows("SELECT COUNT(*) AS c FROM snippets WHERE id = ?", (b_del["id"],))
    check("内置片段删除后 DB 中不存在", rows[0]["c"] == 0, str(rows))
    with open("app/seeds/snippets.json", encoding="utf-8") as f:
        seeds = json.load(f)
    seed = next(x for x in seeds if x["title"] == "复购率")
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT INTO snippets (title, category, dialect, sql_body, note, source, use_count)"
        " VALUES (?, ?, ?, ?, ?, 'builtin', ?)",
        (seed["title"], seed["category"], seed["dialect"], seed["sql_body"],
         seed["note"], builtin_baseline.get(b_del["id"], 0)))
    conn.commit()
    conn.close()
    r = call("GET", "/api/snippets?q=" + urllib.parse.quote("复购"))
    check("内置片段删除探测后已原样恢复",
          r["data"]["total"] == 1 and r["data"]["list"][0]["source"] == "builtin",
          str(r["data"])[:200])
else:
    check("内置片段允许删除", False, "未找到「复购率」种子")
r = call("DELETE", "/api/snippets/99999")
check("删除不存在片段报错", r.get("code") != 0, str(r))

# 收尾：库内恢复为 10 条内置种子（自建片段均已删除，内置 use_count 与基线一致）
rows = db_rows("SELECT COUNT(*) AS c FROM snippets")
check("收尾：库内片段总数恢复 10 条", rows[0]["c"] == 10, str(rows))
rows = db_rows("SELECT COUNT(*) AS c FROM snippets WHERE source != 'builtin'")
check("收尾：无残留自建片段", rows[0]["c"] == 0, str(rows))
# 注：删除探测恢复的「复购率」是新自增 id，故按标题对比 use_count 基线
base_by_title = {s["title"]: s["use_count"] for s in builtin}
after_by_title = {r["title"]: r["use_count"]
                  for r in db_rows("SELECT title, use_count FROM snippets WHERE source = 'builtin'")}
check("收尾：内置种子 use_count 与基线一致（本脚本只复制自建片段）",
      after_by_title == base_by_title, f"before={base_by_title} after={after_by_title}")

# ---------- 汇总 ----------
print(f"\n===== 卡06 自测结果：{passed} 通过 / {len(failed)} 失败 =====")
if failed:
    print("失败项：", "、".join(failed))
    raise SystemExit(1)
