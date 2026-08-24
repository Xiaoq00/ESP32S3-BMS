/*
 * JK-BMS BLE client for ESP32-S3  (NimBLE / Arduino-ESP32 core 3.x)
 * 目标板: JK-BD6A20S6PD, 软件 V20.27
 *
 * v19.2 —— 在 v19.1 基础上 + 写前密码解锁(命令 0x05, 值=6位设备密码):
 *   本板 V20.27 写设置前必须先发密码, 否则 BMS 静默忽略写入(实测 Phase2 初版写不进)。
 *   每次 writeJkRegister 先 unlockPassword() 再发写帧。
 *
 * v19.1 —— 在 v19 清理版基础上 + WiFi STA + PubSubClient -> MQTT 转发:
 *   读 BMS 遥测(同 v18/v19 根因修复: FFE1 隐藏 CCCD, 直接写 0x000F)后,
 *   周期把最新一帧以 JSON 发布到 MQTT 主题 jk-bms/state (retain),
 *   小主机(192.168.1.26:1883)上的 recorder 落盘、看板/agent 读取。
 *   配对非必需 (Just Works 可选); 命令门控在 CCCD 写成功。
 *   VERBOSE 置 1 可重新打开每帧 hex dump。
 */

#include <Arduino.h>
#include <map>
#include "BLEDevice.h"
#include "BLESecurity.h"
#include <host/ble_gatt.h>
#include <WiFi.h>
#include <PubSubClient.h>

// ===== 部署前请修改以下“配置区”常量（不要提交真实密码到公开仓库）=====
#define BMS_MAC       "AA:BB:CC:DD:EE:FF"   // 你的 BMS 蓝牙 MAC（在 JK App 设备信息里看）
#define SERVICE_UUID  0xFFE0
#define CHAR_UUID     0xFFE1
#define SERIAL_BAUD   115200
#define VERBOSE       0           // 1 = 打开全帧 hex dump（调试用，默认关）

// ---- WiFi / MQTT 转发配置（改成你自己的）----
#define WIFI_SSID     "YOUR_WIFI_SSID"      // WiFi 名称
#define WIFI_PASS     "YOUR_WIFI_PASSWORD"  // WiFi 密码
#define MQTT_BROKER   "192.168.1.26"        // 运行 Mosquitto 的小主机局域网 IP（本例 192.168.1.26）
#define MQTT_PORT      1883
#define MQTT_TOPIC    "jk-bms/state"
#define MQTT_TOPIC_SETTINGS "jk-bms/settings-raw"
#define MQTT_TOPIC_SET "jk-bms/set"   // 写指令通道(后端 /api/set 转发到此)
#define PUBLISH_MS     5000

// 设备密码(JK App 设置密码): 6 位, 作为 uint32 发送(命令 0x05)解锁写权限
// 本板 V20.27 写设置前必须先发密码, 否则 BMS 静默忽略写入(实测: 不发密码写 0x0E 读回不变)。
// "000000" -> 整数 0。参考 BHP1000/JK-BMS-BLE-ESP32 命令表(Password 0x05 Send 0x0001E240(123456))。
static const uint32_t JK_PASSWORD = 0;   // 6 位设备密码 "000000" -> 0

#define H0 0x55
#define H1 0xAA
#define H2 0xEB
#define H3 0x90

static const int CANDIDATE_SIZES[] = {300, 320, 340};
static const int N_CAND = 3;

static BLEAddress* pBmsAddr = nullptr;
static BLEClient*  pClient  = nullptr;
static BLERemoteCharacteristic* pChar = nullptr;
static bool deviceConnected = false;
static bool doConnect = false;

static uint8_t frameBuf[1024];
static size_t  frameLen = 0;
static uint32_t g_notify = 0, g_ok = 0, g_crcFail = 0, g_cmdsSent = 0, g_retry = 0;
static bool g_encrypted = false;
static bool g_subbed = false;          // 已安装回调 + 已 raw 写 CCCD
static bool g_triedF = false, g_tried10 = false;
static unsigned long connectStart = 0, g_subT = 0;
static int g_cmdState = 0;
static unsigned long g_cmdT = 0;
static int g_rawRc = -999;

