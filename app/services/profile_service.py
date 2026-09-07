# -*- coding: utf-8 -*-
"""数据体检服务（卡03）。

分块（chunksize=50000）扫描数据文件，输出五类检测报告：
  a) 缺失值：空值/空串/NA、N/A、null、-- 等统一识别，逐字段缺失率，>20% 预警；
  b) 重复行：全列完全重复 + 用户指定列重复，哈希聚合统计，前 20 组明细样例；
  c) 异常值：仅数值列，规则策略（年龄 0~120、金额/价格非负）+ 3σ 统计策略；
  d) 格式不符：日期斜杠/横杠/中文混用、手机号非 11 位、身份证非 18 位、文本首尾空格；
  e) 类型误判：千分位数字文本建议转数值、文本列 80%+ 匹配日期建议转日期。

原始文件只读，本服务不落盘任何数据，报告 JSON 由路由层写入 clean_profiles。
"""
import re
import time
from datetime import datetime

import numpy as np
import pandas as pd

from app.services import file_service as fs

# ---------- 阈值 ----------
MISSING_WARN_RATE = 0.20        # 缺失率预警线
ANOMALY_CAP = 5000              # 异常清单条数上限（超出截断并标记）
ITEM_SAMPLE_CAP = 100           # 格式问题每字段样例上限
DUP_SAMPLE_GROUPS = 20          # 重复明细样例组数
NUMERIC_RATIO = 0.80            # 数值列判定：非缺失值中可转数字占比
NUMERIC_MIN_ROWS = 20           # 数值列最少非缺失行数
DATE_SUGGEST_RATIO = 0.80       # 文本列建议转日期的日期匹配占比
DUP_ROW_SAMPLE_LIMIT = 6        # 每组重复保留的行号个数

PHONE_RE = re.compile(r"^\d{11}$")
IDCARD_RE = re.compile(r"^\d{17}[\dXx]$")

# 标识编码类列名关键字（ID/手机号/身份证等不是度量值，不参与 3σ 异常检测）
_ID_LIKE_EN = ("phone", "mobile", "idcard", "id", "code")
_ID_LIKE_CN = ("手机", "身份证", "编号", "序号", "工号", "单号")


def _is_id_like(col_name: str) -> bool:
    lname = col_name.lower()
    return (any(k in lname for k in _ID_LIKE_EN)
            or any(k in col_name for k in _ID_LIKE_CN))

FMT_LABEL = {"slash": "斜杠（如 2026/9/1）", "dash": "横杠（如 2026-09-01）",
             "cn": "中文（如 2026年9月1日）"}


# ---------- 基础工具 ----------

def _iter_chunks(path, encoding: str | None, file_type: str):
    """逐块产出 DataFrame（全部为字符串文本）。CSV 分块，Excel 单块。"""
    if file_type == "excel":
        df = pd.read_excel(
            path, dtype=str, keep_default_na=False, na_values=[], engine="openpyxl"
        )
        yield df
        return
    enc = encoding or fs.resolve_csv_encoding(path)
    reader = pd.read_csv(
        path, encoding=enc, dtype=str, keep_default_na=False, na_values=[],
        skip_blank_lines=True, chunksize=fs.CHUNK_SIZE,
    )
    try:
        for chunk in reader:
            yield chunk
    finally:
        reader.close()  # Windows 下及时释放文件句柄


def _prep(chunk: pd.DataFrame):
    """返回 (原始文本, 去首尾空格文本, 小写文本, 缺失掩码)，全部为字符串。"""
    text = chunk.where(chunk.notna(), "").astype(str)
    strip = text.apply(lambda s: s.str.strip())
    lower = strip.apply(lambda s: s.str.lower())
    miss = lower.isin(sorted(fs.MISSING_TEXT))
    return text, strip, lower, miss


def _to_num(s: pd.Series) -> pd.Series:
    """转数字：直接转失败的再尝试去千分位逗号；不可转的为 NaN。"""
    direct = pd.to_numeric(s, errors="coerce")
    need = direct.isna() & s.ne("")
    if need.any():
        conv = pd.to_numeric(s[need].str.replace(",", "", regex=False), errors="coerce")
        direct.loc[conv.index] = conv
    return direct


