"""Analysis of delivered shipments: reliability, cost, margin, seasonality."""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd
from scipy import stats

HOLD_MONTHS = (12, 1, 7)  # months in which the simulator makes customs holds more likely

COST_PARTS = {
    "base_freight_eur": "Base freight",
    "fuel_surcharge_eur": "Fuel surcharge",
    "adr_surcharge_eur": "Dangerous-goods surcharge",
    "customs_fee_eur": "Customs and border",
    "handling_eur": "Handling",
    "insurance_eur": "Insurance",
    "late_penalty_eur": "Late-delivery credits",
}


def load_delivered(conn: sqlite3.Connection) -> pd.DataFrame:
    """Delivered shipments with their margin, joined to every dimension."""
    return pd.read_sql_query(
        """
        SELECT s.*, m.margin_eur, m.margin_pct
        FROM mart_shipments s
        JOIN mart_margin m ON m.shipment_id = s.shipment_id
        WHERE s.is_delivered = 1
        """,
        conn,
    )


def kpis(d: pd.DataFrame) -> dict[str, float]:
    return {
        "shipments": int(len(d)),
        "volume_tonnes": float(d.weight_kg.sum() / 1000),
        "on_time_rate": float(1 - d.is_late.mean()),
        "avg_days_late_when_late": float(d.loc[d.is_late == 1, "days_late"].mean()),
        "freight_per_kg": float(d.freight_cost_eur.sum() / d.weight_kg.sum()),
        "margin_pct": float(d.margin_eur.sum() / d.revenue_eur.sum()),
        "adr_share": float(d.is_adr.mean()),
    }


def carrier_scorecard(d: pd.DataFrame) -> pd.DataFrame:
    """One row per carrier: volume, reliability, quote accuracy, price and margin."""
    g = d.groupby("carrier_id")
    out = pd.DataFrame(
        {
            "carrier_name": g.carrier_name.first(),
            "shipments": g.size(),
            "volume_share": g.weight_kg.sum() / d.weight_kg.sum(),
            "on_time_rate": 1 - g.is_late.mean(),
            "avg_excess_days": g.excess_days.mean(),
            "p90_days_late": g.days_late.quantile(0.9),
            "quote_error_days": g.quote_error_days.mean(),
            "freight_per_kg": g.freight_cost_eur.sum() / g.weight_kg.sum(),
            "penalty_per_kg": g.late_penalty_eur.sum() / g.weight_kg.sum(),
            "margin_pct": g.margin_eur.sum() / g.revenue_eur.sum(),
        }
    )
    out["net_cost_per_kg"] = out.freight_per_kg + out.penalty_per_kg
    return out.reset_index()


def lane_cost_breakdown(d: pd.DataFrame) -> pd.DataFrame:
    """Cost per kg by component for every lane."""
    g = d.groupby("lane_name")
    kg = g.weight_kg.sum()
    return pd.DataFrame({label: g[col].sum() / kg for col, label in COST_PARTS.items()}).reset_index()


def margin_by(d: pd.DataFrame, key: str) -> pd.DataFrame:
    g = d.groupby(key)
    return pd.DataFrame({"shipments": g.size(), "revenue_eur": g.revenue_eur.sum(), "margin_pct": g.margin_eur.sum() / g.revenue_eur.sum()}).reset_index()


def seasonality(d: pd.DataFrame) -> dict[str, float]:
    """Late share in the customs-hold months versus the rest, with a two-proportion z-test."""
    month = pd.to_datetime(d.ship_date).dt.month
    hold = month.isin(HOLD_MONTHS)
    a, b = d.loc[hold, "is_late"], d.loc[~hold, "is_late"]
    p = (a.sum() + b.sum()) / (len(a) + len(b))
    z = (a.mean() - b.mean()) / np.sqrt(p * (1 - p) * (1 / len(a) + 1 / len(b)))
    return {"late_share_hold_months": float(a.mean()), "late_share_other_months": float(b.mean()), "z": float(z), "p_value": float(2 * stats.norm.sf(abs(z)))}


def cheapest_carrier_changes(carrier_lane: pd.DataFrame) -> pd.DataFrame:
    """Per lane (standard goods): the cheapest carrier by freight, and by freight plus late credits."""
    std = carrier_lane[carrier_lane.is_adr == 0]
    cheapest_freight = std.loc[std.groupby("lane_id").freight_per_kg.idxmin(), ["lane_id", "carrier_id", "freight_per_kg"]].rename(columns={"carrier_id": "cheapest_by_freight"})
    cheapest_net = std.loc[std.groupby("lane_id").net_cost_per_kg.idxmin(), ["lane_id", "carrier_id", "net_cost_per_kg"]].rename(columns={"carrier_id": "cheapest_by_net_cost"})
    out = cheapest_freight.merge(cheapest_net, on="lane_id")
    out["changes"] = out.cheapest_by_freight != out.cheapest_by_net_cost
    return out


def findings(d: pd.DataFrame, carrier_lane: pd.DataFrame, customers: pd.DataFrame) -> dict:
    """The headline numbers quoted in the README."""
    sc = carrier_scorecard(d).set_index("carrier_id")
    lanes = margin_by(d, "lane_name").sort_values("margin_pct")
    top = customers.sort_values("revenue_rank").iloc[0]
    changes = cheapest_carrier_changes(carrier_lane)
    return {
        "kpis": kpis(d),
        "least_reliable_carrier": sc.on_time_rate.idxmin(),
        "most_reliable_carrier": sc.on_time_rate.idxmax(),
        "cheapest_carrier_by_freight": sc.freight_per_kg.idxmin(),
        "lanes_where_cheapest_changes": int(changes.changes.sum()),
        "lanes_total": int(len(changes)),
        "seasonality": seasonality(d),
        "lowest_margin_lane": lanes.iloc[0].lane_name,
        "lowest_margin_lane_pct": float(lanes.iloc[0].margin_pct),
        "top_customer_revenue_share": float(top.revenue_share),
        "top_customer_margin_pct": float(top.margin_pct),
        "other_customers_margin_pct": float(d[d.customer_id != top.customer_id].margin_eur.sum() / d[d.customer_id != top.customer_id].revenue_eur.sum()),
    }