// 最新遥测(供 MQTT 发布)
static float   g_voltage=0, g_current=0, g_power=0, g_remAh=0, g_totAh=0, g_temp1=0, g_temp2=0, g_cells[20]={0};
static uint8_t g_soc=0;
static bool    g_hasData=false;
static unsigned long g_lastPublish=0;

// 最新设置帧(0x01)原样保存, 供 PC 端标定偏移
static uint8_t g_settingsFrame[340];
static int     g_settingsLen=0;
static bool    g_hasSettings=false;
static unsigned long g_lastSettingsPub=0;
static char    g_model[24]={0};

static WiFiClient espClient;
static PubSubClient mqtt(espClient);

// 写指令回执(诊断): ESP32 收到 jk-bms/set 后回显到 jk-bms/set-ack
static char    g_cmdAck[64] = {0};
static bool    g_cmdAckReady = false;
// 排队的写请求(在主循环/BLE 上下文执行, 绝不在 MQTT 回调里直接调 BLE)
static uint8_t g_wReg = 0;
static uint32_t g_wVal = 0;
static bool    g_wPending = false;

static uint8_t jkCrc(const uint8_t* d, size_t len){ uint8_t c=0; for(size_t i=0;i<len;i++) c+=d[i]; return c; }
static uint32_t rdU32(const uint8_t* f, int o){ return (uint32_t)f[o]|((uint32_t)f[o+1]<<8)|((uint32_t)f[o+2]<<16)|((uint32_t)f[o+3]<<24); }
static uint16_t rdU16(const uint8_t* f, int o){ return (uint16_t)f[o]|((uint16_t)f[o+1]<<8); }
#if VERBOSE
static void hexDump(const uint8_t* b, size_t n){ for(size_t i=0;i<n;i+=16){ Serial.printf("%03u: ",i); for(size_t j=0;j<16;j++){ if(i+j<n) Serial.printf("%02X ",b[i+j]); else Serial.printf("   ");} Serial.println(); } }
#endif

// 原始 CCCD 写回调
static int rawCccdWriteCb(uint16_t conn_handle, const struct ble_gatt_error *error,
                          struct ble_gatt_attr *attr, void *arg) {
  (void)conn_handle; (void)attr; (void)arg;
  Serial.printf("[SUB] CCCD 写完成 status=%d (%s)\n",
                error ? error->status : -1, error && error->status == 0 ? "OK" : "FAIL");
  return 0;
}

static void processBuffer();
static void notifyCb(BLERemoteCharacteristic* pBLERemoteCharacteristic, uint8_t* pData, size_t length, bool isNotify);
static void writeJkRegister(uint8_t reg, uint32_t value);
static void mqttCb(char* topic, uint8_t* payload, unsigned int len);
static void notifyCb(BLERemoteCharacteristic* pBLERemoteCharacteristic, uint8_t* pData, size_t length, bool isNotify) {
  (void)pBLERemoteCharacteristic; (void)isNotify;
  g_notify++;
  if (frameLen + length > sizeof(frameBuf)) frameLen = 0;
  memcpy(frameBuf + frameLen, pData, length); frameLen += length;
  processBuffer();
}

