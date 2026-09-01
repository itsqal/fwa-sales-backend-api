-- Post-deployment health check for the FWA Sales backend.
--
-- Run this AFTER `alembic upgrade head` on any environment, to see what the release
-- actually did to your data and what still needs doing by hand.
--
--   docker compose -f docker-compose.prod.yml exec -T db psql -U postgres -d api_fwa_sales_dev -q < docs/healthcheck.sql
--
-- Read-only: it writes nothing and is safe to run on production at any time.
--
-- BEFORE migrating, run the pre-flight check in DEPLOYMENT.md section 12 instead —
-- it looks at the device catalogue, which migration 0011 writes to.

\echo '=============================================================='
\echo ' 1. Migration state  — expect 0011_seed_prices_and_mpx'
\echo '=============================================================='
SELECT version_num AS current_revision FROM alembic_version;

\echo ''
\echo '=============================================================='
\echo ' 2. AE data — must be UNCHANGED by this release'
\echo '=============================================================='
SELECT
    (SELECT count(*) FROM account_executive) AS account_executives,
    (SELECT count(*) FROM customer)          AS customers,
    (SELECT count(*) FROM activation)        AS activations,
    (SELECT count(*) FROM attendance)        AS attendance_records,
    (SELECT count(*) FROM incentive_ledger)  AS incentive_entries;

\echo ''
\echo '-- Inventory by status. Only these six are visible to the mobile app:'
\echo '-- ALLOCATED, ACTIVATED, CONSUMED, RETURNED, BLOCKED, AVAILABLE'
SELECT status,
       count(*)        AS units,
       count(imei)     AS with_imei,
       count(*) FILTER (WHERE allocated_ae_id IS NOT NULL) AS assigned_to_an_ae
FROM fwa_inventory
GROUP BY status
ORDER BY status;

\echo ''
\echo '=============================================================='
\echo ' 3. Reference data seeded by the migrations'
\echo '=============================================================='
SELECT 'device_partner' AS table_name, count(*) AS rows, 6 AS expected FROM device_partner
UNION ALL SELECT 'mpx',          count(*), 3 FROM mpx
UNION ALL SELECT 'call_plan',    count(*), 4 FROM call_plan
UNION ALL SELECT 'brand',        count(*), 2 FROM brand
UNION ALL SELECT 'device_model', count(*), 3 FROM device_model
ORDER BY table_name;

\echo ''
\echo '-- Device catalogue. Every model needs a PRICE (or it cannot be ordered)'
\echo '-- and a NETWORK GENERATION (or a confirmed activation pays the AE nothing).'
SELECT dm.model_code,
       dp.code                AS supplied_by,
       dm.list_price_idr,
       dm.network_generation,
       CASE
         WHEN dm.list_price_idr IS NULL AND dm.network_generation IS NULL
              THEN 'NEEDS PRICE + NETWORK GEN'
         WHEN dm.list_price_idr IS NULL     THEN 'NEEDS PRICE (cannot be ordered)'
         WHEN dm.network_generation IS NULL THEN 'NEEDS NETWORK GEN (pays no incentive)'
         ELSE 'ok'
       END AS action_required
FROM device_model dm
LEFT JOIN device_partner dp ON dp.device_partner_id = dm.device_partner_id
ORDER BY dm.model_code;

\echo ''
\echo '=============================================================='
\echo ' 4. Dashboard logins — ZERO means nobody can use the web app'
\echo '=============================================================='
SELECT u.role,
       u.username,
       u.full_name,
       COALESCE(dp.code, m.code, '(global read)') AS organisation,
       u.status
FROM admin_user u
LEFT JOIN device_partner dp ON dp.device_partner_id = u.device_partner_id
LEFT JOIN mpx m            ON m.mpx_id            = u.mpx_id
ORDER BY u.role, u.username;

\echo ''
\echo '=============================================================='
\echo ' 5. Every AE must point at an MPX that exists'
\echo '=============================================================='
\echo '-- Any row here is a salesman NO MPX admin can allocate stock to.'
SELECT ae.ae_code, ae.mpx_code AS points_at_missing_mpx
FROM account_executive ae
WHERE ae.mpx_code IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM mpx m WHERE m.code = ae.mpx_code)
ORDER BY ae.ae_code;

\echo ''
\echo '=============================================================='
\echo ' 6. Supply chain activity (all zero on a fresh deploy)'
\echo '=============================================================='
SELECT 'msisdn_po'        AS table_name, count(*) AS rows FROM msisdn_po
UNION ALL SELECT 'device_po',        count(*) FROM device_po
UNION ALL SELECT 'shipment',         count(*) FROM shipment
UNION ALL SELECT 'goods_receipt',    count(*) FROM goods_receipt
UNION ALL SELECT 'stock_allocation', count(*) FROM stock_allocation
UNION ALL SELECT 'address',          count(*) FROM address
ORDER BY table_name;

\echo ''
\echo '=============================================================='
\echo ' 7. Integrity — every row here is a problem'
\echo '=============================================================='
SELECT 'unpaired unit in a visible state' AS problem, count(*) AS rows
FROM fwa_inventory WHERE imei IS NULL AND status <> 'MSISDN_ISSUED'
UNION ALL
SELECT 'allocated to an AE but not ALLOCATED/CONSUMED/ACTIVATED', count(*)
FROM fwa_inventory
WHERE allocated_ae_id IS NOT NULL
  AND status NOT IN ('ALLOCATED','CONSUMED','ACTIVATED','RETURNED','BLOCKED','AVAILABLE')
UNION ALL
SELECT 'ALLOCATED but no allocation record', count(*)
FROM fwa_inventory i
WHERE i.status = 'ALLOCATED'
  AND i.msisdn_po_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM stock_allocation_item a WHERE a.msisdn = i.msisdn)
UNION ALL
SELECT 'device model with no supplying partner', count(*)
FROM device_model WHERE device_partner_id IS NULL;
