-- =====================================================================
-- 002_views.sql - the reporting interface.
-- 
-- 31 views, in dependency order. Everything that reads this
-- database - every Grafana panel, every script under db/scripts - goes
-- through these rather than the tables, so a column can move without
-- breaking a dashboard.
-- 
-- Two rules that are easy to get wrong and expensive to debug:
-- 
--   * Join v_transaction_tags, never transaction_tags. The join table
--     is many-to-many, so joining it multiplies a multi-tagged
--     transaction and inflates every total silently.
--   * Never hand Grafana a bare date or a naive timestamp. It reads
--     both as UTC and shifts them by the dashboard timezone, so a
--     date displays as the day before. Use to_char(...) for display
--     and AT TIME ZONE for a time axis.
-- =====================================================================

-- Balance after each transaction.
CREATE VIEW v_account_running_balance AS
 SELECT t.account_id,
    a.name AS account,
    t.posted_on,
    a.opening_balance + sum(t.amount) OVER (PARTITION BY t.account_id ORDER BY t.posted_on, t.id) AS balance
   FROM transactions t
     JOIN accounts a ON a.id = t.account_id
  WHERE t.status <> 'void'::text;

-- Depth, ancestor arrays and the pre-rolled roll1/roll2/roll3 labels.
-- A transaction tagged shallower than the level being charted shows
-- as "X (unspecified)" rather than dropping out, so slices at any
-- depth still sum to total spending. `internal` is inherited down
-- the tree (t.internal OR c.is_internal), so flagging a branch root
-- covers everything beneath it.
CREATE VIEW v_category_tree AS
 WITH RECURSIVE tree AS (
         SELECT categories.id,
            categories.parent_id,
            categories.name,
            1 AS depth,
            ARRAY[categories.id] AS id_path,
            ARRAY[categories.name] AS name_path,
            categories.is_internal AS internal
           FROM categories
          WHERE categories.parent_id IS NULL
        UNION ALL
         SELECT c.id,
            c.parent_id,
            c.name,
            t.depth + 1,
            t.id_path || c.id,
            t.name_path || c.name,
            t.internal OR c.is_internal
           FROM categories c
             JOIN tree t ON c.parent_id = t.id
        )
 SELECT id,
    depth,
    id_path,
    name_path,
    name_path[1] AS level1,
    name_path[2] AS level2,
    name_path[3] AS level3,
    id_path[1] AS level1_id,
    internal,
    array_to_string(name_path, ' > '::text) AS full_path,
    name_path[1] AS roll1,
        CASE
            WHEN depth >= 2 THEN name_path[2]
            ELSE name_path[depth] || ' (unspecified)'::text
        END AS roll2,
        CASE
            WHEN depth >= 3 THEN name_path[3]
            ELSE name_path[depth] || ' (unspecified)'::text
        END AS roll3
   FROM tree;

-- Spending rolled up by city.
CREATE VIEW v_city_spend AS
 SELECT COALESCE(l.city, 'Unknown'::text) AS city,
    l.state,
    count(t.id) AS transactions,
    sum(- t.amount) AS total_outflow,
    avg(l.latitude) AS latitude,
    avg(l.longitude) AS longitude
   FROM locations l
     JOIN transactions t ON t.location_id = l.id AND t.status <> 'void'::text AND t.transfer_group IS NULL AND t.amount < 0::numeric
  GROUP BY (COALESCE(l.city, 'Unknown'::text)), l.state;

-- How each source direction is used, and how many legs are unpaired.
CREATE VIEW v_direction_summary AS
 SELECT COALESCE(direction, '(none)'::text) AS direction,
    count(*) AS transactions,
    sum(abs(amount)) AS gross_amount,
    count(*) FILTER (WHERE transfer_group IS NULL AND (direction = ANY (ARRAY['Transfer'::text, 'Receive'::text]))) AS unpaired_legs,
    min(posted_on) AS first_seen,
    max(posted_on) AS last_seen
   FROM transactions
  WHERE status <> 'void'::text
  GROUP BY (COALESCE(direction, '(none)'::text));

