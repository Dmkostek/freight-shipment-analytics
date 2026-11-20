import json
import re

import numpy as np
import pandas as pd
import pytest

from freight import analytics as an
from freight import optimize as op
from freight.dashboard import build_dashboard, dashboard_payload
from freight.run import main as run_pipeline
from freight.simulate import CARRIERS, LANES, Assumptions, simulate
from freight.warehouse import DataQualityError, assert_quality, build_warehouse, read_table, run_checks

A = Assumptions()


@pytest.fixture(scope="module")
def tables():
    return simulate(A)


@pytest.fixture(scope="module")
def conn(tables):
    return build_warehouse(tables, A)


@pytest.fixture(scope="module")
def d(conn):
    return an.load_delivered(conn)


@pytest.fixture(scope="module")
def carrier_lane(conn):
    return read_table(conn, "mart_carrier_lane")


# ---------------------------------------------------------------- simulation
def test_simulation_is_deterministic_and_seeded(tables):
    again = simulate(A)
    pd.testing.assert_frame_equal(tables["raw_shipments"], again["raw_shipments"])
    other = simulate(Assumptions(seed=A.seed + 1))
    assert not tables["raw_shipments"].shipment_id.equals(other["raw_shipments"].shipment_id) or len(tables["raw_shipments"]) != len(other["raw_shipments"])


def test_raw_tables_have_the_expected_shape(tables):
    assert set(tables) == {"raw_shipments", "raw_shipment_costs", "raw_lanes", "raw_carriers", "raw_customers", "raw_fuel_index"}
    s = tables["raw_shipments"]
    assert 6000 < s.shipment_id.nunique() < 9000
    assert len(s) > s.shipment_id.nunique()  # duplicated rows are present on purpose
    assert s.carrier_name.nunique() > len(CARRIERS)  # inconsistent spellings are present on purpose
    assert s.delivered_date.isna().sum() > 0  # shipments still in transit


# ---------------------------------------------------------------- warehouse
def test_all_data_quality_checks_pass(conn):
    results = assert_quality(conn)
    assert len(results) >= 12 and not any(results.values())


def test_cleaning_removes_duplicates_and_standardises_carriers(tables, conn):
    stg = read_table(conn, "stg_shipments")
    assert len(stg) == tables["raw_shipments"].shipment_id.nunique()
    assert stg.shipment_id.is_unique
    assert set(stg.carrier_id) == set(CARRIERS.carrier_id)
    assert stg.carrier_id.notna().all()
    assert stg.revenue_eur.sum() == pytest.approx(tables["raw_shipment_costs"].drop_duplicates("shipment_id").revenue_eur.sum())


def test_in_transit_shipments_have_no_transit_metrics(conn):
    m = read_table(conn, "mart_shipments")
    transit = m[m.is_delivered == 0]
    assert len(transit) > 0
    assert transit.transit_days.isna().all() and transit.is_late.isna().all() and transit.late_penalty_eur.isna().all()


def _corrupt(table, mutate):
    """Return a function that copies the raw tables and applies ``mutate`` to one of them."""

    def make(tables):
        t = {k: v.copy() for k, v in tables.items()}
        mutate(t[table])
        return t

    return make


def _negative_freight(df):
    df.loc[5, "base_freight_eur"] = -10.0


def _delivered_before_shipped(df):
    df.loc[7, "delivered_date"] = "2000-01-01"


def _unknown_lane(df):
    df.loc[3, "lane_id"] = "L99"


def _unknown_carrier(df):
    df.loc[4, "carrier_name"] = "Unknown Freight Ltd"


def _dangerous_goods_on_uncertified_carrier(df):
    df.loc[(df.index < 60) & (df.is_adr == 1), "carrier_name"] = "Dagstrom Direct"


@pytest.mark.parametrize(
    "check,corruption",
    [
        ("money_is_non_negative_and_revenue_positive", _corrupt("raw_shipment_costs", _negative_freight)),
        ("delivery_is_not_before_shipping", _corrupt("raw_shipments", _delivered_before_shipped)),
        ("every_lane_exists", _corrupt("raw_shipments", _unknown_lane)),
        ("every_carrier_name_resolves", _corrupt("raw_shipments", _unknown_carrier)),
        ("dangerous_goods_only_with_certified_carriers", _corrupt("raw_shipments", _dangerous_goods_on_uncertified_carrier)),
    ],
)
def test_checks_catch_injected_problems(tables, check, corruption):
    bad = build_warehouse(corruption(tables), A)
    results = run_checks(bad)
    assert results[check] > 0
    with pytest.raises(DataQualityError, match=check):
        assert_quality(bad)


