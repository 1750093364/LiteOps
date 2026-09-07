# -*- coding: utf-8 -*-
"""卡11 AI 构架/精炼与防幻觉 —— 端到端自测脚本。

策略：
- 本机起一个假的 OpenAI 兼容服务（端口 8099），按 system prompt 类型返回预设内容
  （构架 JSON 带代码块围栏、精炼文本故意外造数字、401 鉴权失败、slow-model 延时）；
- 自测 uvicorn 实例跑在 8010（LITEOPS_AI_TIMEOUT=1，勿扰 8000 用户进程）；
- 覆盖：配置读写脱敏、测试连接（成功/401/连不通）、离线拦截、构架防幻觉标红、
  精炼数字保留/外造标红/丢失告警、脱敏字段不入请求体、超时、导出拦截、埋点。
测试结束清理：恢复 ai_config 原值、删除本脚本创建的报告与 AI 埋点。

用法：.venv\\Scripts\\python.exe scripts\\test_card11.py
"""
import http.server
import json
import os
import socketserver
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "liteops.db"
BASE = "http://127.0.0.1:8010"
FAKE_PORT = 8099
FAKE_BASE = f"http://127.0.0.1:{FAKE_PORT}/v1"
FAKE_KEY = "sk-test-fake"
FAKE_MODEL = "fake-model"

PASS, FAIL = 0, 0
REQUESTS = []  # 假服务收到的全部请求体（原文）

FRAMEWORK_RESP = """好的，以下是为你生成的框架建议：
```json
{"sections":[
 {"title":"背景与目标","points":["说明大促背景：活动 2026-09-01 启动，GMV 目标 12,345 万元","以 amount 合计 8700.5 万元为对比基线【数值】"]},
 {"title":"指标口径说明","points":["明确 GMV 与转化率统计口径","可参考行业均值约 35.7% 作外部对比基准"]},
 {"title":"数据表现","points":["分渠道展示转化率趋势","按日跟踪目标达成率【数值】"]},
 {"title":"归因分析","points":["拆解高转化渠道成因","结合活动节奏分析波动原因"]},
 {"title":"结论与建议","points":["输出渠道预算调整建议","列出下一步行动项"]},
 {"title":"附录","points":["附数据来源与字段说明","附计算口径明细"]}
]}
```
请查收。"""

POLISH_RESP = ("经梳理，本期 GMV 达 12,345 万元，转化率为 87%；活动自 2026-09-01 启动。\n\n"
               "值得注意的是，行业平均转化率约 42.0%，2025 年大盘均值约 5,000 万元。")
POLISH_DROP_RESP = "本期 GMV 为 12,345 万元，活动于 2026-09-01 启动。"


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")


# ==================== 假 OpenAI 兼容服务 ====================

class FakeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        REQUESTS.append(raw)
        req = json.loads(raw)
        if self.headers.get("Authorization", "") != f"Bearer {FAKE_KEY}":
            self._send(401, {"error": {"message": "invalid api key", "type": "authentication_error"}})
            return
        model = req.get("model", "")
        msgs = req.get("messages", [])
        sys_c = next((m.get("content", "") for m in msgs if m.get("role") == "system"), "")
        usr_c = next((m.get("content", "") for m in msgs if m.get("role") == "user"), "")
        if model == "slow-model":
            time.sleep(3)  # 超过客户端 1s 超时，触发 APITimeoutError
        if "连通性测试" in sys_c:
            content = "pong"
        elif "报告框架助手" in sys_c:
            content = FRAMEWORK_RESP
        elif "语言编辑" in sys_c:
            content = POLISH_DROP_RESP if "DROPTEST" in usr_c else POLISH_RESP
        else:
            content = "ok"
        self._send(200, {
            "id": "chatcmpl-fake", "object": "chat.completion", "created": 0,
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
        })


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


# ==================== HTTP 工具 ====================

def call(method, path, payload=None, timeout=60):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            body = json.loads(raw.decode("utf-8")) if "json" in headers.get("content-type", "") else None
            return resp.status, body, headers, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw.decode("utf-8")), {k.lower(): v for k, v in e.headers.items()}, raw


