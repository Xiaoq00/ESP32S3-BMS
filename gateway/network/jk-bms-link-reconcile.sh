#!/bin/sh
# ============================================================================
# JK-BMS 小主机「有线 / 无线」链路对账脚本（幂等）
# ============================================================================
#
# 【为什么需要这个脚本 —— 2026-09-14 故障复盘】
#
# 旧版脚本只在 NetworkManager 的 eth0 "up / down" 事件里动作，其中插网线时
# 执行了 `nmcli radio wifi off`。问题在于：射频开关是「有记忆」的持久化状态，
# 会被写进 /var/lib/NetworkManager/NetworkManager.state，重启后依然保持关闭。
#
# 于是出现死锁：
#   插网线 → 射频被关闭（状态被记住）
#     → 重启，开机时网线本来就插着
#     → eth0 没有发生 up/down 状态变化 → 旧脚本不触发
#     → 没有任何代码执行 `nmcli radio wifi on`
#     → WiFi 永远关闭，拔掉网线也无法自动连接
#
# 【设计原则】
#   1. 幂等：不管当前处于什么状态，跑一次就保证结果正确。
#      不是"监听变化"，而是"对账"——先判断现实，再纠正偏差。
#   2. 双保险：dispatcher 负责"秒级响应"，systemd timer 负责"兜底纠偏"。
#      即使某次事件丢了、脚本被跳过、状态被外部改乱，30 秒内也会自愈。
#   3. 判据是「有线是否真的可用」，而不是「网线插没插」。
#      有载波但没有 IPv4（例如 DHCP 失败、地址冲突），依然算不可用。
#
# 【期望行为】
#   有线可用   → 有线独占服务地址，关闭无线射频
#   有线不可用 → 打开无线射频，依次尝试已保存的 WiFi
#
# 该脚本同时被以下两者调用：
#   /etc/NetworkManager/dispatcher.d/99-jk-bms-link-switch   （事件快路径）
#   jk-bms-link-reconcile.timer                              （定时兜底）
# 因此必须保证：重复执行无副作用、任意时刻可安全中断。
# ============================================================================

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

LOG_TAG=jk-bms-link-reconcile
WIRED_IF=eth0
WIFI_IF=wlan0

# 有线连接名：静态 IP 192.168.1.26（服务地址，C3 / MQTT / 看板都依赖它）
WIRED_CONN="Wired connection 1"
# 无线连接名：按优先级从高到低排列，卧室信号强，工具间作为备用
WIFI_CONN_LIST="jk-bms-bedroom-wifi jk-bms-toolroom-wifi"

log() {
    logger -t "$LOG_TAG" -- "$*"
}

# --- 判据一：有线是否真的有载波（网线物理连通）---
wired_has_carrier() {
    [ "$(cat /sys/class/net/$WIRED_IF/carrier 2>/dev/null)" = "1" ]
}

# --- 判据二：有线是否真的拿到了 IPv4 ---
# 只有载波不算可用：可能 DHCP 失败或地址冲突，此时仍应回退到 WiFi。
wired_has_ipv4() {
    ip -4 -o addr show dev "$WIRED_IF" 2>/dev/null | grep -q "inet "
}

# --- 判据三：无线射频当前是否打开 ---
wifi_radio_is_on() {
    nmcli radio wifi 2>/dev/null | grep -qi "enabled"
}

# --- 判据四：无线是否已经拿到 IPv4 ---
wifi_has_ipv4() {
    ip -4 -o addr show dev "$WIFI_IF" 2>/dev/null | grep -q "inet "
}

# --- 动作：让无线连上（依次尝试已保存的网络）---
connect_wifi() {
    wifi_has_ipv4 && return 0
    for c in $WIFI_CONN_LIST; do
        if nmcli connection up "$c" ifname "$WIFI_IF" >/dev/null 2>&1; then
            log "已连接无线配置：$c"
            return 0
        fi
    done
    log "所有已保存的无线配置均连接失败"
    return 1
}

# ---------------------------------------------------------------------------
# 主流程：对账
# ---------------------------------------------------------------------------
main() {
    if wired_has_carrier && wired_has_ipv4; then
        # ---- 有线可用：目标是"无线不占用网络" ----
        if wifi_radio_is_on; then
            nmcli radio wifi off >/dev/null 2>&1
            log "有线可用 → 已关闭无线射频"
        fi
    else
        # ---- 有线不可用：目标是"无线必须工作" ----
        # 注意：这里必须主动打开射频，这正是旧脚本缺失的"开机兜底"逻辑。
        if ! wifi_radio_is_on; then
            nmcli radio wifi on >/dev/null 2>&1
            log "有线不可用 → 已打开无线射频"
        fi
        connect_wifi
    fi
}

# 并发保护：dispatcher 与 timer 可能同时触发，避免重复执行互相打架。
# -n 表示拿不到锁就立即退出（不排队等待，避免阻塞 NetworkManager）。
exec 9>/run/lock/jk-bms-link-reconcile.lock
flock -n 9 || exit 0

main
exit 0
