#!/bin/sh
# ============================================================================
# JK-BMS 小主机「有线 / 无线」链路对账脚本（幂等 + 无抖动）
# ============================================================================
#
# 【为什么需要这个脚本 —— 2026-09-14 故障复盘】
#
# 旧版脚本只在 NetworkManager 的 eth0 "up / down" 事件里动作，其中插网线时
# 执行了 `nmcli radio wifi off`。问题在于：射频开关是「有记忆」的持久化状态，
# 会被写进 /var/lib/NetworkManager/NetworkManager.state，重启后依然保持关闭。
# 于是出现死锁：插网线→射频被关（被记住）→重启时网线本就插着→没有 up 事件
# →没人执行 radio on→WiFi 永远关，拔线也连不上。
#
# 第一次「修」成的版本改用 `wired_has_carrier && wired_has_ipv4` 作判据，但
# 仍有死锁：本机有线和 WiFi 两个连接都配了同一个静态 IP 192.168.1.26（服务
# 地址，C3/MQTT/看板都依赖它）。WiFi 先拿着 .26 时，有线永远拿不到这个 IP，
# 于是 `wired_has_ipv4` 永远为假 → 绝不关 WiFi →「插网线用有线」永远不生效。
#
# 第二次「修」又踩了另一个坑：在"已切到有线、只是 30s timer 又触发"时，脚本
# 没记录当前模式，又掉进 WiFi 分支把射频重新打开，导致 eth0 和 wlan0 同时拿着
# .26（IP 冲突），且"在用有线却开着 WiFi"。
#
# 【本版设计原则】
#   1. 幂等：不管当前处于什么状态，跑一次就保证结果正确；可任意中断、可并发
#      （flock 互斥，拿不到锁立即退出，绝不阻塞 NetworkManager）。
#   2. 双保险：dispatcher 负责「秒级响应」（插拔网线即触发），systemd timer
#      负责「兜底纠偏」（每 30s 跑一次，即使某次事件丢了、脚本被跳过、状态被
#      外部改乱，也会自愈）。
#   3. 判定用「网线载波上升沿 + 网关可达」，而不是「网线插没插」或「eth0 有没有
#      IP」。原因：
#        - 只看 carrier 不验证网关：坏网线（PHY 通但上不了网）会被误判为可用；
#        - 只等有 IP：会因「同 IP 冲突」陷入上面说的死锁。
#      所以做法是：插上网线（carrier 0→1 的上升沿）时，先把 WiFi 射频关掉腾出
#      .26，再拉起有线、ping 网关；通 → 用有线；不通 → 回退 WiFi。拔掉网线
#      （carrier 1→0）时，直接走 WiFi。
#   4. 【关键修正】用 /run/jk-bms-mode 记录"当前到底该用有线还是 WiFi"。一旦
#      切到有线并验证成功，后续 timer 触发时只要网线没被拔插（无上升沿）就直接
#      保持、绝不重新打开 WiFi 射频——彻底消除"在线上却开着 WiFi / IP 冲突"的
#      抖动。仅在「真正插拔网线」时探测有线。
#   5. /run 是 tmpfs，重启后状态清零 → 开机必然重新探测一次，正好破「开机死锁」。
#
# 【期望行为】
#   插网线（且网关可达）   → 关 WiFi 射频，有线独占 192.168.1.26
#   拔网线 / 网线坏 / 开机没插 → 开 WiFi 射频，连 jk-bms-bedroom-wifi（卧室，
#                                信号强），失败再试 jk-bms-toolroom-wifi（工具间）
#
# 该脚本同时被以下两者调用，必须保证重复执行无副作用：
#   /etc/NetworkManager/dispatcher.d/99-jk-bms-link-switch   （事件快路径）
#   jk-bms-link-reconcile.timer                              （定时兜底）
# ============================================================================

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

LOG_TAG=jk-bms-link-reconcile
WIRED_IF=eth0
WIFI_IF=wlan0