def _cap_items(bucket: list, row_nos: np.ndarray, vals: pd.Series, cap: int):
    """向格式问题样例桶追加 (row_no, value)，受 cap 限制。"""
    if len(bucket) >= cap:
        return
    take = min(cap - len(bucket), len(row_nos))
    for k in range(take):
        bucket.append({"row_no": int(row_nos[k]), "value": str(vals.iloc[k])})


def _dup_agg(hash_parts: list) -> dict:
    """重复行聚合：np.unique 向量化统计（内存 O(n)×8B，替代逐行字典）。

    返回 dup_rows（重复多余行数，与 df.duplicated().sum() 口径一致）、
    dup_groups（重复组数）与选中取样所需内部量。
    """
    all_h = np.concatenate(hash_parts) if hash_parts else np.empty(0, dtype=np.int64)
    if len(all_h) == 0:
        return {"dup_rows": 0, "dup_groups": 0, "sel": {}}
    uniq, first_idx, counts = np.unique(all_h, return_index=True, return_counts=True)
    dup_mask = counts > 1
    dup_rows = int((counts[dup_mask] - 1).sum())
    dup_groups = int(dup_mask.sum())

    # 取样：重复组按首见行号排序取前 DUP_SAMPLE_GROUPS 组
    sel: dict[int, dict] = {}
    if dup_groups:
        d_uniq = uniq[dup_mask]
        d_first = first_idx[dup_mask]
        d_cnt = counts[dup_mask]
        order = np.argsort(d_first)
        for k in order[:DUP_SAMPLE_GROUPS]:
            sel[int(d_uniq[k])] = {"cnt": int(d_cnt[k]), "rows": [], "values": None}
    return {"dup_rows": dup_rows, "dup_groups": dup_groups, "sel": sel}


def _collect_dup_samples(path, encoding: str | None, file_type: str,
                         columns: list[str], dup_columns: list[str] | None,
                         full_sel: dict, sub_sel: dict) -> None:
    """第三遍轻扫：仅为选中重复组收集行号样例（≤6/组）与首见行值，可提前结束。"""
    if not full_sel and not sub_sel:
        return
    offset = 0
    for chunk in _iter_chunks(path, encoding, file_type):
        text = chunk.where(chunk.notna(), "").astype(str)
        n = len(chunk)
        h_full = pd.util.hash_pandas_object(chunk[columns], index=False).to_numpy()
        h_sub = (pd.util.hash_pandas_object(chunk[dup_columns], index=False).to_numpy()
                 if sub_sel else None)
        for i in range(n):
            rn = offset + i + 1
            st = full_sel.get(int(h_full[i]))
            if st is not None and (st["values"] is None
                                   or len(st["rows"]) < DUP_ROW_SAMPLE_LIMIT):
                if st["values"] is None:
                    st["values"] = {c: text[c].iloc[i] for c in columns}
                st["rows"].append(rn)
            if sub_sel is not None and h_sub is not None:
                st = sub_sel.get(int(h_sub[i]))
                if st is not None and (st["values"] is None
                                       or len(st["rows"]) < DUP_ROW_SAMPLE_LIMIT):
                    if st["values"] is None:
                        st["values"] = {c: text[c].iloc[i] for c in dup_columns}
                    st["rows"].append(rn)
        offset += n
        # 全部选中组都已收集满 6 行 → 提前结束
        if (all(len(s["rows"]) >= DUP_ROW_SAMPLE_LIMIT for s in full_sel.values())
                and all(len(s["rows"]) >= DUP_ROW_SAMPLE_LIMIT for s in sub_sel.values())):
            break


def _dup_summary(agg: dict) -> dict:
    """从聚合结果 + 取样信息产出重复统计与明细样例（JSON 形状与旧版一致）。"""
    samples = []
    for st in agg["sel"].values():
        if st["values"] is None:
            continue  # 未被第三遍扫到（理论上不会发生）
        samples.append({
            "row_nos": st["rows"],
            "extra": max(0, st["cnt"] - len(st["rows"])),
            "values": st["values"],
        })
    return {"dup_rows": agg["dup_rows"], "dup_groups": agg["dup_groups"],
            "samples": samples}


# ---------- 主流程 ----------

