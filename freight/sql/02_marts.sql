-- Layer 2: business logic and marts.
--   int_shipments   one row per shipment with the derived measures (lateness, landed cost, margin)
--   mart_shipments  int_shipments joined to the lane, carrier, customer and fuel dimensions
--   mart_*          aggregates for analysis and the dashboard
-- Business rules live here, in one place:
--   on time        delivered no later than the quoted transit days
--   late credit    late_penalty_per_day x revenue for each day late, capped at penalty_cap_days
--   landed cost    product + freight + customs + handling + insurance + late credit

DROP TABLE IF EXISTS int_shipments;
CREATE TABLE int_shipments AS
WITH base AS (
    SELECT
        st.*,
        l.base_days,
        CASE WHEN st.delivered_date IS NULL THEN NULL
             ELSE CAST(JULIANDAY(st.delivered_date) - JULIANDAY(st.ship_date) AS INTEGER) END AS transit_days
    FROM stg_shipments st
    JOIN raw_lanes l ON l.lane_id = st.lane_id
),
timed AS (
    SELECT
        *,
        CASE WHEN transit_days IS NULL THEN 0 ELSE 1 END AS is_delivered,
        CASE WHEN transit_days IS NULL THEN NULL
             WHEN transit_days > quoted_days THEN 1 ELSE 0 END AS is_late,
        CASE WHEN transit_days IS NULL THEN NULL
             ELSE MAX(0, MIN((SELECT value FROM ref_params WHERE name = 'penalty_cap_days'),
                             transit_days - quoted_days)) END AS days_late,
        transit_days - base_days AS excess_days,        -- days beyond the normal transit time of the lane
        quoted_days - transit_days AS quote_error_days,  -- positive = quoted more time than was needed
        base_freight_eur + fuel_surcharge_eur + adr_surcharge_eur + customs_fee_eur + handling_eur AS freight_cost_eur
    FROM base
)
SELECT
    *,
    CASE WHEN is_delivered = 0 THEN NULL
         ELSE revenue_eur * (SELECT value FROM ref_params WHERE name = 'late_penalty_per_day') * days_late END AS late_penalty_eur
FROM timed;

DROP TABLE IF EXISTS mart_shipments;
CREATE TABLE mart_shipments AS
SELECT
    i.*,
    SUBSTR(i.ship_date, 1, 7)                       AS ship_month,
    CAST(SUBSTR(i.ship_date, 1, 4) AS INTEGER)      AS ship_year,
    l.origin || ' > ' || l.destination              AS lane_name,
    l.mode,
    car.carrier_name,
    car.adr_certified,
    cu.segment                                      AS customer_segment,
    f.fuel_index,
    i.freight_cost_eur / i.weight_kg                AS freight_per_kg,
    CASE WHEN i.is_delivered = 1 THEN i.freight_cost_eur + i.insurance_eur + i.late_penalty_eur END AS logistics_cost_eur,
    CASE WHEN i.is_delivered = 1 THEN i.product_cost_eur + i.freight_cost_eur + i.insurance_eur + i.late_penalty_eur END AS landed_cost_eur
FROM int_shipments i
JOIN raw_lanes l     ON l.lane_id = i.lane_id
JOIN raw_carriers car ON car.carrier_id = i.carrier_id
JOIN raw_customers cu ON cu.customer_id = i.customer_id
JOIN raw_fuel_index f ON f.month = SUBSTR(i.ship_date, 1, 7);

-- margin is only defined once the shipment has been delivered and any late credit is known
DROP TABLE IF EXISTS mart_margin;
CREATE TABLE mart_margin AS
SELECT
    shipment_id,
    revenue_eur - landed_cost_eur                       AS margin_eur,
    (revenue_eur - landed_cost_eur) / revenue_eur       AS margin_pct
FROM mart_shipments
WHERE is_delivered = 1;

-- carrier x lane x dangerous-goods segment: reliability, price, and the price once late credits are included
DROP TABLE IF EXISTS mart_carrier_lane;
CREATE TABLE mart_carrier_lane AS
WITH agg AS (
    SELECT
        lane_id, carrier_id, is_adr,
        COUNT(*)                                            AS shipments,
        SUM(weight_kg)                                      AS kg,
        AVG(1 - is_late)                                    AS on_time_rate,
        AVG(excess_days)                                    AS avg_excess_days,
        SUM(freight_cost_eur) / SUM(weight_kg)              AS freight_per_kg,
        SUM(late_penalty_eur) / SUM(weight_kg)              AS penalty_per_kg,
        (SUM(freight_cost_eur) + SUM(late_penalty_eur)) / SUM(weight_kg) AS net_cost_per_kg
    FROM mart_shipments
    WHERE is_delivered = 1
    GROUP BY lane_id, carrier_id, is_adr
)
SELECT
    *,
    RANK() OVER (PARTITION BY lane_id, is_adr ORDER BY freight_per_kg)  AS rank_by_freight,
    RANK() OVER (PARTITION BY lane_id, is_adr ORDER BY net_cost_per_kg) AS rank_by_net_cost
FROM agg;

-- month by month, with a rolling three-month on-time rate
DROP TABLE IF EXISTS mart_monthly;
CREATE TABLE mart_monthly AS
WITH m AS (
    SELECT
        ship_month,
        COUNT(*)                                AS shipments,
        AVG(1 - is_late)                        AS on_time_rate,
        SUM(freight_cost_eur) / SUM(weight_kg)  AS freight_per_kg,
        AVG(fuel_index)                         AS fuel_index
    FROM mart_shipments
    WHERE is_delivered = 1
    GROUP BY ship_month
)
SELECT
    *,
    AVG(on_time_rate) OVER (ORDER BY ship_month ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS on_time_rate_3m
FROM m;

-- customers ranked by revenue, with share of total and margin
DROP TABLE IF EXISTS mart_customer;
CREATE TABLE mart_customer AS
WITH agg AS (
    SELECT
        s.customer_id,
        COUNT(*)                                        AS shipments,
        SUM(s.revenue_eur)                              AS revenue_eur,
        SUM(m.margin_eur)                               AS margin_eur,
        SUM(m.margin_eur) / SUM(s.revenue_eur)          AS margin_pct
    FROM mart_shipments s
    JOIN mart_margin m ON m.shipment_id = s.shipment_id
    GROUP BY s.customer_id
)
SELECT
    *,
    revenue_eur / SUM(revenue_eur) OVER ()              AS revenue_share,
    RANK() OVER (ORDER BY revenue_eur DESC)             AS revenue_rank
FROM agg;
