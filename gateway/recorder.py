#!/usr/bin/env python3
# JK-BMS MQTT Recorder
# 订阅 jk-bms/state -> 写 latest.json(当前快照) + history.jsonl(滚动历史) + agg.json(小时/日聚合桶)
# app.py 的 /api/summary 与 /api/history 直接读这些文件，互不干扰。
import json, time, os, threading
import paho.mqtt.client as mqtt

BROKER = "127.0.0.1"
PORT   = 1883
TOPIC  = "jk-bms/state"
DATA_DIR  = "/opt/jk-bms"
LATEST   = os.path.join(DATA_DIR, "latest.json")
HISTORY  = os.path.join(DATA_DIR, "history.jsonl")
AGG      = os.path.join(DATA_DIR, "agg.json")
MAX_HISTORY = 40000          # history.jsonl 超过此行数则保留末尾(~55h 明细，配合 agg 存峰值压差做周总会)
STALE_SEC    = 90            # 超过此秒数无新消息 -> 标记 online=False

def _power(d):
    p = d.get("power")
    if p is None:
        v = d.get("voltage"); c = d.get("current")
        if v is not None and c is not None: p = v * c
    return p

def _cell_delta(d):
    # 返回 (压差V, 最高节号1-based)；算"均衡压差"用。cell_max/cell_min 是 3 位四舍五入值，
    # 可能与 cells 全精度对不上，故优先用 cells 数组直接算极值。
    c = d.get("cells")
    if c and len(c) >= 2:
        mx = max(c); mn = min(c)
        return (mx - mn, c.index(mx) + 1)
    cmax = d.get("cell_max"); cmin = d.get("cell_min")
    if cmax is not None and cmin is not None:
        return (cmax - cmin, None)
    return (None, None)

def _bucket_keys(ts):
    lt = time.localtime(ts)
    hour_key = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, 0, 0, 0, 0, -1)))
    day_key  = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    return hour_key, day_key

def load_agg():
    # JSON 对象 key 必为字符串，统一转回 int 时间戳再使用
    try:
        with open(AGG) as f:
            a = json.load(f)
        a["hourly"] = {int(k): v for k, v in a.get("hourly", {}).items()}
        a["daily"]  = {int(k): v for k, v in a.get("daily", {}).items()}
        return a
    except Exception:
        return {"_last": None, "hourly": {}, "daily": {}}

def save_agg(agg):
    tmp = AGG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(agg, f, ensure_ascii=False)
    os.replace(tmp, AGG)   # 原子替换，避免读取方读到半截

def update_agg(d):
    ts = d.get("ts")
    power = _power(d)
    if ts is None or power is None:
        return
    cdelta, cmax_cell = _cell_delta(d)   # 压差(均衡用)；可能为 None(无 cells 字段)
    try:
        agg = load_agg()
        hour_key, day_key = _bucket_keys(ts)
        prev = agg.get("_last")
        e_wh = 0.0
        if isinstance(prev, dict) and prev.get("ts") is not None:
            dt = ts - prev["ts"]
            if 0 < dt <= 3600:                 # 仅对合理间隔做能量积分，跳过大间隔(重启/掉线)
                pp = prev.get("power")
                if pp is not None:
                    e_wh = (pp + power) / 2.0 * dt / 3600.0   # 梯形积分 Wh
        for key, store in ((hour_key, "hourly"), (day_key, "daily")):
            b = agg[store].setdefault(key, {"wh": 0.0, "sum": 0.0, "n": 0, "min": power, "max": power})
            b["wh"]  += e_wh
            b["sum"] += power
            b["n"]   += 1
            b["min"]  = min(b["min"], power)
            b["max"]  = max(b["max"], power)
            # 峰值压差：桶内保留出现过的最大电芯压差 + 当时最高的那节(用于周总会定位异常电芯)
            if cdelta is not None:
                if "cell_dmax" not in b or cdelta > b["cell_dmax"]:
                    b["cell_dmax"] = cdelta
                    b["cell_dmax_cell"] = cmax_cell
        agg["_last"] = {"ts": ts, "power": power}
        now = time.time()
        agg["hourly"] = {k: v for k, v in agg["hourly"].items() if k >= now - 48 * 3600}
        agg["daily"]  = {k: v for k, v in agg["daily"].items()  if k >= now - 400 * 86400}
        save_agg(agg)
    except Exception as e:
        print("agg err:", e)

def on_connect(cli, userdata, flags, rc):
    cli.subscribe(TOPIC)

def on_message(cli, userdata, msg):
    try:
        d = json.loads(msg.payload)
    except Exception as e:
        print("parse err:", e)
        return
    d["ts"] = time.time()
    # 兼容 app.py 旧字段别名，保证看板/summary 都能用
    if "total" in d and "voltage" not in d: d["voltage"] = d["total"]
    if "min"   in d and "cell_min" not in d: d["cell_min"] = d["min"]
    if "max"   in d and "cell_max" not in d: d["cell_max"] = d["max"]
    try:
        with open(LATEST, "w") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("write latest err:", e)
    try:
        with open(HISTORY, "a") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
        trim_history()
    except Exception as e:
        print("write history err:", e)
    update_agg(d)

def trim_history():
    try:
        with open(HISTORY, "r") as f:
            lines = f.readlines()
        if len(lines) > MAX_HISTORY:
            with open(HISTORY, "w") as f:
                f.writelines(lines[-MAX_HISTORY:])
    except Exception:
        pass

def watchdog():
    # 若长时间无新消息，把 latest.json 的 online 标 False，供看板/agent 判断离线
    while True:
        time.sleep(30)
        try:
            if os.path.exists(LATEST):
                with open(LATEST) as f:
                    d = json.load(f)
                if d.get("online", True) and (time.time() - d.get("ts", 0)) > STALE_SEC:
                    d["online"] = False
                    with open(LATEST, "w") as f:
                        json.dump(d, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

mqttc = mqtt.Client()
mqttc.on_connect = on_connect
mqttc.on_message = on_message
threading.Thread(target=watchdog, daemon=True).start()

while True:
    try:
        mqttc.connect(BROKER, PORT, 60)
        mqttc.loop_forever()
    except Exception as e:
        print("MQTT err, retry 5s:", e)
        time.sleep(5)
