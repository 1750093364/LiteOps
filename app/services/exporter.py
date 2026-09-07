# -*- coding: utf-8 -*-
"""报告导出服务（卡10）：Word 生成 + 打印视图 HTML。

- Word：python-docx 纯库实现（禁 pandoc/LibreOffice 等外部依赖）。解析卡09 编辑器的
  content_html，生成含封面页（标题/日期/AI 复核声明）→ 目录页（收集 H1/H2）→ 正文
  （Heading 样式标题、段落、有序/无序列表、带边框表格、base64 图片、引用卡片转纯文本、
  SQL 代码块等宽字体）的 docx；页眉为报告名，页脚为页码域；
- PDF：后端不做转换，GET print-view 返回独立打印 HTML（内联 CSS + @media print），
  前端新窗口打开后 window.print() 由浏览器"另存为 PDF"。

content_html 约定（卡09 编辑器产出）：
- 块级：h1/h2/h3、p、div、ul/ol>li、table、pre.snippet-block（SQL 代码）、img（base64 data URI）
- 行内：b/strong、i/em、br、span.ref-card（data-ref 属性存来源 JSON，contenteditable=false）
- p.template-hint：框架模板提示语段落，导出时剔除。
"""
import base64
import html as html_mod
import io
import json
import re
from datetime import datetime
from html.parser import HTMLParser

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from app.config import BASE_DIR

EXPORT_DIR = BASE_DIR / "data" / "exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

_BLOCK_TAGS = ("h1", "h2", "h3", "p", "div", "li")

_TMPL_HINT_P_RE = re.compile(
    r'<p\b[^>]*class="[^"]*template-hint[^"]*"[^>]*>.*?</p>', re.I | re.S)
_HEAD_RE = re.compile(r"<(h1|h2)\b[^>]*>(.*?)</\1>", re.I | re.S)
_DATAURI_RE = re.compile(r"^data:image/[a-zA-Z0-9.+-]+;base64,(.*)$", re.S)


# ==================== HTML 解析（标准库 HTMLParser，无第三方依赖） ====================

class _Blk:
    """一个待渲染块。kind: h1/h2/h3/p/li/img/ref/code/table。"""

    def __init__(self, kind):
        self.kind = kind
        self.runs = []        # [(text, bold, italic)]；'\n' 表示换行
        self.list_kind = None  # li 专用：'ul'/'ol'
        self.depth = 0         # li 嵌套深度
        self.img_src = None
        self.ref = None        # ref 专用：data-ref JSON dict
        self.rows = None       # table 专用：[[(text, is_header), ...], ...]
        self.code = ""         # code 专用
        self.skip = False      # template-hint 段落剔除


