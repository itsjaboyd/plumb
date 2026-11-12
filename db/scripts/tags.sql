-- =====================================================================
-- tags.sql - freeform labels, added by hand.
--
-- A transaction has exactly ONE category and ANY NUMBER of tags. Tags
-- answer the questions a tree cannot: what a whole trip cost across
-- groceries, fuel and lodging, or which spending is coming back to you.
--
-- THE ONE RULE: join v_transaction_tags, never transaction_tags. The
-- join table is many-to-many, so joining it multiplies a multi-tagged
-- transaction and inflates every total silently. Section 8 shows the
-- difference in dollars.
--
-- Names normalise to lowercase and trimmed, so 'Reimbursable' and
-- '  REIMBURSABLE  ' are one tag.
--     make psql < db/scripts/tags.sql
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. Find something to tag. The Tags dashboard's bottom table is the
--    same list and easier to read; the id is what you need.
-- ---------------------------------------------------------------------
SELECT v.id, to_char(v.posted_on, 'YYYY-MM-DD') AS on_date,
       v.account, coalesce(v.merchant, v.description) AS merchant,
       v.category_path, v.outflow
FROM v_spending v
WHERE NOT EXISTS (SELECT 1 FROM v_transaction_tags tt
                   WHERE tt.transaction_id = v.id)
ORDER BY v.outflow DESC
LIMIT 20;


-- ---------------------------------------------------------------------
-- 2. Tag one transaction. Creates the tag if it is new, harmless twice,
--    and raises on an id that does not exist rather than doing nothing.
-- ---------------------------------------------------------------------
-- SELECT tag_transaction(44, 'reimbursable');
-- SELECT tag_transaction(44, 'vacation-mexico');   -- both at once is fine


-- ---------------------------------------------------------------------
-- 3. Tag in bulk. Run the SELECT on its own first to see what you are
--    about to tag - a typo'd path creates the tag and matches nothing,
--    which looks like success.
-- ---------------------------------------------------------------------
-- A whole category subtree, at any depth. category_id_path is the
-- ancestor chain including the category itself.
-- SELECT tag_transaction(v.id, 'travel-2026')
-- FROM v_spending v
-- WHERE (SELECT id FROM v_category_tree
--         WHERE full_path = 'Entertainment > Travel') = ANY(v.category_id_path);

-- The days of a trip, on one card:
-- SELECT tag_transaction(v.id, 'vacation-mexico')
-- FROM v_transactions v
-- WHERE v.posted_on BETWEEN '2026-10-14' AND '2026-10-22'
--   AND v.account = 'Chase Sapphire';

-- Every transaction at one merchant:
-- SELECT tag_transaction(v.id, 'work-travel')
-- FROM v_transactions v WHERE v.merchant = 'United Airlines';

-- For a real bulk run, go set-based instead: one statement, one index
-- pass, and psql reports how many rows actually landed.
-- INSERT INTO tags (name) VALUES ('fixed-costs') ON CONFLICT (name) DO NOTHING;
-- INSERT INTO transaction_tags (transaction_id, tag_id)
-- SELECT v.id, (SELECT id FROM tags WHERE name = 'fixed-costs')
-- FROM v_transactions v
-- WHERE EXISTS (SELECT 1 FROM v_category_tree ct
--                WHERE ct.full_path IN ('Housing > Utilities',
--                                       'Transportation > Fuel')
--                  AND ct.id = ANY(v.category_id_path))
-- ON CONFLICT DO NOTHING;

-- v_spending or v_transactions? v_spending is outflows only with
-- internal transfers excluded - right for "what did this cost me".
-- v_transactions also catches refunds, which is what makes the net-cost
-- and "came back" figures work for a reimbursable tag.


-- ---------------------------------------------------------------------
-- 4. Untag. The tag itself survives, so a slip on one row does not
--    cost you the tag.
-- ---------------------------------------------------------------------
-- SELECT untag_transaction(44, 'reimbursable');

-- A whole subtree again:
-- DELETE FROM transaction_tags tt
-- USING tags t, v_transactions v
-- WHERE t.id = tt.tag_id AND v.id = tt.transaction_id
--   AND t.name = 'fixed-costs'
--   AND (SELECT id FROM v_category_tree
--         WHERE full_path = 'Transportation > Fuel') = ANY(v.category_id_path);


-- ---------------------------------------------------------------------
-- 5. Housekeeping on the tags themselves.
-- ---------------------------------------------------------------------
-- Say what a tag means. Shows in the dashboard's "Means" column.
-- UPDATE tags SET note = 'work expense to claim back'
--  WHERE name = 'reimbursable';

-- Rename. Every tagged transaction follows, because the link is by id.
-- UPDATE tags SET name = 'reimburse-work' WHERE name = 'reimbursable';

-- Delete everywhere. transaction_tags cascades; transactions are
-- untouched.
-- DELETE FROM tags WHERE name = 'a-tag-i-regret';


-- ---------------------------------------------------------------------
-- 6. What is tagged now.
-- ---------------------------------------------------------------------
SELECT tag, coalesce(note, '') AS means, transactions,
       spend, received, net, categories_spanned,
       to_char(first_seen, 'YYYY-MM-DD') AS first_seen
FROM v_tag_spend
ORDER BY net DESC;

-- Per transaction, with every tag on one line.
SELECT transaction_id, tag_list, tag_count
FROM v_transaction_tags
ORDER BY tag_count DESC, transaction_id;


-- ---------------------------------------------------------------------
-- 7. Where a tag's money lands in the tree. This is the question tags
--    exist for - one trip as three unrelated categories.
-- ---------------------------------------------------------------------
SELECT tag, category_path, transactions, spend
FROM v_tag_category
ORDER BY tag, spend DESC;

-- Tags that turn up together. Usually deliberate; occasionally a slip.
SELECT tag, also_tagged, transactions FROM v_tag_overlap ORDER BY 1, 2;


-- ---------------------------------------------------------------------
-- 8. The fan-out, in dollars. Both of these look reasonable. One is
--    wrong, and it is wrong silently.
-- ---------------------------------------------------------------------
SELECT 'correct: v_transaction_tags' AS join_used,
       round(sum(v.outflow), 2) AS total_spending
FROM v_spending v
LEFT JOIN v_transaction_tags tt ON tt.transaction_id = v.id
UNION ALL
SELECT 'WRONG: transaction_tags', round(sum(v.outflow), 2)
FROM v_spending v
LEFT JOIN transaction_tags tt ON tt.transaction_id = v.id;
