# -*- coding: utf-8 -*-
"""数据清洗引擎（卡04）。

职责：
  - 支持 10 类清洗动作的向量化执行（禁止逐行 iterrows 处理全量）；
  - 预览：对前 1000 行样本依次试跑，返回每动作影响行数 + 5 组前后对比样例；
  - 执行：先存快照（to_pickle），全量在副本上跑动作链，产物落 CSV 并回写 data_files / data_fields / clean_records；
  - 回滚：删除产物文件与 data_files 记录，恢复原文件状态。

原始文件只读，所有产物为新文件。
"""
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from app.config import BASE_DIR, FILES_DIR, SNAPSHOTS_DIR
from app.services import file_service as fs

PREVIEW_ROWS = 1000
SAMPLE_PAIRS = 5

# 缺失值判定文本集合（与 file_service 一致）
MISSING_TEXT = {"", "na", "n/a", "null", "none", "--", "-"}

# 动作类型元信息（供前端展示与后端校验）
ACTION_TYPES = {
    "fill_missing": {"label": "缺失值处理", "destructive": False},
    "drop_duplicates": {"label": "去重", "destructive": False},
    "convert_type": {"label": "类型转换", "destructive": False},
    "normalize_date": {"label": "日期标准化", "destructive": False},
    "strip_text": {"label": "字符串修剪", "destructive": False},
    "filter_rows": {"label": "行筛选", "destructive": True},
    "rename_field": {"label": "字段重命名", "destructive": False},
    "drop_field": {"label": "删除字段", "destructive": True},
    "sort_rows": {"label": "排序", "destructive": False},
    "handle_anomaly": {"label": "异常值处理", "destructive": False},
}

# 可能删除整行的动作（需用户二次确认）
ROW_DELETING_ACTIONS = {"filter_rows", "handle_anomaly"}
# 删除行/字段的动作（需确认）
DESTRUCTIVE_ACTIONS = {"drop_field"} | ROW_DELETING_ACTIONS


# ---------- 基础工具 ----------

def _is_missing_series(s: pd.Series) -> pd.Series:
    """向量化缺失判定：None / NaN / 空串 / NA / N/A / null / -- / - 均算缺失。"""
    # 不依赖 dtype 判断（pandas 3.0 中 dtype=str 是 string 而非 object）
    low = s.astype(str).str.strip().str.lower()
    return s.isna() | low.isin(sorted(MISSING_TEXT))


def _to_num_series(s: pd.Series) -> pd.Series:
    """转数字：先直接转，失败的去千分位逗号再转；不可转的为 NaN。"""
    s_str = s.astype(str)
    direct = pd.to_numeric(s_str, errors="coerce")
    need = direct.isna() & s_str.ne("")
    if bool(need.any()):
        conv = pd.to_numeric(s_str[need].str.replace(",", "", regex=False), errors="coerce")
        direct.loc[conv.index] = conv
    return direct


# 中文日期预处理正则：2026年5月12日 → 2026-5-12
_CN_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日?")


def _parse_dates(s: pd.Series) -> pd.Series:
    """解析日期列：先把中文日期（2026年5月12日）转为 2026-5-12，再用 format='mixed' 解析。"""
    text = s.astype(str)
    text = text.str.replace(_CN_DATE_RE, r"\1-\2-\3", regex=True)
    return pd.to_datetime(text, errors="coerce", format="mixed")


def _load_df(path: Path, encoding: str | None, file_type: str,
             nrows: int | None = None) -> pd.DataFrame:
    """读取数据为字符串 DataFrame（保留原始文本）。"""
    if file_type == "excel":
        df = pd.read_excel(path, dtype=str, keep_default_na=False,
                           na_values=[], engine="openpyxl")
        if nrows is not None:
            df = df.head(nrows)
        return df
    enc = encoding or fs.resolve_csv_encoding(path)
    return pd.read_csv(
        path, encoding=enc, nrows=nrows, dtype=str,
        keep_default_na=False, na_values=[], skip_blank_lines=True,
    )


def _ensure_columns(df: pd.DataFrame, fields) -> list[str]:
    """校验字段存在，返回规范化字段列表。"""
    if isinstance(fields, str):
        fields = [fields]
    cols = list(df.columns)
    out = []
    for f in fields or []:
        f = str(f)
        if f not in cols:
            raise ValueError(f"字段「{f}」不存在")
        out.append(f)
    return out


