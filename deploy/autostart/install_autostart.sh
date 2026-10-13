#!/usr/bin/env bash
# 给 TBDub 服务配置开机自启 / 取消开机自启。
#
#   bash deploy/autostart/install_autostart.sh            # 默认：只有检测到 systemd 才装
#   bash deploy/autostart/install_autostart.sh --youyun   # 优云智慧算平台（supervisord）专用
#   bash deploy/autostart/install_autostart.sh --cron     # crontab @reboot（需 cron 守护在跑）
#   bash deploy/autostart/install_autostart.sh --uninstall
#
# ⚠️ 重要约定
#   1. 只有 **systemd** 是通用做法：装了 systemd 的机器（虚拟机/物理机）可直接用。
#   2. **没有 systemd** 的机器（容器镜像，例如 **优云智慧算平台** 的 Pod），
#      脚本**默认什么都不做**，只打印提示 —— 那条路（supervisord）是那个平台镜像特有的，
#      别的服务器不适用。必须由开发人员先确认"这台机器属于优云智慧算平台、且该镜像用
#      supervisord 管服务"，再显式加 --youyun 才会安装。
#   3. --cron 同理：只有确认本机 cron 守护在跑、且确实要用 @reboot 时才加。
#
# 三种方式都遵守同一条规则：只托管"接口 + 拉起 Worker"，
# 停/重启服务时不杀正在跑的 inference.py。
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd -- "$HERE/../.." && pwd)"
PY=/usr/local/miniconda3/envs/tbdub/bin/python
[ -x "$PY" ] || PY="$(command -v python3)"

MODE="auto"
case "${1:-}" in
  ""|--auto)   MODE="auto" ;;
  --youyun)    MODE="youyun" ;;
  --cron)      MODE="cron" ;;
  --uninstall) MODE="uninstall" ;;
  -h|--help)
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0 ;;
  *)
    echo "未知参数：$1（用 --help 看用法）" >&2
    exit 2 ;;
esac

echo "仓库路径: $REPO"
echo "解释器  : $PY"

if [ "$MODE" = "uninstall" ]; then
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

install_systemd() {
  echo "==> 检测到 systemd，写入 /etc/systemd/system/tbdub.service"
  sed -e "s|__REPO__|$REPO|g" "$HERE/tbdub.service.template" > /etc/systemd/system/tbdub.service
  systemctl daemon-reload
  systemctl enable --now tbdub
  sleep 10
  systemctl --no-pager status tbdub | head -12
  echo "### AUTOSTART_INSTALLED systemd"
}

install_youyun() {
  if ! command -v supervisorctl >/dev/null 2>&1; then
    echo "这台机器上没有 supervisorctl，--youyun 不适用。" >&2
    exit 1
  fi
  echo "==> 优云智慧算平台模式：写入 /etc/supervisor/conf.d/tbdub.conf"
  echo "    （仅适用于自带 supervisord 的该平台容器镜像；别的服务器请用 systemd）"
  sed -e "s|__REPO__|$REPO|g" -e "s|__PY__|$PY|g" \
    "$HERE/supervisord-tbdub.conf.template" > /etc/supervisor/conf.d/tbdub.conf
  supervisorctl reread
  supervisorctl update
  sleep 15
  supervisorctl status tbdub-worker tbdub-api
  echo
  echo "注意：托管之后 bash stop.sh 会被守护拉起，启停改走 supervisorctl（见 README）。"
  echo "自检: curl -s http://127.0.0.1:8811/api/system/ready"
  echo "### AUTOSTART_INSTALLED youyun-supervisor"
}

install_cron() {
  if ! command -v crontab >/dev/null 2>&1; then
    echo "这台机器上没有 crontab，--cron 不适用。" >&2
    exit 1
  fi
  echo "==> crontab @reboot 模式（务必确认 cron 守护进程在跑，否则不会触发）"
  line="@reboot sleep 30; cd $REPO && bash start.sh >> data/boot.log 2>&1"
  crontab -l 2>/dev/null | grep -v "start.sh" > /tmp/.cron.$$
  echo "$line" >> /tmp/.cron.$$
  crontab /tmp/.cron.$$
  rm -f /tmp/.cron.$$
  echo "  已写入: $line"
  echo "### AUTOSTART_INSTALLED crontab"
}

# 先停掉手工启动的那一份，避免和托管实例抢 8811 端口（inference.py 不受影响）
stop_manual() {
  if [ -f "$REPO/stop.sh" ]; then
    (cd "$REPO" && bash stop.sh >/dev/null 2>&1) || true
    echo "==> 已停止手工启动的实例（正在跑的 inference.py 不受影响）"
  fi
}

# ---------- 显式指定的模式 ----------
if [ "$MODE" = "youyun" ]; then stop_manual; install_youyun; exit 0; fi
if [ "$MODE" = "cron" ];   then install_cron;  exit 0; fi

# ---------- 默认（auto）：只认通用的 systemd ----------
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  stop_manual
  install_systemd
  exit 0
fi

cat <<'EOF'

⚠️  本机没有运行 systemd —— 脚本**不会做任何修改**。

为什么停下来：
  · 通用做法只有 systemd。本机没有 systemd（容器镜像通常如此），
    剩下两条路都是"特定环境专用"，不能盲目套用：
      - supervisord：**仅适用于优云智慧算平台**这类自带 supervisord 的容器镜像，别的服务器不适用；
      - crontab @reboot：需要本机 cron 守护进程确实在跑才行。

请先确认（这一步需要开发人员判断，不要跳）：
  1. 这台机器是不是优云智慧算平台的 Pod？
  2. 该平台/镜像是否用 supervisord 管理服务（`supervisorctl status` 能看到 jupyterlab 等）？

确认适用后再显式安装：
  bash deploy/autostart/install_autostart.sh --youyun     # 优云智慧算平台
  bash deploy/autostart/install_autostart.sh --cron       # 确认有 cron 守护的机器

不适用的机器就不要做自启：手工 `bash start.sh` / `bash stop.sh` 依然可用。
EOF
exit 1