def wait_health(proc, seconds=25):
    for _ in range(seconds * 2):
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def db_query(sql, args=()):
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def main():
    # 0. 前置：记录 ai_config 原值与最大埋点 id（结束恢复/清理）
    conn = sqlite3.connect(str(DB))
    row = conn.execute("SELECT value FROM settings WHERE key='ai_config'").fetchone()
    orig_cfg = row[0] if row else None
    max_ev = conn.execute("SELECT COALESCE(MAX(id),0) FROM event_logs").fetchone()[0]
    conn.close()

    fake = ThreadingServer(("127.0.0.1", FAKE_PORT), FakeHandler)
    import threading
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    print(f"假 OpenAI 服务已启动 :{FAKE_PORT}")

    env = dict(os.environ)
    env["LITEOPS_AI_TIMEOUT"] = "1"  # 超时测试用：1s
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8010", "--host", "127.0.0.1"],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_health(proc):
            print("✗ 8010 自测实例启动失败")
            return 1
        print("自测 uvicorn 实例已启动 :8010（AI 超时 1s）\n")

        # ---------- 1. 配置读取脱敏 ----------
        print("【1】AI 配置读写与脱敏")
        st, body, _, _ = call("GET", "/api/settings/ai")
        check("GET /api/settings/200", st == 200 and body["code"] == 0)
        check("默认 has_key=false", body["data"]["has_key"] is False)
        check("默认返回脱敏字段", "ai_api_key_masked" in body["data"])

        st, body, _, _ = call("PUT", "/api/settings/ai", {
            "ai_base_url": FAKE_BASE, "ai_api_key": FAKE_KEY,
            "ai_model": FAKE_MODEL, "offline_mode": False})
        check("保存配置 code=0", body["code"] == 0)
        check("保存后 has_key=true", body["data"]["has_key"] is True)
        check("Key 脱敏返回 sk-****fake", body["data"]["ai_api_key_masked"] == "sk-****fake")
        check("响应中绝不明文返回 Key", FAKE_KEY not in json.dumps(body, ensure_ascii=False))

        st, body, _, _ = call("PUT", "/api/settings/ai", {
            "ai_base_url": FAKE_BASE, "ai_model": FAKE_MODEL, "offline_mode": False})
        check("不带 api_key 保存不丢 Key", body["code"] == 0 and body["data"]["has_key"] is True)

        # ---------- 2. 测试连接 ----------
        print("\n【2】测试连接")
        st, body, _, _ = call("POST", "/api/settings/ai/test", {
            "ai_base_url": FAKE_BASE, "ai_model": FAKE_MODEL})
        check("正确配置测试成功", body["code"] == 0 and body["data"]["ok"] is True,
              str(body)[:200])
        check("测试成功消息含模型", "fake-model" in body["data"]["msg"])

        st, body, _, _ = call("POST", "/api/settings/ai/test", {
            "ai_base_url": FAKE_BASE, "ai_model": FAKE_MODEL, "ai_api_key": "sk-wrong-key"})
        check("错误 Key → ok=false", body["code"] == 0 and body["data"]["ok"] is False)
        check("401 错误中文提示", "Key" in body["data"]["msg"] or "401" in body["data"]["msg"],
              body["data"]["msg"])

        # 不存在域名（RFC 保留 .invalid TLD）：DNS 立即失败 → APIConnectionError
        st, body, _, _ = call("POST", "/api/settings/ai/test", {
            "ai_base_url": "http://ai-host-not-exist-liteops.invalid/v1", "ai_model": FAKE_MODEL})
        check("连不通地址 → ok=false", body["code"] == 0 and body["data"]["ok"] is False)
        check("连接错误中文提示", "无法连接" in body["data"]["msg"], body["data"]["msg"])

        # ---------- 3. 离线模式拦截 ----------
        print("\n【3】离线模式拦截")
        call("PUT", "/api/settings/ai", {"offline_mode": True})
        st, body, _, _ = call("POST", "/api/ai/framework", {"topic": "测试", "fields": []})
        check("离线时构架被拦截 code!=0", body["code"] != 0 and "离线" in body["msg"], body.get("msg"))
        st, body, _, _ = call("POST", "/api/ai/polish", {"text": "测试文本 123", "mode": "professional"})
        check("离线时精炼被拦截 code!=0", body["code"] != 0 and "离线" in body["msg"], body.get("msg"))
        st, body, _, _ = call("POST", "/api/settings/ai/test", {})
        check("离线时测试连接提示关闭离线", body["code"] == 0 and body["data"]["ok"] is False
              and "离线" in body["data"]["msg"])
        call("PUT", "/api/settings/ai", {"offline_mode": False})

        # 未配 Key 拦截（先清 Key）
        call("PUT", "/api/settings/ai", {"ai_api_key": ""})
        st, body, _, _ = call("POST", "/api/ai/framework", {"topic": "测试"})
        check("无 Key 时构架被拦截", body["code"] != 0 and "Key" in body["msg"], body.get("msg"))
        call("PUT", "/api/settings/ai", {"ai_api_key": FAKE_KEY, "ai_base_url": FAKE_BASE,
                                         "ai_model": FAKE_MODEL})

        # ---------- 4. AI 构架 + 防幻觉 ----------
        print("\n【4】AI 构架（白名单外数字标红）")
        REQUESTS.clear()
        topic = "双11 大促复盘，活动 2026-09-01 启动，GMV 目标 12,345 万元"
        st, body, _, _ = call("POST", "/api/ai/framework", {
            "topic": topic, "template_type": "复盘",
            "fields": ["amount", "channel", "user_phone"],
            "masked_fields": ["user_phone"],
            "refs": [{"field": "amount", "agg_label": "合计", "value": 8700.5}],
        })
        check("构架生成 code=0", body["code"] == 0, str(body)[:300])
        if body["code"] == 0:
            d = body["data"]
            html = d["html"]
            check("返回 6 个章节", len(d["sections"]) == 6, str(len(d["sections"])))
            check("HTML 含 6 个 h2", html.count("<h2>") == 6)
            check("外造数字 35.7% 被标红",
                  'class="unsourced" title="无数据来源，请核对或删除">35.7%</span>' in html)
            check("unsourced_numbers_cnt=1", d["unsourced_numbers_cnt"] == 1,
                  str(d["unsourced_numbers_cnt"]))
            check("白名单数字 12,345 不标红",
                  '>12,345</span>' not in html and 'unsourced" title="无数据来源，请核对或删除">12,345' not in html)
            check("白名单日期 2026-09-01 不标红", "2026-09-01" in html and
                  'unsourced" title="无数据来源，请核对或删除">2026-09-01' not in html)
            check("白名单引用值 8700.5 不标红", "8700.5" in html and
                  'unsourced" title="无数据来源，请核对或删除">8700.5' not in html)
            check("duration_s 存在", isinstance(d["duration_s"], (int, float)))

        # 脱敏：假服务收到的请求体不含 user_phone，含 [已脱敏字段]
        joined = "\n".join(REQUESTS)
        check("请求体不含脱敏字段原名 user_phone", "user_phone" not in joined,
              "脱敏失败：user_phone 出现在请求中")
        check("请求体含 [已脱敏字段] 占位", "[已脱敏字段]" in joined)
        fw_req = json.loads(REQUESTS[-1])
        fw_user_msg = next(m["content"] for m in fw_req["messages"] if m["role"] == "user")
        check("请求仅含字段名（amount/channel）与聚合值",
              "amount" in fw_user_msg and "channel" in fw_user_msg and "8700.5" in fw_user_msg)

        # 空主题
        st, body, _, _ = call("POST", "/api/ai/framework", {"topic": "   "})
        check("空主题 code!=0", body["code"] != 0)

        # ---------- 5. AI 精炼 ----------
        print("\n【5】AI 精炼（数字一个不丢 / 外造标红 / 丢失告警）")
        REQUESTS.clear()
        src_text = "本期 GMV 为 12,345 万元，转化率 87%，活动于 2026-09-01 启动。"
        st, body, _, _ = call("POST", "/api/ai/polish", {
            "text": src_text, "mode": "professional",
            "fields": ["amount", "user_phone"], "masked_fields": ["user_phone"],
            "refs": [{"field": "amount", "agg_label": "合计", "value": 8700.5}],
        })
        check("精炼 code=0", body["code"] == 0, str(body)[:300])
        if body["code"] == 0:
            d = body["data"]
            h = d["html"]
            check("结果分段为 <p>", "<p>" in h)
            check("原文 12,345 保留且不标红", "12,345" in h and
                  'unsourced" title="无数据来源，请核对或删除">12,345' not in h)
            check("原文 87% 保留且不标红", "87%" in h and
                  'unsourced" title="无数据来源，请核对或删除">87%' not in h)
            check("原文日期 2026-09-01 保留且不标红", "2026-09-01" in h and
                  'unsourced" title="无数据来源，请核对或删除">2026-09-01' not in h)
            check("外造 42.0% 标红", '>42.0%</span>' in h)
            check("外造年份 2025 标红", ">2025</span>" in h)
            check("外造 5,000 标红", ">5,000</span>" in h)
            check("unsourced_numbers_cnt=3", d["unsourced_numbers_cnt"] == 3,
                  str(d["unsourced_numbers_cnt"]))
            check("无丢失数字", d["dropped_numbers"] == [], str(d["dropped_numbers"]))
        joined = "\n".join(REQUESTS)
        check("精炼请求体不含脱敏字段名", "user_phone" not in joined)

        # 丢失数字告警
        st, body, _, _ = call("POST", "/api/ai/polish", {
            "text": src_text + " DROPTEST", "mode": "structured"})
        check("丢数字场景 code=0", body["code"] == 0)
        if body["code"] == 0:
            check("dropped_numbers 报出 87%",
                  any("87" in x for x in body["data"]["dropped_numbers"]),
                  str(body["data"]["dropped_numbers"]))

        # 空文本 / 非法模式
        st, body, _, _ = call("POST", "/api/ai/polish", {"text": "", "mode": "professional"})
        check("空文本 code!=0", body["code"] != 0)
        st, body, _, _ = call("POST", "/api/ai/polish", {"text": "abc 123", "mode": "wrong"})
        check("非法模式 code!=0", body["code"] != 0)

        # ---------- 6. 超时 ----------
        print("\n【6】超时处理（1s 超时 vs slow-model 睡 3s）")
        call("PUT", "/api/settings/ai", {"ai_model": "slow-model"})
        t0 = time.time()
        st, body, _, _ = call("POST", "/api/ai/framework", {"topic": "超时测试主题"})
        elapsed = time.time() - t0
        check("超时返回 code!=0", body["code"] != 0 and "超时" in body["msg"], body.get("msg"))
        check("超时在 ~1s 触发（未等 30s）", elapsed < 10, f"{elapsed:.1f}s")
        call("PUT", "/api/settings/ai", {"ai_model": FAKE_MODEL})

        # ---------- 7. 导出拦截（前后端双重） ----------
        print("\n【7】无来源数字导出拦截")
        st, body, _, _ = call("POST", "/api/reports", {"title": "卡11自测-导出拦截"})
        rid = body["data"]["id"]
        bad_html = ('<p>正常段落</p><span class="unsourced" title="无数据来源，请核对或删除">'
                    '35.7%</span><p>尾段</p>')
        call("PUT", f"/api/reports/{rid}", {"content_html": bad_html, "refs_json": "[]"})
        st, body, _, _ = call("POST", f"/api/reports/{rid}/export/word")
        check("含 unsourced → Word 导出被拦截", body["code"] != 0 and "无数据来源" in body["msg"],
              str(body)[:200])
        st, body, headers, raw = call("GET", f"/api/reports/{rid}/print-view")
        check("含 unsourced → 打印视图被拦截", body is not None and body.get("code") != 0
              and "无数据来源" in body.get("msg", ""), str(body)[:200])
        call("PUT", f"/api/reports/{rid}", {"content_html": "<h1>干净报告</h1><p>无外造数字</p>",
                                            "refs_json": "[]"})
        st, body, headers, raw = call("POST", f"/api/reports/{rid}/export/word")
        check("清除后 Word 导出恢复（docx 字节）", raw[:2] == b"PK" and body is None)
        st, body, headers, raw = call("GET", f"/api/reports/{rid}/print-view")
        check("清除后打印视图恢复（HTML）", "干净报告".encode("utf-8") in raw and body is None)
        call("DELETE", f"/api/reports/{rid}")

        # ---------- 8. 埋点 ----------
        print("\n【8】埋点 ai_framework_gen / ai_polish")
        evs = db_query("SELECT event, params_json FROM event_logs WHERE id > ? ORDER BY id", (max_ev,))
        fw_ok = [e for e in evs if e["event"] == "ai_framework_gen"]
        pol_ok = [e for e in evs if e["event"] == "ai_polish"]
        check("ai_framework_gen 埋点存在", len(fw_ok) >= 1)
        if fw_ok:
            succ = [json.loads(e["params_json"]) for e in fw_ok
                    if json.loads(e["params_json"])["result"] == "success"]
            fails = [json.loads(e["params_json"]) for e in fw_ok
                     if json.loads(e["params_json"])["result"] == "failed"]
            check("构架成功埋点带 duration_s/unsourced=1",
                  any(s.get("unsourced_numbers_cnt") == 1 and s.get("duration_s") is not None
                      for s in succ))
            check("构架失败埋点（离线/超时）存在", len(fails) >= 2,
                  f"failed={len(fails)}")
        check("ai_polish 埋点存在", len(pol_ok) >= 1)
        if pol_ok:
            succ = [json.loads(e["params_json"]) for e in pol_ok
                    if json.loads(e["params_json"])["result"] == "success"]
            check("精炼成功埋点含 unsourced/dropped 计数",
                  any(s.get("unsourced_numbers_cnt") == 3 and s.get("dropped_numbers_cnt") == 0
                      for s in succ) and
                  any(s.get("dropped_numbers_cnt") == 1 for s in succ))

    finally:
        # 清理：恢复 ai_config、删除本脚本埋点
        conn = sqlite3.connect(str(DB))
        if orig_cfg is None:
            conn.execute("DELETE FROM settings WHERE key='ai_config'")
        else:
            conn.execute("UPDATE settings SET value=? WHERE key='ai_config'", (orig_cfg,))
        conn.execute("DELETE FROM event_logs WHERE id > ?", (max_ev,))
        conn.commit()
        conn.close()
        fake.shutdown()
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"\n{'='*50}\n结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