def _diff_rows(before: pd.DataFrame, after: pd.DataFrame) -> int:
    """计算受影响行数：行数变化 + 内容变化（按索引对齐比较）。"""
    if len(before) != len(after):
        return abs(len(before) - len(after))
    # 列数不同也算受影响
    if list(before.columns) != list(after.columns):
        common = [c for c in before.columns if c in after.columns]
        if not common:
            return len(before)
        b = before[common]
        a = after[common]
    else:
        b = before
        a = after
    # 向量化比较：任一单元格不同即该行受影响
    try:
        neq = (b.astype(str) != a.astype(str)).any(axis=1)
        return int(neq.sum())
    except Exception:
        return len(before)


def _sample_pairs(before: pd.DataFrame, after: pd.DataFrame,
                  limit: int = SAMPLE_PAIRS) -> list[dict]:
    """取前 N 组前后对比样例（按行索引对齐，列取公共列）。"""
    common = [c for c in before.columns if c in after.columns]
    if not common:
        common = list(before.columns) if len(before.columns) else []
    b = before[common].head(limit)
    a = after[common].head(limit)
    pairs = []
    n = min(len(b), len(a))
    for i in range(n):
        pairs.append({
            "before": {c: _cell(b[c].iloc[i]) for c in common},
            "after": {c: _cell(a[c].iloc[i]) for c in common},
        })
    return pairs


def _cell(val) -> str:
    if val is None:
        return ""
    try:
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    return str(val)


# ---------- 单动作执行 ----------

