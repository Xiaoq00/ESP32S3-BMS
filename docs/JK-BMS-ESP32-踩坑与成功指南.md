# 用 ESP32 读极空(JK) BMS 蓝牙遥测 —— 踩坑 18 版后的成功指南

> 一句话结论：**别去 App 里解绑手机，也别死磕蓝牙配对。真正的坑是 JK-BD6A20S6PD 在 V20.27 固件下把 FFE1 特征的「通知开关(CCCD)」藏起来了，NimBLE 的 `registerForNotify()` 找不到它就会静默失败、数据死活不来。解决办法是绕过发现，直接用 `ble_gattc_write_flat` 往 `value_handle+1` 写 `0x0001` 把开关掰开。**

这篇是给后来者的避坑帖。我把踩过的坑、最后怎么成功的、以及可以直接复现的流程全写出来了，照着做能少走十几版弯路。

---

## 一、背景与目标

我想让一块 **ESP32-S3** 通过蓝牙(BLE)连上家里的 **极空 JK-BD6A20S6PD** 保护板（软件 **V20.27**），24 小时读电池遥测（总电压、电流、SOC、每节电芯电压、温度），以后还能转发到 Home Assistant。

板子型号确认方式：用任意手机装 JK 官方 App 连上，设置里能看到「JK-BD6A20S6PD / V20.27 / 20S」。

硬件拓扑（我这套，供参考）：
- 卧室 PC 写代码、编译
- 工具间一台 Armbian 小主机（IP `192.168.1.26`，SSH key 登录），上面插着 ESP32-S3（USB `/dev/ttyUSB0`，CH340 芯片）
- ESP32-S3 放在 BMS 附近蓝牙连接
- 流程：PC `arduino-cli` 编译 → `scp` 到小主机 → `esptool` 烧录 → `picocom`/`cap.py` 串口回传看结果

---

## 二、先说两个大坑（90% 的人会卡在这）

### 坑 1：以为「BMS 绑定了手机，得先去 App 解绑」—— 错的

网上很多老帖说 JK BMS 只认一个绑定设备，ESP32 连不上是因为手机占着坑，要去 App「解除绑定」。
**在 V20.27 这版固件上这是错的。** 实测：BMS 是「自由」的，任意手机输密码就能新连，**根本没有「解绑/解除绑定」这种操作**。哪怕手机后台清掉、离得老远，BMS 也没有「被谁独占」。所以这个方向纯属浪费时间。

### 坑 2：以为「必须配对(Pairing)数据才来」—— 也不对

