"""Synthetic data for a fictional cross-border chemical freight network.

Everything here is **simulated**: the places, carriers, customers, prices and
delays are all invented, and no real company or route is described. The generator plants a handful of
patterns so that the analysis has something real to find, and so that the tests
can check the analysis recovers them:

1. Carrier C is the cheapest per kilogram but the least reliable; once
   late-delivery credits are counted it is not the cheapest on several lanes.
2. Carrier D quotes optimistic transit times, so its "on time" rate is low
   even though its real transit times are ordinary.
3. Carrier E pads its quotes by a day, so it looks very reliable.
4. Dangerous-goods (ADR) shipments can only travel with certified carriers
   (A, B, D, E) and cost more.
5. Customs holds peak in December, January and July.
6. One large customer buys about a quarter of the volume at a lower markup.
7. The Rheinhafen to Vestmark lane is the lowest-margin lane.

The raw tables are deliberately a little messy (duplicate rows, inconsistent
carrier spellings, shipments still in transit) so that the warehouse layer has
real cleaning to do.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

LANES = pd.DataFrame(
    [
        # lane, origin, destination, mode, km, base days, EUR/kg, customs fee, delay multiplier, share of volume
        ("L1", "Rheinhafen", "Nordfjell", "Road", 1050, 3, 0.085, 75, 1.0, 0.24),
        ("L2", "Elbmark", "Nordfjell", "Road", 1300, 4, 0.100, 75, 1.0, 0.16),
        ("L3", "Saarfeld", "Solvik", "Road", 1450, 4, 0.105, 80, 1.0, 0.14),
        ("L4", "Rheinhafen", "Vestmark", "Road", 1500, 5, 0.130, 85, 1.25, 0.10),
        ("L5", "Weserburg", "Nordfjell", "Ferry", 1100, 2, 0.090, 70, 0.8, 0.20),
        ("L6", "Weserburg", "Brennvik", "Sea", 1200, 6, 0.070, 140, 1.3, 0.16),
    ],
    columns=["lane_id", "origin", "destination", "mode", "distance_km", "base_days", "rate_eur_per_kg", "customs_fee_eur", "delay_mult", "volume_share"],
)

CARRIERS = pd.DataFrame(
    [
        # carrier, modes, ADR certified, price factor, P(delay event), customs-hold multiplier, quote slack days, P(optimistic quote)
        ("A", "Road,Ferry", 1, 1.15, 0.05, 0.7, 0, 0.00),
        ("B", "Road", 1, 1.00, 0.10, 1.0, 0, 0.00),
        ("C", "Road,Ferry", 0, 0.82, 0.30, 1.2, 0, 0.00),
        ("D", "Road,Sea", 1, 0.95, 0.10, 1.0, 0, 0.25),
        ("E", "Ferry,Sea", 1, 1.05, 0.08, 0.8, 1, 0.00),
        ("F", "Road,Ferry", 0, 0.90, 0.15, 1.0, 0, 0.00),
    ],
    columns=["carrier_id", "modes", "adr_certified", "price_factor", "delay_prob", "hold_mult", "quote_slack_days", "optimistic_quote_prob"],
)
CARRIERS["carrier_name"] = ["Kestrel Haulage", "Lindgren & Voss", "Dagstrom Direct", "Meridian Cross-Border", "Halcyon Marine Cargo", "Alderwood Transport"]

# category, cost EUR/kg, P(dangerous goods), share of shipments
CATEGORIES = pd.DataFrame(
    [
        ("Solvents", 1.4, 0.75, 0.25),
        ("Coatings", 3.2, 0.30, 0.25),
        ("Adhesives", 2.6, 0.25, 0.20),
        ("Additives", 4.5, 0.15, 0.15),
        ("Cleaning agents", 1.9, 0.10, 0.15),
    ],
    columns=["category", "cost_eur_per_kg", "adr_prob", "share"],
)

HOLD_SEASON = {12: 1.6, 1: 1.4, 7: 1.2}  # customs holds are more likely in these months
VOLUME_SEASON = {7: 0.75, 8: 0.9, 12: 0.8}
HANDLING_PER_PALLET = {"Road": 6.0, "Ferry": 7.5, "Sea": 11.0}


@dataclass(frozen=True)
class Assumptions:
    start: str = "2023-01-02"
    end: str = "2024-12-31"
    shipments_per_day: float = 14.0
    n_customers: int = 40
    top_customer_share: float = 0.24
    hold_base_prob: float = 0.08
    late_penalty_per_day: float = 0.010  # share of revenue credited per day late (capped at 5 days); applied in SQL
    seed: int = 7


def _fuel_index(months: pd.PeriodIndex, rng: np.random.Generator) -> pd.DataFrame:
    idx, level = [], 105.0
    for _ in months:
        level = 100 + 0.85 * (level - 100) + rng.normal(0, 4)
        idx.append(float(np.clip(level, 85, 130)))
    return pd.DataFrame({"month": months.astype(str), "fuel_index": np.round(idx, 1)})


def simulate(a: Assumptions = Assumptions()) -> dict[str, pd.DataFrame]:
    """Return the raw tables as DataFrames, keyed by table name."""
    rng = np.random.default_rng(a.seed)
    days = pd.bdate_range(a.start, a.end)
    growth = np.linspace(1.0, 1.10, len(days))
    season = np.array([VOLUME_SEASON.get(d.month, 1.0) for d in days])
    counts = rng.poisson(a.shipments_per_day * season * growth)
    ship_date = np.repeat(days.to_numpy(), counts)
    n = len(ship_date)
    month = pd.DatetimeIndex(ship_date).month.to_numpy()

    # lane, category, dangerous goods, customer, weight
    lane_idx = rng.choice(len(LANES), n, p=LANES.volume_share.to_numpy())
    lane = LANES.iloc[lane_idx].reset_index(drop=True)
    cat_idx = rng.choice(len(CATEGORIES), n, p=CATEGORIES.share.to_numpy())
    cat = CATEGORIES.iloc[cat_idx].reset_index(drop=True)
    adr = (rng.random(n) < cat.adr_prob.to_numpy()).astype(int)
    rest = (1 - a.top_customer_share) * rng.dirichlet(np.full(a.n_customers - 1, 2.0))
    cust_p = np.r_[a.top_customer_share, rest]
    cust_idx = rng.choice(a.n_customers, n, p=cust_p)
    weight = np.clip(rng.lognormal(np.log(6000), 0.6, n), 800, 24000).round(-1)
    pallets = np.ceil(weight / 550).astype(int)

    # carrier choice: cheaper carriers win more volume; ADR needs a certified carrier
    lane_noise = np.exp(rng.normal(0, 0.25, (len(LANES), len(CARRIERS))))
    carrier_idx = np.empty(n, dtype=int)
    for li, l in LANES.iterrows():
        serves = CARRIERS.modes.str.split(",").apply(lambda m: l["mode"] in m).to_numpy()
        for flag in (0, 1):
            ok = serves & ((CARRIERS.adr_certified.to_numpy() == 1) if flag else True)
            w = np.where(ok, CARRIERS.price_factor.to_numpy() ** -2.0 * lane_noise[li], 0.0)
            sel = np.flatnonzero((lane_idx == li) & (adr == flag))
            if sel.size:
                carrier_idx[sel] = rng.choice(len(CARRIERS), sel.size, p=w / w.sum())
    car = CARRIERS.iloc[carrier_idx].reset_index(drop=True)

    # transit: base days + delay events + customs holds; quote = base + slack (maybe optimistic)
    base_days = lane.base_days.to_numpy()
    event = rng.random(n) < np.clip(car.delay_prob.to_numpy() * lane.delay_mult.to_numpy(), 0, 0.9)
    event_days = event * (1 + rng.poisson(0.6, n))
    hold_p = a.hold_base_prob * car.hold_mult.to_numpy() * np.array([HOLD_SEASON.get(m, 1.0) for m in month])
    hold = rng.random(n) < hold_p
    hold_days = hold * (1 + (rng.random(n) < 0.4))
    actual = base_days + event_days + hold_days
    optimistic = rng.random(n) < car.optimistic_quote_prob.to_numpy()
    quoted = np.maximum(1, base_days + car.quote_slack_days.to_numpy() - optimistic)
    delivered = pd.DatetimeIndex(ship_date) + pd.to_timedelta(actual, unit="D")
    as_of = pd.Timestamp(a.end)
    delivered = pd.Series(delivered).where(delivered <= as_of)  # still in transit otherwise

    # money
    fuel = _fuel_index(pd.period_range(a.start, a.end, freq="M"), rng)
    fuel_lookup = fuel.set_index("month").fuel_index
    fuel_idx = fuel_lookup.loc[pd.DatetimeIndex(ship_date).to_period("M").astype(str)].to_numpy()
    base_freight = np.maximum(180, weight * lane.rate_eur_per_kg.to_numpy() * car.price_factor.to_numpy() * rng.lognormal(0, 0.04, n))
    fuel_sur = base_freight * 0.10 * (fuel_idx / 100) ** 1.5
    adr_sur = np.where(adr == 1, np.maximum(60, 0.15 * base_freight), 0.0)
    customs = lane.customs_fee_eur.to_numpy() * rng.lognormal(0, 0.08, n)
    handling = pallets * lane["mode"].map(HANDLING_PER_PALLET).to_numpy()
    product_cost = weight * cat.cost_eur_per_kg.to_numpy()
    insurance = 0.0012 * product_cost
    cust_markup = np.r_[0.22, np.clip(rng.normal(0.30, 0.04, a.n_customers - 1), 0.2, 0.4)]
    revenue = product_cost * (1 + cust_markup[cust_idx])

    shipment_id = np.array([f"S{i:06d}" for i in range(1, n + 1)])
    carrier_label = car.carrier_name.to_numpy().astype(object)
    messy = rng.random(n) < 0.08  # inconsistent spellings in the raw feed
    variants = np.array([lambda s: s.lower(), lambda s: s.upper() + " ", lambda s: " " + s])
    for i in np.flatnonzero(messy):
        carrier_label[i] = variants[rng.integers(0, 3)](carrier_label[i])

    shipments = pd.DataFrame(
        {
            "shipment_id": shipment_id,
            "ship_date": pd.DatetimeIndex(ship_date).strftime("%Y-%m-%d"),
            "delivered_date": delivered.dt.strftime("%Y-%m-%d"),
            "lane_id": lane.lane_id.to_numpy(),
            "carrier_name": carrier_label,
            "customer_id": [f"C{c + 1:03d}" for c in cust_idx],
            "product_category": cat.category.to_numpy(),
            "weight_kg": weight,
            "pallets": pallets,
            "is_adr": adr,
            "quoted_days": quoted,
        }
    )
    costs = pd.DataFrame(
        {
            "shipment_id": shipment_id,
            "base_freight_eur": base_freight.round(2),
            "fuel_surcharge_eur": fuel_sur.round(2),
            "adr_surcharge_eur": adr_sur.round(2),
            "customs_fee_eur": customs.round(2),
            "handling_eur": handling.round(2),
            "insurance_eur": insurance.round(2),
            "product_cost_eur": product_cost.round(2),
            "revenue_eur": revenue.round(2),
        }
    )
    dup = rng.random(n) < 0.006  # duplicated rows in both feeds
    shipments = pd.concat([shipments, shipments[dup]], ignore_index=True)
    costs = pd.concat([costs, costs[dup]], ignore_index=True)

    customers = pd.DataFrame({"customer_id": [f"C{c + 1:03d}" for c in range(a.n_customers)], "segment": rng.choice(["Industrial", "Construction", "Retail", "Automotive"], a.n_customers)})
    return {
        "raw_shipments": shipments,
        "raw_shipment_costs": costs,
        "raw_lanes": LANES.drop(columns=["volume_share"]).copy(),
        "raw_carriers": CARRIERS[["carrier_id", "carrier_name", "modes", "adr_certified"]].copy(),
        "raw_customers": customers,
        "raw_fuel_index": fuel,
    }


def write_csv(tables: dict[str, pd.DataFrame], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_csv(out_dir / f"{name}.csv", index=False)
