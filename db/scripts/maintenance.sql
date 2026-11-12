-- =====================================================================
-- maintenance.sql - is the data right?
--
-- Run this after an import, and before trusting a month's numbers.
-- Everything here is read-only except the clearly marked last section.
--     make psql < db/scripts/maintenance.sql
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. Does the computed balance match what the bank said? This is the
--    single most important check in the stack: if it disagrees, every
--    chart above it is wrong. `make reconcile` prints the same thing.
--
--    A difference usually means a missing opening balance - set it with
--    `make set-opening ACCOUNT="x" AMOUNT=123.45` - or a gap in the
--    imported history.
-- ---------------------------------------------------------------------
SELECT account, account_type, opening_balance,
       computed_balance, statement_balance, statement_diff,
       snapshot_balance, snapshot_diff
FROM v_reconciliation
ORDER BY abs(coalesce(statement_diff, snapshot_diff, 0)) DESC;


-- ---------------------------------------------------------------------
-- 1b. Owe-money accounts: what they hold, and what they contribute.
--
-- On a Credit or Loan account the two sources of a balance are signed
-- differently, on purpose:
--
--   * imported transactions are normalised money-out-is-negative by the
--     mapping, cards included, so a card you owe on sits NEGATIVE;
--   * a figure you type in - opening_balance, or a row recorded with
--     `make record-balance` - means the amount OWED, so it is POSITIVE.
--
-- Net worth negates the typed-in part and leaves transactions alone, so
-- both end up subtracting. This lists both numbers side by side, which
-- is the fastest way to see why net worth is what it is. `plumb stats`
-- prints the same pair for every account.
-- ---------------------------------------------------------------------
SELECT a.name AS account, a.account_type,
       a.opening_balance AS typed_in,
       coalesce(sum(t.amount), 0) AS from_transactions,
       a.opening_balance + coalesce(sum(t.amount), 0) AS balance_as_stored,
       CASE WHEN a.account_type IN (SELECT account_type FROM v_liability_types)
            THEN -a.opening_balance ELSE a.opening_balance END
         + coalesce(sum(t.amount), 0) AS contributes_to_net_worth,
       (SELECT b.balance FROM account_balances b
         WHERE b.account_id = a.id ORDER BY b.as_of DESC LIMIT 1)
         AS last_recorded
FROM accounts a
LEFT JOIN transactions t ON t.account_id = a.id AND t.status <> 'void'
WHERE a.account_type IN (SELECT account_type FROM v_liability_types)
GROUP BY a.id, a.name, a.account_type, a.opening_balance
ORDER BY 1;

-- Set a loan to what you owe on it. Positive: net worth subtracts it.
-- UPDATE accounts SET opening_balance = 18500.00 WHERE name = 'Car Loan';

-- Record where a mortgage stands today, as the amount outstanding:
-- INSERT INTO account_balances (account_id, as_of, balance)
-- SELECT id, current_date, 284000.00 FROM accounts WHERE name = 'Mortgage';

-- A negative figure on one of these accounts means real equity, and adds
-- to net worth - which is what you want for a card you have over-paid.


-- ---------------------------------------------------------------------
-- 2. What has been imported, and did any rows fail.
-- ---------------------------------------------------------------------
SELECT b.id, b.mapping, b.source_file,
       b.rows_read, b.rows_inserted, b.rows_duplicate, b.rows_error,
       to_char(b.started_at, 'YYYY-MM-DD HH24:MI') AS started
FROM import_batches b
ORDER BY b.id DESC
LIMIT 20;

-- The rows that failed, with the reason. Fix the mapping, then
-- `make replay` re-runs them - nothing is lost by a failed row.
SELECT s.batch_id, s.line_no, s.error, left(s.raw::text, 120) AS raw
FROM staging_rows s
WHERE s.status = 'error'
ORDER BY s.batch_id DESC, s.line_no
LIMIT 20;

