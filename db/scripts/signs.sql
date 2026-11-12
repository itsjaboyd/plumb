-- =====================================================================
-- signs.sql - is the money pointing the right way?
--
-- Two sign conventions meet in this stack, and net worth depends on
-- both:
--
--   imported transactions   money out is NEGATIVE, every account type,
--                           normalised by the mapping at import
--   hand-entered balances   on a Credit or Loan account, the amount
--                           OWED, so POSITIVE is debt
--
-- Net worth negates the second and takes the first as it is. Run this
-- after adding an account, writing a mapping, or importing someone
-- else's export. Everything here is read-only.
--     make psql < db/scripts/signs.sql
--
-- DO NOT fix any of this with a blanket UPDATE by account type. The
-- imported rows are normally correct, and flipping them would turn
-- correct data into wrong data. Each section below names its own fix.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. Every owe-money account: stored, and what it contributes.
--
-- These two columns differing is normal and is the whole point. What
-- you are looking for is a row where `contributes_to_net_worth` has a
-- sign you did not expect.
-- ---------------------------------------------------------------------
SELECT a.name AS account, a.account_type,
       a.opening_balance AS typed_in,
       coalesce(sum(t.amount), 0) AS from_transactions,
       CASE WHEN a.account_type IN (SELECT account_type FROM v_liability_types)
            THEN -a.opening_balance ELSE a.opening_balance END
         + coalesce(sum(t.amount), 0) AS contributes_to_net_worth
FROM accounts a
LEFT JOIN transactions t ON t.account_id = a.id AND t.status <> 'void'
WHERE a.account_type IN (SELECT account_type FROM v_liability_types)
GROUP BY a.id, a.name, a.account_type, a.opening_balance
ORDER BY 1;


-- ---------------------------------------------------------------------
-- 2. Hand-entered figures that read as equity rather than debt.
--
-- A negative opening_balance or recorded balance on one of these
-- accounts means you have MORE than nothing there - an over-paid card.
-- That is legitimate, but it is also what you get by typing a debt in
-- with a minus sign, so read these rows.
--
-- Fix, if one of them was meant to be a debt:
--     UPDATE accounts SET opening_balance = -opening_balance
--      WHERE name = 'Car Loan';
--
--     UPDATE account_balances SET balance = -balance
--      WHERE account_id = (SELECT id FROM accounts WHERE name = 'Mortgage')
--        AND as_of = '2026-11-01';     -- the pair identifies the row
--
-- Drop the `AND as_of` line to flip every recorded balance for that
-- account at once, but read them first - an account can legitimately
-- have been in credit on some dates and not others.
-- ---------------------------------------------------------------------
SELECT a.name AS account, a.account_type,
       'opening_balance' AS held_in, NULL::date AS as_of,
       a.opening_balance AS value, NULL::text AS recorded_from
FROM accounts a
WHERE a.account_type IN (SELECT account_type FROM v_liability_types)
  AND a.opening_balance < 0
UNION ALL
SELECT a.name, a.account_type,
       'account_balances', b.as_of, b.balance, b.source
FROM account_balances b
JOIN accounts a ON a.id = b.account_id
WHERE a.account_type IN (SELECT account_type FROM v_liability_types)
  AND b.balance < 0
ORDER BY 1, 3, 4;


-- ---------------------------------------------------------------------
-- 3. Imported transactions pointing the wrong way.
--
-- On a credit card, purchases outnumber payments, so most rows should
-- be NEGATIVE. An account where the positives dominate usually means
-- its mapping did not normalise the sign - the export listed charges as
-- positive and the mapping is missing `flip_sign: true`, the way
-- amex.yml has it.
--
-- `suspect` marks the rows worth a look. A brand-new account with one
-- payment and nothing else can trip it, so confirm against the bank
-- before changing anything.
--
-- THE FIX IS NOT SQL. Fix the mapping, then re-derive the amounts from
-- the rows kept verbatim at import - which keeps every transaction id,
-- so tags, transfer pairings and receipt line items all survive:
--     make psql < db/scripts/signs.sql        # note the batch id below
--     $EDITOR etl/mappings/<mapping>.yml      # add flip_sign: true
--     plumb replay --batch <id> --fields amounts --dry-run
--     plumb replay --batch <id> --fields amounts
-- ---------------------------------------------------------------------
SELECT a.name AS account, a.account_type,
       count(*) FILTER (WHERE t.amount < 0) AS money_out_rows,
       count(*) FILTER (WHERE t.amount > 0) AS money_in_rows,
       round(sum(t.amount), 2) AS net,
       string_agg(DISTINCT b.mapping, ', ') AS mappings,
       string_agg(DISTINCT b.id::text, ', ') AS batches,
       count(*) FILTER (WHERE t.amount > 0)
         > count(*) FILTER (WHERE t.amount < 0) AS suspect
FROM accounts a
JOIN transactions t ON t.account_id = a.id AND t.status <> 'void'
LEFT JOIN import_batches b ON b.id = t.import_batch_id
WHERE a.account_type IN (SELECT account_type FROM v_liability_types)
GROUP BY a.id, a.name, a.account_type
ORDER BY 8 DESC, 1;


-- ---------------------------------------------------------------------
-- 4. The authority on all of this: what the bank itself said.
--
-- Where an export carried a balance column, `statement_balance` is the
-- bank's own running figure and `statement_diff` is how far this stack
-- is from it. A difference of roughly TWICE the computed balance, with
-- the opposite sign, is the signature of an inverted sign convention
-- rather than a missing opening balance.
-- ---------------------------------------------------------------------
SELECT account, account_type, opening_balance,
       computed_balance, statement_balance, statement_diff,
       CASE
         WHEN statement_balance IS NULL OR computed_balance IS NULL
              OR computed_balance = 0 THEN NULL
         WHEN sign(statement_balance) <> sign(computed_balance)
              AND abs(statement_diff) BETWEEN abs(computed_balance) * 1.8
                                          AND abs(computed_balance) * 2.2
           THEN 'looks inverted'
         WHEN abs(statement_diff) > 0.01 THEN 'differs - check opening_balance'
       END AS reading
FROM v_reconciliation
WHERE account_type IN (SELECT account_type FROM v_liability_types)
ORDER BY abs(coalesce(statement_diff, 0)) DESC;