def test_sql_results_match_an_independent_pandas_calculation(tables, d):
    """Recompute on-time rates and late credits without the warehouse and compare."""
    s = tables["raw_shipments"].drop_duplicates("shipment_id").copy()
    c = tables["raw_shipment_costs"].drop_duplicates("shipment_id")
    by_name = {n.upper(): i for i, n in zip(CARRIERS.carrier_id, CARRIERS.carrier_name)}
    s["carrier_id"] = s.carrier_name.str.strip().str.upper().map(by_name)
    s = s.merge(c, on="shipment_id").dropna(subset=["delivered_date"])
    s["transit"] = (pd.to_datetime(s.delivered_date) - pd.to_datetime(s.ship_date)).dt.days
    s["late"] = (s.transit > s.quoted_days).astype(int)
    s["days_late"] = (s.transit - s.quoted_days).clip(lower=0, upper=5)
    s["penalty"] = s.revenue_eur * A.late_penalty_per_day * s.days_late
    expected = 1 - s.groupby("carrier_id").late.mean()
    got = an.carrier_scorecard(d).set_index("carrier_id").on_time_rate
    pd.testing.assert_series_equal(got.sort_index(), expected.sort_index(), check_names=False, atol=1e-12)
    assert d.late_penalty_eur.sum() == pytest.approx(s.penalty.sum())
    assert len(d) == len(s)


def test_landed_cost_and_margin_are_consistent(d):
    parts = d.product_cost_eur + d.freight_cost_eur + d.insurance_eur + d.late_penalty_eur
    np.testing.assert_allclose(d.landed_cost_eur, parts)
    np.testing.assert_allclose(d.margin_eur, d.revenue_eur - d.landed_cost_eur)


# ---------------------------------------------------------------- planted patterns
def test_dangerous_goods_only_travel_with_certified_carriers(d):
    certified = set(CARRIERS.loc[CARRIERS.adr_certified == 1, "carrier_id"])
    assert set(d[d.is_adr == 1].carrier_id) <= certified


def test_carriers_only_serve_modes_they_offer(d):
    modes = CARRIERS.set_index("carrier_id").modes.str.split(",")
    assert all(row["mode"] in modes[row.carrier_id] for _, row in d[["carrier_id", "mode"]].drop_duplicates().iterrows())


def test_carrier_c_is_cheapest_per_kg_but_unreliable(d):
    sc = an.carrier_scorecard(d).set_index("carrier_id")
    assert sc.freight_per_kg.idxmin() == "C"
    assert sc.on_time_rate["C"] < 0.7
    assert sc.on_time_rate["E"] > 0.9 and sc.on_time_rate["A"] > 0.9


def test_carrier_d_quotes_optimistically_and_e_pads(d):
    sc = an.carrier_scorecard(d).set_index("carrier_id")
    assert sc.on_time_rate.idxmin() == "D"
    assert sc.quote_error_days["E"] > 0.5
    assert sc.quote_error_days["D"] < sc.quote_error_days["B"]
    # D's real delays are ordinary: its excess transit days are no worse than carrier B's
    assert sc.avg_excess_days["D"] <= sc.avg_excess_days["B"] + 0.05


def test_cheapest_price_is_not_the_cheapest_carrier(carrier_lane):
    changes = an.cheapest_carrier_changes(carrier_lane)
    assert changes.changes.sum() >= 5 and len(changes) == len(LANES)


def test_customs_holds_make_peak_months_later(d):
    s = an.seasonality(d)
    assert s["late_share_hold_months"] > s["late_share_other_months"] + 0.02
    assert s["p_value"] < 0.01


def test_largest_customer_is_a_fifth_of_revenue_at_a_lower_margin(conn, d):
    cust = read_table(conn, "mart_customer").sort_values("revenue_rank")
    assert 0.18 < cust.revenue_share.iloc[0] < 0.28
    top = d[d.customer_id == cust.customer_id.iloc[0]]
    rest = d[d.customer_id != cust.customer_id.iloc[0]]
    assert top.margin_eur.sum() / top.revenue_eur.sum() < rest.margin_eur.sum() / rest.revenue_eur.sum() - 0.03


def test_bergen_is_the_lowest_margin_lane(d):
    lanes = an.margin_by(d, "lane_name").sort_values("margin_pct")
    assert lanes.iloc[0].lane_name == "Rheinhafen > Vestmark"


# ---------------------------------------------------------------- optimiser
@pytest.fixture(scope="module")
def options(d):
    return op.segment_options(d)


