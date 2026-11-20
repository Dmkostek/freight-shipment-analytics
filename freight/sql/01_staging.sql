-- Layer 1: staging. Clean the raw feeds into one typed row per shipment.
--   * drop duplicate rows (the raw feeds repeat some shipments)
--   * standardise carrier names ("carrier a", "CARRIER A ") to the carrier id
--   * join each shipment to its cost record

DROP TABLE IF EXISTS stg_shipments;
CREATE TABLE stg_shipments AS
WITH ship AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY shipment_id ORDER BY ship_date) AS rn
    FROM raw_shipments
),
cost AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY shipment_id ORDER BY shipment_id) AS rn
    FROM raw_shipment_costs
)
SELECT
    s.shipment_id,
    DATE(s.ship_date)       AS ship_date,
    DATE(s.delivered_date)  AS delivered_date,      -- NULL while the shipment is still in transit
    s.lane_id,
    car.carrier_id,
    s.customer_id,
    s.product_category,
    s.weight_kg,
    s.pallets,
    s.is_adr,
    s.quoted_days,
    c.base_freight_eur,
    c.fuel_surcharge_eur,
    c.adr_surcharge_eur,
    c.customs_fee_eur,
    c.handling_eur,
    c.insurance_eur,
    c.product_cost_eur,
    c.revenue_eur
FROM ship s
JOIN cost c
  ON c.shipment_id = s.shipment_id AND c.rn = 1
LEFT JOIN raw_carriers car
  ON UPPER(TRIM(s.carrier_name)) = UPPER(car.carrier_name)
WHERE s.rn = 1;