两个成熟的社区固件（[zhang1997aaa/JK_BMS_TO_WEB-ESP32](https://github.com/zhang1997aaa/JK_BMS_TO_WEB-ESP32)、[BHP1000/JK-BMS-BLE-ESP32](https://github.com/BHP1000/JK-BMS-BLE-ESP32)）**根本不配对**就能连上、读数据。我前面 v5~v16 在「配对参数 / 强制加密 / 重发 Pairing Request」上磨了很久，全是红鲱鱼。配对可选，不是数据的前提。

---

## 三、真正的根因：FFE1 藏起了 CCCD

现象：ESP32 连上了（`conn=1`），找到了 FFE0 服务 / FFE1 特征，命令（`0x96`/`0x97`）发出去 BMS 也回了 ATT 确认（`ok=1`），但 **`notify=0`，一个 0x02 遥测帧都没收到**，配对也从不触发。

我做了个 v17 把 BMS 的所有 GATT 服务/特征/描述符枚举打印出来，真相出来了：

- FFE1 特征的 value handle 是 `0x000E`，属性是 `RWN`（可读可写可通知）
- **它声明了 NOTIFY，却在 GATT Find-Information 里不暴露 CCCD 描述符**
- 同板其他特征都正常暴露（例如 `0x000A`→CCCD 在 `0x000B`，`0x001A`→CCCD 在 `0x001B`）

也就是说：**这是 BMS 固件缺陷——它说「我能推送通知」，但又不把开推送的开关（CCCD）放进标准目录里。**

NimBLE 的 `registerForNotify()` 是靠「查目录找 CCCD → 写 0x0001 打开」来工作的。目录里没有，它就**一声不吭地失败了**（没有报错，只是通道没打开）。于是 BMS 没有往下推数据的管子，命令收得再好也白搭。手机 App 之所以能读，是因为它**直接拿了开关的真实地址去写**，不依赖目录。

---

## 四、成功修复（核心代码）

思路：**`registerForNotify()` 只用来装回调（它自己那次 CCCD 写会失败，无所谓），然后我们手动用 NimBLE 底层 API 把 `0x0001` 写到开关真实所在的 handle = `value_handle + 1 = 0x000F`，绕过目录发现。**

关键代码（基于 Arduino-ESP32 核心 3.x 自带 NimBLE，需 `#include <host/ble_gatt.h>`）：

```cpp
#include "BLEDevice.h"
#include "BLESecurity.h"
#include <host/ble_gatt.h>

static BLEClient* pClient = nullptr;
static BLERemoteCharacteristic* pChar = nullptr;

// CCCD 写完成回调
static int rawCccdWriteCb(uint16_t conn_handle, const struct ble_gatt_error *error,
                          struct ble_gatt_attr *attr, void *arg) {
  (void)conn_handle; (void)attr; (void)arg;
  Serial.printf("[SUB] CCCD 写完成 status=%d (%s)\n",
                error ? error->status : -1,
                error && error->status == 0 ? "OK" : "FAIL");
  return 0;
}

// 装回调 + 直接写 CCCD 到指定 handle（绕过描述符发现）
static void subscribeRaw(uint16_t cccdHandle) {
  if (!pChar) return;
  pChar->registerForNotify(notifyCb);            // 仅装回调，它自带的 CCCD 写会失败，没关系
  uint8_t v[2] = {0x01, 0x00};                  // 0x0001 = 开启通知
  ble_gattc_write_flat(pClient->getConnId(), cccdHandle, v, 2, rawCccdWriteCb, nullptr);
}

// 连上 + 加密后调用：先试 0x000F，8 秒没通知兜底试 0x0010
//   subscribeRaw(0x000F);
```

`error->status == 0` 就说明开关掰开了，接下来 0x02 帧会哗哗来。我这块板 `0x000F` 一次成功；如果你的板 CCCD 在 `0x0010`，把兜底逻辑加上即可（见下方完整流程）。

> 注意：命令发送的门控应该放在「CCCD 写成功」之后，而不是「加密 `enc==1` 之后」。配对/加密本身可选。

---

## 五、完整可复现流程

### 1. 环境
- Arduino CLI：`arduino-cli compile --fqbn esp32:esp32:esp32s3 ...`
- 小主机（Linux）：`pip install esptool pyserial`
- 烧录用 `esptool`，监视用 `cap.py`（就是个 90 秒串口抓取脚本，见文末）

### 2. 编译
```bash
arduino-cli compile --fqbn esp32:esp32:esp32s3 --build-path .build jk-bms-esp32s3.ino
# 产物: .build/jk-bms-esp32s3.ino.merged.bin (约 4MB)
```

### 3. 传到小主机并烧录
```bash
scp .build/jk-bms-esp32s3.ino.merged.bin root@192.168.1.26:/root/jkfw.bin
ssh root@192.168.1.26 \
  "python3 -m esptool --chip esp32s3 --port /dev/ttyUSB0 --baud 921600 \
     write_flash 0x0 /root/jkfw.bin"
```
⚠️ 烧录前务必退出占着串口的 miniterm（`tmux send-keys C-]`），否则 esptool 命令会被当成串口数据发给 ESP32，根本不执行。

### 4. 抓串口看结果
```bash
ssh root@192.168.1.26 "python3 /root/cap.py" > jk-serial.log
```
正常会看到：
```
[SUB] CCCD 写完成 status=0 (OK)
[DEV] JK-BD6A20S6PD
TELEMETRY V=67.017 I=0.000 P=0.000 SOC=99% RemAh=117.805 TotAh=119.000 T1=-200.0 T2=35.4 | cells:3.341,3.341,3.532,...
[status] conn=1 subbed=1 notify=76 ok=15 crcFail=0 retry=1
```

### 5. 协议要点（写给想自己解析的人）
- 服务 `FFE0`，特征 `FFE1`（写命令 + 收通知都用它）
- 请求帧 20 字节：`AA 55 90 EB` + `cmd(1)` + `len(1,=0)` + `value(uint32 LE,=0)` + 补零 + **CRC = 字节 0..18 的 8 位加法和**
- 响应帧 300 字节：`55 AA EB 90` + `frameType(1)`（`0x02`=遥测,`0x01`=设备/`0x03`=信息）+ 数据 + **CRC = 字节 0..298 的和，存于第 299 字节**
- 命令字节：`0x96`=电芯/遥测（触发 0x02 流）、`0x97`=设备信息（响应 0x03）、`0x95`=实时数据
- 把每包通知拼成 300 字节，校验 `55 AA EB 90` 头 + 第 299 字节 CRC，通过再解析

---

## 六、V20.27 已验证偏移表（JK-BD6A20S6PD / 20S）

这些偏移我是拿真实 0x02 帧逐字节对照验证过的，dscao 那套老的「电芯 uint32 / stride 4」在 V20.27 **是错的**，按下面这个来：

| 字段 | 偏移 | 类型 | 换算 | 实测值 |
|------|------|------|------|--------|
| 电芯电压（×20） | `6 + n*2` (n=0..19) | uint16 LE | ×0.001 V | 3.34~3.53 V |
| 总电压 | 150 | int32 LE | ×0.001 V | 67.0 V |
| 功率 | 154 | int32 LE(有符号) | ×0.001 W | 0 W |
| 电流 | 158 | int32 LE(有符号) | ×0.001 A | 0 A |
| 温度1 | 162 | int16 LE | ×0.1 °C | −200.0（=传感器未接哨兵值）|
| 温度2 | 164 | int16 LE | ×0.1 °C | 35.4 °C |
| SOC | 173 | uint8 | ×1 % | 99 % |
| 剩余容量 | 174 | uint32 LE | ×0.001 Ah | 117.8 Ah |
| 总容量 | 178 | uint32 LE | ×0.001 Ah | 119.0 Ah |

> 老社区固件（syssi/esphome-jk-bms, JK02_24S）偏移整体移位，套上来会读到 80V、154% 之类的垃圾，别用。

---

## 七、实测结果（我这块板）

- 板型：JK-BD6A20S6PD，V20.27，20S 锂电
- 总电压 **67.0 V**，电流 **0.0 A**，SOC **99%**，剩余 **117.8 Ah** / 总 **119.0 Ah**
- 20 节电芯 **3.34~3.53 V**（一致、健康；≈20×3.36=67.2V，和总电压吻合）
- 温度2 **35.4 °C**（温度1 显示 −200 是那一路没接传感器，属正常）
- `notify` 持续累加、`crcFail=0`，稳定不掉线

---

## 八、常见问题 FAQ

**Q：连上了但 `notify=0`？**
A：九成是 CCCD 没打开。先看 `[SUB] CCCD 写完成 status=` 是不是 `0`。不是 0 就试把 `0x000F` 换成 `0x0010`（少数板 CCCD 在隔壁）。别再往「解绑手机 / 配对」上想。

**Q：一定要配对/输密码吗？**
A：不用。配对可选，Just-Works 开着也行、关了也行。数据在明文链路上就能来，前提是 CCCD 开了。

**Q：命令发出去 `ok=1` 但还是没数据？**
A：命令被 ACK 只代表 BMS 收到了，不代表它会推。还是回到 CCCD——没开通知通道它就不推。

**Q：不同 JK 型号偏移一样吗？**
A：V20.27 / JK_PB 系列按第六节来。老固件（JK02_24S）整体移位，需要重新标定，方法：抓一包 0x02 帧全 300 字节 hex dump（代码里 `VERBOSE 1` 开关），对着已知量（比如总电压）反推偏移。

**Q：会伤板子吗？**
A：不会。所谓「调试废话」只是往串口打文字，ESP32-S3 干这点活儿毛毛雨，不发热不伤硬件。我发布的 v19 已经把这堆打印去掉了，串口只输出干净的一行行遥测。

---

## 九、固件

完整可用固件：`jk-bms-esp32s3.ino`（v19 清理版）。要点：
- 保留上面第四节的 CCCD 直接写修复
- 串口输出干净的一行 `TELEMETRY ...`（含 20 节电芯），5 秒一条 `[status]`
- 顶部 `#define VERBOSE 0`，改成 `1` 可重新打开每帧 300 字节 hex dump（标定偏移用）
- 实测烧录后 `notify` 稳定、`crcFail=0`

下一步可做（需要你给 WiFi 名/密码）：把遥测通过 WiFi/MQTT 转发到 Home Assistant，彻底不用连串口看。

---

## 十、致谢与参考

- 协议逆向：[dscao/jk_bms_ble](https://github.com/dscao/jk_bms_ble)（V20.27 偏移来源）
- 不配对也能读的社区实现：[zhang1997aaa/JK_BMS_TO_WEB-ESP32](https://github.com/zhang1997aaa/JK_BMS_TO_WEB-ESP32)、[BHP1000/JK-BMS-BLE-ESP32](https://github.com/BHP1000/JK-BMS-BLE-ESP32)（佐证「配对非必需」）
- 底层 CCCD 写法参考 NimBLE `ble_gattc_write_flat` 文档

**核心一句话再强调一次：JK V20.27 的 FFE1 把 CCCD 藏起来了，`registerForNotify()` 会静默失败；绕过发现、直接 `ble_gattc_write_flat` 写 `0x0001` 到 `value_handle+1` 就能打开通知。别再解绑手机、别再死磕配对了。**

---

### 附：cap.py（小主机上抓 90 秒串口用）
```python
import serial, time, sys
port = '/dev/ttyUSB0'
s = serial.Serial(port, 115200, timeout=0.5)
s.dtr = False; s.rts = False; time.sleep(0.15)
s.dtr = True; s.rts = True; time.sleep(2.0)
sys.stderr.write("RESET_DONE\n")
f = open('/root/jk-serial.log', 'wb')
t = time.time()
while time.time() - t < 90:
    d = s.read(256)
    if d:
        sys.stdout.buffer.write(d); f.write(d); f.flush()
s.close()
```
