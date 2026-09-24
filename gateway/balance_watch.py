#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JK-BMS 电量收支观测哨（每小时一行）
================================================================
回答一个问题：**这组电池到底是在充还是在亏？亏多少？**

数据源：recorder 写的 history.jsonl（5 秒级原始遥测）。
        只读，不碰 MQTT / 看板 / 其它脚本的任何文件。

设计要点（与 cell_watch.py / resist_watch.py 一致）：
  1. 按天分文件：/opt/jk-bms/balance/balance-YYYYMMDD.csv，写坏最多丢当天
  2. 每次写完 fsync() —— 断电不丢已写入的行
  3. 幂等：同一小时只写一行
  4. 同时给"今日"和"近 24 小时"两个口径（今日看当天，24h 看滚动趋势）
  5. 收支为负就写告警 —— 连续几天为负 = 光伏配小了，不是电池坏了

用法：
  python3 balance_watch.py            # 记录当前快照（供 systemd timer 每小时调用）
  python3 balance_watch.py --status   # 只打印，不写文件
"""
import json, os, sys, time, datetime, argparse, statistics

DATA   = "/opt/jk-bms"
HIST   = os.path.join(DATA, "history.jsonl")
OUTDIR = os.path.join(DATA, "balance")

# 收支为负超过这个值就告警（Ah/24h）
WARN_DEFICIT_AH = 5.0


def load(seconds):
    """读 history.jsonl 末尾若干字节，返回 (ts, current, voltage, soc, cells) 列表"""
    size = os.path.getsize(HIST)
    cut = time.time() - seconds
    # 5 秒一条，多读一些留余量
    nbytes = int(seconds / 5 * 620) + 2 * 1024 * 1024
    with open(HIST, "rb") as f:
        f.seek(max(0, size - nbytes))
        raw = f.read().decode(errors="ignore")
    rows = []
    for line in raw.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts = d.get("ts")
        if not ts or ts < cut:
            continue
        if "cells" not in d:
            continue
        rows.append(d)
    rows.sort(key=lambda d: d["ts"])
    return rows


def integrate(rows):
    """按符号积分：返回 (充入Ah, 放出Ah, 压差max, 最高节, SOC首, SOC末)"""
    chg = dis = 0.0
    dmax = 0.0; dmax_cell = 0
    soc_first = soc_last = None
    for a, b in zip(rows, rows[1:]):
        dt = b["ts"] - a["ts"]
        if not (0 < dt <= 30):
            continue
        ca, cb = a.get("current") or 0, b.get("current") or 0
        if ca >= 0 and cb >= 0:
            chg += (ca + cb) / 2 * dt / 3600
        elif ca <= 0 and cb <= 0:
            dis += -(ca + cb) / 2 * dt / 3600
        c = a.get("cells") or []
        if len(c) >= 2:
            dv = (max(c) - min(c)) * 1000
            if dv >= dmax:
                dmax = dv; dmax_cell = c.index(max(c)) + 1
        if soc_first is None and a.get("soc") is not None:
            soc_first = a["soc"]
    if rows and rows[-1].get("soc") is not None:
        soc_last = rows[-1]["soc"]
    return chg, dis, dmax, dmax_cell, soc_first, soc_last


HEADER = ("时间,今日充Ah,今日放Ah,今日净Ah,近24h充Ah,近24h放Ah,近24h净Ah,"
          "SOC,今日压差max_mV,今日最高节,告警")


def snapshot():
    now = time.time()
    today0 = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    r24 = load(24 * 3600)
    if not r24:
        return None
    c24, d24, _, _, _, _ = integrate(r24)
    rt = [d for d in r24 if d["ts"] >= today0]
    if rt:
        ct, dt_, dmax, dcell, s0, s1 = integrate(rt)
    else:
        ct = dt_ = 0.0; dmax = 0.0; dcell = 0; s0 = s1 = None

    net24 = c24 - d24
    warns = []
    if net24 < -WARN_DEFICIT_AH:
        warns.append("近24h净亏%.1fAh" % abs(net24))
    if dmax >= 300:
        warns.append("压差峰值%.0fmV(第%d节)" % (dmax, dcell))

    row = [
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "%.2f" % ct, "%.2f" % dt_, "%+.2f" % (ct - dt_),
        "%.2f" % c24, "%.2f" % d24, "%+.2f" % net24,
        str(s1 if s1 is not None else "--"),
        "%.0f" % dmax, str(dcell or "--"),
        "；".join(warns) if warns else "正常",
    ]
    return row


def append_row(row, day):
    os.makedirs(OUTDIR, exist_ok=True)
    path = os.path.join(OUTDIR, "balance-%s.csv" % day)
    need_header = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if need_header:
            f.write(HEADER + "\n")
        f.write(",".join(row) + "\n")
        f.flush(); os.fsync(f.fileno())
    return path


def last_stamp(day):
    path = os.path.join(OUTDIR, "balance-%s.csv" % day)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            lines = [x for x in f.read().splitlines() if x.strip()]
        return lines[-1].split(",")[0] if len(lines) > 1 else None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="只打印，不写文件")
    a = ap.parse_args()

    row = snapshot()
    if row is None:
        print("（history.jsonl 里没有可用数据）", file=sys.stderr)
        return
    if a.status:
        print(HEADER)
        print(",".join(row))
        return

    now = datetime.datetime.now()
    day = now.strftime("%Y%m%d")
    stamp = now.strftime("%Y-%m-%d %H")
    if (last_stamp(day) or "").startswith(stamp):
        print("本小时已写过，跳过（幂等）")
        return
    print("已写入 -> %s" % append_row(row, day))


if __name__ == "__main__":
    main()