-- Rows that imported with a warning recorded.
CREATE VIEW v_import_warnings AS
 SELECT b.id AS batch_id,
    b.source_file,
    b.mapping,
    b.started_at,
    s.warning,
    count(*) AS rows_affected,
    min(s.line_no) AS first_line,
    max(s.line_no) AS last_line
   FROM staging_rows s
     JOIN import_batches b ON b.id = s.batch_id
  WHERE s.warning IS NOT NULL
  GROUP BY b.id, b.source_file, b.mapping, b.started_at, s.warning
  ORDER BY b.id DESC, (count(*)) DESC;

-- Account types whose HAND-ENTERED balance means the amount owed, so
-- net worth subtracts it. Imported transactions are already signed
-- money-out-is-negative by the mapping and are NOT touched. Both
-- net worth views read this, so they cannot drift apart.
CREATE VIEW v_liability_types AS
 SELECT unnest(ARRAY['Credit'::text, 'Loan'::text]) AS account_type;

-- All-time totals per place, with the popularity score.
CREATE VIEW v_location_spend AS
 SELECT l.id AS location_id,
    COALESCE(mode() WITHIN GROUP (ORDER BY m.name), l.label, l.address, l.city, 'Unknown'::text) AS place_label,
    l.label,
    l.latitude,
    l.longitude,
    l.city,
    l.state,
    count(t.id) AS visits,
    sum(- t.amount) AS total_outflow,
    round(avg(- t.amount), 2) AS avg_outflow,
    min(t.posted_on) AS first_visit,
    max(t.posted_on) AS last_visit,
    l.popularity
   FROM locations l
     JOIN transactions t ON t.location_id = l.id AND t.status <> 'void'::text AND t.transfer_group IS NULL AND t.amount < 0::numeric
     LEFT JOIN merchants m ON m.id = t.merchant_id
  GROUP BY l.id, l.label, l.latitude, l.longitude, l.city, l.state, l.popularity;

-- Different businesses at the same coordinates - the case that makes
-- proximity matching alone unsafe.
CREATE VIEW v_locations_sharing_a_point AS
 SELECT coord_key,
    count(*) AS places,
    array_agg(label ORDER BY label) AS labels,
    min(latitude) AS latitude,
    min(longitude) AS longitude
   FROM locations
  WHERE coord_key IS NOT NULL
  GROUP BY coord_key
 HAVING count(*) > 1;

-- One merchant's storefronts.
CREATE VIEW v_merchant_locations AS
 SELECT m.id AS merchant_id,
    m.name AS merchant,
    l.id AS location_id,
    l.label AS place,
    l.city,
    l.state,
    l.latitude,
    l.longitude,
    count(t.id) AS visits,
    sum(- t.amount) AS total_outflow
   FROM merchants m
     JOIN transactions t ON t.merchant_id = m.id AND t.status <> 'void'::text
     JOIN locations l ON l.id = t.location_id
  GROUP BY m.id, m.name, l.id, l.label, l.city, l.state, l.latitude, l.longitude;

-- Tags that turn up on the same transaction.
CREATE VIEW v_tag_overlap AS
 SELECT a.name AS tag,
    b.name AS also_tagged,
    count(*) AS transactions
   FROM transaction_tags ta
     JOIN transaction_tags tb ON tb.transaction_id = ta.transaction_id AND tb.tag_id <> ta.tag_id
     JOIN tags a ON a.id = ta.tag_id
     JOIN tags b ON b.id = tb.tag_id
  GROUP BY a.name, b.name;

-- Tags collapsed to ONE ROW PER TRANSACTION. Join this, never
-- transaction_tags, or a multi-tagged transaction is counted once per
-- tag and spending totals silently inflate.
CREATE VIEW v_transaction_tags AS
 SELECT tt.transaction_id,
    array_agg(t.name ORDER BY t.name) AS tags,
    string_agg(t.name, ', '::text ORDER BY t.name) AS tag_list,
    count(*) AS tag_count
   FROM transaction_tags tt
     JOIN tags t ON t.id = tt.tag_id
  GROUP BY tt.transaction_id;

