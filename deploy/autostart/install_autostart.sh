#!/usr/bin/env bash
# 给 TBDub 服务配置开机自启 / 取消开机自启。
#
#   bash deploy/autostart/install_autostart.sh          # 安装
#   bash deploy/autostart/install_autostart.sh --uninstall
#
# 会按环境自动选机制（顺序即优先级）：
#   1. supervisord —— 容器镜像常见（本机就是这种：没有 systemd、没有 cron）
#   2. systemd     —— 虚拟机 / 物理机
#   3. crontab @reboot —— 兜底（需要 cron 服务在跑）
#
# 三种方式都遵守同一条规则：只托管"接口 + 拉起 Worker"，
# 停/重启服务时不杀正在跑的 inference.py。
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd -- "$HERE/../.." && pwd)"
PY=/usr/local/miniconda3/envs/tbdub/bin/python
[ -x "$PY" ] || PY="$(command -v python3)"

if [ "${1:-}" = "--uninstall" ]; then
  echo "==> 取消开机自启"
  if [ -f /etc/supervisor/conf.d/tbdub.conf ]; then
    rm -f /etc/supervisor/conf.d/tbdub.conf
    supervisorctl reread >/dev/null 2>&1
    supervisorctl update >/dev/null 2>&1
    echo "  supervisord: 已移除 /etc/supervisor/conf.d/tbdub.conf"
  fi
  if [ -f /etc/systemd/system/tbdub.service ]; then
    systemctl disable --now tbdub >/dev/null 2>&1
    rm -f /etc/systemd/system/tbdub.service
    systemctl daemon-reload >/dev/null 2>&1
    echo "  systemd: 已移除 tbdub.service"
  fi
  if command -v crontab >/dev/null 2>&1; then
    crontab -l 2>/dev/null | grep -v "start.sh" > /tmp/.cron.$$ && crontab /tmp/.cron.$$ && rm -f /tmp/.cron.$$
    echo "  crontab: 已清理 @reboot 条目"
  fi
  echo "注意：服务进程本身没有被杀，需要的话自己 bash stop.sh。"
  exit 0
fi

echo "仓库路径: $REPO"
echo "解释器  : $PY"

# 关掉手工启动的那一份，避免和托管实例抢 8811 端口
if [ -f "$REPO/stop.sh" ]; then
  (cd "$REPO" && bash stop.sh >/dev/null 2>&1) || true
  echo "==> 已停止手工启动的实例（正在跑的 inference.py 不受影响）"
fi

# ---------- 1) supervisord ----------
if command -v supervisorctl >/dev/null 2>&1 && { [ -S /var/run/supervisor.sock ] || [ -d /etc/supervisor/conf.d ]; }; then
  echo "==> 检测到 supervisord，写入 /etc/supervisor/conf.d/tbdub.conf"
  sed -e "s|__REPO__|$REPO|g" -e "s|__PY__|$PY|g" \
    "$HERE/supervisord-tbdub.conf.template" > /etc/supervisor/conf.d/tbdub.conf
  supervisorctl reread
  supervisorctl update
  sleep 15
  supervisorctl status tbdub-worker tbdub-api
  echo
  echo "自检: curl -s http://127.0.0.1:8811/api/system/ready"
  echo "常用: supervisorctl restart tbdub-api | supervisorctl stop tbdub-api"
  echo "### AUTOSTART_INSTALLED supervisord"
  exit 0
fi

# ---------- 2) systemd ----------
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  echo "==> 检测到 systemd，写入 /etc/systemd/system/tbdub.service"
  sed -e "s|__REPO__|$REPO|g" "$HERE/tbdub.service.template" > /etc/systemd/system/tbdub.service
  systemctl daemon-reload
  systemctl enable --now tbdub
  sleep 10
  systemctl --no-pager status tbdub | head -12
  echo "### AUTOSTART_INSTALLED systemd"
  exit 0
fi

# ---------- 3) crontab @reboot ----------
if command -v crontab >/dev/null 2>&1; then
  echo "==> 既无 systemd 也无 supervisord，退回 crontab @reboot"
  line="@reboot sleep 30; cd $REPO && bash start.sh >> data/boot.log 2>&1"
  crontab -l 2>/dev/null | grep -v "start.sh" > /tmp/.cron.$$
  echo "$line" >> /tmp/.cron.$$
  crontab /tmp/.cron.$$
  rm -f /tmp/.cron.$$
  echo "  已写入: $line"
  echo "  注意：必须有 cron 守护进程在跑，否则 @reboot 不会触发。"
  echo "### AUTOSTART_INSTALLED crontab"
  exit 0
fi

echo "没有找到任何可用的自启机制（supervisord / systemd / crontab 都没有）。" >&2
echo "可以自己在镜像入口（entrypoint）里加一行： bash $REPO/start.sh" >&2
exit 1