class _HtmlBlocks(HTMLParser):
    """把 content_html 拍平为块列表；行内 b/i/br 记入 runs。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.cur = None
        self.list_stack = []   # ul/ol 嵌套栈
        self.bold = 0
        self.italic = 0
        self.in_pre = False
        self.pre_parts = []
        self.cell = None       # {'parts': [...], 'header': bool}
        self.in_ref = False

    def _flush(self):
        if self.cur is not None:
            self.blocks.append(self.cur)
            self.cur = None
        self.in_ref = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag in ("ul", "ol"):
            self._flush()
            self.list_stack.append(tag)
            return
        if tag == "table":
            self._flush()
            self.cur = _Blk("table")
            self.cur.rows = []
            return
        if tag == "tr":
            if self.cur is not None and self.cur.kind == "table":
                self.cur.rows.append([])
            return
        if tag in ("td", "th"):
            self.cell = {"parts": [], "header": tag == "th"}
            return
        if tag == "img":
            src = a.get("src") or ""
            if src.startswith("data:image"):
                self._flush()
                blk = _Blk("img")
                blk.img_src = src
                self.blocks.append(blk)
            return
        if tag == "pre":
            self._flush()
            self.cur = _Blk("code")
            self.in_pre = True
            self.pre_parts = []
            return
        if tag == "br":
            if self.cell is not None:
                self.cell["parts"].append(" ")
            elif self.cur is not None and not self.cur.skip and not self.in_ref:
                self.cur.runs.append(("\n", False, False))
            return
        if tag in ("b", "strong"):
            self.bold += 1
            return
        if tag in ("i", "em"):
            self.italic += 1
            return
        if tag == "span" and "ref-card" in cls:
            ref = None
            raw = a.get("data-ref")
            if raw:
                try:
                    ref = json.loads(raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    ref = None
            self._flush()
            blk = _Blk("ref")
            blk.ref = ref if isinstance(ref, dict) else {}
            self.cur = blk
            self.in_ref = True
            return
        if tag in _BLOCK_TAGS:
            skip = tag == "p" and "template-hint" in cls
            self._flush()
            blk = _Blk("li" if tag == "li" else tag if tag in ("h1", "h2", "h3") else "p")
            blk.skip = skip
            if blk.kind == "li":
                blk.list_kind = self.list_stack[-1] if self.list_stack else "ul"
                blk.depth = len(self.list_stack)
            self.cur = blk
            return
        # 其它标签（tbody/thead/a/u/sup…）忽略，仅继续收集文本

    def handle_endtag(self, tag):
        if tag in ("b", "strong"):
            self.bold = max(0, self.bold - 1)
            return
        if tag in ("i", "em"):
            self.italic = max(0, self.italic - 1)
            return
        if tag in ("td", "th"):
            if (self.cell is not None and self.cur is not None
                    and self.cur.kind == "table" and self.cur.rows):
                self.cur.rows[-1].append(
                    ("".join(self.cell["parts"]).strip(), self.cell["header"]))
            self.cell = None
            return
        if tag == "table":
            self._flush()
            return
        if tag == "pre":
            if self.cur is not None and self.cur.kind == "code":
                self.cur.code = "".join(self.pre_parts)
            self.in_pre = False
            self._flush()
            return
        if tag == "span" and self.in_ref:
            self._flush()
            return
        if tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self.in_pre:
            self.pre_parts.append(data)
            return
        if self.cell is not None:
            self.cell["parts"].append(data)
            return
        if self.cur is None:
            if data.strip():
                blk = _Blk("p")
                blk.runs.append((data, False, False))
                self.cur = blk
            return
        if self.cur.skip or self.in_ref:
            return
        if data:
            self.cur.runs.append((data, self.bold > 0, self.italic > 0))


def _iter_blocks(content_html: str):
    p = _HtmlBlocks()
    try:
        p.feed(content_html or "")
        p.close()
    except Exception:
        pass
    return p.blocks


def _runs_text(runs) -> str:
    return "".join(t.replace("\n", " ") for t, _, _ in runs).strip()


# ==================== Word 生成 ====================

def _fmt_value(v) -> str:
    """数值转千分位文本，与前端 fmtNum 口径一致（整数去尾巴，小数最多 4 位）。"""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return f"{int(v):,}"
        return f"{v:,.4f}"
    return str(v)


def _cjk_font(style_or_run, east="微软雅黑"):
    rpr = style_or_run.element.get_or_add_rPr().get_or_add_rFonts()
    rpr.set(qn("w:eastAsia"), east)


def _mono_font(run):
    run.font.name = "Consolas"
    run.font.size = Pt(9.5)
    run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "宋体")


def _add_runs(par, runs, mono=False):
    for text, bold, italic in runs:
        parts = text.split("\n")
        for i, part in enumerate(parts):
            if i:
                par.add_run().add_break()
            if not part:
                continue
            r = par.add_run(part)
            if bold:
                r.bold = True
            if italic:
                r.italic = True
            if mono:
                _mono_font(r)


def _setup_styles(doc):
    st = doc.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = Pt(11)
    _cjk_font(st)
    for name in ("Heading 1", "Heading 2", "Heading 3"):
        try:
            _cjk_font(doc.styles[name])
        except KeyError:
            pass


def _add_cover(doc, title: str, has_ai: bool):
    for _ in range(5):
        doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(title or "未命名报告")
    r.bold = True
    r.font.size = Pt(26)
    p2 = doc.add_paragraph()
    p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r2 = p2.add_run(datetime.now().strftime("%Y 年 %m 月 %d 日"))
    r2.font.size = Pt(13)
    r2.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    for _ in range(8):
        doc.add_paragraph()
    # PRD 9.1 首行：封面复核声明。status 含 'ai' 时必加 AI 声明；无 AI 也保留人工复核行
    p3 = doc.add_paragraph()
    p3.alignment = WD_ALIGN_PARAGRAPH.CENTER
    line = ("本报告由 AI 辅助生成，所有数字结论需人工复核" if has_ai
            else "本报告所有数字结论需人工复核后方可对外交付")
    r3 = p3.add_run(line)
    r3.font.size = Pt(11)
    r3.font.color.rgb = RGBColor(0xB0, 0x53, 0x1E)
    doc.add_page_break()


def _add_toc(doc, headings):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("目  录")
    r.bold = True
    r.font.size = Pt(16)
    doc.add_paragraph()
    if not headings:
        pe = doc.add_paragraph()
        re_ = pe.add_run("（正文暂无 H1/H2 章节标题）")
        re_.font.color.rgb = RGBColor(0x99, 0x99, 0x99)
    for kind, text in headings:
        pi = doc.add_paragraph()
        if kind == "h2":
            pi.paragraph_format.left_indent = Inches(0.3)
        ri = pi.add_run(text)
        ri.bold = kind == "h1"
    doc.add_page_break()


def _add_table(doc, rows):
    if not rows:
        return
    ncols = max(len(r) for r in rows) or 1
    t = doc.add_table(rows=len(rows), cols=ncols)
    try:
        t.style = "Table Grid"  # 内置样式，带全边框
    except KeyError:
        pass
    for i, row in enumerate(rows):
        for j in range(ncols):
            if j >= len(row):
                continue
            text, header = row[j]
            cell = t.cell(i, j)
            cell.text = text
            if header:
                for par in cell.paragraphs:
                    for r in par.runs:
                        r.bold = True


def _add_image(doc, src):
    m = _DATAURI_RE.match(src or "")
    if not m:
        return
    try:
        data = base64.b64decode(m.group(1))
        doc.add_picture(io.BytesIO(data), width=Inches(5.8))
    except Exception:
        pass  # 图片损坏不阻断导出


def _add_ref(doc, ref):
    if not ref:
        return
    p = doc.add_paragraph()
    r = p.add_run(_fmt_value(ref.get("value")))
    r.bold = True
    src_text = " · ".join(x for x in (ref.get("file_name"), ref.get("field")) if x)
    if src_text:
        agg = ref.get("agg_label") or ref.get("agg") or ""
        p.add_run("（来源：" + src_text + ((" " + agg) if agg else "") + "）")


def _add_code(doc, code):
    for ln in (code or "").rstrip("\n").split("\n"):
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(0.2)
        shd = OxmlElement("w:shd")  # 浅灰底纹区分代码块
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:fill"), "F5F5F5")
        p._p.get_or_add_pPr().append(shd)
        _mono_font(p.add_run(ln if ln else " "))


def _set_header_footer(doc, title: str):
    sec = doc.sections[0]
    hp = sec.header.paragraphs[0]
    hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    hr = hp.add_run(title or "")
    hr.font.size = Pt(9)
    hr.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fp.add_run("第 ").font.size = Pt(9)
    r2 = fp.add_run()
    fld_b = OxmlElement("w:fldChar")
    fld_b.set(qn("w:fldCharType"), "begin")
    ins = OxmlElement("w:instrText")
    ins.set(qn("xml:space"), "preserve")
    ins.text = "PAGE"  # 页码域：Word/WPS 打开时自动显示当前页
    fld_e = OxmlElement("w:fldChar")
    fld_e.set(qn("w:fldCharType"), "end")
    r2._r.append(fld_b)
    r2._r.append(ins)
    r2._r.append(fld_e)
    r2.font.size = Pt(9)
    fp.add_run(" 页").font.size = Pt(9)


def export_report_docx(title: str, content_html: str, status: str, out_path) -> None:
    """content_html → docx：封面页 → 目录页 → 正文 → 页眉页脚。"""
    doc = Document()
    _setup_styles(doc)
    blocks = _iter_blocks(content_html)
    headings = [(b.kind, _runs_text(b.runs)) for b in blocks
                if b.kind in ("h1", "h2") and not b.skip and _runs_text(b.runs)]

    _add_cover(doc, title, "ai" in (status or "").lower())
    _add_toc(doc, headings)

    ol_counters = {}
    for b in blocks:
        if b.kind != "li":
            ol_counters.clear()
        if b.skip:
            continue
        if b.kind in ("h1", "h2", "h3"):
            doc.add_heading(_runs_text(b.runs), level=int(b.kind[1]))
        elif b.kind == "p":
            if not _runs_text(b.runs):
                continue
            _add_runs(doc.add_paragraph(), b.runs)
        elif b.kind == "li":
            if b.list_kind == "ul":
                p = doc.add_paragraph(style="List Bullet")
                if b.depth > 1:
                    p.paragraph_format.left_indent = Inches(0.25 * (b.depth - 1))
                _add_runs(p, b.runs)
            else:  # 有序列表手动编号，避免 Word 多列表续号问题
                for d in [d for d in ol_counters if d > b.depth]:
                    ol_counters.pop(d, None)
                n = ol_counters.get(b.depth, 0) + 1
                ol_counters[b.depth] = n
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Inches(0.25 * max(1, b.depth))
                p.add_run(f"{n}. ")
                _add_runs(p, b.runs)
        elif b.kind == "table":
            _add_table(doc, b.rows)
        elif b.kind == "img":
            _add_image(doc, b.img_src)
        elif b.kind == "ref":
            _add_ref(doc, b.ref)
        elif b.kind == "code":
            _add_code(doc, b.code)

    _set_header_footer(doc, title)
    doc.save(str(out_path))


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]', "_", (name or "").strip()) or "报告"
    return name[:80]


# ==================== 打印视图 HTML（浏览器另存 PDF） ====================

_PRINT_CSS = """
  * { box-sizing: border-box; }
  body { margin: 0; background: #e8e8e8; font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
         color: #222; font-size: 14px; line-height: 1.75; }
  .toolbar { position: sticky; top: 0; z-index: 9; background: #1f2d3d; color: #fff;
             padding: 10px 24px; display: flex; gap: 12px; align-items: center; }
  .toolbar button { padding: 7px 18px; border: 0; border-radius: 4px; cursor: pointer;
                    font-size: 14px; background: #2f81f7; color: #fff; }
  .toolbar button.ghost { background: rgba(255,255,255,.15); }
  .toolbar .tip { font-size: 12px; opacity: .75; }
  .sheet { width: 210mm; min-height: 297mm; margin: 12px auto; background: #fff;
           padding: 22mm 20mm; box-shadow: 0 2px 10px rgba(0,0,0,.18); page-break-after: always; }
  .sheet:last-child { page-break-after: auto; }
  .cover { display: flex; flex-direction: column; justify-content: center; align-items: center;
           text-align: center; }
  .cover h1 { font-size: 34px; margin: 0 0 18px; letter-spacing: 2px; }
  .cover .date { color: #666; font-size: 15px; }
  .cover .statement { margin-top: 90px; color: #b0531e; font-size: 14px; }
  .toc h2 { text-align: center; font-size: 22px; letter-spacing: 8px; margin-bottom: 28px; }
  .toc-item { padding: 4px 0; }
  .toc-h2 { padding-left: 28px; color: #444; }
  h1 { font-size: 22px; border-bottom: 2px solid #2f81f7; padding-bottom: 6px; }
  h2 { font-size: 18px; margin-top: 26px; }
  h3 { font-size: 16px; margin-top: 20px; }
  table { border-collapse: collapse; width: 100%; margin: 10px 0; }
  th, td { border: 1px solid #999; padding: 6px 10px; text-align: left; }
  th { background: #f2f6fc; }
  img { max-width: 100%; }
  pre { background: #f5f5f5; border: 1px solid #e0e0e0; border-radius: 4px; padding: 10px 12px;
        font-family: Consolas, "Courier New", monospace; font-size: 12.5px;
        white-space: pre-wrap; word-break: break-all; }
  .ref-card { color: #1a7f37; font-weight: 600; }
  @media print {
    body { background: #fff; }
    .toolbar { display: none; }
    .sheet { width: auto; min-height: auto; margin: 0; padding: 0; box-shadow: none; }
    @page { size: A4; margin: 20mm 18mm; }
  }
"""


def _strip_tags(s: str) -> str:
    return html_mod.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def build_print_html(title: str, content_html: str, status: str) -> str:
    """独立打印 HTML：封面 + 目录 + 正文，@media print 隐藏工具栏，A4 分页。"""
    body = _TMPL_HINT_P_RE.sub("", content_html or "")  # 剔除模板提示语段落
    heads = []
    for m in _HEAD_RE.finditer(content_html or ""):
        t = _strip_tags(m.group(2))
        if t:
            heads.append((m.group(1).lower(), t))
    has_ai = "ai" in (status or "").lower()
    statement = ("本报告由 AI 辅助生成，所有数字结论需人工复核" if has_ai
                 else "本报告所有数字结论需人工复核后方可对外交付")
    today = datetime.now().strftime("%Y 年 %m 月 %d 日")
    toc_items = "".join(
        f'<div class="toc-item {"toc-h1" if k == "h1" else "toc-h2"}">{html_mod.escape(t)}</div>'
        for k, t in heads) or '<div class="toc-item toc-h2">（正文暂无 H1/H2 章节标题）</div>'

    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="UTF-8">\n'
        "<title>" + html_mod.escape(title or "报告") + "</title>\n"
        "<style>" + _PRINT_CSS + "</style>\n</head>\n<body>\n"
        '<div class="toolbar no-print">'
        '<button onclick="window.print()">🖨 打印 / 另存 PDF</button>'
        '<button class="ghost" onclick="window.close()">关闭</button>'
        '<span class="tip">在打印对话框中选择「另存为 PDF」即可导出 PDF 文件</span>'
        "</div>\n"
        '<div class="sheet cover">'
        "<h1>" + html_mod.escape(title or "未命名报告") + "</h1>"
        '<div class="date">' + today + "</div>"
        '<div class="statement">' + statement + "</div>"
        "</div>\n"
        '<div class="sheet toc"><h2>目录</h2>' + toc_items + "</div>\n"
        '<div class="sheet body">' + body + "</div>\n"
        "</body>\n</html>"
    )
