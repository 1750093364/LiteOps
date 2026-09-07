#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# Force UTF-8 for Python/pip (avoid GBK decode errors on zh-CN Windows)
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

# 无 venv 则创建
if [ ! -d ".venv" ]; then
    echo "[LiteOps] First run: creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

# 依赖缺失时自动安装
python -c "import fastapi" 2>/dev/null || pip install -r requirements.txt

echo "[LiteOps] Starting server at http://127.0.0.1:8000"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
