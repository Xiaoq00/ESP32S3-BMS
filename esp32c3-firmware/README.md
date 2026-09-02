# ESP32-C3 版 JK-BMS 读取器固件

ESP32-**S3** 版固件的低功耗移植：把采集板从双核 S3 换成单核 C3，
目的是**降低板子自身功耗**，从而减轻「供电一抽电流就压塌」的压力
（根因仍是电源要稳，但 C3 自己少喝一口电，余量更大）。

## 与 S3 版的关系

- **逻辑 / 协议 100% 继承 S3 版**，一行没改：
  - JK V20.27 蓝牙协议（FFE0/FFE1，0x96/0x97 命令，8 位累加 CRC）
  - **隐藏 CCCD 修复**：V20.27 的 FFE1 不暴露 CCCD 描述符，
    NimBLE `registerForNotify()` 会静默失败 → 直接写 `0x000F`（value_handle+1）开户。
  - **写前密码解锁**（命令 0x05，本板 V20.27 写设置前必发）。
  - 偏移 / 遥测解析（电压@150、电流@158、SOC@173、电芯@6+…）全部沿用。
- **仅有的差异**（都在 `jk-bms-esp32c3.ino` 里）：
  - 编译目标 `esp32:esp32:esp32c3`；
  - 设备名 / WiFi 主机名 / MQTT client-id 由 `jk-esp32s3` 改为 `jk-esp32c3`。

## 编译目标

- Arduino IDE：选开发板 **"ESP32C3 Dev Module"**（或等价 C3 板），
  Flash 默认 4MB QIO 即可。
- arduino-cli：
  `arduino-cli compile --fqbn esp32:esp32:esp32c3 --output-dir .build jk-bms-esp32c3.ino`
- 烧录（小主机经 `/dev/ttyUSB0`）：
  `python3 -m esptool --chip esp32c3 --port /dev/ttyUSB0 --baud 921600 write_flash 0x0 <build>/jk-bms-esp32c3.ino.merged.bin`

## 脱敏约定（重要）

仓库内为**模板**：`WIFI_SSID` / `WIFI_PASS` / `BMS_MAC` 均为占位符，
**不提交真实凭据**（与 S3 版一致）。真实参数在本地填，别 push 到公开仓库。

## 验证状态

- ✅ 源码逻辑已与 S3 版对齐，无 S3 专用 API / PSRAM / 特定 GPIO。
- ⏳ **尚未在真实 C3 板 + BMS 前实机烧录验证**（本仓库仅保证源码/逻辑正确）。
  拿到 C3 板后，按上法编译烧录，串口应看到与 S3 版一致的启动 / 连 WiFi /
  连 MQTT / `TELEMETRY` 输出。
