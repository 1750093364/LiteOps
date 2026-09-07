# -*- coding: utf-8 -*-
"""卡13 性能实测：100 万行 CSV 导入 → 体检 → 清洗 三阶段 HTTP 实测。

用法：.venv\\Scripts\\python.exe scripts\\perf_1m.py [BASE_URL] [SERVER_PID]
- BASE 默认 http://127.0.0.1:8000（建议对全新 run.bat 实例测量）；
- SERVER_PID 必传：用于轮询服务器进程内存（PrivateUsage / WorkingSet，ctypes psapi）；
- 数据：data/test/sales_orders_1m.csv（gen_test_data.py --big 生成，100 万行 / 约 49MB）；
- 结束后自动删除本次导入的原文件与清洗产物（避免残留大文件）。

Windows 专用（psapi）。仅用标准库。
"""
import ctypes
import json
import sys
import threading
import time
import uuid
from pathlib import Path

from urllib import request as urlreq

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
SERVER_PID = int(sys.argv[2]) if len(sys.argv) > 2 else 0
ROOT = Path(__file__).resolve().parent.parent
CSV_1M = ROOT / "data" / "test" / "sales_orders_1m.csv"

# 清洗动作链（与内置订单预设同类：去重/转数值/日期标准化/缺失填充/异常剔除）
ACTIONS = [
    {"type": "drop_duplicates", "params": {}},
    {"type": "convert_type", "params": {"field": "amount", "target": "number"}},
    {"type": "normalize_date", "params": {"field": "order_date"}},
    {"type": "fill_missing", "params": {"field": "channel", "strategy": "fixed", "value": "未知"}},
    {"type": "handle_anomaly", "params": {"field": "age", "mode": "drop"}},
]

# ==================== 内存轮询（服务器进程） ====================

class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("WorkingSetSize", ctypes.c_size_t),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


_stage = "idle"
_peak: dict[str, dict] = {}          # stage -> {"priv": MB, "ws": MB}
_stop = threading.Event()
_psapi = ctypes.WinDLL("psapi") if sys.platform == "win32" else None
_kernel = ctypes.WinDLL("kernel32") if sys.platform == "win32" else None
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _poll_loop(pid: int) -> None:
    h = _kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        print(f"[WARN] 无法打开进程 {pid}，内存指标不可用")
        return
    pmc = PROCESS_MEMORY_COUNTERS_EX()
    pmc.cb = ctypes.sizeof(pmc)
    while not _stop.is_set():
        if _psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            cur = _peak.setdefault(_stage, {"priv": 0.0, "ws": 0.0})
            cur["priv"] = max(cur["priv"], pmc.PrivateUsage / 1048576)
            cur["ws"] = max(cur["ws"], pmc.WorkingSetSize / 1048576)
        time.sleep(0.25)
    _kernel.CloseHandle(h)


# ==================== HTTP 工具 ====================

def http_json(method: str, path: str, body: dict | None = None, timeout: int = 1800):
    data = json.dumps(body).encode() if body is not None else None
    req = urlreq.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    with urlreq.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def upload_csv(path: Path) -> dict:
    boundary = "----liteoperf" + uuid.uuid4().hex
    with open(path, "rb") as f:
        content = f.read()
    head = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; "
        f"filename=\"{path.name}\"\r\nContent-Type: text/csv\r\n\r\n"
    ).encode()
    body = head + content + f"\r\n--{boundary}--\r\n".encode()
    req = urlreq.Request(BASE + "/api/files/upload", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urlreq.urlopen(req, timeout=1800) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    if not CSV_1M.is_file():
        print(f"[ERR] 缺少 {CSV_1M}，先运行 gen_test_data.py --big")
        sys.exit(1)
    if not SERVER_PID:
        print("[ERR] 请传服务器进程 PID：perf_1m.py [BASE_URL] [SERVER_PID]")
        sys.exit(1)

    with urlreq.urlopen(BASE + "/api/health", timeout=5) as r:
        json.loads(r.read().decode())
    print(f"[OK] 服务 {BASE} 存活，PID={SERVER_PID}")

    th = threading.Thread(target=_poll_loop, args=(SERVER_PID,), daemon=True)
    th.start()

    results: dict[str, dict] = {}
    try:
        # ---- 阶段 1：导入 ----
        global _stage
        _stage = "import"
        t0 = time.perf_counter()
        up = upload_csv(CSV_1M)
        cost = time.perf_counter() - t0
        assert up.get("code") == 0, up
        f_id = up["data"]["id"]
        results["import"] = {"sec": round(cost, 1),
                             "rows": up["data"].get("row_count"),
                             "size_mb": round(CSV_1M.stat().st_size / 1048576, 1)}
        print(f"[阶段1 导入] {cost:.1f}s  rows={up['data'].get('row_count')}")

        # ---- 阶段 2：体检 ----
        _stage = "profile"
        t0 = time.perf_counter()
        pr = http_json("POST", f"/api/files/{f_id}/profile")
        cost = time.perf_counter() - t0
        assert pr.get("code") == 0, str(pr)[:300]
        s = pr["data"]["report"]["summary"]
        results["profile"] = {"sec": round(cost, 1), "dup_rows": s.get("dup_rows"),
                              "anomaly_cnt": s.get("anomaly_cnt")}
        print(f"[阶段2 体检] {cost:.1f}s  dup_rows={s.get('dup_rows')} anomaly={s.get('anomaly_cnt')}")

        # ---- 阶段 3：清洗（5 动作链全量执行） ----
        _stage = "clean"
        t0 = time.perf_counter()
        ex = http_json("POST", "/api/clean/execute",
                       {"file_id": f_id, "actions": ACTIONS})
        cost = time.perf_counter() - t0
        assert ex.get("code") == 0, str(ex)[:300]
        d = ex["data"]
        results["clean"] = {"sec": round(cost, 1),
                            "rows_before": d.get("stats", {}).get("total_rows_before"),
                            "rows_after": d.get("stats", {}).get("total_rows_after"),
                            "out_file_id": d.get("output_file_id")}
        print(f"[阶段3 清洗] {cost:.1f}s  "
              f"{d.get('stats', {}).get('total_rows_before')} → {d.get('stats', {}).get('total_rows_after')} 行")

        # ---- 清理：先回滚清洗记录（删产物+记录），再删原文件 ----
        _stage = "cleanup"
        recs = http_json("GET", f"/api/clean/records?file_id={f_id}")["data"]["records"]
        for r in recs:
            try:
                http_json("POST", f"/api/clean/records/{r['id']}/rollback")
            except Exception:
                pass
        try:
            http_json("DELETE", f"/api/files/{f_id}")
        except Exception:
            pass
        print(f"[OK] 已清理（回滚 {len(recs)} 条清洗记录 + 删除原文件）")
    finally:
        _stage = "idle"
        time.sleep(0.5)
        _stop.set()
        th.join(timeout=2)

    print("\n================ 性能实测结果（100 万行） ================")
    for k, v in results.items():
        mem = _peak.get(k) or _peak.get("idle") or {}
        print(f"{k:8s} 耗时 {v['sec']:>7}s  "
              f"服务进程内存峰值: 私有 {mem.get('priv', 0):.0f} MB / 工作集 {mem.get('ws', 0):.0f} MB  {v}")
    (ROOT / "data" / "test").mkdir(parents=True, exist_ok=True)
    out = ROOT / "data" / "test" / "perf_1m_result.json"
    out.write_text(json.dumps({"results": results, "memory_peak": _peak},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 结果已写 {out}")


if __name__ == "__main__":
    main()
