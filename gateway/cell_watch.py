#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JK-BMS 重点电芯观测哨（第 1 / 7 / 8 节 + 全组压差）
================================================================
背景：用户反映第 1、7、8 节曾出现"电压乱跳"（疑似采样线接触不良），
      需要一份**完整、抗断电、可长期保存**的记录。

设计要点（为什么这样写）：
  1. 数据源用 recorder 写的 history.jsonl（5 秒级原始数据），而不是自己再存一遍
     —— 这样能捕捉到"分钟内"的瞬间跳变，10 分钟快照那种粒度会漏掉。
  2. 每分钟把"上一分钟"的统计压成一行，追加写入**按天分文件**的 CSV：
        /opt/jk-bms/cellwatch/cellwatch-YYYYMMDD.csv
     按天分文件：万一断电写坏，最多丢当天，不牵连历史。
  3. 每次写完 fsync() 落盘 —— 断电不会丢已经写进去的行。
  4. 幂等：同一分钟只会写一行，重复运行/重启不会产生重复数据。
  5. 告警：第 1/7/8 节的相邻采样跳变 >150mV，且远大于其他节的中位跳变
     （说明不是负载引起的整组波动）→ 标记为异常。

用法：
  python3 cell_watch.py            # 记录最近一分钟（供 systemd timer 每分钟调用）
  python3 cell_watch.py --status   # 只打印当前状态，不写文件
  python3 cell_watch.py --backfill # 补写最近 N 分钟（--minutes）
"""
import json, os, sys, time, argparse, datetime, statistics

DATA = "/opt/jk-bms"
HIST = os.path.join(DATA, "history.jsonl")
LATEST = os.path.join(DATA, "latest.json")
OUTDIR = os.path.join(DATA, "cellwatch")

WATCH_CELLS = [0, 6, 7]          # 重点盯防：第 1 / 7 / 8 节（0-based）
JUMP_MV = 150                    # 跳变告警阈值
HEADER = ("时间,样本数,压差max_mV,压差avg_mV,最高节,最低节,"
          "第1节min,第1节max,第1节跳变mV,"
          "第7节min,第7节max,第7节跳变mV,"
          "第8节min,第8节max,第8节跳变mV,"
          "SOC,总压minV,总压maxV,电流avgA,状态,告警")


def tail_rows(seconds):
    """读取 history.jsonl 末尾，返回 ts >= now-seconds 的样本"""
    if not os.path.exists(HIST):
        return []
    cut = time.time() - seconds
    size = os.path.getsize(HIST)
    with open(HIST, "rb") as f:
        f.seek(max(0, size - 512 * 1024))
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
        c = d.get("cells")
        if not ts or ts < cut or not c or len(c) < 8:
            continue
        rows.append((ts, c, d.get("soc"), d.get("voltage"), d.get("current")))
    rows.sort(key=lambda r: r[0])
    return rows


def analyse(rows, t0, t1):
    """统计 [t0, t1) 窗口"""
    w = [r for r in rows if t0 <= r[0] < t1]
    if not w:
        return None
    n = len(w[0][1])
    dvs = [max(c) - min(c) for _, c, *_ in w]
    dmax = max(dvs)
    davg = sum(dvs) / len(dvs)
    # 最高/最低节：取压差最大那一刻
    worst = w[dvs.index(dmax)]
    hi = worst[1].index(max(worst[1])) + 1
    lo = worst[1].index(min(worst[1])) + 1
    # 各节跳变（相邻采样）
    jump = [0.0] * n
    med_jumps = []
    for a, b in zip(w, w[1:]):
        if b[0] - a[0] > 60:
            continue
        dj = [abs(b[1][i] - a[1][i]) for i in range(n)]
        med_jumps.append(statistics.median(dj))
        for i in range(n):
            jump[i] = max(jump[i], dj[i])
    med = statistics.median(med_jumps) if med_jumps else 0.0
    alerts = []
    for i in WATCH_CELLS:
        if jump[i] * 1000 > JUMP_MV and jump[i] > max(0.05, med * 10):
            alerts.append("第%d节跳变%.0fmV" % (i + 1, jump[i] * 1000))
    cur = [r[4] for r in w if r[4] is not None]
    cavg = sum(cur) / len(cur) if cur else 0.0
    mode = "充电" if cavg > 0.05 else ("放电" if cavg < -0.05 else "静置")
    vs = [r[3] for r in w if r[3] is not None]
    row = [datetime.datetime.fromtimestamp(t0).strftime("%Y-%m-%d %H:%M"), str(len(w)),
           "%.1f" % (dmax * 1000), "%.1f" % (davg * 1000), str(hi), str(lo)]
    for i in WATCH_CELLS:
        cv = [r[1][i] for r in w]
        row += ["%.3f" % min(cv), "%.3f" % max(cv), "%.0f" % (jump[i] * 1000)]
    row += [str(w[-1][2]), "%.3f" % (min(vs) if vs else 0), "%.3f" % (max(vs) if vs else 0),
            "%.2f" % cavg, mode, ("；".join(alerts) if alerts else "")]
    return row


def append_row(row, day):
    os.makedirs(OUTDIR, exist_ok=True)
    path = os.path.join(OUTDIR, "cellwatch-%s.csv" % day)
    need_header = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if need_header:
            f.write(HEADER + "\n")
        f.write(",".join(row) + "\n")
        f.flush()
        os.fsync(f.fileno())          # 抗断电：立刻落盘
    return path


def last_minute_written(day):
    path = os.path.join(OUTDIR, "cellwatch-%s.csv" % day)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [x for x in f.read().splitlines() if x.strip()]
        if len(lines) < 2:
            return None
        return lines[-1].split(",")[0]
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--backfill", type=int, default=0, help="补写最近 N 分钟")
    a = ap.parse_args()

    if a.status:
        rows = tail_rows(300)
        if not rows:
            print("（最近 5 分钟没有数据）")
            return
        c = rows[-1][1]
        mx, mn = max(c), min(c)
        print("时间 %s" % datetime.datetime.fromtimestamp(rows[-1][0]).strftime("%Y-%m-%d %H:%M:%S"))
        print("压差 %.1f mV  最高第%d节 %.3fV  最低第%d节 %.3fV" % (
            (mx - mn) * 1000, c.index(mx) + 1, mx, c.index(mn) + 1, mn))
        print("重点节：第1节 %.3f  第7节 %.3f  第8节 %.3f" % (c[0], c[6], c[7]))
        print("SOC=%s 总压=%.3fV 电流=%sA" % (rows[-1][2], rows[-1][3] or 0, rows[-1][4]))
        return

    now = time.time()
    minutes = a.backfill if a.backfill > 0 else 1
    rows = tail_rows(minutes * 60 + 300)
    written = []
    for k in range(minutes, 0, -1):
        t1 = (int(now // 60) - (k - 1)) * 60
        t0 = t1 - 60
        day = datetime.datetime.fromtimestamp(t0).strftime("%Y%m%d")
        stamp = datetime.datetime.fromtimestamp(t0).strftime("%Y-%m-%d %H:%M")
        if last_minute_written(day) == stamp:
            continue                       # 幂等：这一分钟已写过
        row = analyse(rows, t0, t1)
        if row is None:
            continue
        written.append(append_row(row, day))
    if written:
        print("已写入 %d 行 -> %s" % (len(written), written[-1]))
    else:
        print("无新数据可写")


if __name__ == "__main__":
    main()
