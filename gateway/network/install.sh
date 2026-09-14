#!/bin/sh
# ============================================================================
# JK-BMS 小主机网络自动切换 —— 一键安装 / 升级脚本
# ============================================================================
#
# 在小主机（armbian, 192.168.1.26）上以 root 执行：
#     sudo sh install.sh
#
# 安装内容：
#   /usr/local/sbin/jk-bms-link-reconcile.sh       幂等对账脚本（核心逻辑）
#   /etc/NetworkManager/dispatcher.d/99-jk-bms-link-switch   事件快路径
#   /etc/systemd/system/jk-bms-link-reconcile.service         开机兜底
#   /etc/systemd/system/jk-bms-link-reconcile.timer           30 秒定时兜底
#
# 设计要点见 jk-bms-link-reconcile.sh 顶部注释；这里只负责落盘与启用。
# ============================================================================
set -eu

SRC_DIR=$(dirname "$0")

install -m 0755 "$SRC_DIR/jk-bms-link-reconcile.sh" /usr/local/sbin/jk-bms-link-reconcile.sh
install -m 0755 "$SRC_DIR/99-jk-bms-link-switch"    /etc/NetworkManager/dispatcher.d/99-jk-bms-link-switch
install -m 0644 "$SRC_DIR/jk-bms-link-reconcile.service" /etc/systemd/system/jk-bms-link-reconcile.service
install -m 0644 "$SRC_DIR/jk-bms-link-reconcile.timer"   /etc/systemd/system/jk-bms-link-reconcile.timer

# dispatcher 目录的安全要求：不能对 group/other 可写，否则 NetworkManager 会拒绝执行
chmod 0755 /etc/NetworkManager/dispatcher.d
chown root:root /etc/NetworkManager/dispatcher.d/99-jk-bms-link-switch

systemctl daemon-reload
systemctl enable --now jk-bms-link-reconcile.timer

# 立刻对账一次，把当前状态纠正到正确状态（尤其是被旧脚本关掉的 WiFi 射频）
/usr/local/sbin/jk-bms-link-reconcile.sh || true

echo "安装完成。查看状态："
echo "  systemctl list-timers jk-bms-link-reconcile.timer"
echo "  journalctl -t jk-bms-link-reconcile -n 30 --no-pager"
echo "  nmcli device status"
