# -*- coding: utf-8 -*-
"""卡07 自测脚本：学习文档库上传/摘要抽取/分类CRUD/列表筛选/检索/预览/删除/埋点。

前置：服务已启动（http://127.0.0.1:8000）。用 venv 的 python 运行（需 python-docx
与 openpyxl 造测试文件）。测试产生的 5 个文档与分类保留在库中供浏览器验证。
"""
import base64
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
DB = "data/liteops.db"
TMP = os.path.join(os.path.dirname(__file__), "tmp_card07")

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
    req = urllib.request.Request(BASE + urllib.parse.quote(path, safe="/?&=%"), method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"code": e.code, "msg": f"HTTP {e.code}"}


def upload(filepath):
    """multipart 文件上传。"""
    boundary = "----qxcard07boundary"
    with open(filepath, "rb") as f:
        content = f.read()
    name = os.path.basename(filepath)
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(BASE + "/api/docs/upload", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req) as resp:
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


def raw_call(path):
    """GET 原始响应（用于 /raw 检查 Content-Type 与字节）。"""
    try:
        with urllib.request.urlopen(BASE + path) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, "", b""


# ==================== 0. 造测试文件 ====================
os.makedirs(TMP, exist_ok=True)

# docx（python-docx，含中文段落）
from docx import Document

DOCX_MARK = "卡07文档库指标口径说明"
d = Document()
d.add_heading("指标口径说明", level=1)
d.add_paragraph(f"{DOCX_MARK}：活跃用户指当日登录且产生至少一次有效行为的用户。")
d.add_paragraph("复购率 = 下单两次以上用户 / 总下单用户。")
d.save(os.path.join(TMP, "指标口径说明.docx"))

# pdf（手工构造最小 PDF，含可抽取文本，Helvetica 仅支持 ASCII）
PDF_MARK = "QXPDF07UNIQUE"


def build_pdf(text: str) -> bytes:
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
    ]
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()
    return bytes(out)


pdf_bytes = build_pdf(f"{PDF_MARK} LiteOps card07 searchable body text")
pdf_path = os.path.join(TMP, "card07_search.pdf")
with open(pdf_path, "wb") as f:
    f.write(pdf_bytes)
# 本地先验证 pypdf 能抽出标记词（保证测试用 PDF 可读）
from pypdf import PdfReader

_local_text = "".join((p.extract_text() or "") for p in PdfReader(pdf_path).pages)
assert PDF_MARK in _local_text, "测试 PDF 本地抽取失败，脚本用例有问题"

# xlsx（openpyxl，两个 sheet）
from openpyxl import Workbook

XLSX_MARK = "卡07渠道投放数据"
wb = Workbook()
ws1 = wb.active
ws1.title = "订单"
ws1.append(["订单ID", "渠道", "金额"])
ws1.append([1, XLSX_MARK, 199.5])
ws2 = wb.create_sheet("用户")
ws2.append(["用户ID", "昵称"])
ws2.append([1001, "张三"])
xlsx_path = os.path.join(TMP, "渠道投放样例.xlsx")
wb.save(xlsx_path)

# md（标题/列表/代码块 + 中文特有词）
MD_MARK = "青铜峡独有检索词零七"
md_text = (
    "# 内部工具教程\n\n"
    f"这是{MD_MARK}的使用说明文档。\n\n"
    "## 快速开始\n\n"
    "- 第一步：配置数据源\n"
    "- 第二步：导入文件\n"
    "- 第三步：运行体检\n\n"
    "## 示例代码\n\n"
    "```sql\nSELECT * FROM orders WHERE dt = '2026-09-06';\n```\n\n"
    "完。"
)
md_path = os.path.join(TMP, "工具教程.md")
with open(md_path, "w", encoding="utf-8") as f:
    f.write(md_text)

# png（1x1 像素）
png_path = os.path.join(TMP, "占位图.png")
with open(png_path, "wb") as f:
    f.write(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
        "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="))

# 51MB 超限文件（后缀合法，靠大小触发拒绝）
big_path = os.path.join(TMP, "too_big.md")
with open(big_path, "wb") as f:
    f.write(b"\0" * (51 * 1024 * 1024))

# 3000 字长文本（验证 md/txt 摘要截断 2000）
long_path = os.path.join(TMP, "长文本截断.txt")
with open(long_path, "w", encoding="utf-8") as f:
    f.write("长" * 3000)

print("========== 1. 上传与摘要抽取 ==========")
r = call("GET", "/api/health")
check("健康检查", r.get("code") == 0, str(r))

results = {}
for key, path, dtype in [
    ("docx", os.path.join(TMP, "指标口径说明.docx"), "docx"),
    ("pdf", pdf_path, "pdf"),
    ("xlsx", xlsx_path, "xlsx"),
    ("md", md_path, "md"),
    ("png", png_path, "png"),
]:
    r = upload(path)
    results[key] = r.get("data") or {}
    check(f"上传 {key} 成功", r.get("code") == 0 and (r["data"] or {}).get("doc_type") == dtype,
          str(r)[:200])

