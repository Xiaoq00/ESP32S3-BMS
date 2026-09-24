#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JK-BMS 24 小时综合分析（一次性脚本，不参与定时任务）"""
import json, os, time, datetime, collections, statistics

HIST = "/opt/jk-bms/history.jsonl"
LATEST = "/opt/jk-bms/latest.json"
NOW = time.time()
T0 = NOW - 24*3600

# ---------- 读数据 ----------
size = os.path.getsize(HIST)
with open(HIST, "rb") as f:
    f.seek(max(0, size - 60*1024*1024))
    raw = f.read().decode(errors="ignore")
rows = []
for line in raw.splitlines():
    try: d = json.loads(line)
    except Exception: continue
    ts = d.get("ts")
    if not ts or ts < T0: continue
    if "cells" not in d: continue          # 只统计真遥测
    rows.append(d)
rows.sort(key=lambda d: d["ts"])
print("=" * 74)
print("JK-BMS 24 小时综合分析   (%s ~ %s)" % (
    datetime.datetime.fromtimestamp(T0).strftime("%m-%d %H:%M"),
    datetime.datetime.fromtimestamp(NOW).strftime("%m-%d %H:%M")))
print("=" * 74)

# ---------- 1. 数据覆盖 ----------
print("\n【1】数据覆盖")
if not rows:
    print("  无数据"); raise SystemExit
span = rows[-1]["ts"] - rows[0]["ts"]
print("  样本 %d 条，实际覆盖 %.1f 小时（理想 %.1f）" % (len(rows), span/3600, 24.0))
gaps = []
for a, b in zip(rows, rows[1:]):
    dt = b["ts"] - a["ts"]
    if dt > 30: gaps.append((a["ts"], b["ts"], dt))
tot_gap = sum(g[2] for g in gaps)
print("  断档 %d 处，累计 %.1f 分钟（占 %.1f%%）" % (len(gaps), tot_gap/60, tot_gap/(24*3600)*100))
for s, e, dt in sorted(gaps, key=lambda g: -g[2])[:8]:
    print("    %s → %s   %.1f 分钟" % (
        datetime.datetime.fromtimestamp(s).strftime("%H:%M:%S"),
        datetime.datetime.fromtimestamp(e).strftime("%H:%M:%S"), dt/60))

# ---------- 2. 能量账 ----------
print("\n【2】能量账（充/放电）")
chg = dis = 0.0
chg_wh = dis_wh = 0.0
peak_c = peak_d = 0.0; peak_c_t = peak_d_t = 0
for a, b in zip(rows, rows[1:]):
    dt = b["ts"] - a["ts"]
    if not (0 < dt <= 30): continue
    ca, cb = a.get("current") or 0, b.get("current") or 0
    va, vb = a.get("voltage") or 0, b.get("voltage") or 0
    vavg = (va+vb)/2
    if ca >= 0 and cb >= 0:
        ah = (ca+cb)/2*dt/3600; chg += ah; chg_wh += ah*vavg
    elif ca <= 0 and cb <= 0:
        ah = -(ca+cb)/2*dt/3600; dis += ah; dis_wh += ah*vavg
    for c, t in ((ca, a["ts"]), (cb, b["ts"])):
        if c > peak_c: peak_c, peak_c_t = c, t
        if c < peak_d: peak_d, peak_d_t = c, t
print("  累计充入 %.2f Ah (%.2f kWh)" % (chg, chg_wh/1000))
print("  累计放出 %.2f Ah (%.2f kWh)" % (dis, dis_wh/1000))
print("  净收支 %+.2f Ah  →  %s" % (chg-dis, "在充（电量增加）" if chg>dis else "在亏（电量减少）"))
print("  峰值充电 %+.2f A (%s)" % (peak_c, datetime.datetime.fromtimestamp(peak_c_t).strftime("%H:%M")))
print("  峰值放电 %+.2f A (%s)" % (peak_d, datetime.datetime.fromtimestamp(peak_d_t).strftime("%H:%M")))

# ---------- 3. 电压与压差 ----------
print("\n【3】电压与压差")
vt = [d["voltage"] for d in rows if d.get("voltage")]
print("  总压 %.3f ~ %.3f V（均值 %.3f）" % (min(vt), max(vt), sum(vt)/len(vt)))
deltas = []
runner = collections.Counter()
allmin = [9]*20; allmax = [0]*20
for d in rows:
    c = d["cells"]
    if len(c) < 20: continue
    deltas.append((d["ts"], (max(c)-min(c))*1000, c.index(max(c))+1, max(c), min(c)))
    runner[c.index(max(c))+1] += 1
    for i in range(20):
        allmin[i] = min(allmin[i], c[i]); allmax[i] = max(allmax[i], c[i])
dv = [x[1] for x in deltas]
print("  压差 最小 %.0f / 中位 %.0f / 均值 %.0f / 最大 %.0f mV" % (
    min(dv), statistics.median(dv), sum(dv)/len(dv), max(dv)))
