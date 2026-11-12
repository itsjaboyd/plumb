-- =====================================================================
-- 003_seed.sql - starter data, so the stack is usable on first boot.
--
-- A category tree three levels deep and a set of merchant rules. Both
-- are opinionated starting points, not fixtures: prune, rename and add
-- freely. Renaming a category keeps every transaction attached, because
-- transactions reference the id and not the name.
--
-- Every insert is ON CONFLICT DO NOTHING, so this is safe to re-run and
-- will not fight changes you have made.
-- =====================================================================

-- ---------------------------------------------------------------------
-- The category tree.
-- ---------------------------------------------------------------------
WITH tops AS (
    INSERT INTO categories (parent_id, name)
    VALUES (NULL, 'Income'),
           (NULL, 'Housing'),
           (NULL, 'Transportation'),
           (NULL, 'Food'),
           (NULL, 'Health'),
           (NULL, 'Personal'),
           (NULL, 'Entertainment'),
           (NULL, 'Financial'),
           (NULL, 'Transfer'),
           (NULL, 'Uncategorized')
    ON CONFLICT DO NOTHING
    RETURNING id, name
)
SELECT count(*) FROM tops;

-- Second level. The lookup by name is unambiguous because top-level
-- names are unique among rows with parent_id IS NULL.
INSERT INTO categories (parent_id, name)
SELECT p.id, child.name
FROM (VALUES
    ('Income',         'Salary'),
    ('Income',         'Bonus'),
    ('Income',         'Interest'),
    ('Income',         'Dividends'),
    ('Income',         'Reimbursement'),
    ('Income',         'Refund'),
    ('Income',         'Other Income'),

    ('Housing',        'Rent'),
    ('Housing',        'Mortgage'),
    ('Housing',        'Utilities'),
    ('Housing',        'Internet & Phone'),
    ('Housing',        'Maintenance'),
    ('Housing',        'Furnishings'),
    ('Housing',        'Insurance'),
    ('Housing',        'Property Tax'),

    ('Transportation', 'Fuel'),
    ('Transportation', 'Auto Payment'),
    ('Transportation', 'Auto Insurance'),
    ('Transportation', 'Auto Maintenance'),
    ('Transportation', 'Parking & Tolls'),
    ('Transportation', 'Public Transit'),
    ('Transportation', 'Rideshare'),

    ('Food',           'Groceries'),
    ('Food',           'Dining'),
    ('Food',           'Coffee'),
    ('Food',           'Alcohol'),
    ('Food',           'Delivery'),

    ('Health',         'Medical'),
    ('Health',         'Dental'),
    ('Health',         'Vision'),
    ('Health',         'Pharmacy'),
    ('Health',         'Health Insurance'),
    ('Health',         'Fitness'),

    ('Personal',       'Clothing'),
    ('Personal',       'Household Goods'),
    ('Personal',       'Personal Care'),
    ('Personal',       'Gifts'),
    ('Personal',       'Education'),
    ('Personal',       'Pets'),
    ('Personal',       'Childcare'),

    ('Entertainment',  'Subscriptions'),
    ('Entertainment',  'Streaming'),
    ('Entertainment',  'Hobbies'),
    ('Entertainment',  'Travel'),
    ('Entertainment',  'Events'),
    ('Entertainment',  'Games'),

    ('Financial',      'Bank Fees'),
    ('Financial',      'Interest Charged'),
    ('Financial',      'Taxes'),
    ('Financial',      'Investment Purchase'),
    ('Financial',      'Loan Payment'),
    ('Financial',      'Charity'),

    ('Transfer',       'Account Transfer'),
    ('Transfer',       'Credit Card Payment'),
    ('Transfer',       'Cash Withdrawal')
) AS child(parent, name)
JOIN categories p ON p.name = child.parent AND p.parent_id IS NULL
ON CONFLICT DO NOTHING;

-- A third level, to show how subdivisions work. Add your own as you find
-- you want them; the charts handle any depth.
INSERT INTO categories (parent_id, name)
SELECT p.id, child.name
FROM (VALUES
    ('Groceries',        'Produce'),
    ('Groceries',        'Meat & Seafood'),
    ('Groceries',        'Pantry'),
    ('Groceries',        'Beverages'),
    ('Groceries',        'Household'),
    ('Utilities',        'Electric'),
    ('Utilities',        'Gas'),
    ('Utilities',        'Water & Sewer'),
    ('Utilities',        'Trash'),
    ('Subscriptions',    'Software'),
    ('Subscriptions',    'News & Media'),
    ('Travel',           'Flights'),
    ('Travel',           'Lodging'),
    ('Travel',           'Car Rental')
) AS child(parent, name)
JOIN categories p ON p.name = child.parent AND p.parent_id IS NOT NULL
ON CONFLICT DO NOTHING;


