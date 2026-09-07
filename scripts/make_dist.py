# -*- coding: utf-8 -*-
"""分发包打包脚本（卡14）。

两种模式：
  python scripts/make_dist.py          → dist/LiteOps_分发包.zip（种子用户，不含 deploy/）
  python scripts/make_dist.py --server → dist/LiteOps_server.zip（服务器部署，含 deploy/）

分发包（种子用户）：app/、web/、scripts/gen_test_data.py、examples/、docs/PRD.md、
  requirements.txt、run.bat、run.sh、README.md、首次使用指南.txt
  排除：venv/、data/、__pycache__、.git、dist/、deploy/

服务器包：app/、web/、scripts/、examples/、docs/PRD.md、requirements.txt、deploy/（整个目录）、
  README.md
  排除：venv/、data/、__pycache__、.git、dist/
  不含：首次使用指南.txt、run.bat（服务器用不到）
"""
import sys
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DIST_DIR = BASE_DIR / "dist"

# 排除的目录/文件名（任意层级匹配）
EXCLUDE_DIRS = {"__pycache__", ".git", "venv", ".venv", "data", "dist"}
EXCLUDE_EXTS = {".pyc", ".pyo"}

# ---- 分发包（种子用户）----
DIST_ZIP_NAME = "LiteOps_分发包.zip"
DIST_INCLUDE = [
    "app",
    "web",
    "scripts/gen_test_data.py",
    "examples",
    "docs/PRD.md",
    "requirements.txt",
    "run.bat",
    "run.sh",
    "README.md",
    "首次使用指南.txt",
]
# 分发包额外排除 deploy/（种子用户不需要部署脚本）
DIST_EXTRA_EXCLUDE = {"deploy"}

# ---- 服务器包 ----
SERVER_ZIP_NAME = "LiteOps_server.zip"
SERVER_INCLUDE = [
    "app",
    "web",
    "scripts",
    "examples",
    "docs/PRD.md",
    "requirements.txt",
    "deploy",
    "README.md",
]
# 服务器包额外排除种子用户专属文件（服务器用不到 run.bat 和首次使用指南）
SERVER_EXTRA_EXCLUDE = set()


def should_skip(path: Path, extra_exclude: set | None = None) -> bool:
    """判断路径是否应被排除（目录名或扩展名）。"""
    exclude_set = EXCLUDE_DIRS | (extra_exclude or set())
    for part in path.parts:
        if part in exclude_set:
            return True
    return path.suffix in EXCLUDE_EXTS


def collect_files(include_paths: list[str], extra_exclude: set | None = None) -> list[Path]:
    """收集所有需打包的文件（绝对路径）。"""
    files: list[Path] = []
    for rel in include_paths:
        p = BASE_DIR / rel
        if not p.exists():
            print(f"[WARN] 跳过不存在的路径: {rel}")
            continue
        if p.is_file():
            if not should_skip(p, extra_exclude):
                files.append(p)
        else:
            for f in p.rglob("*"):
                if f.is_file() and not should_skip(f, extra_exclude):
                    files.append(f)
    return files


def build_zip(zip_name: str, include_paths: list[str], extra_exclude: set | None, label: str):
    """打包一个 zip 并打印结果。"""
    files = collect_files(include_paths, extra_exclude)
    if not files:
        print(f"[FAIL] {label}: 没有可打包的文件")
        return

    zip_path = DIST_DIR / zip_name
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            arcname = f.relative_to(BASE_DIR).as_posix()
            zf.write(f, arcname)

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"[OK] {label}: {zip_path}")
    print(f"     共 {len(files)} 个文件，大小 {size_mb:.2f} MB")


def main():
    server_mode = "--server" in sys.argv

    if server_mode:
        print("===== 打包服务器部署包 =====")
        build_zip(SERVER_ZIP_NAME, SERVER_INCLUDE, SERVER_EXTRA_EXCLUDE, "服务器包")
    else:
        print("===== 打包种子用户分发包 =====")
        build_zip(DIST_ZIP_NAME, DIST_INCLUDE, DIST_EXTRA_EXCLUDE, "分发包")


if __name__ == "__main__":
    main()
