# -*- coding: utf-8 -*-
"""全局路径常量与运行时配置。导入时自动创建数据目录。

演示模式（卡14）：环境变量 LITEOPS_DEMO=1 开启，用于公网 Demo 部署。
- /api/health 与设置接口返回 demo_mode:true；
- AI 配置优先读取环境变量 LITEOPS_AI_KEY / LITEOPS_AI_BASE_URL / LITEOPS_AI_MODEL，
  不回传 Key 明文，演示模式下前端隐藏 AI 录入与数据库直连入口；
- 首次启动（库为空）自动导入 examples/ 下的样例数据。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"
FILES_DIR = DATA_DIR / "files"          # 数据文件存储
DOCS_DIR = DATA_DIR / "docs"            # 文档库文件存储
SNAPSHOTS_DIR = DATA_DIR / "snapshots"  # 清洗前快照
WEB_DIR = BASE_DIR / "web"              # 前端静态资源
DB_PATH = DATA_DIR / "liteops.db"       # SQLite 数据库
EXAMPLES_DIR = BASE_DIR / "examples"    # 内置样例数据（演示模式自动导入）

# 启动即保证目录存在
for _dir in (DATA_DIR, FILES_DIR, DOCS_DIR, SNAPSHOTS_DIR):
    _dir.mkdir(parents=True, exist_ok=True)


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ==================== 演示模式 ====================
DEMO_MODE = _env_bool("LITEOPS_DEMO", False)

# ==================== AI 配置（环境变量优先，演示模式下锁定） ====================
# 环境变量存在时优先使用；本地模式下这些值为 None，回退到 settings 表存储。
ENV_AI_KEY = os.environ.get("LITEOPS_AI_KEY") or None
ENV_AI_BASE_URL = os.environ.get("LITEOPS_AI_BASE_URL") or None
ENV_AI_MODEL = os.environ.get("LITEOPS_AI_MODEL") or None


def env_ai_config_present() -> bool:
    """服务端是否通过环境变量注入了 AI 配置（Key 优先判定）。"""
    return bool(ENV_AI_KEY and ENV_AI_BASE_URL and ENV_AI_MODEL)