def run_profile(path, encoding: str | None, file_type: str,
                dup_columns: list[str] | None = None) -> dict:
    """对文件执行五类体检，返回报告字典（JSON 可序列化）。"""
    started = time.time()
    columns: list[str] | None = None
    total_rows = 0

    # pass1 聚合状态
    missing_cnt: dict[str, int] = {}
    nonmiss_cnt: dict[str, int] = {}
    # 数值列 Welford 在线聚合（n/mean/M2），避免大数量级下 sumsq 抵消导致方差失真
    var_n: dict[str, int] = {}
    var_mean: dict[str, float] = {}
    var_m2: dict[str, float] = {}
    thousand_cnt: dict[str, int] = {}
    thousand_sample: dict[str, list] = {}
    date_cnt: dict[str, int] = {}
    date_fmt_cnt: dict[str, dict] = {}
    date_fmt_sample: dict[str, dict] = {}
    space_cnt: dict[str, int] = {}
    space_items: dict[str, list] = {}
    phone_cnt: dict[str, int] = {}
    phone_items: dict[str, list] = {}
    idcard_cnt: dict[str, int] = {}
    idcard_items: dict[str, list] = {}
    full_hash_parts: list = []   # 每块全列哈希数组（np.unique 聚合，内存 O(n)×8B）
    sub_hash_parts: list = []    # 指定列哈希数组

    use_sub = bool(dup_columns)

    offset = 0
    for chunk in _iter_chunks(path, encoding, file_type):
        if columns is None:
            columns = [str(c) for c in chunk.columns]
            for c in columns:
                missing_cnt[c] = nonmiss_cnt[c] = 0
                var_n[c] = 0
                var_mean[c] = var_m2[c] = 0.0
                thousand_cnt[c] = date_cnt[c] = space_cnt[c] = 0
                phone_cnt[c] = idcard_cnt[c] = 0
                thousand_sample[c] = []
                date_fmt_cnt[c] = {"slash": 0, "dash": 0, "cn": 0}
                date_fmt_sample[c] = {"slash": [], "dash": [], "cn": []}
                space_items[c] = []
                phone_items[c] = []
                idcard_items[c] = []

        text, strip, lower, miss = _prep(chunk)
        n = len(chunk)
        total_rows += n
        row_nos = np.arange(offset + 1, offset + n + 1)

        # --- 重复行哈希（全列 + 指定列）：仅累积哈希数组，逐行信息延后到第三遍轻扫 ---
        full_hash_parts.append(
            pd.util.hash_pandas_object(chunk[columns], index=False).to_numpy())
        if use_sub:
            sub_hash_parts.append(
                pd.util.hash_pandas_object(chunk[dup_columns], index=False).to_numpy())

        # --- 逐列统计 ---
        for col in columns:
            s = strip[col]
            m = miss[col]
            nm = ~m
            nonmiss = int(nm.sum())
            nonmiss_cnt[col] += nonmiss
            missing_cnt[col] += int(m.sum())

            # 数值累积（Welford 在线合并本块均值/方差，含千分位转换后的值）
            conv = _to_num(s)
            valid = conv.notna() & nm
            if bool(valid.any()):
                vs = conv[valid]
                nb = int(len(vs))
                mb = float(vs.mean())
                m2b = float(((vs - mb) ** 2).sum())
                na = var_n[col]
                if na == 0:
                    var_n[col] = nb
                    var_mean[col] = mb
                    var_m2[col] = m2b
                else:
                    nt = na + nb
                    delta = mb - var_mean[col]
                    var_mean[col] += delta * nb / nt
                    var_m2[col] += m2b + delta * delta * na * nb / nt
                    var_n[col] = nt

            # 千分位数字文本
            th_mask = s.str.fullmatch(fs.THOUSANDS_RE.pattern).fillna(False) & nm
            th_c = int(th_mask.sum())
            if th_c:
                thousand_cnt[col] += th_c
                if len(thousand_sample[col]) < 3:
                    thousand_sample[col].extend(
                        s[th_mask].head(3 - len(thousand_sample[col])).tolist())

            # 日期匹配与格式分布
            dh = fs._date_match(s) & nm
            dh_c = int(dh.sum())
            date_cnt[col] += dh_c
            if dh_c:
                dv = s[dh]
                for key, sep in (("slash", "/"), ("dash", "-"), ("cn", "年")):
                    fmask = dv.str.contains(sep, regex=False)
                    fc = int(fmask.sum())
                    date_fmt_cnt[col][key] += fc
                    if fc and len(date_fmt_sample[col][key]) < 3:
                        date_fmt_sample[col][key].extend(
                            dv[fmask].head(3 - len(date_fmt_sample[col][key])).tolist())

            # 首尾空格（用原始文本比对）
            sp_mask = (text[col] != s) & nm
            sp_c = int(sp_mask.sum())
            if sp_c:
                space_cnt[col] += sp_c
                _cap_items(space_items[col], row_nos[sp_mask.to_numpy()],
                           text[col][sp_mask], ITEM_SAMPLE_CAP)

            # 手机号位数
            lname = col.lower()
            if "phone" in lname or "手机" in col:
                bad = nm & ~s.str.fullmatch(PHONE_RE.pattern).fillna(False)
                bc = int(bad.sum())
                if bc:
                    phone_cnt[col] += bc
                    _cap_items(phone_items[col], row_nos[bad.to_numpy()],
                               s[bad], ITEM_SAMPLE_CAP)

            # 身份证位数
            if "idcard" in lname or "身份证" in col:
                bad = nm & ~s.str.fullmatch(IDCARD_RE.pattern).fillna(False)
                bc = int(bad.sum())
                if bc:
                    idcard_cnt[col] += bc
                    _cap_items(idcard_items[col], row_nos[bad.to_numpy()],
                               s[bad], ITEM_SAMPLE_CAP)

        offset += n

    if columns is None:
        raise ValueError("文件内容为空，无法体检")

    # ---------- 汇总：缺失值 ----------
    miss_fields = []
    for col in columns:
        rate = missing_cnt[col] / total_rows if total_rows else 0.0
        miss_fields.append({
            "field": col,
            "missing": missing_cnt[col],
            "total": total_rows,
            "rate": round(rate, 4),
            "warn": rate > MISSING_WARN_RATE,
        })
    miss_fields.sort(key=lambda x: -x["rate"])

    # ---------- 汇总：重复行（np.unique 聚合 + 第三遍轻扫取样） ----------
    full_agg = _dup_agg(full_hash_parts)
    full_sel = full_agg["sel"]
    sub_agg = _dup_agg(sub_hash_parts) if use_sub else None
    sub_sel = sub_agg["sel"] if sub_agg else {}
    if full_sel or sub_sel:
        _collect_dup_samples(path, encoding, file_type, columns,
                             dup_columns if use_sub else None, full_sel, sub_sel)
    full_dup = _dup_summary(full_agg)
    sub_dup = None
    if use_sub and set(dup_columns) != set(columns):
        sub_dup = {"columns": list(dup_columns), **_dup_summary(sub_agg)}

    # ---------- 数值列与 3σ 阈值（标识编码类列不参与统计异常检测） ----------
    numeric_stats = {}
    for col in columns:
        nm = nonmiss_cnt[col]
        if (nm >= NUMERIC_MIN_ROWS and not _is_id_like(col)
                and var_n[col] / nm >= NUMERIC_RATIO):
            mean = var_mean[col]
            std = max(0.0, var_m2[col] / var_n[col]) ** 0.5
            numeric_stats[col] = (mean, std, mean - 3 * std, mean + 3 * std)

    # ---------- pass2：异常值（规则 + 3σ） ----------
    anomalies = []
    truncated = False
    offset = 0
    for chunk in _iter_chunks(path, encoding, file_type):
        _, strip, _, miss = _prep(chunk)
        n = len(chunk)
        for col in columns:
            lname = col.lower()
            is_age = ("age" in lname) or ("年龄" in col)
            is_amt = (any(k in lname for k in ("amount", "price"))
                      or ("金额" in col) or ("收入" in col))
            if not (is_age or is_amt or col in numeric_stats):
                continue
            s = strip[col]
            conv = _to_num(s)
            valid = conv.notna() & ~miss[col]

            sigma = pd.Series(False, index=chunk.index)
            if col in numeric_stats:
                mean, std, lo, hi = numeric_stats[col]
                sigma = valid & ((conv < lo) | (conv > hi))
            rule = pd.Series(False, index=chunk.index)
            if is_age:
                rule = valid & ((conv < 0) | (conv > 120))
            elif is_amt:
                rule = valid & (conv < 0)

            hit = (sigma | rule).to_numpy()
            for i in np.where(hit)[0]:
                v = float(conv.iloc[i])
                rules = []
                if bool(rule.iloc[i]):
                    if is_age:
                        rules.append(f"年龄应在 0~120 之间，当前值 {v:g}")
                    else:
                        rules.append(f"金额/价格不应为负，当前值 {v:g}")
                if bool(sigma.iloc[i]):
                    mean, std, lo, hi = numeric_stats[col]
                    rules.append(
                        f"超出 3σ 正常范围（均值 {mean:.2f}，参考区间 [{lo:.2f}, {hi:.2f}]）")
                anomalies.append({
                    "row_no": offset + int(i) + 1,
                    "field": col,
                    "value": s.iloc[i],
                    "rules": "；".join(rules),
                    "confirmed": False,
                })
                if len(anomalies) >= ANOMALY_CAP:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break
        offset += n

    anomalies.sort(key=lambda a: (a["row_no"], a["field"]))
    for idx, a in enumerate(anomalies, start=1):
        a["id"] = idx

    # ---------- 汇总：格式不符 ----------
    date_mixed = []
    for col in columns:
        fc = date_fmt_cnt[col]
        used = [k for k in ("slash", "dash", "cn") if fc[k] > 0]
        if len(used) >= 2:
            date_mixed.append({
                "field": col,
                "formats": [{"format": FMT_LABEL[k], "count": fc[k]} for k in used],
                "samples": {FMT_LABEL[k]: date_fmt_sample[col][k] for k in used},
            })

    phones = [{"field": c, "bad_count": phone_cnt[c], "items": phone_items[c]}
              for c in columns if phone_cnt[c] > 0]
    idcards = [{"field": c, "bad_count": idcard_cnt[c], "items": idcard_items[c]}
               for c in columns if idcard_cnt[c] > 0]
    spaces = [{"field": c, "bad_count": space_cnt[c], "items": space_items[c]}
              for c in columns if space_cnt[c] > 0]
    format_issue_cnt = (len(date_mixed)
                        + sum(p["bad_count"] for p in phones)
                        + sum(p["bad_count"] for p in idcards)
                        + sum(p["bad_count"] for p in spaces))

    # ---------- 汇总：类型误判建议 ----------
    suggestions = []
    for col in columns:
        nm = nonmiss_cnt[col]
        if nm == 0:
            continue
        if thousand_cnt[col] > 0:
            suggestions.append({
                "field": col, "kind": "number", "action": "convert_number",
                "reason": f"含 {thousand_cnt[col]} 个千分位格式数字文本"
                          f"（如 {thousand_sample[col][0]}），去逗号后可转为数值",
                "sample": thousand_sample[col][0],
            })
        if date_cnt[col] / nm >= DATE_SUGGEST_RATIO:
            sample = ""
            for k in ("slash", "dash", "cn"):
                if date_fmt_sample[col][k]:
                    sample = date_fmt_sample[col][k][0]
                    break
            pct = round(date_cnt[col] / nm * 100, 1)
            suggestions.append({
                "field": col, "kind": "date", "action": "convert_date",
                "reason": f"{pct}% 的值匹配日期格式（如 {sample}），建议转为日期类型",
                "sample": sample,
            })
    for idx, sg in enumerate(suggestions, start=1):
        sg["id"] = idx

    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "duration_sec": round(time.time() - started, 2),
        "row_count": total_rows,
        "col_count": len(columns),
        "columns": columns,
        "dup_columns_used": list(dup_columns) if use_sub else None,
        "summary": {
            "missing_fields_cnt": sum(1 for f in miss_fields if f["missing"] > 0),
            "missing_warn_cnt": sum(1 for f in miss_fields if f["warn"]),
            "dup_rows": full_dup["dup_rows"],
            "dup_groups": full_dup["dup_groups"],
            "anomaly_cnt": len(anomalies),
            "anomaly_truncated": truncated,
            "format_issue_cnt": format_issue_cnt,
            "type_suggest_cnt": len(suggestions),
        },
        "missing": {"fields": miss_fields},
        "duplicates": {"full": full_dup, "by_columns": sub_dup},
        "anomalies": {"total": len(anomalies), "truncated": truncated, "items": anomalies},
        "formats": {"date_mixed": date_mixed, "phones": phones,
                    "idcards": idcards, "spaces": spaces},
        "type_suggestions": suggestions,
    }
    return report