-- Unlinked equal-and-opposite pairs.
CREATE VIEW v_transfer_candidates AS
 SELECT a.id AS out_id,
    b.id AS in_id,
    a.account_id AS out_account,
    b.account_id AS in_account,
    a.posted_on AS out_posted,
    b.posted_on AS in_posted,
    a.amount AS out_amount,
    abs(b.posted_on - a.posted_on) AS day_gap
   FROM transactions a
     JOIN transactions b ON b.amount = (- a.amount) AND b.account_id <> a.account_id AND b.posted_on >= (a.posted_on - 5) AND b.posted_on <= (a.posted_on + 5)
  WHERE a.amount < 0::numeric AND a.transfer_group IS NULL AND b.transfer_group IS NULL AND a.status <> 'void'::text AND b.status <> 'void'::text;

-- The merchant-rule to-do list.
CREATE VIEW v_unmatched_descriptions AS
 SELECT raw_description,
    count(*) AS txn_count,
    sum(- amount) AS total_outflow,
    min(posted_on) AS first_seen,
    max(posted_on) AS last_seen
   FROM transactions t
  WHERE merchant_id IS NULL AND transfer_group IS NULL AND raw_description IS NOT NULL AND status <> 'void'::text
  GROUP BY raw_description
  ORDER BY (count(*)) DESC, (sum(- amount)) DESC;

-- Source ids with nothing to point at yet.
CREATE VIEW v_unresolved_refs AS
 SELECT 'merchant'::text AS kind,
    transactions.source_merchant_ref AS ref,
    count(*) AS transactions,
    sum(abs(transactions.amount)) AS gross_amount
   FROM transactions
  WHERE transactions.source_merchant_ref IS NOT NULL AND transactions.merchant_id IS NULL AND transactions.status <> 'void'::text
  GROUP BY 'merchant'::text, transactions.source_merchant_ref
UNION ALL
 SELECT 'location'::text AS kind,
    transactions.source_location_ref AS ref,
    count(*) AS transactions,
    sum(abs(transactions.amount)) AS gross_amount
   FROM transactions
  WHERE transactions.source_location_ref IS NOT NULL AND transactions.location_id IS NULL AND transactions.status <> 'void'::text
  GROUP BY 'location'::text, transactions.source_location_ref;

-- Current computed balance per account.
CREATE VIEW v_account_latest_balance AS
 SELECT DISTINCT ON (account_id) account_id,
    account,
    posted_on,
    balance
   FROM v_account_running_balance
  ORDER BY account_id, posted_on DESC, balance;

-- Every (month, category) the plans imply, with the accrual and any
-- single-month override. The window reaches twelve months past the
-- current month: a budget you cannot look ahead in is not much use.
CREATE VIEW v_budget_months AS
 WITH span AS (
         SELECT ( SELECT min(budget_plans.valid_from) AS min
                   FROM budget_plans) AS start_month,
            GREATEST((date_trunc('month'::text, CURRENT_DATE::timestamp with time zone) + '1 year'::interval)::date, COALESCE(( SELECT max(budget_plans.valid_from) AS max
                   FROM budget_plans), '1900-01-01'::date), COALESCE(( SELECT max(budgets.month) AS max
                   FROM budgets), '1900-01-01'::date), COALESCE(( SELECT date_trunc('month'::text, max(transactions.posted_on)::timestamp with time zone)::date AS date_trunc
                   FROM transactions
                  WHERE transactions.status <> 'void'::text), '1900-01-01'::date)) AS end_month
        ), months AS (
         SELECT generate_series(span.start_month::timestamp with time zone, span.end_month::timestamp with time zone, '1 mon'::interval)::date AS month
           FROM span
          WHERE span.start_month IS NOT NULL
        ), per_category AS (
         SELECT m.month,
            b.category_id
           FROM months m
             CROSS JOIN ( SELECT DISTINCT budget_plans.category_id
                   FROM budget_plans) b
        ), current_plan AS (
         SELECT pc.month,
            pc.category_id,
            p.amount,
            p.period,
            p.carry_forward,
            p.valid_from
           FROM per_category pc
             CROSS JOIN LATERAL ( SELECT bp.amount,
                    bp.period,
                    bp.carry_forward,
                    bp.valid_from
                   FROM budget_plans bp
                  WHERE bp.category_id = pc.category_id AND bp.valid_from <= pc.month
                  ORDER BY bp.valid_from DESC
                 LIMIT 1) p
        )
 SELECT cp.month,
    cp.category_id,
    c.name AS category,
    ct.full_path AS category_path,
    cp.period,
    cp.carry_forward,
    cp.amount AS plan_amount,
    round(cp.amount / budget_period_months(cp.period)::numeric, 2) AS accrual,
    o.amount AS override,
    COALESCE(o.amount, round(cp.amount / budget_period_months(cp.period)::numeric, 2)) AS budget
   FROM current_plan cp
     JOIN categories c ON c.id = cp.category_id
     LEFT JOIN v_category_tree ct ON ct.id = cp.category_id
     LEFT JOIN budgets o ON o.category_id = cp.category_id AND o.month = cp.month
  WHERE cp.amount IS NOT NULL;