r = upload(long_path)
check("长文本 txt 上传成功", r.get("code") == 0, str(r)[:150])
check("txt 摘要截断 2000 字", (r.get("data") or {}).get("summary_len") == 2000, str(r)[:150])
long_txt_id = (r.get("data") or {}).get("id")

r = upload(big_path)
check("51MB 文件被拒", r.get("code") != 0 and "50MB" in (r.get("msg") or ""), str(r)[:150])

r = upload(os.path.join(TMP, "no_such_type.exe")) if os.path.exists(
    os.path.join(TMP, "no_such_type.exe")) else {"code": -9}
with open(os.path.join(TMP, "假文件.exe"), "wb") as f:
    f.write(b"MZ fake")
r = upload(os.path.join(TMP, "假文件.exe"))
check(".exe 被拒并提示支持清单", r.get("code") != 0 and "docx" in (r.get("msg") or ""), str(r)[:150])

print("========== 2. 摘要内容校验 ==========")
docx_id, pdf_id = results["docx"].get("id"), results["pdf"].get("id")
xlsx_id, md_id, png_id = results["xlsx"].get("id"), results["md"].get("id"), results["png"].get("id")

r = call("GET", f"/api/docs/{docx_id}/preview")
check("docx 摘要含段落文本", r.get("code") == 0 and DOCX_MARK in (r["data"]["summary"] or ""),
      str(r)[:200])
r = call("GET", f"/api/docs/{pdf_id}/preview")
check("pdf 摘要含前页文本", r.get("code") == 0 and PDF_MARK in (r["data"]["summary"] or ""),
      str(r)[:200])
r = call("GET", f"/api/docs/{xlsx_id}/preview")
s = (r.get("data") or {}).get("summary") or ""
check("xlsx 摘要含 sheet 名", r.get("code") == 0 and "订单" in s and "用户" in s, s[:150])
check("xlsx 摘要含前 50 行文本", XLSX_MARK in s, s[:150])
r = call("GET", f"/api/docs/{md_id}/preview")
check("md 摘要含全文", r.get("code") == 0 and MD_MARK in (r["data"]["summary"] or ""), str(r)[:200])
r = call("GET", f"/api/docs/{png_id}/preview")
check("图片摘要为空", r.get("code") == 0 and (r["data"]["summary"] or "") == "", str(r)[:150])

print("========== 3. 分类 CRUD（多级 + 删除归未分类） ==========")
r = call("POST", "/api/docs/categories", {"name": "卡07测试分类"})
check("新建根分类", r.get("code") == 0, str(r)[:150])
pid = (r.get("data") or {}).get("id")

r = call("POST", "/api/docs/categories", {"name": "子分类教程", "parent_id": pid})
check("新建子分类", r.get("code") == 0, str(r)[:150])
cid = (r.get("data") or {}).get("id")

r = call("POST", "/api/docs/categories", {"name": "孙子分类", "parent_id": cid})
check("多级：孙分类可建", r.get("code") == 0, str(r)[:150])
gid = (r.get("data") or {}).get("id")

r = call("GET", "/api/docs/categories")
tree = r.get("data") or {}
check("分类树含根分类", any(c["name"] == "卡07测试分类" for c in tree.get("categories", [])),
      str(tree)[:200])

r = call("PUT", f"/api/docs/{md_id}/category", {"category_id": cid})
check("移动 md 到子分类", r.get("code") == 0, str(r)[:150])
r = call("PUT", f"/api/docs/{png_id}/category", {"category_id": gid})
check("移动 png 到孙分类", r.get("code") == 0, str(r)[:150])

r = call("GET", f"/api/docs?category_id={cid}")
lst = (r.get("data") or {}).get("list") or []
check("按子分类筛选命中 md", any(it["id"] == md_id for it in lst), str(lst)[:200])
r = call("GET", f"/api/docs?category_id={gid}")
lst = (r.get("data") or {}).get("list") or []
check("按孙分类筛选命中 png", any(it["id"] == png_id for it in lst), str(lst)[:200])

r = call("PUT", f"/api/docs/categories/{cid}", {"name": "子分类教程改名"})
check("重命名分类", r.get("code") == 0, str(r)[:150])

r = call("DELETE", f"/api/docs/categories/{pid}")
check("删除根分类（级联）", r.get("code") == 0 and (r["data"] or {}).get("removed_categories") == 3,
      str(r)[:200])
check("删除后 2 个文档归未分类", (r["data"] or {}).get("docs_reset") == 2, str(r)[:200])

r = call("GET", "/api/docs/categories")
tree = r.get("data") or {}
check("树中根分类已移除", all(c["id"] != pid for c in tree.get("categories", [])), str(tree)[:200])
r = call("GET", "/api/docs?category_id=-1")
lst = (r.get("data") or {}).get("list") or []
check("未分类列表含 md/png", any(it["id"] == md_id for it in lst) and
      any(it["id"] == png_id for it in lst), str(lst)[:300])