def _act_fill_missing(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    field = _ensure_columns(df, params.get("field"))[0]
    strategy = (params.get("strategy") or "").strip()
    miss = _is_missing_series(df[field])
    if strategy == "drop_row":
        return df[~miss].reset_index(drop=True)
    col = df[field].copy()
    if strategy == "mean":
        nums = _to_num_series(col[~miss])
        val = nums.mean()
        if pd.isna(val):
            raise ValueError(f"字段「{field}」无可计算均值的数值")
        col[miss] = val
    elif strategy == "median":
        nums = _to_num_series(col[~miss])
        val = nums.median()
        if pd.isna(val):
            raise ValueError(f"字段「{field}」无可计算中位数的数值")
        col[miss] = val
    elif strategy == "fixed":
        val = params.get("value")
        if val is None:
            raise ValueError("固定值填充需指定 value")
        col[miss] = val
    elif strategy in ("ffill", "bfill"):
        # 把缺失文本统一为 NaN 后再 ffill/bfill
        col = col.where(~miss, np.nan)
        col = col.ffill() if strategy == "ffill" else col.bfill()
        col = col.where(col.notna(), "")  # 仍为空的回填空串
    else:
        raise ValueError(f"不支持的填充策略「{strategy}」")
    df = df.copy()
    df[field] = col
    return df


def _act_drop_duplicates(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    cols = params.get("columns")
    if cols:
        cols = _ensure_columns(df, cols)
    else:
        cols = list(df.columns)
    return df.drop_duplicates(subset=cols, keep="first").reset_index(drop=True)


def _act_convert_type(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    field = _ensure_columns(df, params.get("field"))[0]
    target = (params.get("target") or "").strip()
    col = df[field].astype(str)
    if target == "number":
        # 去千分位逗号后转数值，转失败保留原值（不丢数据，仅标记为转换）
        num = pd.to_numeric(col.str.replace(",", "", regex=False), errors="coerce")
        converted = num.notna()
        out = col.copy()
        out[converted] = num[converted].astype(str)
        df = df.copy()
        df[field] = out
    elif target == "date":
        dt = _parse_dates(col)
        ok = dt.notna()
        out = col.copy()
        out[ok] = dt[ok].dt.strftime("%Y-%m-%d")
        df = df.copy()
        df[field] = out
    else:
        raise ValueError(f"不支持的目标类型「{target}」，可选 number / date")
    return df


def _act_normalize_date(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    field = _ensure_columns(df, params.get("field"))[0]
    fmt = params.get("output_format") or "%Y-%m-%d"
    fmt = fmt.replace("YYYY", "%Y").replace("MM", "%m").replace("DD", "%d")
    col = df[field].astype(str)
    dt = _parse_dates(col)
    ok = dt.notna()
    out = col.copy()
    out[ok] = dt[ok].dt.strftime(fmt)
    df = df.copy()
    df[field] = out
    return df


def _act_strip_text(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    field = _ensure_columns(df, params.get("field"))[0]
    df = df.copy()
    df[field] = df[field].astype(str).str.strip()
    return df


def _act_filter_rows(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """行筛选：conditions 列表（{field, operator, value}）+ combiner(and/or)。"""
    conditions = params.get("conditions")
    combiner = (params.get("combiner") or "and").lower()
    if not conditions:
        # 兼容单字段写法
        f = params.get("field")
        op = params.get("operator")
        v = params.get("value")
        if f and op is not None:
            conditions = [{"field": f, "operator": op, "value": v}]
        else:
            raise ValueError("筛选需指定 conditions 或 field/operator/value")
    masks = []
    for cond in conditions:
        field = _ensure_columns(df, cond.get("field"))[0]
        op = (cond.get("operator") or "").strip().lower()
        val = cond.get("value")
        col = df[field]
        if op == "in":
            vals = val if isinstance(val, list) else [str(v).strip() for v in str(val).split(",") if str(v).strip()]
            mask = col.astype(str).str.strip().isin([str(v) for v in vals])
        elif op == "=":
            mask = col.astype(str).str.strip() == str(val).strip()
        elif op == ">=":
            mask = _to_num_series(col) >= float(val)
        elif op == "<=":
            mask = _to_num_series(col) <= float(val)
        elif op == ">":
            mask = _to_num_series(col) > float(val)
        elif op == "<":
            mask = _to_num_series(col) < float(val)
        elif op in ("contains", "包含"):
            mask = col.astype(str).str.contains(str(val), na=False, regex=False)
        else:
            raise ValueError(f"不支持的运算符「{op}」")
        masks.append(mask.fillna(False))
    if not masks:
        return df
    final = masks[0]
    for m in masks[1:]:
        final = (final & m) if combiner == "and" else (final | m)
    return df[final].reset_index(drop=True)


def _act_rename_field(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    old = str(params.get("old_name") or "")
    new = str(params.get("new_name") or "").strip()
    if old not in df.columns:
        raise ValueError(f"字段「{old}」不存在")
    if not new:
        raise ValueError("新字段名不能为空")
    if new in df.columns and new != old:
        raise ValueError(f"字段「{new}」已存在，重命名会导致列名冲突")
    df = df.rename(columns={old: new})
    return df


def _act_drop_field(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    field = _ensure_columns(df, params.get("field"))[0]
    return df.drop(columns=[field])


def _act_sort_rows(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    field = _ensure_columns(df, params.get("field"))[0]
    order = (params.get("order") or "asc").lower()
    ascending = order != "desc"
    # 数值列按数值排，文本列按文本排
    col = df[field]
    nums = _to_num_series(col)
    if bool(nums.notna().all()):
        df = df.iloc[nums.argsort(kind="mergesort").to_numpy()]
    else:
        df = df.iloc[col.astype(str).argsort(kind="mergesort").to_numpy()]
    if not ascending:
        df = df.iloc[::-1]
    return df.reset_index(drop=True)


def _act_handle_anomaly(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    field = str(params.get("field") or "")
    mode = (params.get("mode") or "drop").strip().lower()
    anomaly_rows = params.get("anomaly_rows") or []  # 1-based 行号
    if field and field not in df.columns:
        raise ValueError(f"字段「{field}」不存在")

    # 未指定行号时，按字段规则自动检测异常（避免前置删行动作导致行号错位）
    if not anomaly_rows:
        anomaly_rows = _detect_anomaly_rows(df, field)

    # 行号转 0-based 索引；DataFrame 已 reset_index，索引即行号-1
    idx = [int(r) - 1 for r in anomaly_rows
           if isinstance(r, (int, float)) and 1 <= int(r) <= len(df)]
    if mode == "drop":
        keep = ~df.index.isin(idx)
        return df[keep].reset_index(drop=True)
    elif mode == "mark":
        df = df.copy()
        df["is_anomaly"] = "0"
        df.loc[df.index.isin(idx), "is_anomaly"] = "1"
        return df
    else:
        raise ValueError(f"不支持的异常处理模式「{mode}」，可选 drop / mark")


def _detect_anomaly_rows(df: pd.DataFrame, field: str) -> list[int]:
    """按字段规则自动检测异常行号（1-based）。

    - age/年龄：0~120 之外
    - amount/price/金额/收入：负数
    - 其他数值列：超出均值 ± 3σ
    - 未指定 field：对所有数值列应用上述规则
    """
    targets = [field] if field else list(df.columns)
    hit = pd.Series(False, index=df.index)
    for col in targets:
        if col not in df.columns:
            continue
        lname = col.lower()
        nums = _to_num_series(df[col])
        valid = nums.notna()
        if not bool(valid.any()):
            continue
        is_age = "age" in lname or "年龄" in col
        is_amt = any(k in lname for k in ("amount", "price")) or any(k in col for k in ("金额", "收入"))
        if is_age:
            col_hit = valid & ((nums < 0) | (nums > 120))
        elif is_amt:
            col_hit = valid & (nums < 0)
        else:
            mean = float(nums[valid].mean())
            std = float(nums[valid].std())
            if std == 0:
                continue
            col_hit = valid & ((nums < mean - 3 * std) | (nums > mean + 3 * std))
        hit = hit | col_hit.fillna(False)
    return [int(i) + 1 for i in hit[hit].index.tolist()]


_ACTION_HANDLERS = {
    "fill_missing": _act_fill_missing,
    "drop_duplicates": _act_drop_duplicates,
    "convert_type": _act_convert_type,
    "normalize_date": _act_normalize_date,
    "strip_text": _act_strip_text,
    "filter_rows": _act_filter_rows,
    "rename_field": _act_rename_field,
    "drop_field": _act_drop_field,
    "sort_rows": _act_sort_rows,
    "handle_anomaly": _act_handle_anomaly,
}


def _run_action(df: pd.DataFrame, action: dict) -> pd.DataFrame:
    atype = (action.get("type") or "").strip()
    if atype not in _ACTION_HANDLERS:
        raise ValueError(f"不支持的动作类型「{atype}」")
    params = action.get("params") or {}
    return _ACTION_HANDLERS[atype](df, params)


# ---------- 预览 ----------

def preview_actions(path: Path, encoding: str | None, file_type: str,
                    actions: list[dict]) -> dict:
    """对前 1000 行样本依次试跑动作链，返回每个动作的影响行数与前后对比样例。

    单动作失败时返回该动作的 error，不中断后续动作（后续动作在失败前的结果上继续）。
    """
    sample = _load_df(path, encoding, file_type, nrows=PREVIEW_ROWS)
    sample = sample.reset_index(drop=True)
    total_before = len(sample)
    cur = sample.copy()
    results = []
    for action in actions:
        atype = action.get("type")
        params = action.get("params") or {}
        before = cur.copy()
        try:
            after = _run_action(cur, action)
            affected = _diff_rows(before, after)
            results.append({
                "type": atype,
                "ok": True,
                "affected_rows": affected,
                "rows_before": len(before),
                "rows_after": len(after),
                "samples": _sample_pairs(before, after),
                "params": params,
            })
            cur = after
        except Exception as e:
            results.append({
                "type": atype,
                "ok": False,
                "error": str(e),
                "rows_before": len(before),
                "rows_after": len(cur),
                "samples": [],
                "params": params,
            })
            # 失败不中断，cur 保持不变
    return {
        "preview_rows": total_before,
        "actions": results,
    }


# ---------- 执行 ----------

def _new_acc() -> dict:
    """逐动作累计的变更统计容器。"""
    return {
        "missing_filled": 0,        # 填充缺失处数
        "rows_dropped_dedup": 0,    # 去重删除行数
        "fields_converted": [],     # 类型转换字段名
        "dates_normalized": [],     # 日期标准化字段名
        "rows_dropped_anomaly": 0,  # 剔除异常行数
        "rows_dropped_filter": 0,   # 筛选删除行数
        "rows_after_filter": None,  # 筛选后剩余行数（最后一次筛选）
        "strip_count": 0,           # 首尾空格修剪处数
        "rows_dropped_missing": 0,  # 缺失值 drop_row 删除行数
    }


def _capture_before(action: dict, df: pd.DataFrame) -> dict:
    """执行前轻量采集：仅复制统计所需的那一两列（避免全帧 copy 内存峰值翻倍）。"""
    atype = action.get("type")
    params = action.get("params") or {}
    cap = {"rows": len(df), "cols": {}}
    if atype == "fill_missing" and (params.get("strategy") or "").strip() != "drop_row":
        f = params.get("field")
        if f in df.columns:
            cap["cols"][f] = df[f].copy()
    elif atype == "strip_text":
        f = params.get("field")
        if f in df.columns:
            cap["cols"][f] = df[f].copy()
    return cap


def _acc_action_stats(action: dict, cap: dict, after: pd.DataFrame, acc: dict) -> None:
    """按动作类型累计变更统计（不改变动作执行逻辑）。

    cap 为 _capture_before 采集的执行前轻量快照（行数 + 统计所需列）。
    """
    atype = action.get("type")
    params = action.get("params") or {}
    rb, ra = cap["rows"], len(after)
    if atype == "fill_missing":
        strategy = (params.get("strategy") or "").strip()
        if strategy == "drop_row":
            acc["rows_dropped_missing"] += rb - ra
        else:
            f = params.get("field")
            col = cap["cols"].get(f)
            if col is not None:
                acc["missing_filled"] += int(_is_missing_series(col).sum())
    elif atype == "drop_duplicates":
        acc["rows_dropped_dedup"] += rb - ra
    elif atype == "convert_type":
        f = params.get("field")
        if f and f not in acc["fields_converted"]:
            acc["fields_converted"].append(f)
    elif atype == "normalize_date":
        f = params.get("field")
        if f and f not in acc["dates_normalized"]:
            acc["dates_normalized"].append(f)
    elif atype == "handle_anomaly":
        if (params.get("mode") or "drop") == "drop":
            acc["rows_dropped_anomaly"] += rb - ra
    elif atype == "filter_rows":
        acc["rows_after_filter"] = ra
        acc["rows_dropped_filter"] += rb - ra
    elif atype == "strip_text":
        f = params.get("field")
        col = cap["cols"].get(f)
        if col is not None and f in after.columns and rb == ra:
            b = col.astype(str)
            a = after[f].astype(str)
            acc["strip_count"] += int((b != a).sum())


def execute_actions(path: Path, encoding: str | None, file_type: str,
                    actions: list[dict], origin_name: str,
                    file_id: int) -> dict:
    """全量执行动作链：存快照 → 跑动作 → 落 CSV → 返回产物路径与统计。"""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_name = f"{file_id}_{ts}.pkl"
    snap_path = SNAPSHOTS_DIR / snap_name

    # 1) 全量读取（原始文件只读，读取后即可；CSV 天然 RangeIndex，无需 reset 拷贝）
    df = _load_df(path, encoding, file_type)
    rows_before = len(df)
    cols_before = len(df.columns)
    if not isinstance(df.index, pd.RangeIndex):
        df = df.reset_index(drop=True)

    # 2) 存快照（原始数据副本，用于回滚恢复）；之后动作直接在该帧引用上进行
    df.to_pickle(snap_path)

    out_path: Path | None = None
    try:
        # 3) 依次执行动作，逐动作累计变更统计（仅采集统计所需列，不整帧复制）
        cur = df
        acc = _new_acc()
        for action in actions:
            cap = _capture_before(action, cur)
            cur = _run_action(cur, action)
            _acc_action_stats(action, cap, cur, acc)

        # 4) 落 CSV 产物
        out_uuid = uuid.uuid4().hex
        out_name = f"{out_uuid}.csv"
        out_path = FILES_DIR / out_name
        cur.to_csv(out_path, index=False, encoding="utf-8-sig")
    except Exception:
        # 动作执行或落盘失败：清理快照与半成品输出，再抛给调用方
        try:
            snap_path.unlink(missing_ok=True)
        except OSError:
            pass
        if out_path is not None:
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise

    rows_after = len(cur)
    cols_after = len(cur.columns)

    stats = {
        "rows_before": rows_before,
        "rows_after": rows_after,
        "rows_removed": rows_before - rows_after,
        "total_rows_before": rows_before,
        "total_rows_after": rows_after,
        "cols_before": cols_before,
        "cols_after": cols_after,
        "actions_count": len(actions),
        "action_types": [a.get("type") for a in actions],
        "missing_filled": acc["missing_filled"],
        "rows_dropped_dedup": acc["rows_dropped_dedup"],
        "fields_converted": acc["fields_converted"],
        "fields_converted_cnt": len(acc["fields_converted"]),
        "dates_normalized": acc["dates_normalized"],
        "rows_dropped_anomaly": acc["rows_dropped_anomaly"],
        "rows_after_filter": acc["rows_after_filter"],
        "rows_dropped_filter": acc["rows_dropped_filter"],
        "rows_dropped_missing": acc["rows_dropped_missing"],
        "strip_count": acc["strip_count"],
    }

    return {
        "output_path": out_path,
        "output_relative": out_path.relative_to(BASE_DIR).as_posix(),
        "snapshot_path": snap_path.relative_to(BASE_DIR).as_posix(),
        "origin_name": _cleaned_name(origin_name),
        "row_count": rows_after,
        "col_count": cols_after,
        "size_bytes": out_path.stat().st_size,
        "stats": stats,
    }


def _cleaned_name(origin_name: str) -> str:
    """按规则生成清洗产物 origin_name：原名_cleaned_YYYYMMDD_HHMM.后缀。"""
    p = Path(origin_name)
    stem = p.stem
    suffix = p.suffix or ".csv"
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return f"{stem}_cleaned_{ts}{suffix}"
