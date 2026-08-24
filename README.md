# JK-BMS-ESP32 网关（极空 BMS 蓝牙遥测转发 + 看板）

把一块 **ESP32-S3** 用蓝牙(BLE)连上 **极空 JK-BD6A20S6PD**（软件 **V20.27，20S 被动均衡**）保护板，
24 小时读取电池遥测（总压/电流/功率/SOC/每节电芯电压/温度），经 WiFi 转发到局域网内小主机的 MQTT，
再由 Python recorder 落盘、Flask 看板展示。配套还实现了「写设置」通道（均衡开关 / 均衡触发压差），
详见下方「已知限制」——**在 V20.27 固件上写设置被 BMS 静默拒绝，读遥测完全正常**。

> 踩坑全过程（CCCD 隐藏、配对误区、解锁写机制）见 [`docs/JK-BMS-ESP32-踩坑与成功指南.md`](docs/JK-BMS-ESP32-踩坑与成功指南.md)。

---

## 一、系统架构

```
┌─────────────┐  BLE(FFE0/FFE1)   ┌──────────────────┐  WiFi(MQTT)   ┌──────────────────────────┐
│  JK-BD6A20S6PD │ ───────────────► │  ESP32-S3 固件     │ ────────────► │  小主机(Arbian/Mosquitto)  │
│  V20.27 20S    │  读 0x02/0x01 帧  │  jk-bms-esp32s3.ino│  jk-bms/state │  127.0.0.1:1883            │
│  (被动均衡)     │ ◄──── 写 0x1F/0x0E│  (NimBLE+PubSub)  │  jk-bms/set  │       │                      │
└─────────────┘  解锁帧 0x05      └──────────────────┘               │                      │
                                                                     ▼                      ▼
                                                          ┌──────────────┐      ┌──────────────────────┐
                                                          │ recorder.py   │      │  app.py (Flask :8899)  │
                                                          │ (落盘 jsonl)  │      │  看板 API + 写指令转发  │
                                                          └──────────────┘      └──────────────────────┘
                                                                                    │
                                                                                    ▼
                                                                          dashboard.html (浏览器看板)
```

**MQTT 主题约定**
| 主题 | 方向 | 内容 |
|------|------|------|
| `jk-bms/state` | ESP32 → | 最新遥测 JSON（retain） |
| `jk-bms/settings-raw` | ESP32 → | 设置原始帧 `{len, hex}`（retain，供标定偏移） |
| `jk-bms/settings` | ESP32 → | 结构化设置 JSON（retain） |
| `jk-bms/set` | 后端 → | 写指令 `31,0\|1`（均衡开关）/ `14,<mV>`（均衡压差） |
| `jk-bms/set-ack` | ESP32 → | 写指令回执（诊断） |

---

## 二、组件清单（本仓库结构）

```
jk-bms-gateway/
├── esp32-firmware/
│   └── jk-bms-esp32s3.ino     # ESP32-S3 固件（NimBLE 读 BMS + PubSubClient 转发 MQTT）
├── gateway/                   # 小主机侧后端（Python）
│   ├── app.py                 # Flask 看板 + /api/set 写指令转发
│   ├── recorder.py            # 订阅 jk-bms/state 落盘 latest.json / history.jsonl / agg.json
│   ├── dashboard.html         # 单文件看板（相对 fetch BASE+'/api/...'，无硬编码 IP）
│   ├── decode_settings.py     # 解析 settings 帧偏移的参考脚本
│   ├── settings_raw.json      # 一帧设置原始帧样例（标定用）
│   ├── requirements.txt       # Flask==3.0.0, paho-mqtt==1.6.1
│   ├── mosquitto.conf.example # listener 1883 0.0.0.0 + allow_anonymous true 示例
│   └── systemd/
│       ├── jk-bms-dashboard.service
│       └── jk-bms-recorder.service
├── docs/
│   └── JK-BMS-ESP32-踩坑与成功指南.md
├── .gitignore
└── README.md
```

> **未入库**：`.build/`、`.jkbuild/`、`.bin` 编译产物、`history.jsonl`/`agg.json`/`latest.json` 运行数据、
> `*.bak` 备份、`jk-ref/`（第三方协议资料）、`venv/`。见 `.gitignore`。

---

## 三、硬件 / 软件前提

