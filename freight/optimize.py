"""Carrier allocation as a linear programme.

For every segment (lane x dangerous-goods flag) the volume can be split across
the carriers that serve it. The programme chooses the split that minimises the
expected total logistics cost (freight plus late-delivery credits) subject to:

* every segment meets an on-time target (capped at the best any carrier achieves there),
* no carrier carries more than a maximum share of total volume.

Costs and on-time rates are historical averages per carrier, lane and segment,
so the savings are an estimate. They assume prices and performance stay as they
were, and that carriers can absorb the shifted volume.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import linprog


class InfeasibleAllocation(ValueError):
    """No allocation satisfies the on-time and carrier-share constraints."""


@dataclass(frozen=True)
class Allocation:
    table: pd.DataFrame  # one row per lane x ADR x carrier with current and recommended share
    summary: dict[str, float]


def segment_options(d: pd.DataFrame, min_shipments: int = 30) -> pd.DataFrame:
    """Historical performance of every carrier in every segment with enough data."""
    g = d.groupby(["lane_id", "is_adr", "carrier_id"])
    seg = pd.DataFrame(
        {
            "shipments": g.size(),
            "kg": g.weight_kg.sum(),
            "on_time": 1 - g.is_late.mean(),
            "cost_per_kg": (g.freight_cost_eur.sum() + g.late_penalty_eur.sum()) / g.weight_kg.sum(),
        }
    ).reset_index()
    seg = seg[seg.shipments >= min_shipments].copy()
    seg["segment"] = seg.lane_id + np.where(seg.is_adr == 1, " (dangerous goods)", "")
    seg["segment_kg"] = seg.groupby("segment").kg.transform("sum")
    seg["current_share"] = seg.kg / seg.segment_kg
    return seg.reset_index(drop=True)


def optimize_allocation(options: pd.DataFrame, on_time_target: float = 0.85, max_carrier_share: float = 0.45) -> Allocation:
    seg = options.copy()
    n = len(seg)
    segments = seg.segment.unique()
    total_kg = seg.groupby("segment").segment_kg.first().sum()

    c = (seg.segment_kg * seg.cost_per_kg).to_numpy()  # cost if the whole segment used this option

    a_eq = np.zeros((len(segments), n))
    for r, s in enumerate(segments):
        a_eq[r, (seg.segment == s).to_numpy()] = 1.0
    b_eq = np.ones(len(segments))

    best = seg.groupby("segment").on_time.max()
    seg_target = np.minimum(on_time_target, best.loc[segments].to_numpy())
    a_ub = [-(a_eq[r] * seg.on_time.to_numpy()) for r in range(len(segments))]
    b_ub = list(-seg_target)
    for carrier in seg.carrier_id.unique():  # carrier share cap
        a_ub.append(((seg.carrier_id == carrier) * seg.segment_kg).to_numpy() / total_kg)
        b_ub.append(max_carrier_share)

    res = linprog(c, A_ub=np.array(a_ub), b_ub=np.array(b_ub), A_eq=a_eq, b_eq=b_eq, bounds=(0, 1), method="highs")
    if not res.success:
        raise InfeasibleAllocation(f"no allocation meets an on-time target of {on_time_target:.0%} with a {max_carrier_share:.0%} carrier cap: {res.message}")

    seg["recommended_share"] = np.where(res.x < 1e-9, 0.0, res.x)
    kg = seg.segment_kg
    baseline_cost = float((kg * seg.current_share * seg.cost_per_kg).sum())
    optimized_cost = float((kg * seg.recommended_share * seg.cost_per_kg).sum())
    volume = float((kg * seg.current_share).sum())
    summary = {
        "on_time_target": on_time_target,
        "max_carrier_share": max_carrier_share,
        "baseline_cost_eur": baseline_cost,
        "optimized_cost_eur": optimized_cost,
        "saving_eur": baseline_cost - optimized_cost,
        "saving_pct": (baseline_cost - optimized_cost) / baseline_cost,
        "baseline_cost_per_kg": baseline_cost / volume,
        "optimized_cost_per_kg": optimized_cost / volume,
        "baseline_on_time": float((kg * seg.current_share * seg.on_time).sum() / volume),
        "optimized_on_time": float((kg * seg.recommended_share * seg.on_time).sum() / volume),
    }
    table = seg[["segment", "carrier_id", "shipments", "on_time", "cost_per_kg", "current_share", "recommended_share"]].copy()
    return Allocation(table=table, summary=summary)


def price_of_reliability(options: pd.DataFrame, targets: np.ndarray | None = None, max_carrier_share: float = 0.45) -> pd.DataFrame:
    """Optimised cost per kg and achieved on-time rate for a range of on-time targets."""
    targets = np.round(np.arange(0.60, 0.951, 0.025), 3) if targets is None else targets
    rows = []
    for t in targets:
        try:
            s = optimize_allocation(options, float(t), max_carrier_share).summary
        except InfeasibleAllocation:
            continue
        rows.append({"target": float(t), "cost_per_kg": s["optimized_cost_per_kg"], "on_time": s["optimized_on_time"], "baseline_cost_per_kg": s["baseline_cost_per_kg"], "baseline_on_time": s["baseline_on_time"]})
    return pd.DataFrame(rows)