-- Net worth from recorded statement balances, each account's latest
-- figure carried forward - so a date sums every account you have
-- ever recorded, not just the ones recorded that day. The accounts
-- column says how many that is; fewer than you have is a partial
-- total, visibly.
CREATE VIEW v_net_worth AS
 WITH dates AS (
         SELECT DISTINCT account_balances.as_of
           FROM account_balances
        ), recorded AS (
         SELECT DISTINCT account_balances.account_id
           FROM account_balances
        ), carried AS (
         SELECT d.as_of,
            r.account_id,
                CASE
                    WHEN (a.account_type IN ( SELECT v_liability_types.account_type
                       FROM v_liability_types)) THEN - latest.balance
                    ELSE latest.balance
                END AS balance
           FROM dates d
             CROSS JOIN recorded r
             JOIN accounts a ON a.id = r.account_id
             LEFT JOIN LATERAL ( SELECT b.balance
                   FROM account_balances b
                  WHERE b.account_id = r.account_id AND b.as_of <= d.as_of
                  ORDER BY b.as_of DESC
                 LIMIT 1) latest ON true
        )
 SELECT (as_of::timestamp without time zone AT TIME ZONE 'America/Denver'::text) AS "time",
    as_of,
    sum(balance) AS net_worth,
    count(balance) AS accounts
   FROM carried
  GROUP BY ((as_of::timestamp without time zone AT TIME ZONE 'America/Denver'::text)), as_of
  ORDER BY ((as_of::timestamp without time zone AT TIME ZONE 'America/Denver'::text));

-- Net worth per day from opening balances plus transactions, covering
-- every account. A Credit or Loan opening_balance is the amount
-- OWED and is subtracted; transactions are used as signed.
CREATE VIEW v_net_worth_daily AS
 WITH days AS (
         SELECT DISTINCT transactions.posted_on
           FROM transactions
          WHERE transactions.status <> 'void'::text
        ), per_account AS (
         SELECT d.posted_on,
            a.id AS account_id,
                CASE
                    WHEN (a.account_type IN ( SELECT v_liability_types.account_type
                       FROM v_liability_types)) THEN - a.opening_balance
                    ELSE a.opening_balance
                END + COALESCE(( SELECT sum(t.amount) AS sum
                   FROM transactions t
                  WHERE t.account_id = a.id AND t.status <> 'void'::text AND t.posted_on <= d.posted_on), 0::numeric) AS balance
           FROM days d
             CROSS JOIN accounts a
        )
 SELECT (posted_on::timestamp without time zone AT TIME ZONE 'America/Denver'::text) AS "time",
    posted_on,
    sum(balance) AS net_worth
   FROM per_account
  GROUP BY ((posted_on::timestamp without time zone AT TIME ZONE 'America/Denver'::text)), posted_on
  ORDER BY ((posted_on::timestamp without time zone AT TIME ZONE 'America/Denver'::text));

