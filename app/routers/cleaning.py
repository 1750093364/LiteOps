# -*- coding: utf-8 -*-
"""数据清洗模块路由。

卡03：数据体检报告（/api/files/{id}/profile 系列）。
卡04：清洗引擎（/api/clean 前缀：preview / execute / rollback / records）。
卡05：清洗预设（/api/clean/presets CRUD + apply）与变更统计、埋点。
"""
import json
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import BASE_DIR
from app.db import get_conn
from app.services import file_service as fs
from app.services import profile_service as ps
from app.services import clean_service as cs
from app.services import preset_service as preset_svc

# 卡04 清洗动作执行前缀
router = APIRouter(prefix="/api/cleaning", tags=["cleaning"])
clean_router = APIRouter(prefix="/api/clean", tags=["cleaning"])

# 卡03 体检报告挂在 /api/files 前缀下（与文件资源同根）
profile_router = APIRouter(prefix="/api/files", tags=["cleaning"])


def _ok(data=None):
    return {"code": 0, "data": data}


def _fail(msg: str, code: int = 1):
    """业务失败：HTTP 200 + code!=0，避免前端拿到 500。"""
    return JSONResponse(status_code=200, content={"code": code, "msg": msg})


def _log_event(conn, event: str, params: dict) -> None:
    conn.execute(
        "INSERT INTO event_logs (event, params_json) VALUES (?, ?)",
        (event, json.dumps(params, ensure_ascii=False)),
    )


@router.get("")
def placeholder():
    """占位接口：清洗流程编排与执行将在卡04实现。"""
    return {"code": 0, "data": None}


# ==================== 数据体检报告 ====================

class ProfileBody(BaseModel):
    columns: list[str] | None = None  # 按指定列检测重复；不传 = 全列完全重复


class ConfirmBody(BaseModel):
    ids: list[int] = []


@profile_router.post("/{file_id}/profile")
def create_profile(file_id: int, body: ProfileBody | None = None):
    """对文件执行五类体检：结果存 clean_profiles，data_files.status 流转为「体检完成」。"""
    with get_conn() as conn:
        rec = conn.execute(
            """SELECT id, origin_name, file_type, path, encoding, status
               FROM data_files WHERE id = ?""",
            (file_id,),
        ).fetchone()
        if not rec:
            return _fail(f"文件不存在（id={file_id}）")
        field_names = [
            r["field_name"]
            for r in conn.execute(
                "SELECT field_name FROM data_fields WHERE file_id = ? ORDER BY id",
                (file_id,),
            ).fetchall()
        ]

    path = BASE_DIR / rec["path"]
    if not path.is_file():
        return _fail(f"物理文件已丢失（{rec['path']}），无法体检")

    dup_columns = None
    if body and body.columns:
        dup_columns = [str(c).strip() for c in body.columns if str(c).strip()]
        unknown = [c for c in dup_columns if c not in field_names]
        if unknown:
            return _fail(f"指定列不存在：{'、'.join(unknown)}")
        if not dup_columns:
            dup_columns = None

    try:
        report = ps.run_profile(path, rec["encoding"], rec["file_type"], dup_columns)
    except ValueError as e:
        return _fail(str(e))
    except Exception:
        return _fail("体检扫描失败，文件可能已损坏或格式不正确")

    report["file_id"] = file_id
    report["file_name"] = rec["origin_name"]

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO clean_profiles (file_id, report_json) VALUES (?, ?)",
            (file_id, json.dumps(report, ensure_ascii=False)),
        )
        profile_id = cur.lastrowid
        conn.execute(
            "UPDATE data_files SET status = '体检完成' WHERE id = ?", (file_id,)
        )
        _log_event(conn, "data_profile_view", {
            "file_id": file_id,
            "missing_fields_cnt": report["summary"]["missing_fields_cnt"],
            "dup_rows": report["summary"]["dup_rows"],
            "anomaly_cnt": report["summary"]["anomaly_cnt"],
            "duration_s": report["duration_sec"],
        })

    return _ok({"profile_id": profile_id, "report": report})


