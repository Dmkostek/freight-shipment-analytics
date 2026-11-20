-- Data-quality checks. Each block returns one number: the count of violating rows.
-- The pipeline stops if any check returns more than zero.

-- check: shipment_id_is_unique
SELECT COUNT(*) - COUNT(DISTINCT shipment_id) FROM stg_shipments;

-- check: every_carrier_name_resolves
SELECT COUNT(*) FROM stg_shipments WHERE carrier_id IS NULL;

-- check: every_lane_exists
SELECT COUNT(*) FROM stg_shipments WHERE lane_id NOT IN (SELECT lane_id FROM raw_lanes);

-- check: every_customer_exists
SELECT COUNT(*) FROM stg_shipments WHERE customer_id NOT IN (SELECT customer_id FROM raw_customers);

-- check: no_shipment_lost_in_cleaning
SELECT (SELECT COUNT(DISTINCT shipment_id) FROM raw_shipments) - (SELECT COUNT(*) FROM stg_shipments);

-- check: money_is_non_negative_and_revenue_positive
SELECT COUNT(*) FROM stg_shipments
WHERE base_freight_eur < 0 OR fuel_surcharge_eur < 0 OR adr_surcharge_eur < 0 OR customs_fee_eur < 0
   OR handling_eur < 0 OR insurance_eur < 0 OR product_cost_eur <= 0 OR revenue_eur <= 0;

-- check: weight_and_quote_are_positive
SELECT COUNT(*) FROM stg_shipments WHERE weight_kg <= 0 OR pallets <= 0 OR quoted_days < 1;

-- check: delivery_is_not_before_shipping
SELECT COUNT(*) FROM stg_shipments WHERE delivered_date IS NOT NULL AND delivered_date < ship_date;

-- check: only_recent_shipments_are_in_transit
SELECT COUNT(*) FROM stg_shipments
WHERE delivered_date IS NULL
  AND ship_date < DATE((SELECT value_text FROM ref_params WHERE name = 'as_of'), '-10 days');

-- check: dangerous_goods_only_with_certified_carriers
SELECT COUNT(*) FROM mart_shipments WHERE is_adr = 1 AND adr_certified = 0;

-- check: carrier_serves_the_lane_mode
SELECT COUNT(*) FROM mart_shipments m
JOIN raw_carriers c ON c.carrier_id = m.carrier_id
WHERE INSTR(',' || c.modes || ',', ',' || m.mode || ',') = 0;

-- check: margin_identity_holds
SELECT COUNT(*) FROM mart_shipments s
JOIN mart_margin m ON m.shipment_id = s.shipment_id
WHERE ABS(s.revenue_eur - s.landed_cost_eur - m.margin_eur) > 0.01;

-- check: marts_keep_every_staged_shipment
SELECT (SELECT COUNT(*) FROM stg_shipments) - (SELECT COUNT(*) FROM mart_shipments);