r = call("PUT", f"/api/docs/{md_id}/category", {"category_id": 99999})
check("移动到不存在分类被拒", r.get("code") != 0, str(r)[:150])

print("========== 4. 检索（name + summary LIKE） ==========")
r = call("GET", f"/api/docs/search?q={PDF_MARK}")
lst = (r.get("data") or {}).get("list") or []
check("搜 pdf 正文词命中", any(it["id"] == pdf_id for it in lst), str(lst)[:250])

r = call("GET", f"/api/docs/search?q={MD_MARK}")
lst = (r.get("data") or {}).get("list") or []
check("搜 md 正文词命中", any(it["id"] == md_id for it in lst), str(lst)[:250])

r = call("GET", "/api/docs/search?q=指标口径")
lst = (r.get("data") or {}).get("list") or []
check("搜中文关键词命中 docx", any(it["id"] == docx_id for it in lst), str(lst)[:250])
check("命中结果带摘要片段", any(it.get("summary_snippet") for it in lst if it["id"] == docx_id),
      str(lst)[:250])

r = call("GET", "/api/docs/search?q=")
check("空关键字返回空列表", r.get("code") == 0 and (r["data"] or {}).get("total") == 0, str(r)[:150])

print("========== 5. /raw 与列表分页 ==========")
status, ctype, body = raw_call(f"/api/docs/{pdf_id}/raw")
check("pdf raw 200 + application/pdf", status == 200 and "application/pdf" in ctype, f"{status} {ctype}")
check("pdf raw 字节以 %PDF 开头", body.startswith(b"%PDF"), str(body[:20]))
status, ctype, body = raw_call(f"/api/docs/{png_id}/raw")
check("png raw 200 + image/png", status == 200 and "image/png" in ctype, f"{status} {ctype}")
status, ctype, body = raw_call(f"/api/docs/{md_id}/raw")
check("md raw 可读且含标记", status == 200 and MD_MARK.encode("utf-8") in body, f"{status} {ctype}")
status, ctype, _ = raw_call("/api/docs/999999/raw")
check("raw 不存在返回业务错误", status == 200 and ctype == "application/json", f"{status} {ctype}")

r = call("GET", "/api/docs?page=1&page_size=2")
d0 = r.get("data") or {}
check("分页 page_size=2", r.get("code") == 0 and len(d0.get("list") or []) == 2, str(d0)[:200])
check("total >= 6", d0.get("total", 0) >= 6, str(d0.get("total")))
check("列表带 category_name 字段", all("category_name" in it for it in d0.get("list") or []),
      str(d0)[:200])
r = call("GET", "/api/docs?page=0&page_size=200")
check("分页参数非法被拒", r.get("code") != 0, str(r)[:150])

print("========== 6. 删除（上传者可删 + 物理文件同步删） ==========")
path_row = db_rows("SELECT path FROM documents WHERE id = ?", (long_txt_id,))
phys = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    path_row[0]["path"]) if path_row else None
check("删除前物理文件存在", bool(phys and os.path.isfile(phys)), str(phys))
r = call("DELETE", f"/api/docs/{long_txt_id}")
check("删除文档成功", r.get("code") == 0, str(r)[:150])
check("物理文件同步删除", bool(phys) and not os.path.isfile(phys), str(phys))
check("记录已从库中移除", not db_rows("SELECT id FROM documents WHERE id = ?", (long_txt_id,)))
r = call("DELETE", "/api/docs/999999")
check("删除不存在的文档被拒", r.get("code") != 0, str(r)[:150])

print("========== 7. 埋点 ==========")
up_ev = db_rows("SELECT params_json FROM event_logs WHERE event='doc_upload' ORDER BY id DESC LIMIT 10")
check("doc_upload 埋点 >= 6 条（本脚本 5 类+txt）", len(up_ev) >= 6, str(len(up_ev)))
params = [json.loads(e["params_json"]) for e in up_ev]
check("doc_upload 含 doc_type 参数", all("doc_type" in p for p in params[:6]), str(params[:2]))
se_ev = db_rows("SELECT params_json FROM event_logs WHERE event='doc_search' ORDER BY id DESC LIMIT 5")
check("doc_search 埋点已记录", len(se_ev) >= 4, str(len(se_ev)))
sparams = [json.loads(e["params_json"]) for e in se_ev]
check("doc_search 含 keyword/result_cnt", all("keyword" in p and "result_cnt" in p for p in sparams),
      str(sparams[:2]))

# ---------- 汇总 ----------
print(f"\n===== 自测结果：{passed} 通过 / {len(failed)} 失败 =====")
if failed:
    print("失败项：")
    for f_ in failed:
        print(" -", f_)
    sys.exit(1)