-- Rows that imported but recorded a warning - an ambiguous category
-- name resolved to the topmost match, most often.
SELECT * FROM v_import_warnings LIMIT 20;


-- ---------------------------------------------------------------------
-- 3. Internal movement. A transfer between your own accounts appears
--    twice - once negative, once positive - and must not count as
--    either spending or income. Unpaired legs usually mean an account
--    you have not imported yet.
-- ---------------------------------------------------------------------
SELECT * FROM v_direction_summary ORDER BY transactions DESC;

-- Equal-and-opposite pairs that have not been linked. `make
-- link-transfers` pairs them.
SELECT * FROM v_transfer_candidates LIMIT 20;


-- ---------------------------------------------------------------------
-- 4. Spending with no category. Clear this before reading any chart -
--    an uncategorised pile is the one thing that makes a roll-up lie.
-- ---------------------------------------------------------------------
SELECT to_char(v.posted_on, 'YYYY-MM-DD') AS on_date, v.account,
       coalesce(v.merchant, v.description) AS merchant, v.outflow
FROM v_spending v
WHERE v.category_id IS NULL
ORDER BY v.outflow DESC
LIMIT 25;


-- ---------------------------------------------------------------------
-- 5. Possible duplicates. The importer dedupes on
--    (account_id, external_id), so re-importing an overlapping file is
--    already safe. This catches the other case: the same purchase
--    present twice because two different exports described it
--    differently.
-- ---------------------------------------------------------------------
SELECT a.account, to_char(a.posted_on, 'YYYY-MM-DD') AS on_date,
       a.amount, count(*) AS copies,
       string_agg(DISTINCT coalesce(a.merchant, a.description), ' | ') AS seen_as
FROM v_transactions a
GROUP BY a.account, a.posted_on, a.amount
HAVING count(*) > 1
ORDER BY abs(a.amount) DESC
LIMIT 20;


-- ---------------------------------------------------------------------
-- 6. Shape of the data: what is covered and how far back.
-- ---------------------------------------------------------------------
SELECT a.name AS account, a.account_type,
       count(t.id) AS transactions,
       to_char(min(t.posted_on), 'YYYY-MM-DD') AS first,
       to_char(max(t.posted_on), 'YYYY-MM-DD') AS last,
       round(a.opening_balance, 2) AS opening_balance
FROM accounts a
LEFT JOIN transactions t ON t.account_id = a.id AND t.status <> 'void'
GROUP BY a.id, a.name, a.account_type, a.opening_balance
ORDER BY a.name;

-- Months with no data at all, which is what a gap in a chart means.
SELECT to_char(m.month, 'YYYY-MM') AS month,
       count(t.id) AS transactions
FROM generate_series(
         (SELECT date_trunc('month', min(posted_on)) FROM transactions),
         date_trunc('month', current_date),
         interval '1 month') AS m(month)
LEFT JOIN transactions t
       ON date_trunc('month', t.posted_on) = m.month AND t.status <> 'void'
GROUP BY 1
HAVING count(t.id) = 0
ORDER BY 1;


-- ---------------------------------------------------------------------
-- 7. Source ids with nothing to point at - a ledger export that used
--    "Merchant ID: 28" and no name. Export that table and run
--    `plumb import-refs` to resolve them.
-- ---------------------------------------------------------------------
SELECT * FROM v_unresolved_refs LIMIT 20;


-- ---------------------------------------------------------------------
-- 8. Writes. Nothing above this line changes anything.
-- ---------------------------------------------------------------------
-- Void a transaction rather than deleting it: views already exclude
-- status 'void', and the raw row stays in staging_rows.
-- UPDATE transactions SET status = 'void' WHERE id = 123;

-- Record what the bank says a balance is, so reconciliation has
-- something to compare against:
-- INSERT INTO account_balances (account_id, as_of, balance, source)
-- SELECT id, current_date, 4210.55, 'statement'
-- FROM accounts WHERE name = 'Chase Checking';

-- After a large import, refresh the planner's statistics:
-- VACUUM ANALYZE;