static void handleFrame(const uint8_t* f, int size) {
  uint8_t t = f[4];
  if (t == 0x02) {
    int32_t  voltage = (int32_t)rdU32(f, 150);
    int32_t  power   = (int32_t)rdU32(f, 154);
    int32_t  current = (int32_t)rdU32(f, 158);
    int16_t  temp1   = (int16_t)rdU16(f, 162);
    int16_t  temp2   = (int16_t)rdU16(f, 164);
    uint8_t  soc     = f[173];
    uint32_t remAh   = rdU32(f, 174);
    uint32_t totAh   = rdU32(f, 178);
    // 存最新值(供 MQTT 发布)
    g_voltage = voltage*0.001f; g_current = current*0.001f; g_power = power*0.001f;
    g_soc = soc; g_remAh = remAh*0.001f; g_totAh = totAh*0.001f;
    g_temp1 = temp1*0.1f; g_temp2 = temp2*0.1f;
    for (int c=0;c<20;c++) g_cells[c] = rdU16(f, 6+c*2)*0.001f;
    g_hasData = true;
    // 干净的一行遥测
    Serial.printf("TELEMETRY V=%.3f I=%.3f P=%.3f SOC=%u%% RemAh=%.3f TotAh=%.3f T1=%.1f T2=%.1f | cells:",
                  g_voltage, g_current, g_power, soc, g_remAh, g_totAh, g_temp1, g_temp2);
    for (int c=0;c<20;c++){ Serial.printf("%.3f", g_cells[c]); if(c<19) Serial.print(","); }
    Serial.println();
#if VERBOSE
    Serial.println("[全帧 hex 0..299]"); hexDump(f, 300);
#endif
  } else if (t==0x03) {
    // 设备信息：型号字符串在 @6
    char name[24]={0}; for(int i=0;i<16 && f[6+i] && f[6+i]!=0;i++) name[i]=(char)f[6+i];
    strncpy(g_model, name, sizeof(g_model)-1);
    Serial.printf("[DEV] %s\n", name);
#if VERBOSE
    hexDump(f, 64);
#endif
  }
  else if (t==0x01) {
    // 设置帧: 原样保存, 由 PC 端对照已知量(串数=20/容量=119Ah)反推偏移
    if (size > 0 && size <= (int)sizeof(g_settingsFrame)) {
      memcpy(g_settingsFrame, f, size); g_settingsLen = size; g_hasSettings = true;
    }
    Serial.printf("[SET] 收到设置帧 %d 字节\n", size);
  }
  // 其它帧型暂不打印
}

static void publishTelemetry() {
  if (!g_hasData) return;
  float cmin=1e9, cmax=-1e9;
  for (int c=0;c<20;c++){ if (g_cells[c]<cmin) cmin=g_cells[c]; if (g_cells[c]>cmax) cmax=g_cells[c]; }
  char buf[1024];
  int n = snprintf(buf, sizeof(buf),
    "{\"online\":true,\"voltage\":%.3f,\"current\":%.3f,\"power\":%.3f,\"soc\":%u,"
    "\"remaining_ah\":%.3f,\"total_ah\":%.3f,\"temp1\":%.1f,\"temp2\":%.1f,\"cells\":[",
    g_voltage, g_current, g_power, g_soc, g_remAh, g_totAh, g_temp1, g_temp2);
  for (int c=0;c<20;c++){ n += snprintf(buf+n, sizeof(buf)-n, c<19?"%.3f,":"%.3f", g_cells[c]); }
  n += snprintf(buf+n, sizeof(buf)-n,
    "],\"cell_min\":%.3f,\"cell_max\":%.3f,\"bal\":%.3f,\"rssi\":%d,\"total\":%.3f,\"min\":%.3f,\"max\":%.3f}",
    cmin, cmax, cmax-cmin, WiFi.RSSI(), g_voltage, cmin, cmax);
  if (mqtt.publish(MQTT_TOPIC, buf, true)) {
    Serial.printf(">>> [MQTT] 发布 OK (%d B)\n", n);
  } else {
    Serial.println(">>> [MQTT] 发布失败");
  }
}