-- Every non-void transaction, flattened, with a timestamptz "time"
-- column at local midnight. is_internal fires on any of three
-- signals: a paired transfer, a Transfer/Receive direction in the
-- source file, or an is_internal category. Each is wrapped in
-- coalesce(..., false) - without it a row with no direction is
-- NULL, and WHERE NOT is_internal silently discards it.
CREATE VIEW v_transactions AS
 SELECT (t.posted_on::timestamp without time zone AT TIME ZONE 'America/Denver'::text) AS "time",
    t.id,
    t.account_id,
    a.name AS account,
    a.account_type,
    t.merchant_id,
    m.name AS merchant,
    t.amount,
    GREATEST(- t.amount, 0::numeric) AS outflow,
    GREATEST(t.amount, 0::numeric) AS inflow,
    t.transfer_group IS NOT NULL AS is_transfer,
    t.status,
    COALESCE(t.description, t.raw_description) AS description,
    t.raw_description,
    t.category_id,
    ct.depth AS category_depth,
    ct.full_path AS category_path,
    ct.id_path AS category_id_path,
    COALESCE(ct.roll1, 'Uncategorized'::text) AS roll1,
    COALESCE(ct.roll2, 'Uncategorized'::text) AS roll2,
    COALESCE(ct.roll3, 'Uncategorized'::text) AS roll3,
    t.location_id,
    t.direction,
    t.transfer_group IS NOT NULL OR COALESCE(t.direction = ANY (ARRAY['Transfer'::text, 'Receive'::text]), false) OR COALESCE(ct.internal, false) AS is_internal,
    t.source_merchant_ref,
    t.source_location_ref,
    t.tax,
    t.discount,
    t.posted_on,
    t.transacted_on
   FROM transactions t
     JOIN accounts a ON a.id = t.account_id
     LEFT JOIN merchants m ON m.id = t.merchant_id
     LEFT JOIN v_category_tree ct ON ct.id = t.category_id
  WHERE t.status <> 'void'::text;

-- Money from outside, internal movement excluded.
CREATE VIEW v_income AS
 SELECT "time",
    id,
    account_id,
    account,
    account_type,
    merchant_id,
    merchant,
    amount,
    outflow,
    inflow,
    is_transfer,
    status,
    description,
    raw_description,
    category_id,
    category_depth,
    category_path,
    category_id_path,
    roll1,
    roll2,
    roll3,
    location_id,
    direction,
    is_internal,
    source_merchant_ref,
    source_location_ref,
    tax,
    discount,
    posted_on,
    transacted_on
   FROM v_transactions
  WHERE NOT is_internal AND inflow > 0::numeric;

-- Income, spending, net and internal movement by month.
CREATE VIEW v_monthly_cashflow AS
 SELECT (date_trunc('month'::text, posted_on::timestamp with time zone)::timestamp without time zone AT TIME ZONE 'America/Denver'::text) AS "time",
    date_trunc('month'::text, posted_on::timestamp with time zone)::date AS month,
    sum(inflow) FILTER (WHERE NOT is_internal) AS income,
    sum(outflow) FILTER (WHERE NOT is_internal) AS spending,
    sum(inflow) FILTER (WHERE NOT is_internal) - sum(outflow) FILTER (WHERE NOT is_internal) AS net,
    sum(outflow) FILTER (WHERE is_internal) AS internal_out,
    sum(inflow) FILTER (WHERE is_internal) AS internal_in
   FROM v_transactions
  GROUP BY ((date_trunc('month'::text, posted_on::timestamp with time zone)::timestamp without time zone AT TIME ZONE 'America/Denver'::text)), (date_trunc('month'::text, posted_on::timestamp with time zone)::date);

