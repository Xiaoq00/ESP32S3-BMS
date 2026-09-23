#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JK-BMS 均衡线电阻观测哨（20 节，逐分钟一行）
================================================================
数据源：recorder 写的 latest.json 里的 cell_res_raw（固件发的原始 uint16）。
        本脚本**只读 latest.json**，不碰 MQTT / 看板 / history.jsonl / agg.json，
        也**完全不动** cellwatch / delta_watch / delta_report 的任何文件。

设计要点（与 cell_watch.py 保持一致）：
  1. 按天分文件：/opt/jk-bms/resistwatch/resist-YYYYMMDD.csv，写坏最多丢当天。
  2. 每次写完 fsync() —— 断电不丢已写入的行。
  3. 幂等：同一分钟只写一行，重复运行/重启不产生重复数据。
  4. 同时保留 raw 整数与换算后 Ω 值，并**记录当次使用的换算系数**
     —— 将来校准改了系数，历史行仍可反算回 raw（raw = 值 ÷ 系数）。
  5. 告警分两层：
       ① BMS 原生报警掩码（厂商判断，最可信，不依赖我们的换算）
       ② 相对中位数倍数（不依赖绝对量纲，规避校准未完成的风险）

术语：这里读的是「均衡线电阻」（采样/均衡线 + 端子接触电阻），
      不是电芯内阻。官方定义："连接均衡器到电池电极之间连线的电阻，
      粗略计算，用于发现接错线或接触不良"。

用法：
  python3 resist_watch.py            # 记录当前快照（供 systemd timer 每分钟调用）
  python3 resist_watch.py --status   # 只打印，不写文件
"""
import json, os, sys, datetime, argparse, statistics

DATA   = "/opt/jk-bms"
LATEST = os.path.join(DATA, "latest.json")
OUTDIR = os.path.join(DATA, "resistwatch")

RES_OHM_SCALE = 0.001     # 与 app.py 的 RES_OHM_SCALE 保持一致（校准只改这两处）
REL_WARN = 2.0            # 辅助告警：达到中位数 2 倍
REL_BAD  = 3.0            # 辅助告警：达到中位数 3 倍


def build_header(n):
    cols = ["时间", "换算系数", "在线", "SOC", "总压V", "电流A",
            "Rmin", "Rmax", "Ravg", "R中位", "最高节", "最低节", "Rdiff",
            "报警掩码", "报警节号", "告警"]
    cols += ["第%d节R" % i for i in range(1, n + 1)]
    cols += ["第%d节raw" % i for i in range(1, n + 1)]
    return ",".join(cols)


def snapshot():
    """返回 (row, n)；拿不到线阻数据则返回 None"""
    try:
        with open(LATEST) as f:
            d = json.load(f)
    except Exception:
        return None
    raw = d.get("cell_res_raw") or []
    if len(raw) < 2:
        return None
    n = len(raw)
    ohm = [v * RES_OHM_SCALE for v in raw]
    med = statistics.median(ohm)
    mx, mn = max(ohm), min(ohm)
    mask = int(d.get("cell_res_alert") or 0)
    masked = [str(i + 1) for i in range(n) if (mask >> i) & 1]

    rel = []
    if med > 0:
        for i, v in enumerate(ohm):
            if v >= med * REL_BAD:
                rel.append("第%d节%.3fΩ(%.1f倍)" % (i + 1, v, v / med))
            elif v >= med * REL_WARN:
                rel.append("第%d节%.3fΩ(%.1f倍)" % (i + 1, v, v / med))

    row = [datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
           "%g" % RES_OHM_SCALE,
           "1" if d.get("online", True) else "0",
           str(d.get("soc")),
           "%.3f" % (d.get("voltage") or d.get("total") or 0.0),
           "%.2f" % (d.get("current") or 0.0),
           "%.4f" % mn, "%.4f" % mx, "%.4f" % (sum(ohm) / n), "%.4f" % med,
           str(ohm.index(mx) + 1), str(ohm.index(mn) + 1), "%.4f" % (mx - mn),
           str(mask),
           ";".join(masked) if masked else "无",
           "；".join(rel) if rel else ""]
    row += ["%.4f" % v for v in ohm]
    row += [str(v) for v in raw]
    return row, n


def append_row(row, n, day):
    os.makedirs(OUTDIR, exist_ok=True)
    path = os.path.join(OUTDIR, "resist-%s.csv" % day)
    need_header = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if need_header:
            f.write(build_header(n) + "\n")
        f.write(",".join(row) + "\n")
        f.flush()
        os.fsync(f.fileno())          # 抗断电：立刻落盘
    return path


def last_minute_written(day):
    path = os.path.join(OUTDIR, "resist-%s.csv" % day)
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
    ap.add_argument("--status", action="store_true", help="只打印当前快照，不写文件")
    a = ap.parse_args()

    snap = snapshot()
    if snap is None:
        print("（拿不到 latest.json 或 cell_res_raw；可能固件未升级 / 未收到 0x02 帧 / 线阻区为空）",
              file=sys.stderr)
        return
    row, n = snap
    if a.status:
        print(build_header(n))
        print(",".join(row))
        return

    now = datetime.datetime.now()
    day = now.strftime("%Y%m%d")
    stamp = now.strftime("%Y-%m-%d %H:%M")
    if last_minute_written(day) == stamp:
        print("本分钟已写过，跳过（幂等）")
        return
    path = append_row(row, n, day)
    print("已写入 -> %s" % path)


if __name__ == "__main__":
    main()
