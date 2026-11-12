-- =====================================================================
-- budgets.sql - plans, envelopes and overrides.
--
-- budget_plans holds INTENT: one row per change, not one per month. Set
-- a budget once and it rolls forward until you change it. `amount` is
-- the cost per PERIOD, so an annual $1,200 bill is
-- (1200.00, 'annual') and accrues at $100 a month.
--
-- Two kinds of category, and the difference decides how to read them:
--   carry_forward = false   flow spending. Each month stands alone.
--   carry_forward = true    a periodic cost. The comparison is
--                           cumulative, so eleven quiet months fund the
--                           twelfth instead of looking like underspend.
--
-- The usual way in is the importer:
--     make import-budget CSV=data/inbox/budget.csv DRY=1
-- This file is for when you want to do it by hand.
--     make psql < db/scripts/budgets.sql
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. Where everything stands this month. `make budget` prints this.
-- ---------------------------------------------------------------------
SELECT category, period, carry_forward,
       budget, spent, variance, pct_used,
       envelope_balance, envelope_shortfall, months_of_cover
FROM v_budget_status
ORDER BY carry_forward, budget DESC NULLS LAST;


-- ---------------------------------------------------------------------
-- 2. Envelopes only, worst first. months_of_cover under 1 on a bill due
--    soon is the thing to act on. A balance of zero means the envelope
--    has been emptied - it cannot go negative.
-- ---------------------------------------------------------------------
SELECT s.category, s.period,
       m.plan_amount      AS per_period,
       s.budget           AS accrues_monthly,
       s.envelope_balance AS balance,
       s.months_of_cover
FROM v_budget_status s
JOIN v_budget_months m ON m.category_id = s.category_id AND m.month = s.month
WHERE s.carry_forward
ORDER BY s.months_of_cover NULLS FIRST;


-- ---------------------------------------------------------------------
-- 3. Any month, in full. is_override marks a month whose budget came
--    from the budgets table rather than the plan.
-- ---------------------------------------------------------------------
SELECT to_char(month, 'YYYY-MM') AS month, category,
       budget, spent, variance, pct_used, is_override,
       envelope_balance, envelope_shortfall
FROM v_budget_vs_actual
WHERE month = date_trunc('month', current_date)::date
ORDER BY budget DESC NULLS LAST;


-- ---------------------------------------------------------------------
-- 4. Months an envelope ran dry - spent past everything it had
--    accrued. This replaced "balance went negative", which can no
--    longer happen.
-- ---------------------------------------------------------------------
SELECT to_char(month, 'YYYY-MM') AS month, category,
       budget, spent, envelope_shortfall AS forgiven
FROM v_budget_vs_actual
WHERE coalesce(envelope_shortfall, 0) > 0
ORDER BY month DESC, forgiven DESC;


-- ---------------------------------------------------------------------
-- 5. Spending with no budget at all - the gap in your plan.
-- ---------------------------------------------------------------------
SELECT v.roll1 AS category, round(sum(v.outflow), 2) AS spend_12m
FROM v_spending v
WHERE v.posted_on >= current_date - interval '12 months'
  AND NOT EXISTS (
      SELECT 1 FROM budget_plans p
       WHERE p.amount IS NOT NULL
         AND p.category_id = ANY(v.category_id_path))
GROUP BY 1
ORDER BY 2 DESC;


-- ---------------------------------------------------------------------
-- 6. The audit trail: every plan row ever loaded.
-- ---------------------------------------------------------------------
SELECT c.name AS category,
       to_char(p.valid_from, 'YYYY-MM') AS from_month,
       p.amount AS per_period, p.period,
       round(p.amount / budget_period_months(p.period), 2) AS monthly,
       p.carry_forward, p.opening_balance, coalesce(p.note, '') AS note
FROM budget_plans p
JOIN categories c ON c.id = p.category_id
ORDER BY c.name, p.valid_from DESC;


-- ---------------------------------------------------------------------
-- 7. Setting a budget by hand.
--
-- valid_from is always the 1st. Re-running with a different amount for
-- the same month updates it rather than erroring.
-- ---------------------------------------------------------------------
-- A flow category:
-- INSERT INTO budget_plans (category_id, valid_from, amount, period)
-- SELECT id, date_trunc('month', current_date)::date, 800.00, 'monthly'
-- FROM v_category_tree WHERE full_path = 'Food'
-- ON CONFLICT (category_id, valid_from) DO UPDATE SET amount = excluded.amount;

-- A periodic bill, with money already set aside:
-- INSERT INTO budget_plans (category_id, valid_from, amount, period,
--                           carry_forward, opening_balance, note)
-- SELECT id, '2026-01-01', 1200.00, 'annual', true, 400.00, 'insurance'
-- FROM v_category_tree WHERE full_path = 'Health'
-- ON CONFLICT (category_id, valid_from) DO UPDATE
--    SET amount = excluded.amount, period = excluded.period,
--        carry_forward = excluded.carry_forward,
--        opening_balance = excluded.opening_balance;

-- Change an amount from a given month on - the old row stays as history:
-- INSERT INTO budget_plans (category_id, valid_from, amount, period)
-- SELECT id, '2027-01-01', 900.00, 'monthly'
-- FROM v_category_tree WHERE full_path = 'Food';

-- Stop budgeting a category: a NULL amount, not a zero. Zero means
-- "budget nothing and judge me against it", which is a different claim.
-- INSERT INTO budget_plans (category_id, valid_from, amount)
-- SELECT id, date_trunc('month', current_date)::date, NULL
-- FROM v_category_tree WHERE full_path = 'Entertainment';

-- Re-baseline an envelope to what is really in the account. Accrual
-- restarts from here rather than from the arithmetic.
-- INSERT INTO budget_plans (category_id, valid_from, amount, period,
--                           carry_forward, opening_balance)
-- SELECT id, date_trunc('month', current_date)::date, 1200.00, 'annual',
--        true, 650.00
-- FROM v_category_tree WHERE full_path = 'Health'
-- ON CONFLICT (category_id, valid_from) DO UPDATE
--    SET opening_balance = excluded.opening_balance;

-- One month that is genuinely different - December groceries, say.
-- This beats the plan's accrual for that month only.
-- INSERT INTO budgets (category_id, month, amount)
-- SELECT id, '2026-12-01', 1000.00
-- FROM v_category_tree WHERE full_path = 'Food'
-- ON CONFLICT (category_id, month) DO UPDATE SET amount = excluded.amount;

-- Clearing out: plans and overrides are separate tables, and an
-- override with no plan behind it is invisible until a plan appears,
-- then silently wins. Delete both.
-- DELETE FROM budget_plans;
-- DELETE FROM budgets;