- 极空 **JK-BD6A20S6PD**，固件 **V20.27**，20 串，被动均衡（电阻泄放，非主动均衡）。
- **ESP32-S3** 开发板（带 USB 转串口，CH340/CP210x 均可），刷 Arduino-ESP32 core **3.x**。
- 一台小主机（Arbian/Debian 系，本例 `192.168.1.26`），装 Mosquitto + Python3 venv。
- 编译用 PC：装 [`arduino-cli`](https://arduino.github.io/arduino-cli/)（本例 Windows，加 ESP32 板卡支持）。

---

## 四、ESP32 固件：构建与烧录

### 1. 修改配置区（部署前必做）

打开 `esp32-firmware/jk-bms-esp32s3.ino`，改顶部「配置区」常量：

```cpp
#define BMS_MAC       "AA:BB:CC:DD:EE:FF"   // ← 改成你的 BMS 蓝牙 MAC（JK App 设备信息里看）
#define WIFI_SSID     "YOUR_WIFI_SSID"      // ← 改成你的 WiFi
#define WIFI_PASS     "YOUR_WIFI_PASSWORD"  // ← 改成你的 WiFi 密码
#define MQTT_BROKER   "192.168.1.26"        // ← 改成跑 Mosquitto 的小主机局域网 IP
static const uint32_t JK_PASSWORD = 0;      // 6 位设备密码 "000000" -> 0（V20.27 写设置前解锁用）
```

> ⚠️ **不要把真实 WiFi 密码 / BMS MAC 提交到公开仓库**。本仓库已用占位符脱敏，
> 部署时本地改成真实值即可（或走 CI 注入 / 环境变量）。

### 2. 编译（arduino-cli）

```bash
arduino-cli compile -b esp32:esp32:esp32s3 \
  --output-dir .build \
  esp32-firmware/jk-bms-esp32s3.ino
# 产物：.build/jk-bms-esp32s3.ino.merged.bin
```

### 3. 烧录（小主机上 esptool）

```bash
# 1) 把 merged.bin 传到小主机
scp .build/jk-bms-esp32s3.ino.merged.bin root@192.168.1.26:/tmp/jkfw.bin

# 2) 小主机上先释放串口占用，再烧录
ssh root@192.168.1.26
fuser -k /dev/ttyUSB0            # 杀掉占用串口的进程（如残留 picocom）
esptool --chip esp32s3 --port /dev/ttyUSB0 --baud 921600 \
  write_flash -z 0x0 /tmp/jkfw.bin
```

烧录后串口默认 115200  baud，可看到 `TELEMETRY V=...` 心跳行即正常。

---

## 五、小主机部署（Mosquitto + Flask + recorder + systemd）

### 1. Mosquitto

```bash
sudo apt install -y mosquitto mosquitto-clients
# 用仓库里的示例覆盖（匿名 + 监听 1883），或自建：
sudo cp gateway/mosquitto.conf.example /etc/mosquitto/conf.d/jk-bms.conf
sudo systemctl restart mosquitto
```

`mosquitto.conf.example` 默认 `listener 1883 0.0.0.0` + `allow_anonymous true`（内网演示用；
上公网请改 `password_file` + 用户密码，示例注释已给出 `mosquitto_passwd` 命令）。

### 2. Python 后端（venv）

```bash
sudo mkdir -p /opt/jk-bms && sudo chown $USER /opt/jk-bms
cp gateway/app.py gateway/recorder.py gateway/dashboard.html /opt/jk-bms/
cd /opt/jk-bms
python3 -m venv venv && source venv/bin/activate
pip install -r gateway/requirements.txt   # Flask==3.0.0 paho-mqtt==1.6.1
```

`app.py` / `recorder.py` 的 `BROKER` 默认 `127.0.0.1`（Mosquitto 同机）；跨机请改 IP。
后端**不硬编码任何 WiFi 密码 / BMS MAC**，可安全上传。

### 3. systemd 开机自启

```bash
sudo cp gateway/systemd/jk-bms-dashboard.service /etc/systemd/system/
sudo cp gateway/systemd/jk-bms-recorder.service  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jk-bms-dashboard
sudo systemctl enable --now jk-bms-recorder
```

- `jk-bms-dashboard`：运行 `app.py`，监听 **8899** 端口（看板 `http://<小主机IP>:8899/`）。
- `jk-bms-recorder`：运行 `recorder.py`，落盘到 `/opt/jk-bms/`（`latest.json`/`history.jsonl`/`agg.json`）。

> 两个 service 里的 `User=` / `WorkingDirectory=` / `ExecStart=` 路径请按实际部署调整。

---

## 六、后端 API（app.py）

| 方法 & 路径 | 说明 |
|------|------|
| `GET /` | 返回 `dashboard.html` 看板 |
| `GET /api/summary` | 当前汇总（电压/电流/SOC/温度/最值电芯） |
| `GET /api/settings` | 当前 BMS 设置（结构化 JSON） |
| `GET /api/history` | 历史时序（recorder 落盘） |
| `GET /api/data` | 原始 latest.json |
| `POST /api/set` | **写指令** —— 仅接受 JSON（见下），表单会被拒 |

`POST /api/set` 请求体（JSON）：

```json
{ "action": "balance",         "value": 1 }      // 均衡开关：value ∈ {0,1} → 发 "31,0|1"（寄存器 0x1F）
{ "action": "balance_trigger", "value": 25 }      // 均衡触发压差：value ∈ 1..500(mV) → 发 "14,25"（寄存器 0x0E）
```

后端把指令 publish 到 `jk-bms/set`，ESP32 收到后在 BLE 主循环里排队执行 `writeJkRegister`。

```bash
# 正确调用示例（必须是 JSON，不是表单）：
curl -X POST -H "Content-Type: application/json" \
  -d '{"action":"balance_trigger","value":25}' \
  http://192.168.1.26:8899/api/set
# → {"queued":true,"payload":"14,25"}
```

---

## 七、协议与偏移速查（JK02 帧）

帧头固定 `55 AA EB 90`，第 5 字节为帧类型。

### 遥测帧 `0x02`（ESP32 → `jk-bms/state`）

| 字段 | 偏移 | 类型 | 换算 |
|------|------|------|------|
| 总电压 | 150 | int32 | ×0.001 V |
| 功率 | 154 | int32 | ×0.001 W |
| 电流 | 158 | int32 | ×0.001 A（放电容正/充负依固件） |
| 温度1 | 162 | int16 | ×0.1 ℃ |
| 温度2 | 164 | int16 | ×0.1 ℃ |
| SOC | 173 | uint8 | % |
| 剩余容量 | 174 | uint32 | ×0.001 Ah |
| 总容量 | 178 | uint32 | ×0.001 Ah |
| 电芯电压×20 | 6 + i×2 | uint16 | ×0.001 V（i=0..19） |

### 设置帧 `0x01`（ESP32 → `jk-bms/settings` / `settings-raw`）

| 字段 | 偏移 | 类型 |
|------|------|------|
| 电芯数 | 114 | uint8（本板=20） |
| 电芯类型 raw | 116 | uint8 |
| 均衡使能 raw | 118 | uint8 |
| 容量 | 130 | uint32 ×0.001 Ah |
| 均衡触发压差 raw | 26 | uint16（raw 值，单位需对照 JK App 实测） |

### 写寄存器（ESP32 → BMS）

| 寄存器 | 含义 | 值编码 |
|--------|------|--------|
| `0x1F` (31) | 均衡开关 | 0 / 1 |
| `0x0E` (14) | 均衡触发压差 | mV（详细编码请对照 JK App 实测，本板未验证） |

写帧格式：`AA 55 90 EB | reg(1B) | 0x04 | uint32 LE 值 | 9×0x00 | CRC(sum[0..18])`。
写前先发 **解锁帧**：`AA 55 90 EB | 0x05 | 0x04 | 密码 uint32 LE | 9×0 | CRC`（命令 `0x05` 参考 BHP1000/JK-BMS-BLE-ESP32 命令表；密码为本板 6 位设备密码，本例 `000000`→0）。

---

## 八、已知限制（重要）

1. **V20.27 写设置被静默拒绝（已实测）**
   即便加了 `0x05`+密码(0) 解锁帧，向 `0x0E` 写 25mV、监听 `jk-bms/settings` 16s，`balance_start_mv_raw` 恒为 10 不变。
   推测本板是固件变体（读命令已反转），解锁机制不同于 v19H 参考（可能需 JK App 改设置时的「第 3 个未知命令 / 3 声 beep」或不同密码编码）。
   **结论：读遥测完全可靠；写设置在本板当前固件下不可用。代码已就位，待找到正确解锁方式后可启用。**

2. **被动均衡**：本板是电阻泄放式被动均衡，均衡电流小、发热，调理用途而非快速均衡。

3. **配对非必需**：V20.27 下任意手机/ESP32 输密码即可新连，无「解绑」概念；配对可选，不是数据前提。

---

## 九、安全 / 脱敏说明

- 仓库内 **不含** 任何真实 WiFi 密码、BMS MAC、SSH 密钥。固件配置区已用占位符。
- 部署时本地改 `.ino` 配置区即可，请勿把真实值 `git commit`。
- `mosquitto.conf.example` 默认匿名，仅供内网；公网请启用 `password_file`。
- `app.py` / `recorder.py` 无硬编码凭证，可安全开源。

---

## 十、上传到 GitHub

本仓库已整理为「可上传状态」：源码 + 配置 + 文档齐全，密钥已脱敏，无关产物已 `.gitignore` 排除。
等你给 GitHub 仓库地址后，执行：

```bash
cd jk-bms-gateway
git init
git add -A
git commit -m "JK-BMS-ESP32 gateway: BLE telemetry forward + Flask dashboard (V20.27)"
git remote add origin <你给的 GitHub 地址>
git branch -M main
git push -u origin main
```

> 注意：首次 `git add -A` 前请确认 `git status` 里没有 `.bin` / `history.jsonl` / `*.bak` 等被误纳入
> （已被 `.gitignore` 覆盖，但仍建议过一眼）。