-- Computed balance against what the bank reported.
CREATE VIEW v_reconciliation AS
 WITH computed AS (
         SELECT v_account_latest_balance.account_id,
            v_account_latest_balance.balance,
            v_account_latest_balance.posted_on
           FROM v_account_latest_balance
        ), statements AS (
         SELECT DISTINCT ON (transactions.account_id) transactions.account_id,
            transactions.posted_on,
            transactions.statement_balance
           FROM transactions
          WHERE transactions.statement_balance IS NOT NULL AND transactions.status <> 'void'::text
          ORDER BY transactions.account_id, transactions.posted_on DESC, transactions.id DESC
        ), snapshots AS (
         SELECT DISTINCT ON (account_balances.account_id) account_balances.account_id,
            account_balances.as_of,
            account_balances.balance
           FROM account_balances
          ORDER BY account_balances.account_id, account_balances.as_of DESC
        )
 SELECT a.id AS account_id,
    a.name AS account,
    a.account_type,
    a.opening_balance,
    c.balance AS computed_balance,
    c.posted_on AS computed_through,
    s.statement_balance,
    s.posted_on AS statement_through,
    round(c.balance - s.statement_balance, 2) AS statement_diff,
    n.balance AS snapshot_balance,
    n.as_of AS snapshot_as_of,
    round(c.balance - n.balance, 2) AS snapshot_diff
   FROM accounts a
     LEFT JOIN computed c ON c.account_id = a.id
     LEFT JOIN statements s ON s.account_id = a.id
     LEFT JOIN snapshots n ON n.account_id = a.id;

-- Outflows only, internal movement excluded - so a credit card payment
-- is not counted as spending alongside the purchases it settles.
-- Most panels read this.
CREATE VIEW v_spending AS
 SELECT "time",
    id,
    account_id,
    account,
    account_type,
    merchant_id,
    merchant,
    amount,
    outflow,
    inflow,
    is_transfer,
    status,
    description,
    raw_description,
    category_id,
    category_depth,
    category_path,
    category_id_path,
    roll1,
    roll2,
    roll3,
    location_id,
    direction,
    is_internal,
    source_merchant_ref,
    source_location_ref,
    tax,
    discount,
    posted_on,
    transacted_on
   FROM v_transactions
  WHERE NOT is_internal AND outflow > 0::numeric;

-- Per-tag spending by month.
CREATE VIEW v_tag_monthly AS
 SELECT t.name AS tag,
    date_trunc('month'::text, v.posted_on::timestamp with time zone)::date AS month,
    COALESCE(sum(v.outflow) FILTER (WHERE NOT v.is_internal), 0::numeric) AS spend,
    COALESCE(sum(v.inflow) FILTER (WHERE NOT v.is_internal), 0::numeric) AS received,
    count(*) AS transactions
   FROM tags t
     JOIN transaction_tags tt ON tt.tag_id = t.id
     JOIN v_transactions v ON v.id = tt.transaction_id
  GROUP BY t.name, (date_trunc('month'::text, v.posted_on::timestamp with time zone));

-- Per-tag totals, all-time. Rows are independent: a two-tag
-- transaction appears under both, so this column does NOT sum to
-- total spending.
CREATE VIEW v_tag_spend AS
 SELECT t.id AS tag_id,
    t.name AS tag,
    t.note,
    count(v.id) AS transactions,
    count(v.id) FILTER (WHERE v.is_internal) AS internal_transactions,
    COALESCE(sum(v.outflow) FILTER (WHERE NOT v.is_internal), 0::numeric) AS spend,
    COALESCE(sum(v.inflow) FILTER (WHERE NOT v.is_internal), 0::numeric) AS received,
    COALESCE(sum(v.outflow) FILTER (WHERE NOT v.is_internal), 0::numeric) - COALESCE(sum(v.inflow) FILTER (WHERE NOT v.is_internal), 0::numeric) AS net,
    min(v.posted_on) AS first_seen,
    max(v.posted_on) AS last_seen,
    count(DISTINCT date_trunc('month'::text, v.posted_on::timestamp with time zone)) AS months_active,
    count(DISTINCT v.category_id) AS categories_spanned,
    count(DISTINCT v.account_id) AS accounts_spanned
   FROM tags t
     LEFT JOIN transaction_tags tt ON tt.tag_id = t.id
     LEFT JOIN v_transactions v ON v.id = tt.transaction_id
  GROUP BY t.id, t.name, t.note;