# 有线连接名：静态 IP 192.168.1.26（服务地址，C3 / MQTT / 看板都依赖它）
WIRED_CONN="Wired connection 1"
# 无线连接名：按优先级从高到低，卧室信号强、工具间备用
WIFI_CONN_LIST="jk-bms-bedroom-wifi jk-bms-toolroom-wifi"
# 网关（用于验证有线是否真的能上网，而不只是 PHY 通）
GATEWAY=192.168.1.1

# 状态文件（/run 是 tmpfs，重启后清零 → 开机必然重新探测一次，正好破「开机死锁」）
PREV_CARRIER=/run/jk-bms-prev-carrier
MODE_FILE=/run/jk-bms-mode

log() {
    logger -t "$LOG_TAG" -- "$*"
}

# --- 判据一：有线是否真的有载波（网线物理连通）---
wired_has_carrier() {
    [ "$(cat /sys/class/net/$WIRED_IF/carrier 2>/dev/null)" = "1" ]
}

# --- 判据二：有线拉起后，网关是否真的可达（过滤「PHY 通但上不了网」的坏线）---
# 拉起有线、释放 .26 之后调用；ping 3 次，至少 2 次通才算可用，避免偶然丢包误判。
wired_reaches_gateway() {
    ok=0
    for _i in 1 2 3; do
        if ping -c1 -W2 "$GATEWAY" >/dev/null 2>&1; then
            ok=$((ok + 1))
        fi
    done
    [ "$ok" -ge 2 ]
}

# --- 判据三：无线射频当前是否打开 ---
wifi_radio_is_on() {
    nmcli radio wifi 2>/dev/null | grep -qi "enabled"
}

# --- 动作：让无线连上（依次尝试已保存的网络）---
connect_wifi() {
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
    # 读取/初始化上一次载波状态（文件不存在 → 视为 0，使开机首跑必然探测有线）
    prev=$(cat "$PREV_CARRIER" 2>/dev/null)
    [ -z "$prev" ] && prev=0
    cur=0
    wired_has_carrier && cur=1
    echo "$cur" > "$PREV_CARRIER"

    mode=$(cat "$MODE_FILE" 2>/dev/null)

    if [ "$cur" = "1" ]; then
        if [ "$prev" = "0" ]; then
            # 网线载波上升沿（刚插入 / 开机就有线）→ 试探切有线
            log "检测到网线插入，尝试切换有线…"
            # 先关 WiFi 射频，腾出 192.168.1.26 给有线（打破同 IP 死锁）
            if wifi_radio_is_on; then
                nmcli radio wifi off >/dev/null 2>&1
            fi
            # 拉起有线（静态 .26/24 gw .1，无需 DHCP，立即拿到 IP）
            nmcli connection up "$WIRED_CONN" ifname "$WIRED_IF" >/dev/null 2>&1
            sleep 2
            if wired_reaches_gateway; then
                echo wired > "$MODE_FILE"
                log "有线可用（网关可达）→ 已切换有线，WiFi 射频关闭"
                return 0
            fi
            # 网线坏 / 网关不可达：回退 WiFi（prev 已是 1，下次不会重复探测，直到拔插）
            log "有线不可用（网关不可达）→ 回退 WiFi"
        else
            # 载波一直为 1，且没有发生新的插拔（prev=1）
            if [ "$mode" = "wired" ]; then
                # 已在有线上且没拔插 → 保持，绝不再打开 WiFi 射频（消除抖动/IP 冲突）
                return 0
            fi
            # mode=wifi（之前回退过）→ 保持 WiFi，不重新探测有线
            log "保持 WiFi（有线此前不可用）"
        fi
    fi

    # WiFi 路径：开机没插线、拔了线、或网线坏的回退，都走这里
    echo wifi > "$MODE_FILE"
    if ! wifi_radio_is_on; then
        nmcli radio wifi on >/dev/null 2>&1
        log "已打开无线射频"
    fi
    connect_wifi
}

# 并发保护：dispatcher 与 timer 可能同时触发，避免重复执行互相打架。
# -n 表示拿不到锁就立即退出（不排队等待，避免阻塞 NetworkManager）。
exec 9>/run/lock/jk-bms-link-reconcile.lock
flock -n 9 || exit 0

main
exit 0
