# -*- coding: utf-8 -*-
"""数据文件导入与解析服务。

职责：保存上传文件、检测编码、分块解析统计（行数/列数/缺失率/示例值）、
字段类型推断、前 N 行预览读取。原始文件只读落盘，解析结果由路由层写库。
"""
import re
import uuid
from pathlib import Path

import chardet
import pandas as pd

from app.config import BASE_DIR, FILES_DIR

# ---------- 上限 ----------
MAX_CSV_BYTES = 500 * 1024 * 1024        # CSV 500MB
MAX_CSV_ROWS = 1_000_000                 # CSV 100 万行
MAX_EXCEL_BYTES = 50 * 1024 * 1024       # Excel 50MB
CHUNK_SIZE = 50_000                      # CSV 分块读取行数

ALLOWED_SUFFIX = {".xlsx", ".xls", ".csv"}

# ---------- 缺失值判定 ----------
MISSING_TEXT = {"", "na", "n/a", "null", "none", "--", "-"}

# ---------- 类型推断正则 ----------
INT_RE = re.compile(r"^[+-]?\d+$")
FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+)$")
THOUSANDS_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
BOOL_RE = re.compile(r"^(?:true|false|yes|no|y|n|是|否)$", re.IGNORECASE)
# 日期：2026/9/1、2026-09-01、2026年9月1日，可带时间 12:30 或 12:30:45
DATE_RE = re.compile(
    r"^(\d{4})(?:[/\-](\d{1,2})[/\-]|年(\d{1,2})月)(\d{1,2})日?"
    r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$"
)

SAMPLE_LIMIT = 3  # 每列示例值个数


