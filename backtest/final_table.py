#!/usr/bin/env python3
"""Final consolidated table for the trailing-stop study (adverse intrabar)."""
from __future__ import annotations
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/workspace/Ayzr1435")
sys.path.insert(0, HERE)
import owner_plan as OP  # noqa: E402
import exit_compare as EC  # noqa: E402

TFS = ("5m", "15m", "30m", "1h", "4h", "1d")


def load_yahoo():
    return {tf: json.load(open(os.path.join(HERE, f"yahoo_{tf}.json")))
            for tf in TFS}


def net(trades, rule, cost, istop=9.0):
    res = [EC.score(t, rule, init_stop=istop, mode="adverse") for t in trades]
    wins = sum(1 for x in res if x > 0)
    return (100 * wins / len(res), sum(res) - cost * len(res))


RULES = [("الحالي: وقف $9 ثابت", EC.r_fixed, 9.0),
         ("خطتك: تأمين $5 ثم $10", EC.r_owner, 9.0),
         ("تتبّع $2", EC.make_trail(2), 9.0),
         ("تتبّع $3", EC.make_trail(3), 9.0),
         ("تتبّع $4", EC.make_trail(4), 9.0),
         ("تتبّع $6", EC.make_trail(6), 9.0),
         ("تتبّع $9", EC.make_trail(9), 9.0)]


def run(name, bars):
    sig = OP.build_signals(bars)
    trades = EC.collect_entries(sig)
    days = sorted({s["t"][:10] for s in sig})
    print(f"\n### {name}: {len(trades)} trades, {days[0]}..{days[-1]}")
    print(f"{'rule':26s} {'win%':>6s} {'net@0.5':>9s} {'net@1.5':>9s}")
    for rname, rule, istop in RULES:
        w, n05 = net(trades, rule, 0.5, istop)
        _, n15 = net(trades, rule, 1.5, istop)
        print(f"{rname:26s} {w:>6.1f} {n05:>9.1f} {n15:>9.1f}")


if __name__ == "__main__":
    run("DUKA spot XAUUSD (the feed the bot trades)", json.load(open(OP.DATA)))
    run("YAHOO COMEX futures (proxy, losing window)", load_yahoo())
