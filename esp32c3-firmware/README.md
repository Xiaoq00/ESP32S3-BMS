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

## ⚠️ 必读：C3 板必须换 DIO 版 bootloader，否则无限重启

**症状**：烧录完全成功（esptool 校验通过），但板子上电后死循环：

```
E (24) flash_parts: partition 0 invalid magic number 0xdcdf
E (24) boot: Failed to verify partition table
E (24) boot: load partition table error!
```

**根因**：Arduino-ESP32 的 C3 板型定义里写死了 `esp32c3.build.boot=qio`，
于是 `arduino-cli` 从 `tools/esp32c3-libs/<ver>/bin/bootloader_qio_80m.elf`
取 bootloader。**不少 C3 板的闪存根本跑不了 QIO**，bootloader 一运行就把
闪存切到 QIO，随之读到损坏数据 → 分区表校验失败 → 软件复位 → 死循环。

关键点：**用 `esptool --flash-mode dio` 刷是没用的**，那只改镜像头（影响 ROM
首次加载），bootloader 自己会切回 QIO。必须**换掉 bootloader 二进制本身**。

### 解决：改用 DIO 版 bootloader

```bash
SDK=<Arduino15>/packages/esp32/tools/esp32c3-libs/<ver>/bin

# 1) 从 DIO 版 ELF 生成 bootloader 二进制
esptool --chip esp32c3 elf2image --flash-mode dio --flash-freq 40m \
        --flash-size 4MB -o bootloader_dio40m.bin $SDK/bootloader_dio_40m.elf

# 2) 固件本身也要按 DIO 编译
arduino-cli compile \
  --fqbn esp32:esp32:esp32c3:CDCOnBoot=cdc,FlashMode=dio,FlashFreq=40 \
  --output-dir .build jk-bms-esp32c3.ino

# 3) 用 DIO bootloader 烧录
esptool --chip esp32c3 --port <PORT> --baud 460800 write-flash -z \
        --flash-mode dio --flash-freq 40m --flash-size 4MB \
        0x0      bootloader_dio40m.bin \
        0x8000   .build/jk-bms-esp32c3.ino.partitions.bin \
        0xe000   boot_app0.bin \
        0x10000  .build/jk-bms-esp32c3.ino.bin
```

**`CDCOnBoot=cdc` 不能省**：不带串口芯片的 C3 板只有原生 USB Serial/JTAG，
不开 CDC 就收不到任何 `Serial` 日志。

### 一劳永逸：改 `boards.local.txt`

在 `<Arduino15>/packages/esp32/hardware/esp32/<ver>/` 下建 `boards.local.txt`：

```
esp32c3.build.boot=dio
```

之后 `arduino-cli upload` 会自动改用 `bootloader_dio_<freq>.elf`，无需手工替换。

## 编译目标

- Arduino IDE：选开发板 **"ESP32C3 Dev Module"**（或等价 C3 板），
  **Flash Mode 必须选 DIO**（并配合上面的 DIO bootloader）。
- arduino-cli（C3 无串口芯片时务必带 `CDCOnBoot=cdc`）：
  `arduino-cli compile --fqbn esp32:esp32:esp32c3:CDCOnBoot=cdc,FlashMode=dio,FlashFreq=40 --output-dir .build jk-bms-esp32c3.ino`
- 烧录：见上一节的 3 段式命令（**不要用 `merged.bin` 一条命令刷**，
  因为 merged.bin 里内嵌的是 QIO 版 bootloader）。

## 脱敏约定（重要）

仓库内为**模板**：`WIFI_SSID` / `WIFI_PASS` / `BMS_MAC` 均为占位符，
**不提交真实凭据**（与 S3 版一致）。真实参数在本地填，别 push 到公开仓库。

## 验证状态

- ✅ 源码逻辑已与 S3 版对齐，无 S3 专用 API / PSRAM / 特定 GPIO。
- ✅ **C3 板实机点亮**：板子为无串口芯片的 ESP32-C3（rev v0.4，外挂 4MB
  GD25Q32，原生 USB Serial/JTAG）。用上面的 DIO bootloader 方案后，
  最小 Blink 固件已成功启动并稳定输出（此前一直卡在分区表校验死循环）。
- ✅ 固件本体编译通过：约 1.25MB，占 4MB 闪存默认分区 app0（1310720 B）的 95%，
  全局变量 40KB / 327KB，C3 装得下。
- ✅ **卧室端联网 / MQTT 链路已验证**：电脑 USB 连接 C3；卧室专用路由器的
  `CU_hSUF1` 可扫描并连接，曾拿到 `192.168.1.11`，并成功连接小主机 MQTT
  `192.168.1.26:1883`、订阅 `jk-bms/set`。这只是卧室烧录测试网络，不是现场部署网络。
- ✅ **最终烧录配置已改回工具间 WiFi**：当前写入 C3 的本地固件使用默认的
  `CU_hSUF`，对应父亲卧室、靠近工具间的路由器；`CU_hSUF1` 不再用于现场固件。
- ⏳ **BLE 尚未验证**：BMS 在工具间，C3 仍在卧室时 `conn=0` 是正常的；需把板子
  带到工具间、接 BMS 后验证。现场应使用 `CU_hSUF`，不要根据卧室 `CU_hSUF1` 的
  连接结果判断现场网络。

## 排障记录（这次踩到的坑）

| 现象 | 结论 |
|---|---|
| QIO/QOUT 刷入 → ROM 阶段 `TG0WDT_SYS_RST` | 这颗闪存完全不支持四线 |
| DIO/DOUT 刷入 → ROM 能加载，bootloader 报 `invalid magic number` | QIO 版 bootloader 自己切回 QIO |
| 换 20/26/40/80MHz、2MB/4MB 全都一样 | 不是频率/容量问题 |
| 两个完全不同的 app 报出**完全相同**的魔数 `0xdcdf` | 读到的不是闪存内容，是损坏值 |
| 整片填 `0x11` 后魔数变成 `0xdddd` | 读的确实是闪存，但每字节被"或"上 `0xCC`（QIO 采样错位） |
| 换 DIO 版 bootloader 后立刻正常 | **根因坐实** |
