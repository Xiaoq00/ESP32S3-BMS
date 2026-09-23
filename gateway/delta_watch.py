#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JK-BMS 电芯压差「观测哨」——每 10 分钟追加一条快照
--------------------------------------------------
用途：对某一段时间的均衡效果做连续监测（例如"改了均衡设置后 24 小时"）。
特点：
  * 只追加，不覆盖 —— 不会丢历史，方便事后看趋势。
  * 只读 latest.json（recorder 的快照），不碰 MQTT / 看板，零风险。
  * 每行记录：时间、压差、最高/最低节及其电压、SOC、总压、电流、状态。
用法：
  python3 delta_watch.py                 # 追加一条
  python3 delta_watch.py --status        # 只看当前一条，不写文件
"""
import json, os, sys, argparse, datetime

LATEST = "/opt/jk-bms/latest.json"
OUT = "/opt/jk-bms/delta_watch.csv"
HEADER = "时间,压差mV,最高节,最高V,最低节,最低V,SOC,总压V,电流A,状态"


def snapshot():
    try:
        d = json.load(open(LATEST))
    except Exception:
        return None
    cells = d.get("cells") or []
    if len(cells) < 2:
        return None
    mx, mn = max(cells), min(cells)
    cur = d.get("current") or 0.0
    mode = "充电" if cur > 0.05 else ("放电" if cur < -0.05 else "静置")
    return [
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "%.1f" % ((mx - mn) * 1000),
        str(cells.index(mx) + 1), "%.3f" % mx,
        str(cells.index(mn) + 1), "%.3f" % mn,
        str(d.get("soc")),
        "%.3f" % (d.get("voltage") or 0.0),
        "%.2f" % cur,
        mode,
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="只打印当前快照，不写文件")
    a = ap.parse_args()
    row = snapshot()
    if row is None:
        print("（拿不到 latest.json 或电芯数据）", file=sys.stderr)
        return
    line = ",".join(row)
    if a.status:
        print(HEADER)
        print(line)
        return
    new = not os.path.exists(OUT)
    with open(OUT, "a", encoding="utf-8") as f:
        if new:
            f.write(HEADER + "\n")
        f.write(line + "\n")
    print(line)


if __name__ == "__main__":
    main()
