#!/usr/bin/env bash
# 七鱼会话分析 · Linux 服务器一键部署
#
#   sudo bash deploy/install.sh
#
# 做的事：建用户 → 建目录 → 拷代码 → 建 venv（可选）→ 装 systemd 单元
# 不做的事：不碰 .env（凭证你自己填），不启动（你自己 enable）
set -euo pipefail

APP_DIR=/opt/qiyu-analysis
APP_USER=qiyu
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\033[36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31m[失败] %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "需要 root：sudo bash deploy/install.sh"

say "1/6 自检"
python3 deploy/preflight.py || die "自检未通过，先解决上面的阻塞项"

PY=$(command -v python3) || die "找不到 python3"
say "   使用解释器：$PY ($($PY -V 2>&1))"

say "2/6 建系统用户 $APP_USER"
if id "$APP_USER" >/dev/null 2>&1; then
  say "   已存在，跳过"
else
  useradd --system --shell /usr/sbin/nologin --home-dir "$APP_DIR" "$APP_USER"
fi

say "3/6 部署代码到 $APP_DIR"
mkdir -p "$APP_DIR"
# --delete 会删掉服务器上的 data/ logs/，所以排除掉
rsync -a --delete \
  --exclude '.git' --exclude '__pycache__' --exclude 'data' --exclude 'logs' \
  --exclude '.env' --exclude '*.pyc' \
  "$SRC_DIR"/ "$APP_DIR"/
mkdir -p "$APP_DIR/data" "$APP_DIR/logs"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 750 "$APP_DIR/data" "$APP_DIR/logs"

say "4/6 配置 .env"
if [ -f "$APP_DIR/.env" ]; then
  say "   已存在，保留不动"
else
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  say "   已从模板生成，务必填：QIYU_APP_KEY / QIYU_APP_SECRET / FEISHU_*"
fi

say "5/6 安装 systemd 单元"
install -m 644 "$APP_DIR/deploy/qiyu-analysis.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/qiyu-analysis.timer"   /etc/systemd/system/
systemctl daemon-reload

say "6/6 完成"
cat <<'EOF'

接下来三步（手工执行）：

  1) 填凭证
     sudo -u qiyu nano /opt/qiyu-analysis/.env

  2) 首次手动跑一次，看能不能拉到数据
     sudo -u qiyu sh -c 'cd /opt/qiyu-analysis && python3 run.py doctor'
     sudo -u qiyu sh -c 'cd /opt/qiyu-analysis && python3 run.py pull --days 7'
     sudo -u qiyu sh -c 'cd /opt/qiyu-analysis && python3 run.py extract'

  3) 确认没问题再开定时
     sudo systemctl enable --now qiyu-analysis.timer
     systemctl list-timers qiyu-analysis.timer

排查：
     journalctl -u qiyu-analysis -n 100 --no-pager
     journalctl -u qiyu-analysis -f
EOF
