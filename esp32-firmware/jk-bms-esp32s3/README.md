# ESP32-S3 版 JK-BMS 固件包

这是 ESP32-S3 版本的独立固件包，负责通过 BLE 读取 JK-BD6A20S6PD（V20.27）数据，并通过 WiFi/MQTT 转发到小主机。

## 配置区

打开 `jk-bms-esp32s3.ino` 顶部配置区，只在本地填写真实参数：

```cpp
#define BMS_MAC       "AA:BB:CC:DD:EE:FF"   // BMS 蓝牙 MAC，占位符
#define WIFI_SSID     "YOUR_WIFI_SSID"      // WiFi 名称，占位符
#define WIFI_PASS     "YOUR_WIFI_PASSWORD"  // WiFi 密码，占位符
#define MQTT_BROKER   "YOUR_MQTT_BROKER_IP" // Mosquitto 小主机地址，占位符
```

仓库中的源码只保留以上占位符。**不要把真实 WiFi 账号、WiFi 密码或 BMS 蓝牙 MAC 提交到公开仓库。**

## 编译

```bash
arduino-cli compile \
  --fqbn esp32:esp32:esp32s3 \
  --output-dir .build \
  jk-bms-esp32s3.ino
```

## 烧录

```bash
esptool --chip esp32s3 --port <PORT> --baud 921600 \
  write-flash -z 0x0 .build/jk-bms-esp32s3.ino.merged.bin
```

## 固件能力

- JK-BMS V20.27 BLE 通讯；
- FFE1 隐藏 CCCD 的 `0x000F` 订阅修复；
- 遥测帧解析与 MQTT 发布；
- BMS 设置读取与只读看板支持；
- 写设置前发送密码解锁帧（V20.27 实测写设置仍可能被静默拒绝）。

ESP32-C3 版本位于同级目录 `../esp32c3-firmware/`，两个版本分别维护、分别编译烧录。