@profile_router.get("/{file_id}/profile")
def get_profile(file_id: int):
    """取最近一次体检报告；从未体检时返回 exists=false（附文件元信息与字段名）。"""
    with get_conn() as conn:
        rec = conn.execute(
            """SELECT id, origin_name, file_type, row_count, col_count,
                      status, encoding
               FROM data_files WHERE id = ?""",
            (file_id,),
        ).fetchone()
        if not rec:
            return _fail(f"文件不存在（id={file_id}）")
        fields = conn.execute(
            "SELECT field_name FROM data_fields WHERE file_id = ? ORDER BY id",
            (file_id,),
        ).fetchall()
        prof = conn.execute(
            """SELECT id, report_json, created_at FROM clean_profiles
               WHERE file_id = ? ORDER BY id DESC LIMIT 1""",
            (file_id,),
        ).fetchone()

    file_meta = {
        "id": rec["id"],
        "origin_name": rec["origin_name"],
        "file_type": rec["file_type"],
        "row_count": rec["row_count"],
        "col_count": rec["col_count"],
        "status": rec["status"],
        "encoding": rec["encoding"],
        "columns": [f["field_name"] for f in fields],
    }
    if not prof:
        return _ok({"exists": False, "file": file_meta})

    report = json.loads(prof["report_json"])
    return _ok({
        "exists": True,
        "profile_id": prof["id"],
        "created_at": prof["created_at"],
        "file": file_meta,
        "report": report,
    })


@profile_router.post("/{file_id}/profile/anomaly-confirm")
def confirm_anomalies(file_id: int, body: ConfirmBody):
    """用户勾选确认异常项：在最近一次报告 JSON 上回写 confirmed 标记。"""
    idset = {int(i) for i in (body.ids or [])}
    with get_conn() as conn:
        prof = conn.execute(
            """SELECT id, report_json FROM clean_profiles
               WHERE file_id = ? ORDER BY id DESC LIMIT 1""",
            (file_id,),
        ).fetchone()
        if not prof:
            return _fail("该文件尚未生成体检报告，无法确认异常项")
        report = json.loads(prof["report_json"])
        confirmed = 0
        for item in report.get("anomalies", {}).get("items", []):
            if item.get("id") in idset and not item.get("confirmed"):
                item["confirmed"] = True
                confirmed += 1
        conn.execute(
            "UPDATE clean_profiles SET report_json = ? WHERE id = ?",
            (json.dumps(report, ensure_ascii=False), prof["id"]),
        )
    return _ok({"confirmed": confirmed, "total": len(idset)})


# ==================== 卡04 清洗引擎 ====================

class ActionItem(BaseModel):
    type: str
    params: dict = {}


class PreviewBody(BaseModel):
    file_id: int
    actions: list[ActionItem] = []


class ExecuteBody(BaseModel):
    file_id: int
    actions: list[ActionItem] = []
    preset_id: int | None = None  # 套用预设执行时记录来源预设


def _get_file_record(conn, file_id: int):
    return conn.execute(
        """SELECT id, origin_name, file_type, path, encoding, status, row_count, col_count
           FROM data_files WHERE id = ?""",
        (file_id,),
    ).fetchone()


@clean_router.post("/preview")
def clean_preview(body: PreviewBody):
    """对前 1000 行样本依次试跑动作链，返回每动作影响行数与前后对比样例。"""
    if not body.actions:
        return _fail("请至少添加一个清洗动作")
    with get_conn() as conn:
        rec = _get_file_record(conn, body.file_id)
        if not rec:
            return _fail(f"文件不存在（id={body.file_id}）")
    path = BASE_DIR / rec["path"]
    if not path.is_file():
        return _fail(f"物理文件已丢失（{rec['path']}），无法预览")
    actions = [a.model_dump() for a in body.actions]
    try:
        result = cs.preview_actions(path, rec["encoding"], rec["file_type"], actions)
    except ValueError as e:
        return _fail(str(e))
    except Exception:
        return _fail("预览失败，文件可能已损坏")
    result["file_id"] = body.file_id
    return _ok(result)