def _is_missing(val) -> bool:
    """空值/空串/NA/N/A/null/-- 均算缺失。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return True
    return str(val).strip().lower() in MISSING_TEXT


def _date_match(s: pd.Series) -> pd.Series:
    """向量化日期匹配（含月/日/时/分/秒数值范围校验）。"""
    hit = s.str.match(DATE_RE.pattern, na=False)
    if not hit.any():
        return hit
    # 命中子集提取分组：1年 2月(斜杠) 3月(中文) 4日 5时 6分 7秒
    ext = s[hit].str.extract(DATE_RE)
    month = pd.to_numeric(ext[1].fillna(ext[2]), errors="coerce")
    day = pd.to_numeric(ext[3], errors="coerce")
    hour = pd.to_numeric(ext[4], errors="coerce")
    minute = pd.to_numeric(ext[5], errors="coerce")
    sec = pd.to_numeric(ext[6], errors="coerce")
    valid = month.between(1, 12) & day.between(1, 31)
    valid &= hour.isna() | hour.between(0, 23)
    valid &= minute.isna() | minute.between(0, 59)
    valid &= sec.isna() | sec.between(0, 59)
    hit.loc[valid.index] = valid
    return hit


def infer_column_type(values: list[str]) -> str:
    """推断一列类型。

    返回：整数 / 小数 / 日期 / 布尔 / 文本 / 混合，附加提示用 · 连接
    （如 "混合·含千分位文本"、"文本·疑似数字"、"日期·疑似日期"）。
    """
    n = len(values)
    if n == 0:
        return "文本"

    s = pd.Series(values, dtype="object").astype(str).str.strip()
    int_c = int(s.str.fullmatch(INT_RE).sum())
    float_c = int(s.str.fullmatch(FLOAT_RE).sum())
    thousand_c = int(s.str.fullmatch(THOUSANDS_RE).sum())
    bool_c = int(s.str.fullmatch(BOOL_RE).sum())
    date_c = int(_date_match(s).sum())
    num_c = int_c + float_c  # 纯数字（不含千分位）

    if bool_c == n:
        return "布尔"
    if date_c == n:
        return "日期·疑似日期"
    if num_c == n:
        return "小数" if float_c > 0 else "整数"
    if thousand_c == n:
        return "文本·疑似数字"
    text_like = n - num_c - bool_c - date_c  # 千分位文本 + 普通文本统称文本组
    if num_c > 0 and text_like > 0:
        # 数字列混入文本（千分位文本属于文本）
        return "混合·含千分位文本" if thousand_c > 0 else "混合"
    if date_c > 0 and num_c + thousand_c == 0:
        return "日期·疑似日期"
    return "文本"


def detect_raw_encoding(path: Path) -> tuple[str | None, float]:
    """chardet 检测编码，返回 (原始编码名, 置信度)。"""
    try:
        with open(path, "rb") as f:
            raw = f.read(65536)
        if not raw:
            return None, 0.0
        r = chardet.detect(raw)
        return r.get("encoding"), float(r.get("confidence") or 0.0)
    except OSError:
        return None, 0.0


def try_read_csv(path: Path, encoding: str, nrows: int | None = None):
    """用指定编码试读 CSV（dtype=str 保留原始文本），失败抛异常。"""
    return pd.read_csv(
        path,
        encoding=encoding,
        nrows=nrows,
        dtype=str,
        keep_default_na=False,
        na_values=[],
        skip_blank_lines=True,
        chunksize=CHUNK_SIZE if nrows is None else None,
    )


ENCODING_CANDIDATES = ["utf-8-sig", "gbk", "gb2312"]
_WHITELIST = ("utf8", "ascii", "gbk", "gb2312", "gb18030")


def resolve_csv_encoding(path: Path) -> str:
    """UTF-8 / GBK / GB2312 依次尝试（chardet 结果优先），全失败报中文错误。"""
    raw_guess, conf = detect_raw_encoding(path)
    guess = None
    if raw_guess:
        g = raw_guess.lower().replace("-", "").replace("_", "")
        if g not in _WHITELIST and conf >= 0.6:
            # chardet 高置信度判定为其他编码（如 shift-jis），直接拒绝避免乱码入库
            raise ValueError(
                f"编码非 UTF-8/GBK，已尝试自动转码失败（检测编码为 {raw_guess}，暂不支持）"
            )
        if g in ("utf8", "ascii"):
            guess = "utf-8-sig"
        elif g in ("gbk", "gb2312", "gb18030"):
            guess = "gbk"

    candidates = list(ENCODING_CANDIDATES)
    if guess and guess in candidates:
        candidates.remove(guess)
        candidates.insert(0, guess)
    for enc in candidates:
        try:
            try_read_csv(path, enc, nrows=10)
            return enc
        except (UnicodeDecodeError, UnicodeError):
            continue
        except (pd.errors.ParserError, pd.errors.EmptyDataError):
            return enc  # 结构问题不属于编码问题，交给后续完整解析报错
    raise ValueError("编码非 UTF-8/GBK，已尝试自动转码失败（尝试顺序：" + "、".join(candidates) + "）")


def analyze_csv(path: Path, encoding: str) -> dict:
    """分块解析 CSV：行数/列数/每列缺失率与示例值/类型推断。"""
    reader = try_read_csv(path, encoding)
    try:
        return _analyze_blocks(reader)
    finally:
        reader.close()  # 及时关闭句柄，否则 Windows 下物理文件无法删除


def analyze_excel(path: Path) -> dict:
    """解析 Excel（.xlsx 用 openpyxl；.xls 需要可选依赖 xlrd）。"""
    engine = "openpyxl" if path.suffix.lower() == ".xlsx" else "xlrd"
    try:
        df = pd.read_excel(path, dtype=str, keep_default_na=False, na_values=[], engine=engine)
    except ImportError:
        raise ValueError("解析旧版 .xls 需要安装 xlrd，请将文件另存为 .xlsx 后重新上传")
    if len(df) > MAX_CSV_ROWS:
        raise ValueError(f"文件超过 {MAX_CSV_ROWS:,} 行上限，请拆分后导入")
    return _analyze_blocks([df])


def _analyze_blocks(blocks) -> dict:
    """消费 DataFrame 块，统计行数/列数/缺失率/示例值/类型。"""
    columns: list[str] | None = None
    total_rows = 0
    missing_cnt: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    non_missing_vals: dict[str, list[str]] = {}

    for chunk in blocks:
        if columns is None:
            columns = [str(c) for c in chunk.columns]
            missing_cnt = {c: 0 for c in columns}
            samples = {c: [] for c in columns}
            non_missing_vals = {c: [] for c in columns}
        total_rows += len(chunk)
        if total_rows > MAX_CSV_ROWS:
            raise ValueError(f"文件超过 {MAX_CSV_ROWS:,} 行上限，请拆分后导入")
        for col in columns:
            s = chunk[col].astype("object")
            stripped = s.astype(str).str.strip().str.lower()
            miss_mask = s.isna() | stripped.isin(MISSING_TEXT)
            missing_cnt[col] += int(miss_mask.sum())
            ok = s[~miss_mask]
            if len(ok):
                raw_vals = ok.astype(str)  # 示例值保留原始文本（含首尾空格）
                vals = raw_vals.str.strip()  # 类型推断用清洗后的文本
                if len(samples[col]) < SAMPLE_LIMIT:
                    samples[col].extend(raw_vals.head(SAMPLE_LIMIT - len(samples[col])).tolist())
                bucket = non_missing_vals[col]
                if len(bucket) < 5000:  # 类型推断采样上限，控制内存与耗时
                    bucket.extend(vals.head(5000 - len(bucket)).tolist())

    if columns is None:
        raise ValueError("文件内容为空，无法解析")

    fields = []
    for col in columns:
        total = total_rows
        rate = round(missing_cnt[col] / total, 4) if total else 0.0
        fields.append({
            "field_name": col,
            "inferred_type": infer_column_type(non_missing_vals[col]),
            "missing_rate": rate,
            "sample_values": samples[col],
        })
    return {"row_count": total_rows, "col_count": len(columns), "fields": fields}


def analyze_dataframe(df) -> dict:
    """对已加载的 DataFrame 做与文件导入一致的统计（行数/列数/缺失率/示例值/类型推断）。

    用于数据库取数等非文件来源的产物，使 data_fields 与卡01 导入产物完全同构。
    """
    return _analyze_blocks([df])


def read_head_rows(path: Path, encoding: str | None, n: int = 5) -> tuple[list[str], list[list[str]]]:
    """读取前 n 行用于预览，返回 (columns, rows)，全部转文本。

    CSV 用 nrows 截断读取，Excel 同样 nrows 截断（openpyxl 只读前 n 行），
    杜绝大文件全量加载导致的行数失控。
    """
    suffix = path.suffix.lower()
    if suffix == ".csv":
        enc = encoding or resolve_csv_encoding(path)
        df = pd.read_csv(
            path, encoding=enc, nrows=n, dtype=str,
            keep_default_na=False, na_values=[], skip_blank_lines=True,
        )
    else:
        df = pd.read_excel(path, nrows=n, dtype=str, keep_default_na=False,
                           na_values=[], engine="openpyxl")
    columns = [str(c) for c in df.columns]
    rows = df.astype("object").where(df.notna(), "").astype(str).values.tolist()
    return columns, rows


def save_upload_to_disk(fileobj, suffix: str, max_bytes: int, limit_desc: str) -> tuple[Path, int]:
    """上传内容流式落盘：data/files/{uuid}{后缀}，返回 (物理路径, 字节数)。

    超过 max_bytes 抛 ValueError（含中文提示），并清理半成品文件。
    """
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    path = FILES_DIR / f"{uuid.uuid4().hex}{suffix}"
    size = 0
    try:
        with open(path, "wb") as out:
            while True:
                chunk = fileobj.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"文件超过 {limit_desc} 上限，请压缩或拆分后再上传")
                out.write(chunk)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path, size


def remove_physical_file(relative_path: str | None) -> None:
    """删除物理文件（原始文件只读约定仅指内容不改写，删除跟随记录生命周期）。"""
    if not relative_path:
        return
    p = BASE_DIR / relative_path
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass  # 物理文件缺失不阻断记录删除
