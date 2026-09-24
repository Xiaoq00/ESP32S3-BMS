#!/usr/bin/env python3
# RK3566 看板：订阅 MQTT(jk-bms/state JSON) → 提供移动端网页
# 手机通过 Tailscale Funnel 的 /bms 路径(同一网址)即可看电量；局域网也可直连 8899
# 运行：pip3 install -r requirements.txt && python3 app.py
import json, threading, os, time, datetime
import paho.mqtt.client as mqtt
from flask import Flask, request, send_file

BROKER = "127.0.0.1"   # 若 Mosquitto 在容器/其他地址请改
PORT   = 1883
TOPIC  = "jk-bms/state"
SETTINGS_TOPIC = "jk-bms/settings"   # ESP32 破解读取的 BMS 设置帧(0x01)结构化 JSON
DATA_DIR = "/opt/jk-bms"   # recorder 落盘目录(latest.json / history.jsonl / agg.json)
HISTORY  = os.path.join(DATA_DIR, "history.jsonl")
AGG      = os.path.join(DATA_DIR, "agg.json")

# ===== 均衡线电阻(固件只发原始 uint16, 这里决定 Ω 值) =====
# 校准(系数/单位修正)只改这一处, 不用重刷固件。
RES_OHM_SCALE = 0.001
# 遥测 JSON 行变长(约 431B → 590B)后, "读文件末尾 N 字节"的窗口会缩短:
#   trend 窗口若仍是 64KB 只够 ~9.4 分钟 < 代码里写死的 10 分钟回看 → voltage_10min_ago
#   会静默消失, 故提到 160KB(~37 分钟)。
HIST_TREND_BYTES = 160 * 1024
# /api/history?range=day 的窗口: 12MB 从 ~39h 降到 ~28.5h(够一天但余量少), 提到 16MB(~40h)。
HIST_DAY_BYTES   = 16 * 1024 * 1024
# 「充满/放完」预测用的电流均值窗口(秒)。太短会被瞬时波动带偏, 太长反应迟钝。
RATE_WINDOW_S    = 300

state = {"cells": [], "total": 0.0, "min": 0.0, "max": 0.0, "bal": 0.0, "rssi": 0,
         "cell_res_raw": [], "cell_res_alert": 0, "online": False}
lock = threading.Lock()

# BMS 设置(来自 jk-bms/settings，破解读取 0x01 帧；关键阈值建议以官方 JK App 为准)
settings = {"model": None, "cell_count": None, "capacity_ah": None,
            "cell_type_raw": None, "balance_enabled_raw": None,
            "balance_start_mv_raw": None, "thresholds_mv": {}}
settings_lock = threading.Lock()

def on_connect(cli, userdata, flags, rc):
    cli.subscribe(TOPIC)
    cli.subscribe(SETTINGS_TOPIC)

def on_message(cli, userdata, msg):
    try:
        d = json.loads(msg.payload)
        if msg.topic == SETTINGS_TOPIC:
            with settings_lock:
                settings.clear(); settings.update(d)
            return
        with lock:
            # 白名单: 新字段不加进来就会"静默透不出"(不报错, 最难查)
            for k in ("cells", "total", "min", "max", "bal", "rssi",
                      "cell_res_raw", "cell_res_alert", "fw"):
                if k in d: state[k] = d[k]
            state["online"] = bool(d.get("online", True))
    except Exception as e:
        print("parse err:", e)

mqttc = mqtt.Client()
mqttc.on_connect = on_connect
mqttc.on_message = on_message
try:
    mqttc.connect(BROKER, PORT, 60)
except Exception as e:
    print("MQTT 连接失败(看板仍会启动,稍后重试):", e)
threading.Thread(target=mqttc.loop_forever, daemon=True).start()

app = Flask(__name__)