static void publishSettingsRaw() {
  if (!g_hasSettings || g_settingsLen<=0) return;
  char buf[1200];
  int n = snprintf(buf, sizeof(buf), "{\"len\":%d,\"hex\":\"", g_settingsLen);
  for (int i=0;i<g_settingsLen;i++){ n += snprintf(buf+n, sizeof(buf)-n, "%02X", g_settingsFrame[i]); }
  n += snprintf(buf+n, sizeof(buf)-n, "\"}");
  if (mqtt.publish(MQTT_TOPIC_SETTINGS, buf, true)) Serial.printf(">>> [MQTT] 设置原始帧发布 OK (%d B)\n", n);
  else Serial.println(">>> [MQTT] 设置原始帧发布失败");
}

static void publishSettings() {
  if (!g_hasSettings || g_settingsLen < 140) return;
  const uint8_t* f = g_settingsFrame;
  char buf[1200];
  int n = snprintf(buf, sizeof(buf),
    "{\"model\":\"%s\",\"cell_count\":%d,\"capacity_ah\":%.2f,\"cell_type_raw\":%d,"
    "\"balance_enabled_raw\":%d,\"balance_start_mv_raw\":%d,\"thresholds_mv\":{",
    g_model, (int)f[114], (float)rdU32(f,130)/1000.0f, (int)f[116], (int)f[118], (int)rdU16(f,26));
  bool first=true;
  for (int off=6; off<=46; off+=2) {
    int v = rdU16(f, off);
    n += snprintf(buf+n, sizeof(buf)-n, "%s\"%d\":%d", first?"":",", off, v);
    first=false;
  }
  n += snprintf(buf+n, sizeof(buf)-n, "}}");
  if (mqtt.publish("jk-bms/settings", buf, true)) Serial.printf(">>> [MQTT] 设置结构化发布 OK (%d B)\n", n);
  else Serial.println(">>> [MQTT] 设置结构化发布失败");
}

static void processBuffer() {
  while (frameLen >= (size_t)CANDIDATE_SIZES[0]) {
    bool header = (frameBuf[0]==H0&&frameBuf[1]==H1&&frameBuf[2]==H2&&frameBuf[3]==H3);
    if (header) {
      bool parsed=false;
      for (int k=0;k<N_CAND;k++){ int S=CANDIDATE_SIZES[k];
        if ((int)frameLen>=S){ uint8_t crc=jkCrc(frameBuf,S-1); if(crc==frameBuf[S-1]){ g_ok++; handleFrame(frameBuf,S); memmove(frameBuf,frameBuf+S,frameLen-S); frameLen-=S; parsed=true; break; } } }
      if (parsed) continue;
      if ((int)frameLen>=CANDIDATE_SIZES[N_CAND-1]){ memmove(frameBuf,frameBuf+1,frameLen-1); frameLen-=1; g_crcFail++; }
      else break;
    } else {
      size_t i=1; bool found=false;
      while(i<frameLen-3){ if(frameBuf[i]==H0&&frameBuf[i+1]==H1&&frameBuf[i+2]==H2&&frameBuf[i+3]==H3){found=true;break;} i++; }
      if(found){ memmove(frameBuf,frameBuf+i,frameLen-i); frameLen-=i; } else frameLen=0;
    }
  }
}

static void sendJkCommand(uint8_t cmd) {
  if (!pChar) return;
  uint8_t frame[20]; memset(frame,0,sizeof(frame));
  frame[0]=0xAA;frame[1]=0x55;frame[2]=0x90;frame[3]=0xEB;frame[4]=cmd;frame[5]=0x00;
  uint8_t sum=0; for(int i=0;i<19;i++) sum+=frame[i]; frame[19]=sum;
  pChar->writeValue(frame,20,true); g_cmdsSent++;
}