-- ---------------------------------------------------------------------
-- Transfer is movement between your own accounts, not spending.
--
-- Without this a credit card payment counts as a purchase, alongside the
-- purchases it settles. The flag is inherited through v_category_tree, so
-- marking the branch root covers Account Transfer, Credit Card Payment
-- and Cash Withdrawal beneath it. Mark your own the same way:
--
--     UPDATE categories SET is_internal = true
--      WHERE id = (SELECT id FROM v_category_tree
--                   WHERE full_path = 'Financial > Loan Payment');
--
-- db/scripts/categories.sql section 5b lists what is currently flagged.
-- ---------------------------------------------------------------------
UPDATE categories SET is_internal = true
 WHERE parent_id IS NULL AND name = 'Transfer' AND NOT is_internal;


-- ---------------------------------------------------------------------
-- Merchant rules. A bank description matching the pattern resolves to
-- the merchant, and optionally to a category. Patterns are ILIKE unless
-- match_type is 'regex'; the lowest priority number wins.
--
-- Add your own with:
--   plumb add-alias --merchant "King Soopers" \
--                    --pattern "%KING SOOPERS%" --category "Food > Groceries"
-- ---------------------------------------------------------------------
-- Merchants, with the category they usually belong to.
INSERT INTO merchants (name, default_category_id)
SELECT seed.name, c.id
FROM (VALUES
    ('Netflix',          'Entertainment > Streaming'),
    ('Spotify',          'Entertainment > Streaming'),
    ('Amazon',           'Personal > Household Goods'),
    ('Whole Foods',      'Food > Groceries'),
    ('King Soopers',     'Food > Groceries'),
    ('Chipotle',         'Food > Dining'),
    ('United Airlines',  'Entertainment > Travel'),
    ('Xcel Energy',      'Housing > Utilities'),
    ('Uber',             'Transportation > Rideshare'),
    ('Shell',            'Transportation > Fuel')
) AS seed(name, path)
LEFT JOIN LATERAL (
    SELECT ct.id
    FROM v_category_tree ct
    WHERE ct.full_path = seed.path
) c ON true
ON CONFLICT (name) DO NOTHING;

-- Patterns. Note the two King Soopers rules: fuel at a grocery store is
-- transportation, not groceries, and the lower priority number makes the
-- more specific rule win.
INSERT INTO merchant_aliases (merchant_id, pattern, match_type, priority, category_id)
SELECT m.id, seed.pattern, seed.match_type, seed.priority, c.id
FROM (VALUES
    ('Netflix',         '%NETFLIX%',            'like',  100, NULL),
    ('Spotify',         '%SPOTIFY%',            'like',  100, NULL),
    ('Amazon',          '%AMAZON%',             'like',  100, NULL),
    ('Amazon',          '%AMZN MKTP%',          'like',  100, NULL),
    ('Whole Foods',     '%WHOLE FOODS%',        'like',  100, NULL),
    ('Whole Foods',     '%WHOLEFDS%',           'like',  100, NULL),
    ('King Soopers',    '%KING SOOPERS FUEL%',  'like',   10,
                        'Transportation > Fuel'),
    ('King Soopers',    '%KING SOOPERS%',       'like',  100, NULL),
    ('Chipotle',        '%CHIPOTLE%',           'like',  100, NULL),
    ('United Airlines', '%UNITED AIRLINES%',    'like',  100, NULL),
    ('Xcel Energy',     '%XCEL ENERGY%',        'like',  100, NULL),
    ('Uber',            '^UBER(?! EATS)',       'regex',  50, NULL),
    ('Uber',            '%UBER EATS%',          'like',   10, 'Food > Delivery'),
    ('Shell',           '%SHELL OIL%',          'like',  100, NULL)
) AS seed(merchant, pattern, match_type, priority, category_path)
JOIN merchants m ON m.name = seed.merchant
LEFT JOIN LATERAL (
    SELECT ct.id
    FROM v_category_tree ct
    WHERE ct.full_path = seed.category_path
) c ON true
ON CONFLICT (pattern) DO NOTHING;