def test_allocation_is_a_valid_split_that_beats_the_current_mix(options):
    al = op.optimize_allocation(options, on_time_target=0.85, max_carrier_share=0.45)
    shares = al.table.groupby("segment").recommended_share.sum()
    np.testing.assert_allclose(shares, 1.0)
    assert (al.table.recommended_share >= -1e-12).all()
    s = al.summary
    assert s["optimized_cost_eur"] <= s["baseline_cost_eur"] + 1e-6
    assert s["optimized_on_time"] >= s["baseline_on_time"]


def test_allocation_respects_the_on_time_target_and_carrier_cap(options):
    target, cap = 0.85, 0.45
    al = op.optimize_allocation(options, target, cap)
    t = al.table.merge(options[["segment", "carrier_id", "segment_kg"]], on=["segment", "carrier_id"])
    for seg, g in t.groupby("segment"):
        achievable = min(target, g.on_time.max())
        assert (g.recommended_share * g.on_time).sum() >= achievable - 1e-9
    total = options.groupby("segment").segment_kg.first().sum()
    by_carrier = (t.recommended_share * t.segment_kg).groupby(t.carrier_id).sum() / total
    assert by_carrier.max() <= cap + 1e-9


def test_allocation_raises_when_no_split_is_possible(options):
    with pytest.raises(op.InfeasibleAllocation):
        op.optimize_allocation(options, on_time_target=0.85, max_carrier_share=0.10)


def test_price_of_reliability_never_falls_as_the_target_rises(options):
    f = op.price_of_reliability(options)
    assert len(f) > 8
    assert (np.diff(f.cost_per_kg) >= -1e-9).all()
    assert (np.diff(f.on_time) >= -1e-9).all()


# ---------------------------------------------------------------- dashboard and pipeline
def test_dashboard_embeds_every_delivered_shipment(tmp_path, d, options):
    al, fr = op.optimize_allocation(options), op.price_of_reliability(options)
    path = build_dashboard(d, al, fr, tmp_path / "index.html", repo_url="https://example.com/repo")
    html = path.read_text()
    assert "__DATA__" not in html and "__REPO_URL__" not in html and "https://example.com/repo" in html
    assert 'src="plotly.min.js"' in html and "cdn.plot.ly" not in html  # the chart library ships with the page
    data = json.loads(re.search(r"const D = (\{.*?\});\n", html, re.S).group(1))
    assert data["n"] == len(d)
    for col in ("month", "lane", "car", "mode", "adr", "kg", "tr", "q", "late", "base", "rev"):
        assert len(data[col]) == len(d)
    assert data["carriers"] == list(CARRIERS.carrier_name) and len(data["lanes"]) == len(LANES)
    assert path.stat().st_size < 3_000_000


def test_dashboard_payload_matches_the_scorecard(d, options):
    p = dashboard_payload(d, op.optimize_allocation(options), op.price_of_reliability(options))
    late = np.array(p["late"])
    car = np.array(p["car"])
    sc = an.carrier_scorecard(d).set_index("carrier_id")
    for i, c in enumerate(p["carrier_ids"]):
        assert (late[car == i] == 0).mean() == pytest.approx(sc.on_time_rate[c])


def test_full_pipeline_writes_every_output(tmp_path):
    run_pipeline(["--out", str(tmp_path)])
    for rel in ("docs/index.html", "results/RESULTS.md", "results/findings.json", "results/carrier_scorecard.csv", "figures/carrier_scorecard.png", "figures/freight_vs_net_cost.png", "data/freight.db"):
        assert (tmp_path / rel).exists(), rel
    findings = json.loads((tmp_path / "results/findings.json").read_text())
    assert findings["lanes_where_cheapest_changes"] >= 5
    assert findings["optimization"]["saving_eur"] > 0


FICTIONAL_PLACES = {"Rheinhafen", "Elbmark", "Saarfeld", "Weserburg", "Nordfjell", "Solvik", "Vestmark", "Brennvik"}


def test_the_network_is_entirely_fictional(tmp_path, tables, d):
    """Every place in the data and on the dashboard is one of the invented names."""
    lanes = tables["raw_lanes"]
    assert set(lanes.origin) | set(lanes.destination) == FICTIONAL_PLACES
    opts = op.segment_options(d)
    path = build_dashboard(d, op.optimize_allocation(opts), op.price_of_reliability(opts), tmp_path / "i.html")
    payload = json.loads(re.search(r"const D = (\{.*?\});\n", path.read_text(), re.S).group(1))
    shown = {place for lane in payload["lanes"] for place in lane.split(" \u2192 ")}
    assert shown == FICTIONAL_PLACES
