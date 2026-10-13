#!/usr/bin/env bash
# 验证开机自启脚本的"适用性闸门"：
#   1) 默认调用（本机无 systemd）必须**拒绝安装且不做任何改动**
#   2) --youyun 才真正安装优云智慧算平台的 supervisord 配置
#   3) --uninstall 能干净取消
set -uo pipefail
cd /AI/TBDub-master || exit 1
INSTALL="bash deploy/autostart/install_autostart.sh"

echo "########## 0) 环境判断 ##########"
printf "systemd 运行时: "; [ -d /run/systemd/system ] && echo "有" || echo "无（容器镜像）"
printf "supervisord    : "; command -v supervisorctl >/dev/null 2>&1 && echo "有" || echo "无"
printf "crontab        : "; command -v crontab >/dev/null 2>&1 && echo "有" || echo "无"

echo
echo "########## 1) 默认调用：应当拒绝且零改动 ##########"
BEFORE_SUP=$(ls /etc/supervisor/conf.d/ 2>/dev/null | md5sum)
BEFORE_SYS=$(ls /etc/systemd/system/ 2>/dev/null | md5sum)
$INSTALL
echo "退出码=$?（期望 1）"
AFTER_SUP=$(ls /etc/supervisor/conf.d/ 2>/dev/null | md5sum)
AFTER_SYS=$(ls /etc/systemd/system/ 2>/dev/null | md5sum)
[ "$BEFORE_SUP" = "$AFTER_SUP" ] && echo "✅ supervisor 目录未被改动" || echo "❌ supervisor 目录被改了"
[ "$BEFORE_SYS" = "$AFTER_SYS" ] && echo "✅ systemd 目录未被改动" || echo "❌ systemd 目录被改了"

echo
echo "########## 2) --youyun：显式安装（优云智慧算平台）##########"
$INSTALL --youyun 2>&1 | tail -12

echo
echo "########## 3) 状态确认 ##########"
supervisorctl status tbdub-api tbdub-worker
printf "服务就绪: "; curl -s --max-time 12 http://127.0.0.1:8811/api/system/ready | head -c 120; echo

echo
echo "########## 4) 说明 ##########"
echo "现在这台机器（优云智慧算平台）保持 --youyun 安装状态。"
echo "换到别的机器：有 systemd 就默认装 systemd；都没有就别做自启。"
echo "### AUTOSTART_GATE_TEST_DONE"
