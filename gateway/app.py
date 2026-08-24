#!/usr/bin/env python3
# RK3566 看板：订阅 MQTT(jk-bms/state JSON) → 提供移动端网页
# 手机通过 Tailscale Funnel 的 /bms 路径(同一网址)即可看电量；局域网也可直连 8899
# 运行：pip3 install -r requirements.txt && python3 app.py
import json, threading, os, time, datetime
import paho.mqtt.client as mqtt
from flask import Flask, request

BROKER = "127.0.0.1"   # 若 Mosquitto 在容器/其他地址请改
PORT   = 1883
TOPIC  = "jk-bms/state"
SETTINGS_TOPIC = "jk-bms/settings"   # ESP32 破解读取的 BMS 设置帧(0x01)结构化 JSON
DATA_DIR = "/opt/jk-bms"   # recorder 落盘目录(latest.json / history.jsonl / agg.json)
HISTORY  = os.path.join(DATA_DIR, "history.jsonl")
AGG      = os.path.join(DATA_DIR, "agg.json")

state = {"cells": [], "total": 0.0, "min": 0.0, "max": 0.0, "bal": 0.0, "rssi": 0, "online": False}
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
            for k in ("cells", "total", "min", "max", "bal", "rssi"):
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
           "trend": {}, "summary_text": "暂无电池数据"}
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

    # 趋势：只读 history.jsonl 末尾 ~64KB，避免整文件载入
    trend = {}
    try:
        with open(hist_path, "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size-65536))
            tail = f.read().decode(errors="ignore")
        samples = [json.loads(x) for x in tail.splitlines() if x.strip()]
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
        pts = []
        peak_delta = 0.0; peak_delta_cell = None
        try:
            with open(HISTORY, "rb") as f:
                f.seek(0, 2); size = f.tell()
                f.seek(max(0, size - 6 * 1024 * 1024))   # 读末尾最多 6MB（覆盖 24h@5s）
                tail = f.read().decode(errors="ignore")
            for line in tail.splitlines():
                if not line.strip():
                    continue
                try:
                    s = json.loads(line)
                except Exception:
                    continue
                ts = s.get("ts"); p = _p(s)
                if ts is None or p is None:
                    continue
                # 峰值压差(均衡用): 用 cells 数组直接算极值，记下最高的那节
                cc = s.get("cells")
                if cc and len(cc) >= 2:
                    dm = max(cc) - min(cc)
                    if dm > peak_delta:
                        peak_delta = dm; peak_delta_cell = cc.index(max(cc)) + 1
                if ts >= now - 86400:
                    pts.append((ts, p))
        except Exception:
            pass
        pts.sort()
        if len(pts) > 240:                              # 降采样到 <=240 点，手机图表更顺
            step = len(pts) / 240.0
            pts = [pts[int(i * step)] for i in range(240)]
        out["series"] = [{"t": int(ts * 1000), "power": round(p, 1), "energy_kwh": None}
                         for ts, p in pts]
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

    # 今日用电量（来自聚合日桶）
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8899)
