"""Build the interactive dashboard: one self-contained HTML page.

All delivered shipments are embedded as compact column arrays, and the page
filters and aggregates them in the browser, so it needs no server. The Plotly
library is bundled next to the page (docs/plotly.min.js), so it also works offline.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .optimize import Allocation

TEMPLATE = Path(__file__).parent / "dashboard_template.html"
REPO_URL = "https://github.com/Dmkostek/freight-shipment-analytics"
PLACEHOLDER = "/*__DATA__*/ null"


def dashboard_payload(d: pd.DataFrame, allocation: Allocation, frontier: pd.DataFrame) -> dict:
    """The JSON data the page needs: shipment columns plus the optimiser output."""
    d = d.sort_values("shipment_id").reset_index(drop=True)
    lane_ids = sorted(d.lane_id.unique())
    lane_name = d.drop_duplicates("lane_id").set_index("lane_id").lane_name.str.replace(" > ", " → ", regex=False)
    carriers = sorted(d.carrier_id.unique())
    carrier_names = d.drop_duplicates("carrier_id").set_index("carrier_id").carrier_name
    customers = sorted(d.customer_id.unique())
    modes = sorted(d["mode"].unique())
    months = sorted(d.ship_month.unique())

    def idx(col: str, values: list) -> list[int]:
        lookup = {v: i for i, v in enumerate(values)}
        return [lookup[v] for v in d[col]]

    def rounded(col: str, nd: int = 2) -> list[float]:
        return [round(float(v), nd) for v in d[col]]

    columns = {
        "month": idx("ship_month", months),
        "year": [int(y) for y in d.ship_year],
        "lane": idx("lane_id", lane_ids),
        "car": idx("carrier_id", carriers),
        "cus": idx("customer_id", customers),
        "mode": idx("mode", modes),
        "adr": [int(v) for v in d.is_adr],
        "kg": [int(v) for v in d.weight_kg],
        "tr": [int(v) for v in d.transit_days],
        "q": [int(v) for v in d.quoted_days],
        "late": [int(v) for v in d.days_late],
        "base": rounded("base_freight_eur"),
        "fuel": rounded("fuel_surcharge_eur"),
        "adrs": rounded("adr_surcharge_eur"),
        "cust": rounded("customs_fee_eur"),
        "hand": rounded("handling_eur"),
        "ins": rounded("insurance_eur"),
        "pen": rounded("late_penalty_eur"),
        "pc": rounded("product_cost_eur"),
        "rev": rounded("revenue_eur"),
    }
    names = {lid: lane_name.loc[lid] for lid in lane_ids}
    alloc = allocation.table.copy()
    alloc["label"] = [names[s.split(" ")[0]] + (" (dangerous goods)" if "dangerous" in s else "") for s in alloc.segment]
    return {
        "n": len(d),
        "lanes": [names[l] for l in lane_ids],
        "carriers": [carrier_names[c] for c in carriers],
        "carrier_ids": carriers,
        "customers": customers,
        "modes": modes,
        "months": months,
        **columns,
        "frontier": frontier.round(5).to_dict("records"),
        "allocation_summary": allocation.summary,
        "allocation": [
            {"segment": r.label, "carrier": carrier_names[r.carrier_id], "on_time": round(float(r.on_time), 4), "cost_per_kg": round(float(r.cost_per_kg), 4), "current": round(float(r.current_share), 4), "recommended": round(float(r.recommended_share), 4)}
            for r in alloc.itertuples()
        ],
    }


def build_dashboard(d: pd.DataFrame, allocation: Allocation, frontier: pd.DataFrame, out_path: Path, repo_url: str = REPO_URL) -> Path:
    html = TEMPLATE.read_text().replace(PLACEHOLDER, json.dumps(dashboard_payload(d, allocation, frontier), separators=(",", ":"))).replace("__REPO_URL__", repo_url)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    return out_path