// ===== 解锁写权限(本板 V20.27 写设置前必须先发密码)=====
// 命令 0x05, 值 = 6 位设备密码(uint32 LE)。参考 BHP1000/JK-BMS-BLE-ESP32 命令表:
//   "Password 0x05  Send 0x0001E240 (123456) to unlock settings/MOS writes"
// 每次写设置前都先发一次(与 JK App 行为一致: 每次改设置先输密码)。
static void unlockPassword() {
  if (!pChar) { Serial.println(">>> [UNLOCK] 无 BLE 连接, 跳过"); return; }
  uint8_t frame[20]; memset(frame,0,sizeof(frame));
  frame[0]=0xAA;frame[1]=0x55;frame[2]=0x90;frame[3]=0xEB;
  frame[4]=0x05; frame[5]=0x04;                 // reg=0x05, len=4
  uint32_t pw = JK_PASSWORD;
  frame[6]=(uint8_t)(pw & 0xFF);
  frame[7]=(uint8_t)((pw>>8)&0xFF);
  frame[8]=(uint8_t)((pw>>16)&0xFF);
  frame[9]=(uint8_t)((pw>>24)&0xFF);
  uint8_t sum=0; for(int i=0;i<19;i++) sum+=frame[i]; frame[19]=sum;
  pChar->writeValue(frame,20,true); g_cmdsSent++;
  Serial.printf(">>> [UNLOCK] 发送密码解锁帧 pw=%u (frame:%02X%02X%02X%02X %02X%02X%02X%02X %02X)\n",
                pw, frame[0],frame[1],frame[2],frame[3],frame[4],frame[5],frame[6],frame[7],frame[19]);
}

// ===== 写 BMS 设置寄存器（Phase 2，仅允许名单内）=====
// 写帧: AA 55 90 EB | reg | 0x04 | val(uint32 LE) | 9×0 | CRC(sum 0..18)
// 允许名单: 0x1F=均衡开关(1开/0关), 0x0E=均衡触发压差(mV, uint32)
static void writeJkRegister(uint8_t reg, uint32_t value) {
  if (!pChar) { Serial.println(">>> [WRITE] 无 BLE 连接, 跳过"); return; }
  if (reg != 0x1F && reg != 0x0E) {
    Serial.printf(">>> [WRITE] 拒绝: reg=0x%02X 不在允许名单(仅 0x1F/0x0E)\n", reg);
    return;
  }
  unlockPassword();          // 先解锁写权限(本板 V20.27 要求, 否则静默忽略)
  delay(150);                // 等解锁生效再发写帧
  uint8_t frame[20]; memset(frame,0,sizeof(frame));
  frame[0]=0xAA;frame[1]=0x55;frame[2]=0x90;frame[3]=0xEB;
  frame[4]=reg; frame[5]=0x04;
  frame[6]=(uint8_t)(value & 0xFF);
  frame[7]=(uint8_t)((value>>8)&0xFF);
  frame[8]=(uint8_t)((value>>16)&0xFF);
  frame[9]=(uint8_t)((value>>24)&0xFF);
  uint8_t sum=0; for(int i=0;i<19;i++) sum+=frame[i]; frame[19]=sum;
  pChar->writeValue(frame,20,true);
  g_cmdsSent++;
  Serial.printf(">>> [WRITE] reg=0x%02X value=%u (frame:%02X%02X%02X%02X %02X%02X%02X%02X %02X)\n",
                reg, value, frame[0],frame[1],frame[2],frame[3],frame[4],frame[5],frame[6],frame[7],frame[19]);
  // 写完立即请求刷新设置帧(本板 0x95=settings), 便于读回校验
  sendJkCommand(0x95);
}

