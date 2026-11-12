-- =====================================================================
-- locations.sql - places.
--
-- A location's identity is coordinates AND name. Two shops a few metres
-- apart must stay separate, and the same storefront jittering by a few
-- metres between visits must not become two rows - so matching is by
-- proximity within a radius, and only when the place name agrees.
--     make psql < db/scripts/locations.sql
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. How much spending has a place attached. Read every map against
--    this number - the map can only show what carries coordinates.
-- ---------------------------------------------------------------------
SELECT * FROM v_location_coverage;


-- ---------------------------------------------------------------------
-- 2. Where the money goes. One row per place however often you visit.
-- ---------------------------------------------------------------------
SELECT place_label AS place, city, state, visits,
       total_outflow, avg_outflow, popularity
FROM v_location_spend
ORDER BY total_outflow DESC NULLS LAST
LIMIT 25;

-- Rolled up by city, which is usually the more useful cut for travel.
SELECT city, state, transactions, total_outflow
FROM v_city_spend
ORDER BY total_outflow DESC NULLS LAST
LIMIT 25;


-- ---------------------------------------------------------------------
-- 3. One chain's storefronts - where you actually shop, not just which
--    brand you spend most with.
-- ---------------------------------------------------------------------
SELECT merchant, place, city, state, visits, total_outflow
FROM v_merchant_locations
ORDER BY merchant, total_outflow DESC
LIMIT 30;


-- ---------------------------------------------------------------------
-- 4. Different businesses sharing one coordinate. This is the case that
--    makes proximity matching alone unsafe: a mall or a strip of units
--    geocodes to a single point, so only the name tells them apart.
-- ---------------------------------------------------------------------
SELECT * FROM v_locations_sharing_a_point ORDER BY labels;


-- ---------------------------------------------------------------------
-- 5. Transactions that should have a place but do not. Online merchants
--    legitimately never will.
-- ---------------------------------------------------------------------
SELECT coalesce(v.merchant, v.description) AS merchant,
       count(*) AS transactions,
       round(sum(v.outflow), 2) AS spend
FROM v_spending v
WHERE v.location_id IS NULL
GROUP BY 1
ORDER BY 3 DESC
LIMIT 20;


-- ---------------------------------------------------------------------
-- 6. Coordinates that look wrong. 0,0 is the classic - a geocoder
--    saying "no idea" - and is dropped on import, but a file loaded
--    before that rule existed may still carry some.
-- ---------------------------------------------------------------------
SELECT id, label, city, state, latitude, longitude
FROM locations
WHERE (latitude = 0 AND longitude = 0)
   OR latitude  NOT BETWEEN -90 AND 90
   OR longitude NOT BETWEEN -180 AND 180
ORDER BY id;


-- ---------------------------------------------------------------------
-- 7. Fixing things.
-- ---------------------------------------------------------------------
-- Correct one place's coordinates. coord_key is maintained by the
-- importer, so set it too or proximity matching will miss this row.
-- UPDATE locations
--    SET latitude = 39.739236, longitude = -104.990251,
--        coord_key = round(39.739236, 5)::text || ',' ||
--                    round(-104.990251, 5)::text
--  WHERE id = 42;

-- Merge two rows that are the same place, then drop the duplicate.
-- UPDATE transactions SET location_id = 10 WHERE location_id = 11;
-- DELETE FROM locations WHERE id = 11;

-- Rows imported before the coordinate columns existed can be filled in
-- from the raw CSV kept in staging_rows:
--     make backfill-locations
