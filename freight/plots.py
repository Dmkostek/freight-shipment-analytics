"""Static figures for the README. Every function saves a PNG and returns its path."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import PercentFormatter

CARRIER_COLORS = {"A": "#2a6fdb", "B": "#e8833a", "C": "#d6453d", "D": "#8e5ac8", "E": "#2a9d8f", "F": "#7a8691"}
def short(name: str) -> str:
    """First word of a carrier name, for chart axes ("Lindgren & Voss" becomes "Lindgren")."""
    return name.split()[0]


plt.rcParams.update({"axes.grid": True, "grid.alpha": 0.3, "axes.spines.top": False, "axes.spines.right": False})


def _save(fig, out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path


def carrier_scorecard(sc: pd.DataFrame, out_dir: Path) -> Path:
    colors = [CARRIER_COLORS[c] for c in sc.carrier_id]
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.6))
    labels = [short(n) for n in sc.carrier_name]
    axes[0].bar(labels, sc.on_time_rate, color=colors)
    for i, v in enumerate(sc.on_time_rate):
        axes[0].text(i, v + 0.01, f"{v:.0%}", ha="center")
    axes[0].set(title="On-time rate", ylim=(0, 1.05))
    axes[0].yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    axes[1].bar(labels, sc.quote_error_days, color=colors)
    axes[1].axhline(0, color="k", lw=1)
    for i, v in enumerate(sc.quote_error_days):
        axes[1].text(i, v + (0.03 if v >= 0 else -0.07), f"{v:+.2f}", ha="center")
    axes[1].set(title="Quoted minus actual transit days\n(negative = optimistic quote)")
    x = np.arange(len(sc))
    axes[2].bar(x - 0.2, sc.freight_per_kg, 0.4, color="#9fb3c8", label="Freight price")
    axes[2].bar(x + 0.2, sc.net_cost_per_kg, 0.4, color=colors, label="Freight + late credits")
    axes[2].set_xticks(x, labels)
    axes[2].set(title="Cost per kg (EUR)", ylim=(0, sc.net_cost_per_kg.max() * 1.3))
    axes[2].legend(loc="upper right", fontsize=9)
    return _save(fig, out_dir, "carrier_scorecard.png")


def freight_vs_net(carrier_lane: pd.DataFrame, lane_names: dict[str, str], carrier_names: dict[str, str], out_dir: Path) -> Path:
    """Standard-goods cost per kg by lane and carrier: freight price plus late credits, cheapest marked."""
    std = carrier_lane[carrier_lane.is_adr == 0]
    lanes = sorted(std.lane_id.unique())
    fig, axes = plt.subplots(2, 3, figsize=(15, 8), sharey=True)
    for ax, lane in zip(axes.ravel(), lanes):
        g = std[std.lane_id == lane].sort_values("carrier_id")
        x = np.arange(len(g))
        ax.bar(x, g.freight_per_kg, color=[CARRIER_COLORS[c] for c in g.carrier_id], label="Freight price (carrier colour)")
        ax.bar(x, g.penalty_per_kg, bottom=g.freight_per_kg, color="#444f5a", label="Late credits")
        ax.set_xticks(x, [short(carrier_names[c]) for c in g.carrier_id])
        best_f, best_n = int(np.argmin(g.freight_per_kg.to_numpy())), int(np.argmin(g.net_cost_per_kg.to_numpy()))
        ax.text(best_f, g.freight_per_kg.iloc[best_f] / 2, "cheapest\nprice", ha="center", color="white", fontsize=8)
        ax.text(best_n, g.net_cost_per_kg.iloc[best_n] + 0.003, "★ cheapest\nin total", ha="center", fontsize=8)
        ax.set_title(lane_names[lane])
        ax.set_ylim(0, std.net_cost_per_kg.max() * 1.25)
    axes[0, 0].set_ylabel("EUR per kg")
    axes[1, 0].set_ylabel("EUR per kg")
    axes[0, 0].legend(loc="upper left", fontsize=8)
    fig.suptitle("Standard goods: the cheapest price is not the cheapest carrier", y=1.0)
    return _save(fig, out_dir, "freight_vs_net_cost.png")


def monthly_performance(monthly: pd.DataFrame, out_dir: Path) -> Path:
    months = pd.to_datetime(monthly.ship_month + "-01")
    fig, ax = plt.subplots(figsize=(11, 4.6))
    for m, name in zip(months, monthly.ship_month):
        if m.month in (12, 1, 7):
            ax.axvspan(m - pd.Timedelta(days=15), m + pd.Timedelta(days=15), color="#f3d9d7", alpha=0.7, lw=0)
    ax.plot(months, monthly.on_time_rate, "o-", color="#d6453d", label="On-time rate")
    ax.plot(months, monthly.on_time_rate_3m, "-", color="#1c2733", lw=2, label="3-month average")
    ax.set(ylabel="On-time rate", title="On-time rate by month (shaded: December, January, July, when customs holds are likelier)", ylim=(0.6, 0.9))
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.legend(loc="lower left")
    return _save(fig, out_dir, "monthly_on_time.png")


def price_of_reliability(frontier: pd.DataFrame, out_dir: Path) -> Path:
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(frontier.on_time, frontier.cost_per_kg, "o-", color="#2a6fdb", label="Optimised carrier mix")
    ax.plot(frontier.baseline_on_time.iloc[0], frontier.baseline_cost_per_kg.iloc[0], "D", color="#d6453d", ms=10, label="Current mix")
    ax.set(xlabel="Volume-weighted on-time rate", ylabel="Expected cost, EUR per kg", title="The price of reliability")
    ax.xaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.legend()
    return _save(fig, out_dir, "price_of_reliability.png")


def margin_overview(by_lane: pd.DataFrame, top_vs_rest: dict[str, float], out_dir: Path) -> Path:
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.4), gridspec_kw={"width_ratios": [3, 1.4]})
    by_lane = by_lane.sort_values("margin_pct")
    axes[0].barh(by_lane.lane_name.str.replace(" > ", " → "), by_lane.margin_pct, color="#2a6fdb")
    for y, v in enumerate(by_lane.margin_pct):
        axes[0].text(v + 0.002, y, f"{v:.1%}", va="center")
    axes[0].set(title="Margin by lane", xlim=(0, by_lane.margin_pct.max() * 1.15))
    axes[1].bar(list(top_vs_rest), list(top_vs_rest.values()), color=["#d6453d", "#2a9d8f"])
    for i, v in enumerate(top_vs_rest.values()):
        axes[1].text(i, v + 0.004, f"{v:.1%}", ha="center")
    axes[1].set(title="Largest customer vs the rest", ylim=(0, max(top_vs_rest.values()) * 1.2))
    return _save(fig, out_dir, "margin_overview.png")