// MQTT 指令通道: 主题 jk-bms/set, 负载格式 "reg,value"(十进制)
// 例: "31,0" => 关均衡; "14,30" => 均衡触发压差=30mV
static void mqttCb(char* topic, uint8_t* payload, unsigned int len) {
  if (!topic) return;
  String t(topic);
  if (t != "jk-bms/set") return;
  if (len == 0) return;
  char buf[32]; unsigned int n = len < (sizeof(buf)-1) ? len : (sizeof(buf)-1);
  memcpy(buf, payload, n); buf[n] = 0;
  snprintf(g_cmdAck, sizeof(g_cmdAck), "%s", buf); g_cmdAckReady = true;  // 回执(诊断)
  int reg = -1; long value = 0;
  if (sscanf(buf, "%d,%ld", &reg, &value) == 2) {
    if (reg == 0x1F || reg == 0x0E) {
      g_wReg = (uint8_t)reg; g_wVal = (uint32_t)value; g_wPending = true;
      Serial.printf("[SET] 收到指令 reg=%d value=%ld (已排队, 等主循环执行)\n", reg, value);
    } else {
      Serial.printf("[SET] 拒写: reg=%d 不在允许名单(仅 31/14)\n", reg);
    }
  } else {
    Serial.printf("[SET] 指令格式错误: %s (应为 reg,value)\n", buf);
  }
}

// 安装回调 + 原始写 CCCD 到指定 handle
static void subscribeRaw(uint16_t cccdHandle) {
  if (!pChar) return;
  pChar->registerForNotify(notifyCb);   // 仅安装回调(CCCD 写会失败, 但回调已挂上)
  uint8_t v[2] = {0x01, 0x00};
  g_rawRc = ble_gattc_write_flat(pClient->getConnId(), cccdHandle, v, 2, rawCccdWriteCb, nullptr);
  Serial.printf(">>> [SUB] 原始写 CCCD@0x%04X -> ble_gattc_write rc=%d\n", cccdHandle, g_rawRc);
}

class JkSecurity : public BLESecurityCallbacks {
  void onAuthenticationComplete(ble_gap_conn_desc *desc) override {
    if (desc) { g_encrypted = desc->sec_state.encrypted; }
    else { g_encrypted=false; }
  }
};

class JkClientCb : public BLEClientCallbacks {
  void onConnect(BLEClient* p) override { (void)p; deviceConnected=true; g_retry++; connectStart=millis(); Serial.printf(">>> [CONN] 已连接 (retry=%u)\n", g_retry); }
  void onDisconnect(BLEClient* p) override { (void)p; deviceConnected=false; g_subbed=false; g_triedF=false; g_tried10=false; g_encrypted=false; g_cmdState=0; doConnect=true; BLESecurity::resetSecurity(); Serial.println(">>> [CONN] 断开, 重连"); }
};

void setup() {
  Serial.begin(SERIAL_BAUD); delay(150);
  Serial.println("\n=== JK-BMS ESP32-S3 V20.27 读取器 v19.2 (+WiFi/MQTT/写前密码解锁) BOOT ===");
  delay(2000);
  // WiFi STA
  WiFi.mode(WIFI_STA);
  WiFi.setHostname("jk-esp32s3");
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf(">>> [WIFI] 连接 %s ...\n", WIFI_SSID);
  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setBufferSize(2048);
  mqtt.setCallback(mqttCb);
  // BLE
  BLEDevice::init("jk-esp32s3");
  pClient = BLEDevice::createClient();
  pClient->setClientCallbacks(new JkClientCb());
  pBmsAddr = new BLEAddress(BMS_MAC);
  BLESecurity::setCapability(ESP_IO_CAP_NONE);
  BLESecurity::setAuthenticationMode(true, false, false);
  BLEDevice::setSecurityCallbacks(new JkSecurity());
  BLESecurity::setForceAuthentication(true);
  doConnect = true;
  Serial.println("就绪");
}