print("  压差最大的 5 个时刻：")
for ts, d, cell, mx, mn in sorted(deltas, key=lambda x: -x[1])[:5]:
    print("    %s  压差 %5.0f mV  最高第%d节 %.3f  最低 %.3f" % (
        datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S"), d, cell, mx, mn))
print("  「最高节」出现次数统计（谁是常客）：")
for cell, n in runner.most_common(6):
    print("    第%2d节: %4d 次 (%.0f%%)" % (cell, n, n/len(deltas)*100))
print("  各节电压区间（24h 内）：")
for i in range(20):
    flag = " ★" if (i+1) in [c for c,_ in runner.most_common(3)] else ""
    print("    第%2d节 %.3f ~ %.3f V%s" % (i+1, allmin[i], allmax[i], flag))

# ---------- 4. 线阻 ----------
print("\n【4】均衡线电阻（BMS 自测）")
res = [(d["ts"], d.get("cell_res_raw")) for d in rows if d.get("cell_res_raw")]
if res:
    print("  有记录的样本 %d 条" % len(res))
    first, last = res[0][1], res[-1][1]
    print("  最早(%s): %d~%d mΩ 中位 %d" % (
        datetime.datetime.fromtimestamp(res[0][0]).strftime("%m-%d %H:%M"),
        min(first), max(first), sorted(first)[len(first)//2]))
    print("  最新(%s): %d~%d mΩ 中位 %d" % (
        datetime.datetime.fromtimestamp(res[-1][0]).strftime("%m-%d %H:%M"),
        min(last), max(last), sorted(last)[len(last)//2]))
    # 逐节 24h 均值
    avg = [0.0]*20
    for _, r in res:
        for i in range(min(20, len(r))): avg[i] += r[i]
    avg = [a/len(res) for a in avg]
    print("  逐节 24h 均值（mΩ，升序）：")
    for i in sorted(range(20), key=lambda k: -avg[k])[:8]:
        print("    第%2d节 %6.0f mΩ" % (i+1, avg[i]))
    print("  （社区正常带 45~75 mΩ；本组全部 380~510，是 5~8 倍）")
    alert = [d.get("cell_res_alert") for d in rows if d.get("cell_res_alert")]
    if alert: print("  BMS 报警掩码取值集合: %s" % set(alert))

# ---------- 5. 温度 ----------
print("\n【5】温度")
t2 = [d.get("temp2") for d in rows if d.get("temp2") is not None and d["temp2"] > -100]
t1 = [d.get("temp1") for d in rows if d.get("temp1") is not None and d["temp1"] > -100]
if t2: print("  温度2(电芯/环境) %.1f ~ %.1f ℃（均值 %.1f）" % (min(t2), max(t2), sum(t2)/len(t2)))
else:  print("  温度2: 无有效数据（探头断线）")
print("  温度1: %s" % ("%.1f ~ %.1f ℃" % (min(t1), max(t1)) if t1 else "断线（哨兵 -200）"))

# ---------- 6. SOC ----------
print("\n【6】SOC / 容量")
socs = [(d["ts"], d.get("soc"), d.get("remaining_ah"), d.get("total_ah")) for d in rows if d.get("soc") is not None]
if socs:
    print("  SOC %.0f%% ~ %.0f%%（首 %s%% → 末 %s%%）" % (
        min(s[1] for s in socs), max(s[1] for s in socs), socs[0][1], socs[-1][1]))
    print("  剩余Ah %.1f ~ %.1f（首 %.1f → 末 %.1f）" % (
        min(s[2] for s in socs if s[2] is not None), max(s[2] for s in socs if s[2] is not None),
        socs[0][2] or 0, socs[-1][2] or 0))
    print("  上报总容量集合: %s" % set(round(s[3],1) for s in socs if s[3]))

# ---------- 7. 逐小时 ----------
print("\n【7】逐小时明细")
print("  时间    充Ah   放Ah   净Ah   压差max  压差avg  最高节  SOC  电流avg")
hb = collections.defaultdict(lambda: {"c":0,"d":0,"dmax":0,"dsum":0,"n":0,"cell":0,"cur":[],"soc":None})
for d in rows:
    k = int(d["ts"]//3600)*3600
    e = hb[k]; c = d["cells"]
    e["cur"].append(d.get("current") or 0)
    if d.get("soc") is not None: e["soc"] = d["soc"]
    if len(c) >= 2:
        dv2 = (max(c)-min(c))*1000
        if dv2 >= e["dmax"]: e["dmax"] = dv2; e["cell"] = c.index(max(c))+1
        e["dsum"] += dv2; e["n"] += 1
for a, b in zip(rows, rows[1:]):
    dt = b["ts"]-a["ts"]
    if not (0 < dt <= 30): continue
    k = int(a["ts"]//3600)*3600
    ca, cb = a.get("current") or 0, b.get("current") or 0
    if ca >= 0 and cb >= 0: hb[k]["c"] += (ca+cb)/2*dt/3600
    elif ca <= 0 and cb <= 0: hb[k]["d"] += -(ca+cb)/2*dt/3600
for k in sorted(hb):
    e = hb[k]
    cavg = sum(e["cur"])/len(e["cur"]) if e["cur"] else 0
    print("  %s %6.2f %6.2f %+6.2f %7.0f %8.0f %6s %5s %+7.2f" % (
        datetime.datetime.fromtimestamp(k).strftime("%m-%d %H:%M"),
        e["c"], e["d"], e["c"]-e["d"], e["dmax"],
        e["dsum"]/e["n"] if e["n"] else 0, e["cell"] or "-", e["soc"], cavg))
