# -*- coding: utf-8 -*-
"""轻析 LiteOps 应用入口：托管 /api 接口与 web/ 静态资源（单端口 8000）。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import WEB_DIR, DEMO_MODE
from app.db import init_db, get_conn
from app.services import preset_service, ticket_service
from app.routers import ai, cleaning, dbsource, docs, files, reports, settings, snippets, tickets
from app.services.demo_seed import ensure_demo_samples


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    preset_service.ensure_builtin_presets()
    ticket_service.ensure_default_weights()  # 卡08：排序权重种子 0.5/0.3/0.2
    if DEMO_MODE:
        ensure_demo_samples()  # 卡14：演示模式首次启动自动导入内置样例
    yield


app = FastAPI(title="轻析 LiteOps", version="1.0", lifespan=lifespan)


@app.get("/api/health", tags=["system"])
def health():
    return {"code": 0, "data": {"status": "ok", "version": "1.0", "demo_mode": DEMO_MODE}}


# 注册业务路由（骨架均为占位接口）
for _module in (files, cleaning, snippets, docs, tickets, reports, ai, settings, dbsource):
    app.include_router(_module.router)

# 分类目录路由（在 files.py 内定义，独立前缀 /api/categories）
app.include_router(files.cat_router)

# 卡09 报告框架模板路由（在 reports.py 内定义，独立前缀 /api/report-templates）
app.include_router(reports.tpl_router)

# 卡03 数据体检报告路由（在 cleaning.py 内定义，前缀 /api/files）
app.include_router(cleaning.profile_router)

# 卡04 清洗引擎路由（在 cleaning.py 内定义，前缀 /api/clean）
app.include_router(cleaning.clean_router)

# 卡11 AI 设置路由（在 ai.py 内定义，前缀 /api/settings）
app.include_router(ai.ai_settings_router)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """统一异常响应：/api 返回统一 JSON；其余路径 404 回退 index.html（SPA）。"""
    if exc.status_code == 404 and not request.url.path.startswith("/api"):
        return FileResponse(WEB_DIR / "index.html")
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "msg": str(exc.detail)},
    )


# 静态资源托管：挂在最后，不抢占 /api 路由
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