void loop() {
  // ===== WiFi / MQTT 维护 + 发布 =====
  if (WiFi.status() != WL_CONNECTED) {
    static unsigned long wt=0;
    if (millis()-wt > 5000) { wt=millis(); Serial.println(">>> [WIFI] 未连, 重试..."); WiFi.begin(WIFI_SSID, WIFI_PASS); }
  } else {
    if (!mqtt.connected()) {
      static unsigned long mt=0;
      if (millis()-mt > 5000) {
        mt=millis();
        Serial.printf(">>> [MQTT] 连接 %s:%d ...\n", MQTT_BROKER, MQTT_PORT);
        // LWT 遗嘱: 异常断线时 broker 保留 online:false
        if (mqtt.connect("jk-esp32s3", nullptr, nullptr, MQTT_TOPIC, 0, true, "{\"online\":false}")) {
          Serial.println(">>> [MQTT] 已连接");
          mqtt.subscribe(MQTT_TOPIC_SET);
          Serial.printf(">>> [MQTT] 已订阅指令通道 %s\n", MQTT_TOPIC_SET);
        } else {
          Serial.printf(">>> [MQTT] 失败 rc=%d\n", mqtt.state());
        }
      }
    } else {
      mqtt.loop();
      if (g_hasData && (millis()-g_lastPublish > PUBLISH_MS)) {
        g_lastPublish = millis();
        publishTelemetry();
      }
      if (g_hasSettings && (millis()-g_lastSettingsPub > PUBLISH_MS)) {
        g_lastSettingsPub = millis();
        publishSettingsRaw();
        publishSettings();
      }
      // 执行排队的写指令(主循环上下文, 安全调用 BLE); 并回显指令收据
      if (g_wPending) {
        g_wPending = false;
        writeJkRegister(g_wReg, g_wVal);
      }
      if (g_cmdAckReady) {
        g_cmdAckReady = false;
        char ab[80]; snprintf(ab, sizeof(ab), "{\"recv\":\"%s\"}", g_cmdAck);
        mqtt.publish("jk-bms/set-ack", ab, false);
      }
    }
  }

  // ===== BLE 连接 BMS =====
  if (doConnect) { doConnect=false;
    if (pClient->connect(*pBmsAddr)) { Serial.println(">>> [CONN] connect 成功"); pClient->setMTU(247); delay(400);
      BLERemoteService* pSvc = pClient->getService(BLEUUID((uint16_t)SERVICE_UUID));
      if (pSvc) pChar = pSvc->getCharacteristic(BLEUUID((uint16_t)CHAR_UUID));
      Serial.printf(">>> FFE1 pChar=%s\n", pChar?"OK":"NULL");
    } else { Serial.println("连接失败"); doConnect=true; delay(2000); }
  }

  unsigned long now = millis();
  // 订阅: 加密后, 先试 0x000F, 若 8s 无通知再试 0x0010
  if (deviceConnected && pChar && g_encrypted && !g_subbed) {
    g_subbed = true; g_subT = now; g_triedF = true;
    subscribeRaw(0x000F);
  }
  if (deviceConnected && g_subbed && g_triedF && !g_tried10 && g_notify == 0 && now - g_subT > 8000) {
    g_tried10 = true;
    Serial.println(">>> [SUB] 0x000F 无通知, 兜底试 0x0010");
    subscribeRaw(0x0010);
  }

  // 命令 (门控在订阅成功, 而非加密)
  if (deviceConnected && pChar && g_subbed) {
    if (g_cmdState==0 && now - g_subT > 800) { sendJkCommand(0x97); g_cmdState=1; g_cmdT=now; }
    else if (g_cmdState==1 && now - g_cmdT >= 500) { sendJkCommand(0x96); g_cmdState=2; g_cmdT=now; }
    else if (g_cmdState==2 && now - g_cmdT >= 500) { sendJkCommand(0x95); g_cmdState=3; g_cmdT=now; }
    else if (g_cmdState==3 && now - g_cmdT >= 2500) { sendJkCommand(0x96); g_cmdT=now; }
  }

  static unsigned long last=0;
  if (now-last>5000){ last=now;
    Serial.printf("[status] wifi=%s mqtt=%s conn=%d subbed=%d notify=%u ok=%u crcFail=%u retry=%u\n",
                  WiFi.status()==WL_CONNECTED?"Y":"N", mqtt.connected()?"Y":"N",
                  deviceConnected, g_subbed, g_notify, g_ok, g_crcFail, g_retry);
  }
  delay(50);
}
