-- =====================================================================
-- merchants.sql - turning bank descriptions into businesses.
--
-- A bank spells one business several ways: "SQ *BLUE BOTTLE 1234",
-- "BLUE BOTTLE COFFEE #12", "TST* BLUE BOTTLE". A merchant_aliases row
-- maps a pattern to a merchant, and optionally to a category, so all of
-- them land in one place on a chart.
--
-- Patterns are ILIKE unless match_type is 'regex'. The LOWEST priority
-- number wins when several match, which is how a specific rule beats a
-- general one.
--     make psql < db/scripts/merchants.sql
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. The to-do list: descriptions no rule matches yet, biggest first.
--    `make unmatched` prints this.
-- ---------------------------------------------------------------------
SELECT * FROM v_unmatched_descriptions LIMIT 25;


-- ---------------------------------------------------------------------
-- 2. The rules you have, in the order they are tried.
-- ---------------------------------------------------------------------
SELECT a.priority, m.name AS merchant, a.match_type, a.pattern,
       coalesce(ct.full_path, '(no category)') AS sets_category
FROM merchant_aliases a
JOIN merchants m ON m.id = a.merchant_id
LEFT JOIN v_category_tree ct ON ct.id = a.category_id
ORDER BY a.priority, m.name;


-- ---------------------------------------------------------------------
-- 3. A specific rule beating a general one. The fuel pump at a grocery
--    store is Transportation, not Food, so it gets a lower number.
-- ---------------------------------------------------------------------
SELECT a.priority, m.name AS merchant, a.pattern,
       coalesce(ct.full_path, '(no category)') AS sets_category
FROM merchant_aliases a
JOIN merchants m ON m.id = a.merchant_id
LEFT JOIN v_category_tree ct ON ct.id = a.category_id
WHERE a.pattern ILIKE '%KING SOOPERS%'
ORDER BY a.priority;


-- ---------------------------------------------------------------------
-- 4. What each merchant is actually worth to you.
-- ---------------------------------------------------------------------
SELECT coalesce(v.merchant, '(no merchant yet)') AS merchant,
       count(*) AS visits,
       round(sum(v.outflow), 2) AS total,
       round(avg(v.outflow), 2) AS average,
       to_char(max(v.posted_on), 'YYYY-MM-DD') AS last_seen
FROM v_spending v
GROUP BY 1
ORDER BY 3 DESC
LIMIT 25;

-- Merchants defined but never matched - usually a seeded rule for a
-- business you do not use, or a pattern that never fires.
SELECT m.name
FROM merchants m
WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.merchant_id = m.id)
ORDER BY m.name;


-- ---------------------------------------------------------------------
-- 5. Adding rules.
--
-- The CLI is easier and validates the category path:
--   plumb add-alias --merchant "King Soopers" \
--                    --pattern "%KING SOOPERS%" --category "Food > Groceries"
-- then re-run them over rows already imported:
--   make categorize
-- ---------------------------------------------------------------------
-- By hand, if you prefer:
-- WITH m AS (
--     INSERT INTO merchants (name) VALUES ('Blue Bottle Coffee')
--     ON CONFLICT (name) DO UPDATE SET name = excluded.name
--     RETURNING id
-- )
-- INSERT INTO merchant_aliases (merchant_id, pattern, match_type, priority,
--                               category_id)
-- SELECT m.id, '%BLUE BOTTLE%', 'like', 100,
--        (SELECT id FROM v_category_tree WHERE full_path = 'Food > Coffee')
-- FROM m;

-- A regex, for when ILIKE cannot express it:
-- INSERT INTO merchant_aliases (merchant_id, pattern, match_type, priority)
-- SELECT id, '^(SQ|TST)\*', 'regex', 50 FROM merchants WHERE name = 'Square';

-- Merge two merchants that are the same business:
-- UPDATE transactions SET merchant_id = 3 WHERE merchant_id = 17;
-- UPDATE merchant_aliases SET merchant_id = 3 WHERE merchant_id = 17;
-- DELETE FROM merchants WHERE id = 17;

-- Changing a rule only affects future imports until you replay the
-- stored raw rows against it:
--     make replay-dry    # see what would change
--     make replay        # apply; no deletes, line items and tags survive
