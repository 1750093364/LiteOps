#!/usr/bin/env bash
# =============================================================================
# LiteOps Demo 一键部署脚本（卡14）
# 目标：Ubuntu 22.04 LTS，root 身份执行，幂等可重复运行。
# 作用：安装依赖 -> 同步代码到 /opt/liteops -> 建 venv -> 配置 env ->
#       装 systemd 服务 -> 配 nginx 反代 + BasicAuth -> 自检。
# 安全：所有密码/Key 仅通过交互输入写入 /etc/liteops/liteops.env（chmod 600），
#       不写进仓库、不打印明文。
# =============================================================================
set -e

# ---------- 颜色 ----------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }

# ---------- 0. 必须 root ----------
if [ "$(id -u)" -ne 0 ]; then
  error "请用 root 身份执行：sudo bash $0"
fi

SRC_DIR="/root/upload/liteops"
DST_DIR="/opt/liteops"
ENV_FILE="/etc/liteops/liteops.env"
HTPASSWD_FILE="/etc/nginx/.htpasswd_liteops"
SERVICE_FILE="/etc/systemd/system/liteops.service"

# ---------- 1. 安装系统依赖 ----------
info "更新 apt 索引..."
apt-get update -y

PYTHON_BIN=""
PY_VER=""

# 优先 deadsnakes 装 3.11；失败回退系统自带 3.10
info "安装 Python 3.11（deadsnakes）..."
if ! apt-get install -y software-properties-common 2>/dev/null; then
  warn "software-properties-common 安装失败，继续尝试"
fi
add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || warn "deadsnakes PPA 添加失败，回退系统 Python"
apt-get update -y

if apt-get install -y python3.11 python3.11-venv python3.11-dev 2>/dev/null; then
  PYTHON_BIN="python3.11"
  PY_VER="3.11"
  info "已安装 Python 3.11"
else
  warn "Python 3.11 安装失败，回退到系统自带 Python 3.10"
  apt-get install -y python3 python3-venv python3-dev
  PYTHON_BIN="python3"
  PY_VER="$($PYTHON_BIN -c 'import sys;print(".".join(map(str,sys.version_info[:2])))' 2>/dev/null || echo 3.10)"
fi

info "安装 nginx 与 apache2-utils（htpasswd）..."
apt-get install -y nginx apache2-utils

# ---------- 2. 创建系统用户 liteops ----------
if ! id -u liteops >/dev/null 2>&1; then
  useradd -r -m -s /usr/sbin/nologin liteops
  info "已创建系统用户 liteops"
else
  info "用户 liteops 已存在，跳过创建"
fi

# ---------- 3. 同步代码到 /opt/liteops ----------
if [ ! -d "$SRC_DIR" ]; then
  error "源码目录不存在：$SRC_DIR，请先把分发包解压到该路径"
fi
info "同步代码 $SRC_DIR -> $DST_DIR（排除 venv/data/__pycache__/.git）..."
mkdir -p "$DST_DIR"
rsync -a --delete \
  --exclude '.venv' --exclude 'venv' --exclude 'data' \
  --exclude '__pycache__' --exclude '.git' --exclude 'dist' \
  "$SRC_DIR/" "$DST_DIR/" || {
  # 回退到 cp（部分精简镜像无 rsync）
  warn "rsync 不可用，使用 cp 同步"
  cp -r "$SRC_DIR/." "$DST_DIR/"
}
chown -R liteops:liteops "$DST_DIR"

# ---------- 4. 建 venv 并装依赖 ----------
info "创建虚拟环境并安装依赖（清华镜像源）..."
if [ ! -d "$DST_DIR/venv" ]; then
  "$PYTHON_BIN" -m venv "$DST_DIR/venv"
fi
"$DST_DIR/venv/bin/pip" install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
"$DST_DIR/venv/bin/pip" install -r "$DST_DIR/requirements.txt" \
  -i https://pypi.tuna.tsinghua.edu.cn/simple
