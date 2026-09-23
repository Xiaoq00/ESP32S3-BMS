#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JK-BMS 电芯压差记录 / 报告生成器
---------------------------------
作用：把 history.jsonl（5 秒级原始电芯电压）与 agg.json（长期日/时聚合）
      汇总成两份人类可读的记录：
        --out  delta_report.md   最近 N 天的压差报告（Markdown）
        --csv  delta_log.csv     逐小时压差明细（CSV，供长期趋势）

设计要点：
  * 只读，不碰正在运行的 recorder/dashboard，零风险。
  * 幂等：每次运行都从头重算并覆盖输出文件，重复跑不会产生重复数据。
  * 保留"最高节"定位，用于判断到底是哪一节在拖后腿。
用法：
  python3 delta_report.py --days 2 --out /opt/jk-bms/delta_report.md \
          --csv /opt/jk-bms/delta_log.csv
"""
import json, os, sys, time, argparse, datetime
from collections import Counter

DATA = "/opt/jk-bms"
HIST = os.path.join(DATA, "history.jsonl")
AGG = os.path.join(DATA, "agg.json")

# 压差分级（mV）：均衡良好的 LFP 组静置应 <20mV
TH_OK, TH_WARN, TH_BAD = 30, 80, 150


def load_rows(days):
    """读取 history.jsonl 末尾若干天，返回 [(ts, cells, soc, v, cur, pw)]"""
    cut = time.time() - days * 86400
    if not os.path.exists(HIST):
        return []
    size = os.path.getsize(HIST)
    with open(HIST, "rb") as f:
        f.seek(max(0, size - 64 * 1024 * 1024))   # 最多读末尾 64MB
        raw = f.read().decode(errors="ignore")
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts = d.get("ts")
        cells = d.get("cells")
        if not ts or ts < cut or not cells or len(cells) < 2:
            continue
        rows.append((ts, cells, d.get("soc"), d.get("voltage"),
                     d.get("current"), d.get("power")))
    rows.sort(key=lambda r: r[0])
    return rows


def fmt(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def grade(mv):
    if mv < TH_OK:
        return "良好"
    if mv < TH_WARN:
        return "偏大"
    if mv < TH_BAD:
        return "严重"
    return "极严重"


def build(rows, days):
    out = []
    now = time.time()
    out.append("# 电芯压差记录报告")
    out.append("")
    out.append("- 生成时间：%s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if not rows:
        out.append("- ⚠ 近 %d 天没有电芯数据（小主机可能离线）" % days)
        return "\n".join(out), []
    out.append("- 数据范围：%s ~ %s（近 %d 天，%d 个样本）" % (
        fmt(rows[0][0]), fmt(rows[-1][0]), days, len(rows)))
    dvs = [(max(c) - min(c)) for _, c, *_ in rows]
    last = rows[-1]
    lmx, lmn = max(last[1]), min(last[1])
    out.append("- 当前静置压差：**%.0f mV**（第%d节 %.3fV 最高 / 第%d节 %.3fV 最低）" % (
        (lmx - lmn) * 1000, last[1].index(lmx) + 1, lmx,
        last[1].index(lmn) + 1, lmn))
    out.append("- 近 %d 天压差：最小 %.0f / 平均 %.0f / 最大 %.0f mV → 判定 **%s**" % (
        days, min(dvs) * 1000, sum(dvs) / len(dvs) * 1000, max(dvs) * 1000,
        grade(max(dvs) * 1000)))
    out.append("")

    # 逐小时
    buckets = {}
    for ts, cells, soc, v, cur, pw in rows:
        h = int(ts // 3600) * 3600
        b = buckets.setdefault(h, {"dmax": 0, "dsum": 0, "n": 0, "cell": None,
                                   "s0": None, "s1": None, "chg": 0, "vmin": 9, "vmax": 0})
        dv = max(cells) - min(cells)
        if dv >= b["dmax"]:
            b["dmax"] = dv
            b["cell"] = cells.index(max(cells)) + 1
        b["dsum"] += dv
        b["n"] += 1
        if b["s0"] is None:
            b["s0"] = soc
        b["s1"] = soc
        if cur and cur > 0.05:
            b["chg"] += 1
        b["vmin"] = min(b["vmin"], min(cells))
        b["vmax"] = max(b["vmax"], max(cells))
    out.append("## 逐小时明细")
    out.append("")
    out.append("| 时间 | 最大压差 | 平均压差 | 最高节 | SOC 起→止 | 充电样本 | 分级 |")
    out.append("|---|---|---|---|---|---|---|")
    csv = ["时间,最大压差mV,平均压差mV,最高节,SOC起,SOC止,充电样本,分级"]
    for h in sorted(buckets):
        b = buckets[h]
        dmv = b["dmax"] * 1000
        amv = b["dsum"] / b["n"] * 1000
        out.append("| %s | %.0f mV | %.0f mV | 第%s节 | %s→%s | %d | %s |" % (
            datetime.datetime.fromtimestamp(h).strftime("%m-%d %H:00"),
            dmv, amv, b["cell"], b["s0"], b["s1"], b["chg"], grade(dmv)))
        csv.append("%s,%.0f,%.0f,%s,%s,%s,%d,%s" % (
            datetime.datetime.fromtimestamp(h).strftime("%Y-%m-%d %H:00"),
            dmv, amv, b["cell"], b["s0"], b["s1"], b["chg"], grade(dmv)))
    out.append("")

    # 长期日峰值（agg.json）
    out.append("## 每日峰值压差（长期记录，来自 agg.json）")
    out.append("")
    out.append("| 日期 | 峰值压差 | 最高节 | 分级 |")
    out.append("|---|---|---|---|")
    try:
        agg = json.load(open(AGG))
        daily = agg.get("daily", {})
        for k in sorted(int(x) for x in daily)[-14:]:
            b = daily[str(k)]
            cd = b.get("cell_dmax")
            if cd is None:
                continue
            out.append("| %s | %.0f mV | 第%s节 | %s |" % (
                datetime.datetime.fromtimestamp(k).strftime("%Y-%m-%d"),
                cd * 1000, b.get("cell_dmax_cell"), grade(cd * 1000)))
    except Exception as e:
        out.append("| （agg.json 读取失败：%s） | | | |" % e)
    out.append("")

    # 各节极值
    n = len(rows[0][1])
    mx = [0.0] * n
    mn = [9.0] * n
    for _, cells, *_ in rows:
        for i, c in enumerate(cells[:n]):
            mx[i] = max(mx[i], c)
            mn[i] = min(mn[i], c)
    out.append("## 各节 %d 天极值（找长期偏高/偏低的节）" % days)
    out.append("")
    out.append("| 节 | 最高 | 最低 | 极差 | 标记 |")
    out.append("|---|---|---|---|---|")
    for i in range(n):
        flag = []
        if mx[i] > 3.6:
            flag.append("曾超 3.6V")
        if mx[i] - mn[i] > 0.3:
            flag.append("波动>300mV")
        out.append("| 第%d节 | %.3f | %.3f | %.0f mV | %s |" % (
            i + 1, mx[i], mn[i], (mx[i] - mn[i]) * 1000, "、".join(flag)))
    out.append("")

    # 异常事件
    out.append("## 异常事件")
    out.append("")
    worst = max(rows, key=lambda r: max(r[1]) - min(r[1]))
    out.append("- 最大压差时刻：%s，%.0f mV（第%d节 %.3fV / 第%d节 %.3fV）" % (
        fmt(worst[0]), (max(worst[1]) - min(worst[1])) * 1000,
        worst[1].index(max(worst[1])) + 1, max(worst[1]),
        worst[1].index(min(worst[1])) + 1, min(worst[1])))
    for cell, c in Counter(r[1].index(max(r[1])) + 1 for r in rows).most_common(3):
        out.append("- 「最高节」第%d节出现 %d 次（%.1f%%）" % (cell, c, 100.0 * c / len(rows)))
    hi_cells = [i + 1 for i in range(n) if mx[i] > 3.6]
    if hi_cells:
        out.append("- 曾超过 3.6V 的节：%s（单体过压保护会在此触发，导致充不满）" %
                   "、".join("第%d节" % c for c in hi_cells))
    out.append("")
    out.append("> 分级标准：<30mV 良好 / 30-80mV 偏大 / 80-150mV 严重 / >150mV 极严重")
    return "\n".join(out), csv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=2)
    ap.add_argument("--out", default=os.path.join(DATA, "delta_report.md"))
    ap.add_argument("--csv", default=os.path.join(DATA, "delta_log.csv"))
    a = ap.parse_args()
    rows = load_rows(a.days)
    md, csv = build(rows, a.days)
    try:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(md + "\n")
    except Exception as e:
        print("写报告失败:", e, file=sys.stderr)
    if csv:
        try:
            with open(a.csv, "w", encoding="utf-8") as f:
                f.write("\n".join(csv) + "\n")
        except Exception as e:
            print("写 CSV 失败:", e, file=sys.stderr)
    print(md)


if __name__ == "__main__":
    main()
