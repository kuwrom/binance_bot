"""Round-4 Deflated Sharpe correction (docs/failures/ROUND4.md): `--dsr=clustered` vs `--dsr=legacy`.

Legacy (report.build): N = every trial in the ledger, V[SR] = variance of the per-trade Sharpe of every trial with
n >= 10. The review found both inputs distorted: clone variants (e.g. "+exit_be" re-runs the base book, a no-op
funding budget equals its base) count as extra independent trials, and tiny partitions (n 10-60) add pure sampling
noise (Var(SR_hat) ~ 1/(n-1)) to V[SR], which raises the hurdle SR* = sqrt(V[SR]) x E[max of N normals].

Clustered:
- every trial's daily R series (sum of R by entry day, 0 on days without trades, over the union of days);
- average-linkage hierarchical clustering on 1 - corr, cut at 1 - CORR_CUT: trials whose daily PnL correlates
  > 0.7 on average are one family (one independent trial);
- N_eff = number of clusters (trials with < MIN_DAYS trading days cannot be correlated and stay singletons, which
  is the conservative choice);
- V[SR] = variance across FAMILIES with n >= MIN_N_VAR of the family's mean per-trade SR (clusters restricted to
  their n >= MIN_N_VAR members); a clone therefore counts once and a tiny partition not at all.
The DSR is reported three ways: legacy (raw N, legacy V), raw N with the corrected V, effective N with the corrected
V (the one the clustered verdicts use)."""

from __future__ import annotations

import statistics
from typing import Any

import numpy as np

from .stats import dsr, sharpe

CORR_CUT = 0.7
MIN_N_VAR = 100
MIN_DAYS = 3


def daily_series(trials: dict[str, list[dict]]) -> tuple[list[str], np.ndarray, list[str]]:
    """(trial names with >= MIN_DAYS trading days, matrix trials x days of daily R sums, the rest)."""
    days = sorted({t["day"] for tr in trials.values() for t in tr})
    col = {d: k for k, d in enumerate(days)}
    names, rows, rest = [], [], []
    for name, tr in trials.items():
        v = np.zeros(len(days))
        for t in tr:
            v[col[t["day"]]] += t["r"]
        if np.count_nonzero(v) >= MIN_DAYS and v.std() > 0:
            names.append(name)
            rows.append(v)
        else:
            rest.append(name)
    return names, (np.vstack(rows) if rows else np.zeros((0, len(days)))), rest


def average_linkage(corr: np.ndarray, cut: float = CORR_CUT) -> list[list[int]]:
    """Agglomerative clustering, average linkage on similarity = corr (Lance-Williams update): merge the most
    similar pair of clusters while their mean pairwise correlation exceeds `cut`."""
    s = corr.astype(float).copy()
    np.fill_diagonal(s, -np.inf)
    members = {k: [k] for k in range(s.shape[0])}
    alive = np.ones(s.shape[0], bool)
    while alive.sum() > 1:
        masked = np.where(np.outer(alive, alive), s, -np.inf)
        a, b = np.unravel_index(int(np.argmax(masked)), s.shape)
        if masked[a, b] <= cut:
            break
        na, nb = len(members[a]), len(members[b])
        s[a, :] = s[:, a] = (na * s[a, :] + nb * s[b, :]) / (na + nb)
        s[a, a] = -np.inf
        members[a] += members.pop(b)
        alive[b] = False
    return [members[k] for k in sorted(members)]


def families(trials: dict[str, list[dict]], cut: float = CORR_CUT) -> list[list[str]]:
    """Trial names grouped into correlated families (singletons for trials that cannot be correlated)."""
    names, x, rest = daily_series(trials)
    out = []
    if len(names):
        corr = np.nan_to_num(np.corrcoef(x)) if len(names) > 1 else np.ones((1, 1))
        out = [[names[k] for k in cl] for cl in average_linkage(corr, cut)]
    return out + [[n] for n in rest]


def corrected_var_sr(trials: dict[str, list[dict]], fams: list[list[str]], min_n: int = MIN_N_VAR) -> tuple[float, int]:
    """(V[SR] across families' mean SR using members with n >= min_n, number of families used)."""
    srs = []
    for fam in fams:
        xs = [s for n in fam if len(trials[n]) >= min_n and (s := sharpe([t["r"] for t in trials[n]])) is not None]
        if xs:
            srs.append(statistics.fmean(xs))
    return (statistics.variance(srs) if len(srs) >= 2 else 0.0), len(srs)


def legacy_var_sr(trials: dict[str, list[dict]]) -> float:
    srs = [s for tr in trials.values() if len(tr) >= 10 and (s := sharpe([t["r"] for t in tr])) is not None]
    return statistics.variance(srs) if len(srs) >= 2 else 0.0


def deflate(trials: dict[str, list[dict]], mode: str = "legacy") -> dict[str, Any]:
    """Ledger-level DSR inputs and every trial's DSR under legacy / raw-N / effective-N. `dsr` is the one the
    verdicts use: legacy in mode "legacy", effective N + corrected V in mode "clustered"."""
    n_raw, v_leg = len(trials), legacy_var_sr(trials)
    out: dict[str, Any] = {"mode": mode, "n_trials": n_raw, "var_sr_legacy": round(v_leg, 4), "per_trial": {}}
    if mode == "clustered":
        fams = families(trials)
        v_cor, n_fam = corrected_var_sr(trials, fams)
        out.update(n_eff=len(fams), var_sr=round(v_cor, 4), n_var_families=n_fam,
                   families=sorted(fams, key=lambda f: (-len(f), f[0])))
    else:
        v_cor, out["var_sr"], out["n_eff"] = v_leg, round(v_leg, 4), n_raw
    for name, tr in trials.items():
        rs = [t["r"] for t in tr]
        if len(rs) < 3:
            out["per_trial"][name] = {"dsr": None}
            continue
        leg = dsr(rs, n_raw, v_leg)
        row = dict(leg, dsr_legacy=leg["dsr"])
        if mode == "clustered":
            raw_c, eff_c = dsr(rs, n_raw, v_cor), dsr(rs, out["n_eff"], v_cor)
            row.update(eff_c, dsr_legacy=leg["dsr"], dsr_rawN=raw_c["dsr"], sr0_rawN=raw_c["sr0"],
                       dsr_effN=eff_c["dsr"])
        out["per_trial"][name] = row
    return out
