# -*- coding: utf-8 -*-
"""演示模式样例数据自动导入（卡14）。

首次启动（data_files 表为空）时，把 examples/ 下的内置样例文件
（sales_orders_50k.csv、activity_small.xlsx）走正常导入流程落库，
便于公网 Demo 用户开箱即用。

复用 file_service 的分析函数与 files 路由的写库结构，保证样例数据与
用户上传数据在 data_files / data_fields 中完全同构。
"""
import json
import shutil
import time
import uuid
from pathlib import Path

from app.config import BASE_DIR, EXAMPLES_DIR, FILES_DIR
from app.db import get_conn
from app.services import file_service as fs

# 需要自动导入的样例文件（相对 examples/ 目录）
DEMO_SAMPLES = ["sales_orders_50k.csv", "activity_small.xlsx"]


def _is_db_empty() -> bool:
    """data_files 表是否为空（首次启动判定）。"""
    with get_conn() as conn:
        cnt = conn.execute("SELECT COUNT(*) AS c FROM data_files").fetchone()["c"]
    return cnt == 0


def _import_one(src: Path) -> None:
    """把单个样例文件导入 data_files + data_fields（与上传接口同构）。"""
    suffix = src.suffix.lower()
    is_csv = suffix == ".csv"

    # 1. 复制到 data/files/{uuid}{suffix}（与上传落盘一致）
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    dst = FILES_DIR / f"{uuid.uuid4().hex}{suffix}"
    shutil.copyfile(src, dst)
    size = dst.stat().st_size

    # 2. 解析统计（复用 file_service）
    try:
        if is_csv:
            encoding = fs.resolve_csv_encoding(dst)
            result = fs.analyze_csv(dst, encoding)
        else:
            encoding = None
            result = fs.analyze_excel(dst)
    except Exception:
        # 样例文件解析失败不阻断启动，仅回滚物理文件
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    relative = dst.relative_to(BASE_DIR).as_posix()
    origin_name = src.name

    # 3. 写库（与 files.upload_file 写库逻辑一致）
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO data_files
               (origin_name, name, file_type, source, path, encoding,
                row_count, col_count, size_bytes, status)
               VALUES (?, ?, ?, '演示样例', ?, ?, ?, ?, ?, '已导入')""",
            (origin_name, origin_name, "csv" if is_csv else "excel", relative, encoding,
             result["row_count"], result["col_count"], size),
        )
        file_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO data_fields
               (file_id, field_name, inferred_type, missing_rate, sample_values)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (file_id, f["field_name"], f["inferred_type"],
                 f["missing_rate"], json.dumps(f["sample_values"], ensure_ascii=False))
                for f in result["fields"]
            ],
        )
        # 演示样例导入也埋点，保持事件统计完整
        conn.execute(
            "INSERT INTO event_logs (event, params_json) VALUES (?, ?)",
            ("file_import", json.dumps({
                "file_id": file_id, "name": origin_name,
                "file_type": "csv" if is_csv else "excel",
                "rows": result["row_count"], "cols": result["col_count"],
                "size_mb": round(size / 1048576, 2),
                "result": "success", "source": "demo_seed",
            }, ensure_ascii=False)),
        )


def ensure_demo_samples() -> None:
    """演示模式首次启动：库为空时导入 examples/ 下的样例文件。

    幂等：已有数据时不重复导入；单个样例失败不影响其余样例。
    """
    if not _is_db_empty():
        return
    if not EXAMPLES_DIR.is_dir():
        return
    for name in DEMO_SAMPLES:
        src = EXAMPLES_DIR / name
        if not src.is_file():
            continue
        try:
            t0 = time.time()
            _import_one(src)
            print(f"[LiteOps demo] 已导入样例 {name}（{time.time() - t0:.1f}s）")
        except Exception as e:
            print(f"[LiteOps demo] 样例 {name} 导入失败：{e}")
