#!/bin/bash
# ============================================================
# ESP32-C3 固件一键发布（OTA）
#   用法:  ./release_c3.sh <版本号>       例如 ./release_c3.sh v25.20260924
#          ./release_c3.sh <版本号> --push   发布后立刻用 espota 推到设备
#
# 做四件事:
#   1. 把固件里的 FW_VERSION 改成指定版本号
#   2. 编译（含 min_spiffs 分区方案，OTA 必需）
#   3. 把 app.bin 传到小主机 /opt/jk-bms/fw/firmware.bin，并更新 version 文件
#   4. （可选 --push）用 espota 立刻推到设备，不用等它自查
#
# 注意: OTA 推的是 app.bin（xxx.ino.bin），不是 merged.bin
# ============================================================
set -e

VER="$1"
PUSH="$2"
if [ -z "$VER" ]; then
  echo "用法: $0 <版本号> [--push]"
  echo "例如: $0 v25.20260924"
  exit 1
fi

WS="/c/Users/xiaoq/WorkBuddy AI/2026-08-23-09-03-41"
INO="$WS/jk-bms-esp32c3/jk-bms-esp32c3.ino"
BUILD="$WS/.build-c3-ota"
HOST="root@192.168.1.26"
KEY="/c/Users/xiaoq/.ssh/id_ed25519"
ARD="/c/Program Files/Arduino CLI/arduino-cli.exe"
FQBN="esp32:esp32:esp32c3:CDCOnBoot=cdc,FlashMode=dio,FlashFreq=40,PartitionScheme=min_spiffs"
ESPOTA="/c/Users/xiaoq/AppData/Local/Arduino15/packages/esp32/hardware/esp32/3.3.8-cn/tools/espota.exe"

cd "$WS"

echo "=== [1/4] 改版本号 -> $VER ==="
sed -i "s|#define FW_VERSION      \"[^\"]*\"|#define FW_VERSION      \"$VER\"|" "$INO"
grep -oE 'FW_VERSION      "[^"]*"' "$INO"

echo
echo "=== [2/4] 编译 ==="
"$ARD" compile -b "$FQBN" --output-dir "$BUILD" jk-bms-esp32c3 2>&1 | tail -4

echo
echo "=== [3/4] 发布到小主机 ==="
scp -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 \
  "$BUILD/jk-bms-esp32c3.ino.bin" "$HOST:/opt/jk-bms/fw/firmware.bin"
ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 "$HOST" \
  "echo '$VER' > /opt/jk-bms/fw/version; echo -n '  服务器 version = '; cat /opt/jk-bms/fw/version; ls -l /opt/jk-bms/fw/firmware.bin"

echo
echo "=== [4/4] 同步归档副本（脱敏）==="
A="$WS/jk-bms-gateway/esp32c3-firmware/jk-bms-esp32c3/jk-bms-esp32c3.ino"
cp "$INO" "$A"
sed -i 's|#define BMS_MAC       "28:d4:1e:a8:9c:d1"|#define BMS_MAC       "AA:BB:CC:DD:EE:FF"|' "$A"
sed -i 's|#define WIFI_SSID     "CU_hSUF"|#define WIFI_SSID     "YOUR_WIFI_SSID"|' "$A"
sed -i 's|#define WIFI_PASS     "x4tshu4"|#define WIFI_PASS     "YOUR_WIFI_PASSWORD"|' "$A"
sed -i 's|#define WIFI_PASS     "x4tshuu4"|#define WIFI_PASS     "YOUR_WIFI_PASSWORD"|' "$A"
echo "  归档差异行数（应为 6）: $(diff "$A" "$INO" | grep -c '^[<>]')"

if [ "$PUSH" = "--push" ]; then
  echo
  echo "=== [附加] 用 espota 立刻推送到设备 ==="
  "$ESPOTA" -i 192.168.1.11 -p 3232 -r -f "$BUILD/jk-bms-esp32c3.ino.bin" 2>&1 | tr '\r' '\n' | tail -3
fi

echo
echo "=== 完成 ==="
echo "  设备会自动升级（开机后 90 秒×5 次 / 之后每 6 小时自查）"
echo "  想立刻确认: ssh $HOST \"timeout 16 mosquitto_sub -h 127.0.0.1 -t jk-bms/state -C 1 -W 13\" | grep -o '\\\"fw\\\":\\\"[^\\\"]*'"