-- Budget against actual. Actuals roll up through category_id_path, so
-- a budget on a top-level category covers its descendants.
-- envelope_balance is floored at zero - an overspent envelope resets
-- instead of carrying a deficit - and the forgiven amount surfaces as
-- envelope_shortfall in the month it ran dry.
CREATE VIEW v_budget_vs_actual AS
 WITH actual AS (
         SELECT bm.month,
            bm.category_id,
            COALESCE(sum(v.outflow), 0::numeric) AS spent
           FROM v_budget_months bm
             LEFT JOIN v_spending v ON date_trunc('month'::text, v.posted_on::timestamp with time zone)::date = bm.month AND (bm.category_id = ANY (v.category_id_path))
          GROUP BY bm.month, bm.category_id
        ), joined AS (
         SELECT bm.month,
            bm.category_id,
            bm.category,
            bm.category_path,
            bm.period,
            bm.carry_forward,
            bm.budget,
            bm.override,
            a.spent,
            bm.budget - a.spent AS variance,
            ( SELECT bp.opening_balance
                   FROM budget_plans bp
                  WHERE bp.category_id = bm.category_id AND bp.valid_from <= bm.month AND bp.opening_balance IS NOT NULL
                  ORDER BY bp.valid_from DESC
                 LIMIT 1) AS seed,
            ( SELECT max(bp.valid_from) AS max
                   FROM budget_plans bp
                  WHERE bp.category_id = bm.category_id AND bp.valid_from <= bm.month AND bp.opening_balance IS NOT NULL) AS seed_from
           FROM v_budget_months bm
             JOIN actual a ON a.month = bm.month AND a.category_id = bm.category_id
        ), running AS (
         SELECT j.month,
            j.category_id,
            j.category,
            j.category_path,
            j.period,
            j.carry_forward,
            j.budget,
            j.override,
            j.spent,
            j.variance,
            j.seed,
            j.seed_from,
                CASE
                    WHEN j.carry_forward THEN COALESCE(j.seed, 0::numeric) + sum(j.variance) OVER (PARTITION BY j.category_id, j.seed_from ORDER BY j.month ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                    ELSE NULL::numeric
                END AS raw_balance
           FROM joined j
        ), floored AS (
         SELECT r.month,
            r.category_id,
            r.category,
            r.category_path,
            r.period,
            r.carry_forward,
            r.budget,
            r.override,
            r.spent,
            r.variance,
            r.seed,
            r.seed_from,
            r.raw_balance,
                CASE
                    WHEN r.carry_forward THEN - LEAST(0::numeric, min(r.raw_balance) OVER (PARTITION BY r.category_id, r.seed_from ORDER BY r.month ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW))
                    ELSE NULL::numeric
                END AS forgiven
           FROM running r
        )
 SELECT month,
    category_id,
    category,
    category_path,
    period,
    carry_forward,
    budget,
    override IS NOT NULL AS is_override,
    spent,
    variance,
    round(100.0 * spent / NULLIF(budget, 0::numeric), 1) AS pct_used,
    raw_balance + forgiven AS envelope_balance,
        CASE
            WHEN carry_forward THEN forgiven - COALESCE(lag(forgiven) OVER (PARTITION BY category_id, seed_from ORDER BY month), 0::numeric)
            ELSE NULL::numeric
        END AS envelope_shortfall
   FROM floored;

-- How much spending has a place attached.
CREATE VIEW v_location_coverage AS
 SELECT count(*) AS spending_rows,
    count(*) FILTER (WHERE location_id IS NOT NULL) AS with_location,
    round(100.0 * count(*) FILTER (WHERE location_id IS NOT NULL)::numeric / NULLIF(count(*), 0)::numeric, 1) AS pct_with_location
   FROM v_spending;

-- Monthly totals by top-level category.
CREATE VIEW v_monthly_category_spend AS
 SELECT (date_trunc('month'::text, posted_on::timestamp with time zone)::timestamp without time zone AT TIME ZONE 'America/Denver'::text) AS "time",
    date_trunc('month'::text, posted_on::timestamp with time zone)::date AS month,
    roll1 AS category,
    sum(outflow) AS spend
   FROM v_spending
  GROUP BY ((date_trunc('month'::text, posted_on::timestamp with time zone)::timestamp without time zone AT TIME ZONE 'America/Denver'::text)), (date_trunc('month'::text, posted_on::timestamp with time zone)::date), roll1;

-- Spending rows carrying coordinates - the map.
CREATE VIEW v_spending_geo AS
 SELECT v."time",
    v.posted_on,
    v.id,
    v.account,
    v.amount,
    v.outflow,
    v.merchant,
    v.roll1,
    v.roll2,
    v.roll3,
    v.category_path,
    v.description,
    l.id AS location_id,
    l.latitude,
    l.longitude,
    l.label AS place,
    l.address,
    l.city,
    l.state,
    l.zip,
    l.country,
    COALESCE(v.merchant, l.label, l.address, l.city, 'Unknown'::text) AS place_label,
    l.popularity
   FROM v_spending v
     JOIN locations l ON l.id = v.location_id
  WHERE l.latitude IS NOT NULL AND l.longitude IS NOT NULL;

-- Where a tag's money lands in the category tree.
CREATE VIEW v_tag_category AS
 SELECT t.name AS tag,
    v.roll1 AS category,
    v.roll2 AS subcategory,
    COALESCE(v.category_path, 'Uncategorized'::text) AS category_path,
    count(*) AS transactions,
    COALESCE(sum(v.outflow), 0::numeric) AS spend
   FROM tags t
     JOIN transaction_tags tt ON tt.tag_id = t.id
     JOIN v_spending v ON v.id = tt.transaction_id
  GROUP BY t.name, v.roll1, v.roll2, (COALESCE(v.category_path, 'Uncategorized'::text));

-- This month only, plus months_of_cover for envelope categories.
CREATE VIEW v_budget_status AS
 SELECT month,
    category_id,
    category,
    category_path,
    period,
    carry_forward,
    budget,
    is_override,
    spent,
    variance,
    pct_used,
    envelope_balance,
    envelope_shortfall,
        CASE
            WHEN carry_forward AND budget > 0::numeric THEN round(envelope_balance / budget, 1)
            ELSE NULL::numeric
        END AS months_of_cover
   FROM v_budget_vs_actual b
  WHERE month = date_trunc('month'::text, CURRENT_DATE::timestamp with time zone)::date;

-- =====================================================================
-- Comments.
-- =====================================================================

COMMENT ON VIEW v_budget_status IS 'This month only. months_of_cover can no longer be negative, so an envelope in trouble shows as a zero balance plus a shortfall.';
COMMENT ON VIEW v_budget_vs_actual IS 'Budget against actual. envelope_balance is floored at zero: an overspent envelope resets rather than carrying a deficit, and the amount forgiven appears as envelope_shortfall in that month.';
COMMENT ON VIEW v_liability_types IS 'Account types whose hand-entered balance (opening_balance, or a recorded account_balances row) means the amount owed, so net worth subtracts it. Does not affect imported transactions, which are already signed money-out-is-negative.';
COMMENT ON VIEW v_net_worth IS 'Net worth from recorded statement balances, each account''s latest balance carried forward. `accounts` is how many accounts the figure covers - fewer than you have means a partial total. A Credit or Loan balance is the amount owed and is subtracted.';
COMMENT ON VIEW v_net_worth_daily IS 'Net worth per day from opening balances plus transactions, covering every account. A Credit or Loan opening_balance is the amount owed and is subtracted; transactions are used as signed.';
COMMENT ON VIEW v_tag_spend IS 'Per-tag totals. Rows are independent: a two-tag transaction appears under both, so the spend column does not sum to total spending.';
COMMENT ON VIEW v_transaction_tags IS 'Tags collapsed to one row per transaction - join this, never transaction_tags, or spending multiplies by the tag count.';