@clean_router.post("/execute")
def clean_execute(body: ExecuteBody):
    """全量执行动作链：存快照 → 跑动作 → 落 CSV 产物 → 回写 data_files / data_fields / clean_records。"""
    if not body.actions:
        return _fail("请至少添加一个清洗动作")
    with get_conn() as conn:
        rec = _get_file_record(conn, body.file_id)
        if not rec:
            return _fail(f"文件不存在（id={body.file_id}）")
        original_status = rec["status"]

    path = BASE_DIR / rec["path"]
    if not path.is_file():
        return _fail(f"物理文件已丢失（{rec['path']}），无法执行清洗")

    actions = [a.model_dump() for a in body.actions]
    started = time.time()
    out_file_id = None
    snapshot_path = None
    try:
        result = cs.execute_actions(
            path, rec["encoding"], rec["file_type"], actions,
            rec["origin_name"], rec["id"],
        )
        out_path = Path(result["output_path"])
        snapshot_path = result["snapshot_path"]
        stats = result["stats"]
        stats["duration_sec"] = round(time.time() - started, 2)
        stats["original_status"] = original_status

        # 重新推断产物字段
        out_enc = "utf-8-sig"
        field_info = fs.analyze_csv(out_path, out_enc)

        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO data_files
                   (origin_name, name, file_type, source, path, encoding,
                    row_count, col_count, size_bytes, status, parent_file_id)
                   VALUES (?, ?, 'csv', 'cleaned', ?, ?, ?, ?, ?, '清洗完成', ?)""",
                (result["origin_name"], result["origin_name"],
                 result["output_relative"], out_enc,
                 result["row_count"], result["col_count"], result["size_bytes"],
                 rec["id"]),
            )
            out_file_id = cur.lastrowid
            conn.executemany(
                """INSERT INTO data_fields
                   (file_id, field_name, inferred_type, missing_rate, sample_values)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (out_file_id, f["field_name"], f["inferred_type"],
                     f["missing_rate"], json.dumps(f["sample_values"], ensure_ascii=False))
                    for f in field_info["fields"]
                ],
            )
            # 原文件状态流转为「清洗完成」（回滚时恢复 original_status）
            conn.execute(
                "UPDATE data_files SET status = '清洗完成' WHERE id = ?",
                (rec["id"],),
            )
            cur2 = conn.execute(
                """INSERT INTO clean_records
                   (file_id, preset_id, actions_json, stats_json, output_file_id, snapshot_path)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (rec["id"], body.preset_id, json.dumps(actions, ensure_ascii=False),
                 json.dumps(stats, ensure_ascii=False), out_file_id, snapshot_path),
            )
            record_id = cur2.lastrowid
            _log_event(conn, "clean_execute", {
                "file_id": rec["id"],
                "output_file_id": out_file_id,
                "rows_removed": stats["rows_removed"],
                "fields_converted": stats["fields_converted_cnt"],
                "missing_filled": stats["missing_filled"],
                "actions_cnt": stats["actions_count"],
                "preset_id": body.preset_id,
                "duration_s": stats["duration_sec"],
            })
    except ValueError as e:
        # 业务异常：清理已落盘的半成品（含快照）
        _cleanup_output(out_file_id, snapshot_path)
        return _fail(str(e))
    except Exception:
        _cleanup_output(out_file_id, snapshot_path)
        return _fail("清洗执行失败，已回滚半成品，请检查动作参数后重试")

    return _ok({
        "record_id": record_id,
        "output_file_id": out_file_id,
        "output_name": result["origin_name"],
        "stats": stats,
    })


def _cleanup_output(output_file_id: int | None, snapshot_path: str | None = None) -> None:
    """清理半成品：删产物文件 + data_files + data_fields + clean_records + 快照。"""
    if output_file_id is not None:
        with get_conn() as conn:
            rec = conn.execute(
                "SELECT path FROM data_files WHERE id = ?", (output_file_id,)
            ).fetchone()
            if rec:
                fs.remove_physical_file(rec["path"])
            conn.execute("DELETE FROM data_fields WHERE file_id = ?", (output_file_id,))
            conn.execute("DELETE FROM data_files WHERE id = ?", (output_file_id,))
            conn.execute("DELETE FROM clean_records WHERE output_file_id = ?", (output_file_id,))
    if snapshot_path:
        fs.remove_physical_file(snapshot_path)


@clean_router.get("/records")
def clean_records_list(file_id: int):
    """列出某文件的清洗记录（含预设名/产物信息/变更统计，供时间线回看与回滚）。"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT r.id, r.file_id, r.preset_id, r.actions_json, r.stats_json,
                      r.output_file_id, r.snapshot_path, r.created_at,
                      p.name AS preset_name, p.is_builtin AS preset_builtin,
                      o.origin_name AS output_name, o.row_count AS output_rows,
                      o.col_count AS output_cols
               FROM clean_records r
               LEFT JOIN data_files o ON o.id = r.output_file_id
               LEFT JOIN clean_presets p ON p.id = r.preset_id
               WHERE r.file_id = ?
               ORDER BY r.id DESC""",
            (file_id,),
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        try:
            d["actions"] = json.loads(d["actions_json"] or "[]")
        except json.JSONDecodeError:
            d["actions"] = []
        try:
            d["stats"] = json.loads(d["stats_json"] or "{}")
        except json.JSONDecodeError:
            d["stats"] = {}
        d.pop("actions_json", None)
        d.pop("stats_json", None)
        items.append(d)
    return _ok({"file_id": file_id, "records": items})


@clean_router.post("/records/{record_id}/rollback")
def clean_rollback(record_id: int):
    """回滚清洗记录：删除产物文件与 data_files 记录，恢复原文件状态。"""
    with get_conn() as conn:
        rec = conn.execute(
            """SELECT id, file_id, output_file_id, snapshot_path, stats_json
               FROM clean_records WHERE id = ?""",
            (record_id,),
        ).fetchone()
        if not rec:
            return _fail(f"清洗记录不存在（id={record_id}）")
        file_id = rec["file_id"]
        output_file_id = rec["output_file_id"]
        try:
            stats = json.loads(rec["stats_json"] or "{}")
        except json.JSONDecodeError:
            stats = {}
        original_status = stats.get("original_status") or "已导入"

        # 删除产物文件与记录
        if output_file_id:
            out = conn.execute(
                "SELECT path FROM data_files WHERE id = ?", (output_file_id,)
            ).fetchone()
            if out:
                fs.remove_physical_file(out["path"])
            conn.execute("DELETE FROM data_fields WHERE file_id = ?", (output_file_id,))
            conn.execute("DELETE FROM data_files WHERE id = ?", (output_file_id,))

        # 删除快照文件
        if rec["snapshot_path"]:
            fs.remove_physical_file(rec["snapshot_path"])

        # 删除清洗记录
        conn.execute("DELETE FROM clean_records WHERE id = ?", (record_id,))

        # 恢复原文件状态
        conn.execute(
            "UPDATE data_files SET status = ? WHERE id = ?",
            (original_status, file_id),
        )
        _log_event(conn, "clean_rollback", {
            "file_id": file_id,
            "record_id": record_id,
            "output_file_id": output_file_id,
        })
    return _ok({"record_id": record_id, "file_id": file_id})


# ==================== 卡05 清洗预设 ====================

class PresetBody(BaseModel):
    name: str
    scenario: str = ""
    actions: list[ActionItem] = []


class PresetUpdateBody(BaseModel):
    name: str | None = None
    scenario: str | None = None
    actions: list[ActionItem] | None = None


class ApplyBody(BaseModel):
    file_id: int


def _parse_actions(raw: str | None) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


@clean_router.get("/presets")
def preset_list():
    """预设列表：内置在前（不可删改），自建在后；含动作数。"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, name, scenario, actions_json, is_builtin, created_at
               FROM clean_presets ORDER BY is_builtin DESC, id""",
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        actions = _parse_actions(d["actions_json"])
        d["actions"] = actions
        d["actions_cnt"] = len(actions)
        d.pop("actions_json", None)
        items.append(d)
    return _ok({"presets": items})


@clean_router.post("/presets")
def preset_create(body: PresetBody):
    """保存当前编排为自建预设；写埋点 clean_preset_save。"""
    name = body.name.strip()
    if not name:
        return _fail("预设名称不能为空")
    if len(name) > 50:
        return _fail("预设名称不能超过 50 字")
    if not body.actions:
        return _fail("当前流程为空，请先编排至少一个清洗动作再保存")
    actions = [a.model_dump() for a in body.actions]
    with get_conn() as conn:
        dup = conn.execute(
            "SELECT id FROM clean_presets WHERE name = ? AND is_builtin = 0", (name,)
        ).fetchone()
        if dup:
            return _fail(f"已存在同名预设「{name}」，请换个名称")
        cur = conn.execute(
            """INSERT INTO clean_presets (name, scenario, actions_json, is_builtin)
               VALUES (?, ?, ?, 0)""",
            (name, body.scenario.strip(), json.dumps(actions, ensure_ascii=False)),
        )
        preset_id = cur.lastrowid
        _log_event(conn, "clean_preset_save", {
            "preset_id": preset_id,
            "actions_cnt": len(actions),
            "scenario_tag": (body.scenario.strip() or "未分类")[:20],
        })
    return _ok({"preset_id": preset_id, "name": name})


@clean_router.put("/presets/{preset_id}")
def preset_update(preset_id: int, body: PresetUpdateBody):
    """修改自建预设（内置预设不可修改）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, is_builtin FROM clean_presets WHERE id = ?", (preset_id,)
        ).fetchone()
        if not row:
            return _fail(f"预设不存在（id={preset_id}）")
        if row["is_builtin"]:
            return _fail("内置预设不可修改")
        sets, vals = [], []
        if body.name is not None:
            name = body.name.strip()
            if not name:
                return _fail("预设名称不能为空")
            sets.append("name = ?")
            vals.append(name)
        if body.scenario is not None:
            sets.append("scenario = ?")
            vals.append(body.scenario.strip())
        if body.actions is not None:
            if not body.actions:
                return _fail("预设至少需要包含一个清洗动作")
            sets.append("actions_json = ?")
            vals.append(json.dumps(
                [a.model_dump() for a in body.actions], ensure_ascii=False))
        if not sets:
            return _fail("未提供任何需要修改的内容")
        vals.append(preset_id)
        conn.execute(
            f"UPDATE clean_presets SET {', '.join(sets)} WHERE id = ?", vals
        )
    return _ok({"preset_id": preset_id})