chown -R liteops:liteops "$DST_DIR"

# ---------- 5. 交互式收集配置 ----------
echo ""
info "===== 配置 LiteOps Demo ====="

# BasicAuth 用户名
read -r -p "BasicAuth 用户名 [默认 demo]: " BA_USER
BA_USER="${BA_USER:-demo}"

# BasicAuth 密码（两次校验）
while true; do
  read -r -s -p "BasicAuth 密码: " BA_PASS; echo
  read -r -s -p "再次输入密码确认: " BA_PASS2; echo
  if [ -z "$BA_PASS" ]; then
    warn "密码不能为空，请重新输入"
    continue
  fi
  if [ "$BA_PASS" = "$BA_PASS2" ]; then
    break
  fi
  warn "两次密码不一致，请重新输入"
done

# DeepSeek API Key（可留空）
read -r -p "DeepSeek API Key（可直接回车留空，留空则 AI 功能不可用）: " AI_KEY
AI_KEY="${AI_KEY:-}"

# Base URL
read -r -p "AI Base URL [默认 https://api.deepseek.com/v1]: " AI_BASE
AI_BASE="${AI_BASE:-https://api.deepseek.com/v1}"

# Model
read -r -p "AI 模型名 [默认 deepseek-chat]: " AI_MODEL
AI_MODEL="${AI_MODEL:-deepseek-chat}"

# ---------- 6. 写 liteops.env ----------
info "写入 $ENV_FILE ..."
mkdir -p /etc/liteops
cat > "$ENV_FILE" <<EOF
LITEOPS_DEMO=1
LITEOPS_AI_KEY=${AI_KEY}
LITEOPS_AI_BASE_URL=${AI_BASE}
LITEOPS_AI_MODEL=${AI_MODEL}
PYTHONUTF8=1
EOF
chmod 600 "$ENV_FILE"
chown root:root "$ENV_FILE"

# ---------- 7. htpasswd ----------
info "生成 BasicAuth 密码文件 $HTPASSWD_FILE ..."
htpasswd -bc "$HTPASSWD_FILE" "$BA_USER" "$BA_PASS"
chmod 640 "$HTPASSWD_FILE"
chown root:www-data "$HTPASSWD_FILE" 2>/dev/null || chown root:root "$HTPASSWD_FILE"

# ---------- 8. 安装 systemd 服务 ----------
info "安装 systemd 服务 liteops.service ..."
cp "$DST_DIR/deploy/liteops.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable liteops
systemctl restart liteops

# ---------- 9. 安装 nginx 配置 ----------
info "安装 nginx 反代配置 ..."
cp "$DST_DIR/deploy/nginx_liteops.conf" /etc/nginx/sites-available/liteops
ln -sf /etc/nginx/sites-available/liteops /etc/nginx/sites-enabled/liteops
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# ---------- 10. 自检 ----------
echo ""
info "===== 自检 ====="

ACTIVE=$(systemctl is-active liteops 2>/dev/null || echo "unknown")
echo -e "systemctl is-active liteops : ${GREEN}${ACTIVE}${NC}"

echo -n "curl -I 127.0.0.1:8000  -> "
sleep 2
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8000/api/health || echo "FAILED"

echo -n "curl -I http://127.0.0.1 (expect 401) -> "
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1/ || echo "FAILED"

echo ""
if [ "$PY_VER" != "3.11" ]; then
  warn "注意：当前 Python 版本为 $PY_VER（非 3.11），功能可正常使用，但与产品基准版本存在差异。"
fi
info "部署完成！"
echo -e "  访问地址: ${GREEN}http://39.106.48.34${NC}"
echo -e "  BasicAuth 用户: ${GREEN}${BA_USER}${NC}"
echo -e "  服务日志: journalctl -u liteops -n 100"
echo -e "  服务状态: systemctl status liteops"
