-- =====================================================================
-- categories.sql - the category tree.
--
-- A transaction has exactly ONE category. That is what makes the
-- roll-up charts honest: every transaction lands in exactly one bucket
-- at each depth, so slices always sum to total spending. Use tags
-- (db/scripts/tags.sql) for anything that needs to overlap.
--
-- Read-only queries run as-is. Anything that writes is commented out.
--     make psql < db/scripts/categories.sql
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. The whole tree, as rows. `plumb categories` prints the same thing.
-- ---------------------------------------------------------------------
SELECT depth, full_path, id
FROM v_category_tree
ORDER BY full_path;


-- ---------------------------------------------------------------------
-- 2. One subtree: a category and every descendant.
--
-- category_id_path is the ancestor chain INCLUDING the category itself,
-- so matching any element of it catches the whole branch at any depth.
-- ---------------------------------------------------------------------
SELECT ct.depth, ct.full_path, ct.id
FROM v_category_tree ct
WHERE (SELECT id FROM v_category_tree WHERE full_path = 'Food') = ANY(ct.id_path)
ORDER BY ct.full_path;


-- ---------------------------------------------------------------------
-- 3. Every transaction in a subtree.
--
-- Swap v_transactions for v_spending to get outflows only, with
-- internal transfers excluded.
-- ---------------------------------------------------------------------
SELECT v.id,
       to_char(v.posted_on, 'YYYY-MM-DD') AS on_date,
       v.account,
       coalesce(v.merchant, v.description) AS merchant,
       v.category_path,
       v.amount
FROM v_transactions v
JOIN v_category_tree ct ON ct.id = ANY(v.category_id_path)
WHERE ct.full_path = 'Entertainment > Travel'
ORDER BY v.posted_on;

-- Several branches at once. Use EXISTS rather than a join when the
-- paths might overlap - a join would count a transaction once per
-- matching branch.
-- SELECT v.id, v.category_path, v.amount
-- FROM v_transactions v
-- WHERE EXISTS (SELECT 1 FROM v_category_tree ct
--                WHERE ct.full_path IN ('Food', 'Food > Groceries')
--                  AND ct.id = ANY(v.category_id_path));


-- ---------------------------------------------------------------------
-- 4. Spending by category, at each depth. Totals are equal at every
--    level, because nothing drops out - see roll1/roll2/roll3.
-- ---------------------------------------------------------------------
SELECT roll1 AS level_1, round(sum(outflow), 2) AS spend
FROM v_spending GROUP BY 1 ORDER BY 2 DESC;

SELECT roll1 AS level_1, roll2 AS level_2, round(sum(outflow), 2) AS spend
FROM v_spending GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 20;


-- ---------------------------------------------------------------------
-- 5. Housekeeping. These are the two that actually need attention.
-- ---------------------------------------------------------------------
-- Categories the importer invented because a CSV named a leaf it had
-- never seen. File them somewhere sensible.
SELECT ct.full_path, count(t.id) AS transactions,
       round(coalesce(sum(-t.amount) FILTER (WHERE t.amount < 0), 0), 2) AS outflow
FROM categories c
JOIN v_category_tree ct ON ct.id = c.id
LEFT JOIN transactions t ON t.category_id = c.id AND t.status <> 'void'
WHERE ct.full_path LIKE 'Imported%'
GROUP BY ct.full_path
ORDER BY 3 DESC;

-- Names that appear in more than one branch. Not an error, but a bare
-- name resolves to whichever sits closest to the root, so always give
-- the full path for these.
SELECT c.name, count(*) AS branches,
       string_agg(ct.full_path, '  |  ' ORDER BY ct.depth) AS paths
FROM categories c
JOIN v_category_tree ct ON ct.id = c.id
GROUP BY c.name
HAVING count(*) > 1
ORDER BY 2 DESC, 1;

-- Categories nothing has ever landed in, descendants included.
SELECT ct.full_path
FROM categories c
JOIN v_category_tree ct ON ct.id = c.id
WHERE NOT EXISTS (
    SELECT 1 FROM v_transactions v WHERE c.id = ANY(v.category_id_path))
ORDER BY ct.full_path;


-- ---------------------------------------------------------------------
-- 5b. Categories that are not spending.
--
-- A credit card payment or a bank transfer is movement between your own
-- accounts, not money leaving your finances. Three things can mark a
-- transaction as internal, and spending excludes it if ANY of them says
-- so:
--
--   * `plumb link-transfers` paired it with its opposite leg;
--   * the source file had a Direction column saying Transfer / Receive;
--   * its category is flagged below.
--
-- The flag is the one that covers a transfer whose other leg lives in an
-- account you have not imported - a credit card payment, usually. It is
-- inherited, so flagging a branch root covers everything beneath it.
-- ---------------------------------------------------------------------
SELECT full_path, internal
FROM v_category_tree
WHERE internal
ORDER BY full_path;

-- What is currently being treated as internal, and how much of it:
SELECT coalesce(ct.full_path, '(none)') AS category,
       count(*) AS transactions,
       round(sum(abs(t.amount)), 2) AS moved
FROM transactions t
LEFT JOIN v_category_tree ct ON ct.id = t.category_id
WHERE t.status <> 'void' AND coalesce(ct.internal, false)
GROUP BY 1
ORDER BY 3 DESC;

-- Flag one of your own. The Transfer branch is flagged by the schema;
-- add anything else that moves money rather than spending it.
-- UPDATE categories SET is_internal = true
--  WHERE id = (SELECT id FROM v_category_tree
--               WHERE full_path = 'Financial > Loan Payment');

-- And to stop treating one as internal:
-- UPDATE categories SET is_internal = false
--  WHERE id = (SELECT id FROM v_category_tree
--               WHERE full_path = 'Transfer > Cash Withdrawal');


-- ---------------------------------------------------------------------
-- 6. Changing the tree. Transactions reference the id, never the name,
--    so renaming and re-parenting keep every row attached.
-- ---------------------------------------------------------------------
-- Rename:
-- UPDATE categories SET name = 'Groceries & Household'
--  WHERE id = (SELECT id FROM v_category_tree
--               WHERE full_path = 'Food > Groceries');

-- Re-parent (or use: plumb move-category --name X --under "A > B"):
-- UPDATE categories SET parent_id = (SELECT id FROM v_category_tree
--                                     WHERE full_path = 'Personal')
--  WHERE id = (SELECT id FROM v_category_tree
--               WHERE full_path = 'Imported > Amazon Shopping');

-- Add a branch:
-- INSERT INTO categories (parent_id, name)
-- SELECT id, 'Pet Food' FROM v_category_tree WHERE full_path = 'Food';

-- Merge one category into another, then drop the empty one:
-- UPDATE transactions SET category_id =
--        (SELECT id FROM v_category_tree WHERE full_path = 'Food > Dining')
--  WHERE category_id =
--        (SELECT id FROM v_category_tree WHERE full_path = 'Food > Takeaway');
-- DELETE FROM categories
--  WHERE id = (SELECT id FROM v_category_tree
--               WHERE full_path = 'Food > Takeaway');