@clean_router.delete("/presets/{preset_id}")
def preset_delete(preset_id: int):
    """删除自建预设；内置预设禁止删除。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, is_builtin FROM clean_presets WHERE id = ?", (preset_id,)
        ).fetchone()
        if not row:
            return _fail(f"预设不存在（id={preset_id}）")
        if row["is_builtin"]:
            return _fail(f"内置预设「{row['name']}」不可删除")
        conn.execute("DELETE FROM clean_presets WHERE id = ?", (preset_id,))
    return _ok({"preset_id": preset_id})


@clean_router.post("/presets/{preset_id}/apply")
def preset_apply(preset_id: int, body: ApplyBody):
    """套用预设：展开符号字段（如 __all_text__）后返回动作链；写埋点 clean_preset_apply。"""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT id, name, actions_json, is_builtin
               FROM clean_presets WHERE id = ?""",
            (preset_id,),
        ).fetchone()
        if not row:
            return _fail(f"预设不存在（id={preset_id}）")
        file_rec = conn.execute(
            "SELECT id FROM data_files WHERE id = ?", (body.file_id,)
        ).fetchone()
        if not file_rec:
            return _fail(f"文件不存在（id={body.file_id}）")
        actions = _parse_actions(row["actions_json"])
        if not actions:
            return _fail("预设动作配置为空或已损坏")
        expanded = preset_svc.expand_text_symbol(conn, body.file_id, actions)
        _log_event(conn, "clean_preset_apply", {
            "preset_id": preset_id,
            "is_builtin": row["is_builtin"],
            "actions_cnt": len(expanded),
            "file_id": body.file_id,
        })
    return _ok({
        "preset_id": preset_id,
        "preset_name": row["name"],
        "is_builtin": row["is_builtin"],
        "actions": expanded,
    })