# 禁止任何中间层(Funnel/CDN/浏览器)缓存，保证每次都拿最新电瓶数据
@app.after_request
def _nocache(resp):
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>锂电池实时状态</title>
<style>
  :root{--bg:#0d1117;--card:#161b22;--fg:#e6edf3;--mut:#8b949e;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--bar:#1f6feb}
  *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,"PingFang SC","Microsoft YaHei",sans-serif}
  .wrap{max-width:860px;margin:0 auto;padding:18px}
  h1{font-size:20px;margin:0 0 12px}
  .top{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
  .stat{background:var(--card);border:1px solid #21262d;border-radius:10px;padding:12px 16px;flex:1;min-width:130px}
  .stat .k{color:var(--mut);font-size:12px}.stat .v{font-size:24px;font-weight:600;margin-top:4px}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--bad);margin-right:6px}
  .dot.on{background:var(--ok)}
  .cells{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
  .cell{background:var(--card);border:1px solid #21262d;border-radius:8px;padding:10px}
  .cell .t{font-size:12px;color:var(--mut)}.cell .n{font-size:18px;font-weight:600}
  .track{height:8px;background:#21262d;border-radius:5px;margin-top:8px;overflow:hidden}
  .fill{height:100%;background:var(--bar);border-radius:5px;transition:width .4s}
</style>
</head>
<body>
<div class="wrap">
  <h1><span id="dot" class="dot"></span>离网锂电池实时状态</h1>
  <div class="top">
    <div class="stat"><div class="k">总电压</div><div class="v"><span id="tv">--</span> V</div></div>
    <div class="stat"><div class="k">压差(均衡)</div><div class="v"><span id="bd">--</span> mV</div></div>
    <div class="stat"><div class="k">最低/最高</div><div class="v"><span id="mn">--</span>/<span id="mx">--</span></div></div>
    <div class="stat"><div class="k">蓝牙信号</div><div class="v"><span id="rssi">--</span></div></div>
  </div>
  <div class="cells" id="cells"></div>
</div>
<script>
function color(d){return d>30?'var(--bad)':d>10?'var(--warn)':'var(--ok)'}
function fmt(v){return Number(v).toFixed(3)}
async function load(){
  try{
    const BASE = location.pathname.startsWith('/bms') ? '/bms' : '';
    const r=await fetch(BASE + '/api/data');const j=await r.json();
    document.getElementById('dot').className='dot'+(j.online?' on':'');
    document.getElementById('tv').textContent=j.online?fmt(j.total):'--';
    document.getElementById('bd').textContent=j.online?Math.round(j.bal*1000):'--';
    document.getElementById('mn').textContent=j.online?fmt(j.min):'--';
    document.getElementById('mx').textContent=j.online?fmt(j.max):'--';
    document.getElementById('rssi').textContent=j.rssi;
    const c=document.getElementById('cells');c.innerHTML='';
    (j.cells||[]).forEach((v,i)=>{
      const d=Math.round((j.max-v)*1000);
      const el=document.createElement('div');el.className='cell';
      el.innerHTML='<div class="t">电芯 '+(i+1)+'</div><div class="n">'+fmt(v)+' V</div>'+
        '<div class="track"><div class="fill" style="width:'+Math.min(100,v/4.2*100)+'%;background:'+color(d)+'"></div></div>';
      c.appendChild(el);
    });
  }catch(e){}
}
load();setInterval(load,2000);
</script>
</body>
</html>
'''

@app.route("/")
@app.route("/bms/")
def index():
    # 优先返回独立看板文件（父亲友好版），缺失时回退到内置 HTML 常量
    try:
        with open(os.path.join(DATA_DIR, "dashboard.html")) as f:
            return f.read()
    except Exception:
        return HTML

@app.route("/api/data")
@app.route("/bms/api/data")
def data():
    with lock:
        return json.dumps(state)

@app.route("/api/settings")
@app.route("/bms/api/settings")
def api_settings():
    # BMS 当前设置(破解读取，只读展示)；写回控制见 Phase 2
    with settings_lock:
        return json.dumps(settings, ensure_ascii=False)

@app.route("/api/set", methods=["POST"])
@app.route("/bms/api/set", methods=["POST"])
def api_set():
    # 写 BMS 设置(Phase 2): 仅允许名单(均衡开关 / 均衡触发压差)
    # 转发到 MQTT 主题 jk-bms/set, 由 ESP32 写入 BMS; 实际生效以 /api/settings 读回为准
    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        body = {}
    action = body.get("action")
    if action == "balance":
        val = 1 if body.get("value") else 0
        payload, reg_name = "31,%d" % val, "均衡开关"
    elif action == "balance_trigger":
        try:
            mv = int(body.get("value"))
        except Exception:
            return json.dumps({"error": "阈值需为整数 mV(1-500)"}), 400
        if mv < 1 or mv > 500:
            return json.dumps({"error": "阈值超出范围(1-500 mV)"}), 400
        payload, reg_name = "14,%d" % mv, "均衡触发压差"
    else:
        return json.dumps({"error": "未知操作(仅支持 balance / balance_trigger)"}), 400
    try:
        info = mqttc.publish("jk-bms/set", payload, False)
        if info.rc != 0:
            return json.dumps({"error": "MQTT 发布失败 rc=%d" % info.rc}), 500
    except Exception as e:
        return json.dumps({"error": "MQTT 发布异常: %s" % e}), 500
    return json.dumps({"queued": True, "payload": payload, "reg": reg_name})

@app.route("/api/summary")
@app.route("/bms/api/summary")
def summary():
    # 给 agent 用的结构化 + 中文总结接口，直接读 recorder 落盘文件
    latest_path = os.path.join(DATA_DIR, "latest.json")
    hist_path   = os.path.join(DATA_DIR, "history.jsonl")
    out = {"online": False, "updated_at": None, "age_seconds": None,
           "pack": {}, "cells": [], "cell_stats": {}, "rssi": 0,
           "cell_res": [], "cell_res_raw": [], "cell_res_alert": 0, "res_stats": {},
           "trend": {}, "rate": {}, "balance": {}, "summary_text": "暂无电池数据"}
    if not os.path.exists(latest_path):
        return json.dumps(out, ensure_ascii=False)
    try:
        with open(latest_path) as f:
            d = json.load(f)
    except Exception:
        return json.dumps(out, ensure_ascii=False)

    now = datetime.datetime.now().timestamp()
    ts  = d.get("ts") or now
    age = int(now - ts)
    out["online"]     = bool(d.get("online", True))
    out["updated_at"] = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    out["age_seconds"] = age
    out["rssi"] = d.get("rssi", 0)
    out["fw"] = d.get("fw")          # 设备固件版本（OTA 验证用）

    pack = {}
    for k in ("voltage","current","power","soc","remaining_ah","total_ah","temp1","temp2"):
        if k in d: pack[k] = d[k]
    out["pack"] = pack

    cells = d.get("cells") or []
    out["cells"] = cells
    if cells:
        cmin, cmax = min(cells), max(cells)
        out["cell_stats"] = {"min_v": round(cmin,4), "max_v": round(cmax,4),
                             "delta_mv": round((cmax-cmin)*1000,1),
                             "mean_v": round(sum(cells)/len(cells),4)}

    # ===== 均衡线电阻: 原始整数 → Ω(换算系数在网关侧, 见 RES_OHM_SCALE) =====
    # 注意: 这是"均衡线电阻"(采样/均衡线 + 端子接触电阻), 不是电芯内阻。
    # 未与 JK App 逐节对表前绝对量纲存疑 → 前端只用相对判据(见 dashboard.html)。
    raw_res = d.get("cell_res_raw") or []
    out["cell_res_raw"] = raw_res
    out["cell_res_alert"] = int(d.get("cell_res_alert") or 0)
    ohm = [round(v * RES_OHM_SCALE, 4) for v in raw_res if isinstance(v, (int, float))]
    out["cell_res"] = ohm
    if len(ohm) >= 2:
        srt = sorted(ohm)
        out["res_stats"] = {
            "min_ohm": round(min(ohm), 4), "max_ohm": round(max(ohm), 4),
            "mean_ohm": round(sum(ohm)/len(ohm), 4),
            "median_ohm": round(srt[len(srt)//2], 4),
            "max_cell": ohm.index(max(ohm)) + 1, "min_cell": ohm.index(min(ohm)) + 1,
            "scale": RES_OHM_SCALE,
        }

    # 趋势：只读 history.jsonl 末尾若干字节(窗口见 HIST_TREND_BYTES)，避免整文件载入
    trend = {}
    samples = []
    try:
        with open(hist_path, "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size-HIST_TREND_BYTES))
            tail = f.read().decode(errors="ignore")
        # 逐行解析并跳过坏行: seek 到字节偏移会切在行中间, 第一行必然是半截 JSON。
        # 原来写成列表推导式, 一行坏就整段抛异常被 except 吞掉 → trend 永远是空(既有 bug)。
        samples = []
        for x in tail.splitlines():
            if not x.strip():
                continue
            try:
                samples.append(json.loads(x))
            except Exception:
                continue
        if samples:
            trend["samples_recent"] = len(samples)
            t0 = samples[0].get("ts")
            if t0: trend["window_seconds"] = int(now - t0)
            cut = now - 600
            older = [s for s in samples if (s.get("ts") or 0) <= cut]
            if older:
                o = older[-1]
                if "voltage" in o: trend["voltage_10min_ago"] = o["voltage"]
                if "soc" in o:     trend["soc_10min_ago"]     = o["soc"]
    except Exception:
        pass
    out["trend"] = trend

    # ===== 充放电速率与「充满 / 放完」预测 =====
    # 用最近 RATE_WINDOW_S 秒的电流均值(比瞬时值稳得多)，再按剩余/待充电量推算时间。
    # 约定: 电流为正是充电、为负是放电(已用水壶负载实验确认)。
    rate = {"mode": "idle", "avg_a": 0.0, "window_s": 0, "eta_h": None, "eta_at": None}
    try:
        cut_r = now - RATE_WINDOW_S
        w = [s for s in samples if (s.get("ts") or 0) >= cut_r]
        cur = [s.get("current") for s in w if s.get("current") is not None]
        if len(cur) >= 5:
            avg = sum(cur) / len(cur)
            tot = d.get("total_ah") or 0.0
            rem = d.get("remaining_ah") or 0.0
            rate["avg_a"] = round(avg, 2)
            rate["window_s"] = int(now - (w[0].get("ts") or now))
            if avg > 0.3 and tot > 0:                 # 充电中
                rate["mode"] = "charging"
                rate["eta_h"] = round(max(0.0, tot - rem) / avg, 2)
            elif avg < -0.3 and rem > 0:              # 放电中
                rate["mode"] = "discharging"
                rate["eta_h"] = round(rem / abs(avg), 2)
            if rate["eta_h"] is not None:
                rate["eta_at"] = datetime.datetime.fromtimestamp(
                    now + rate["eta_h"] * 3600).strftime("%H:%M")
    except Exception:
        pass
    out["rate"] = rate

    # ===== 电量收支（读 balance_watch.py 每小时写的 CSV 最后一行）=====
    # 用途：一眼看出这组电池是在充还是在亏（亏 = 光伏配小了 / 负载重了，不是电池坏）
    bal = {}
    try:
        bp = os.path.join(DATA_DIR, "balance", "balance-%s.csv" % datetime.datetime.now().strftime("%Y%m%d"))
        with open(bp, encoding="utf-8") as f:
            lines = [x for x in f.read().splitlines() if x.strip()]
        if len(lines) >= 2:
            v = lines[-1].split(",")
            if len(v) >= 11:
                bal = {"at": v[0],
                       "today_chg_ah": float(v[1]), "today_dis_ah": float(v[2]), "today_net_ah": float(v[3]),
                       "h24_chg_ah": float(v[4]), "h24_dis_ah": float(v[5]), "h24_net_ah": float(v[6]),
                       "warn": v[10]}
    except Exception:
        pass
    out["balance"] = bal

    # 中文一句话总结（agent 可直接复述给用户）
    if not out["online"]:
        out["summary_text"] = "电池当前离线（最后更新 %s，距今 %d 秒）" % (out["updated_at"], age)
    else:
        parts = ["电池在线，最后更新于 %s（%d 秒前）" % (out["updated_at"], age)]
        if "voltage" in pack: parts.append("总电压 %.2fV" % pack["voltage"])
        if "current" in pack:
            ci = pack["current"]
            parts.append(("充电 %.2fA" % ci) if ci > 0.01 else (("放电 %.2fA" % -ci) if ci < -0.01 else "静置 0A"))
        if "soc" in pack: parts.append("SOC %d%%" % pack["soc"])
        if "remaining_ah" in pack: parts.append("剩余 %.1fAh" % pack["remaining_ah"])
        if cells:
            cs = out["cell_stats"]
            parts.append("%d芯电压 %.3f~%.3fV，压差 %.0fmV" % (len(cells), cs["min_v"], cs["max_v"], cs["delta_mv"]))
            parts.append("均衡%s" % ("良好" if cs["delta_mv"] < 20 else ("一般" if cs["delta_mv"] < 50 else "偏大需关注")))
        if "temp1" in pack:
            if pack["temp1"] > -100: parts.append("温度1 %.1f℃" % pack["temp1"])
            else: parts.append("温度1 断线")
        if "temp2" in pack:
            if pack["temp2"] > -100: parts.append("温度2 %.1f℃" % pack["temp2"])
            else: parts.append("温度2 断线")
        out["summary_text"] = "，".join(parts) + "。"

    return json.dumps(out, ensure_ascii=False)

@app.route("/api/history")
@app.route("/bms/api/history")
def history():
    # 用电波形时序接口：day=最近24h功率曲线(降采样) + 今日电量；week/month=每日聚合(日电量+日均功率)
    rng = (request.args.get("range") or "day").lower()
    now = time.time()
    out = {"range": rng, "unit": "point", "series": [], "today_energy_kwh": None}

    def _p(d):
        p = d.get("power")
        if p is None:
            v = d.get("voltage"); c = d.get("current")
            if v is not None and c is not None: p = v * c
        return p

    if rng == "day":
        # 今日 00:00–24:00 按小时聚合，供前端画「以 0 线为基准的发散柱状图」。
        # 每小时返回净电量 energy_kwh：正=放电（柱向上）、负=充电（柱向下）。
        # 方向以 V*I 的符号判定（JK 约定：电流 I>0 为充电、I<0 为放电），
        # 比直接用上报的 power 字段更可靠（个别固件 power 符号与电流不一致）。
        lt = time.localtime(now)
        today0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        buckets = [{"e_wh": 0.0, "psum": 0.0, "n": 0, "pmax": 0.0, "soc": None}
                   for _ in range(24)]
        peak_delta = 0.0; peak_delta_cell = None
        prev_ts = None; prev_p = None
        try:
            with open(HISTORY, "rb") as f:
                f.seek(0, 2); size = f.tell()
                f.seek(max(0, size - HIST_DAY_BYTES))   # 读末尾最多 16MB（覆盖今日全部@5s；行变长后仍够一整天）
                tail = f.read().decode(errors="ignore")
            for line in tail.splitlines():
                if not line.strip():
                    continue
                try:
                    s = json.loads(line)
                except Exception:
                    continue
                ts = s.get("ts")
                if ts is None:
                    continue
                v = s.get("voltage"); c = s.get("current")
                p = (v * c) if (v is not None and c is not None) else _p(s)
                if ts < today0 or ts > now:            # 今天之前的采样只用于给首个点做积分
                    prev_ts, prev_p = ts, p
                    continue
                h = time.localtime(ts).tm_hour
                b = buckets[h]
                if p is not None:
                    b["psum"] += p; b["n"] += 1
                    b["pmax"] = max(b["pmax"], abs(p))
                    # 梯形积分（Wh）：仅对合理间隔积分，跳过大间隔（重启/掉线）
                    if prev_ts is not None and prev_p is not None:
                        dt = ts - prev_ts
                        if 0 < dt <= 3600:
                            b["e_wh"] += (prev_p + p) / 2.0 * dt / 3600.0
                sc = s.get("soc")
                if sc is not None:
                    b["soc"] = sc
                # 峰值压差(均衡用)：只统计今天
                cc = s.get("cells")
                if cc and len(cc) >= 2:
                    dm = max(cc) - min(cc)
                    if dm > peak_delta:
                        peak_delta = dm; peak_delta_cell = cc.index(max(cc)) + 1
                prev_ts, prev_p = ts, p
        except Exception:
            pass

        series = []
        for h in range(24):
            b = buckets[h]
            series.append({
                "h": h,
                "t": int((today0 + h * 3600) * 1000),
                "energy_kwh": round(-b["e_wh"] / 1000.0, 3),   # 正=放电、负=充电
                "power_avg": round(b["psum"] / b["n"], 1) if b["n"] else None,
                "power_max": round(b["pmax"], 1) if b["n"] else None,
                "soc": b["soc"],
            })
        out["unit"] = "hour"
        out["series"] = series

        dis = sum(x["energy_kwh"] for x in series if x["energy_kwh"] > 0)
        chg = sum(-x["energy_kwh"] for x in series if x["energy_kwh"] < 0)
        out["discharge_kwh"] = round(dis, 3)
        out["charge_kwh"] = round(chg, 3)
        out["net_kwh"] = round(dis - chg, 3)
        out["today_energy_kwh"] = round(dis, 3)            # 兼容旧字段：今日放电量
        pd_ = max(series, key=lambda x: x["energy_kwh"])
        pc_ = min(series, key=lambda x: x["energy_kwh"])
        if pd_["energy_kwh"] > 0:
            out["peak_discharge_kwh"] = pd_["energy_kwh"]
            out["peak_discharge_hour"] = pd_["h"]
        if pc_["energy_kwh"] < 0:
            out["peak_charge_kwh"] = round(-pc_["energy_kwh"], 3)
            out["peak_charge_hour"] = pc_["h"]
        if peak_delta > 0:
            out["peak_cell_delta_mv"] = round(peak_delta * 1000, 1)
            out["peak_cell_delta_cell"] = peak_delta_cell
    else:
        days = 7 if rng == "week" else 30
        out["unit"] = "day"
        daily = {}
        try:
            with open(AGG) as f:
                daily = {int(k): v for k, v in json.load(f).get("daily", {}).items()}
        except Exception:
            pass
        keys = sorted([k for k in daily if k <= now + 86400])[-days:]
        series = []
        for k in keys:
            b = daily[k]
            avg = (b["sum"] / b["n"]) if b.get("n") else 0.0
            item = {"t": int(k * 1000),
                    "power": round(avg, 1),
                    "energy_kwh": round(abs(b.get("wh", 0)) / 1000.0, 3)}
            if b.get("cell_dmax") is not None:          # 该日峰值压差 + 异常电芯(周总会用)
                item["cell_delta_max_mv"] = round(abs(b["cell_dmax"]) * 1000, 1)
                item["cell_delta_max_cell"] = b.get("cell_dmax_cell")
            series.append(item)
        out["series"] = series

    # 今日用电量（来自聚合日桶）；day 分支已按小时精算过，不覆盖
    if out.get("today_energy_kwh") is None:
        try:
            with open(AGG) as f:
                agg = json.load(f)
            lt = time.localtime(now)
            today_key = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
            ad = {int(k): v for k, v in agg.get("daily", {}).items()}
            if today_key in ad:
                out["today_energy_kwh"] = round(abs(ad[today_key].get("wh", 0)) / 1000.0, 3)
        except Exception:
            pass

    return json.dumps(out, ensure_ascii=False)

# ===== OTA 固件分发（给 ESP32 自动升级用）=====
# 设备开机 90 秒后、以及之后每 6 小时，GET /fw/version 比对版本号；
# 与自身 FW_VERSION 不同就 GET /fw/firmware.bin 下载并自刷（自动重启）。
OTA_DIR = os.path.join(DATA_DIR, "fw")

@app.route("/fw/version")
@app.route("/bms/fw/version")
def fw_version():
    try:
        with open(os.path.join(OTA_DIR, "version")) as f:
            return f.read().strip() or "none"
    except Exception:
        return "none"

@app.route("/fw/firmware.bin")
@app.route("/bms/fw/firmware.bin")
def fw_bin():
    p = os.path.join(OTA_DIR, "firmware.bin")
    if not os.path.exists(p):
        return "no firmware", 404
    return send_file(p, mimetype="application/octet-stream")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8899)
