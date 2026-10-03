"""Replay scan_triggers' move and volume rules on real 15m bars (data/hist5m.db): coins triggered per 15m window.
Results: docs/SCANNING.md "Measured trigger rates". Run: python -m shortbot.backtest.trigger_rates [hist5m.db]"""
import sqlite3, sys
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as sw


def main() -> None:
    db = sqlite3.connect(f"file:{sys.argv[1] if len(sys.argv) > 1 else 'data/hist5m.db'}?mode=ro", uri=True)
    syms = [r[0] for r in db.execute("SELECT DISTINCT symbol FROM bars5")]
    M15 = 900_000
    per = {}
    for s in syms:
        a = np.array(db.execute("SELECT ts, c, qv FROM bars5 WHERE symbol=? ORDER BY ts", (s,)).fetchall())
        if len(a) < 3000:
            continue
        b = a[:, 0] // M15
        keys, idx = np.unique(b, return_index=True)
        last = np.r_[idx[1:] - 1, len(a) - 1]
        c = a[last, 1]; v = np.add.reduceat(a[:, 2], idx)
        full = np.diff(np.r_[idx, len(a)]) == 3
        per[s] = (keys[full], c[full], v[full])
    print("symbols", len(per), "of", len(syms))
    counts = {k: {} for k in ("z1h_2", "z4h_2", "z1h_3", "z4h_3", "vol_2", "vol_3", "any_2", "any_3")}
    persist = {"z4h_2": [0, 0], "z1h_2": [0, 0]}
    for s, (k, c, v) in per.items():
        if len(c) < 130: continue
        r = np.diff(np.log(c)); kk = k[1:]
        out = {}
        for name, h in (("z1h", 4), ("z4h", 16)):
            move = sw(r, h).sum(1)                       # move ending at i+h-1
            base = sw(r, 96)                              # 96 returns
            sd = base.std(1)                              # sd of returns i..i+95
            # move ending at t uses sd of r[t-h-95 .. t-h]
            n = len(r)
            t = np.arange(h + 95, n)
            z = move[t - h + 1] / (sd[t - h - 95] * np.sqrt(h))
            out[name] = (kk[t], z)
        hs = sw(v, 4).sum(1)                              # hour ending at j+3
        # z of hour ending at t vs prior 29 hours ending t-4, t-8, ...
        t = np.arange(3 + 4 * 29, len(v))
        prior = np.stack([hs[t - 3 - 4 * j] for j in range(1, 30)], 1)
        med = np.median(prior, 1); mad = np.median(np.abs(prior - med[:, None]), 1) * 1.4826
        mad = np.where(mad > 0, mad, prior.std(1)); vz = (hs[t - 3] - med) / np.where(mad > 0, mad, 1)
        out["vol"] = (k[t], vz)
        for thr in (2, 3):
            hit_any = set()
            for name in ("z1h", "z4h", "vol"):
                ts, z = out[name]
                hit = ts[(np.abs(z) if name != "vol" else z) >= thr]
                for x in hit: counts[f"{name}_{thr}"][x] = counts[f"{name}_{thr}"].get(x, 0) + 1
                hit_any |= set(hit.tolist())
            for x in hit_any: counts[f"any_{thr}"][x] = counts[f"any_{thr}"].get(x, 0) + 1
        for name in ("z1h", "z4h"):
            ts, z = out[name]; h = np.abs(z) >= 2
            persist[f"{name}_2"][0] += int((h[1:] & h[:-1]).sum()); persist[f"{name}_2"][1] += int(h[1:].sum())
    allw = sorted(set().union(*[set(d) for d in counts.values()]))
    lo, hi = allw[0], allw[-1]; W = np.arange(lo, hi + 1)
    print("windows", len(W), "days", round(len(W) / 96, 1))
    for name, d in counts.items():
        x = np.array([d.get(w, 0) for w in W])
        print(f"{name:7s} coins/window: mean {x.mean():5.1f}  median {np.median(x):4.0f}  p90 {np.percentile(x, 90):4.0f}  p99 {np.percentile(x, 99):4.0f}  max {x.max()}")
    for k2, (a, b) in persist.items():
        print(f"{k2}: share of triggers that also triggered the previous window {a / max(b, 1):.2f}")


if __name__ == "__main__":
    main()
