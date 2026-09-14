#!/bin/sh
# ============================================================================
# 在小主机上执行的部署脚本（由电脑端 scp 上传后调用）
# 用法：nohup sh /tmp/deploy-remote.sh >/dev/null 2>&1 &
# 结果写入 /tmp/deploy.log，避免 SSH 掉线导致部署中断。
# ============================================================================
exec > /tmp/deploy.log 2>&1

echo "=== 时间 ==="
date

echo "=== 1. 停用会关射频的旧脚本（关键）==="
if [ -f /etc/NetworkManager/dispatcher.d/99-jk-bms-link-switch ]; then
    mv -f /etc/NetworkManager/dispatcher.d/99-jk-bms-link-switch \
          /root/99-jk-bms-link-switch.disabled && echo "旧脚本已停用"
else
    echo "旧脚本不存在（可能已停用）"
fi
ls -l /etc/NetworkManager/dispatcher.d/ 2>&1

echo "=== 2. 安装新脚本 ==="
sh /tmp/jknet/install.sh

echo "=== 3. 部署后网络状态 ==="
ip -4 -br addr
cat /var/lib/NetworkManager/NetworkManager.state
timeout 20 nmcli device status

echo "=== 4. 服务状态 ==="
timeout 10 systemctl is-active mosquitto jk-bms-dashboard jk-bms-recorder
timeout 10 systemctl list-timers jk-bms-link-reconcile.timer --no-pager | head -4

echo "=== 5. 看板 API ==="
timeout 6 curl -fsS http://127.0.0.1:8899/api/summary | head -c 240

echo ""
echo "== DEPLOY DONE =="
