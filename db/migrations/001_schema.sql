-- =====================================================================
-- 001_schema.sql - tables, indexes, functions and triggers.
-- 
-- This is the baseline: the schema as it actually runs, generated from
-- a live database and verified by rebuilding from this file and diffing
-- the result. It replaces the incremental migrations the schema was
-- developed through - there is no history to replay.
-- 
-- Conventions worth knowing before reading on:
-- 
--   * transactions.amount is SIGNED from your point of view. Negative
--     left your accounts, positive arrived. Every view follows this.
--   * A transaction has exactly ONE category and ANY NUMBER of tags.
--   * Money is numeric, never float.
--   * A BALANCE you type in on a Credit or Loan account is the amount
--     OWED - positive is debt - and net worth subtracts it. Imported
--     transactions are signed as above on every account type.
--   * Views are the interface. Panels and scripts read views, never
--     tables, so the schema can change underneath them.
-- =====================================================================

-- =====================================================================
-- Tables
-- =====================================================================

-- What the bank said, for reconciliation.
--

CREATE TABLE public.account_balances (
    account_id integer NOT NULL,
    as_of date NOT NULL,
    balance numeric(18,2) NOT NULL,
    source text
);

-- active is generated from closed, so it cannot drift. On a Credit or
-- Loan account, opening_balance is the amount OWED - positive is
-- debt - and net worth subtracts it. See v_liability_types.
--

CREATE TABLE public.accounts (
    id integer NOT NULL,
    name text NOT NULL,
    account_type text NOT NULL,
    purpose text,
    institution text,
    last4 text,
    currency text DEFAULT 'USD'::text NOT NULL,
    opened date,
    closed date,
    active boolean GENERATED ALWAYS AS ((closed IS NULL)) STORED,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    opening_balance numeric(14,2) DEFAULT 0 NOT NULL,
    opening_balance_as_of date,
    CONSTRAINT accounts_account_type_check CHECK ((account_type = ANY (ARRAY['Checking'::text, 'Savings'::text, 'Credit'::text, 'Investment'::text, 'Loan'::text, 'Cash'::text, 'Retirement'::text, 'Crypto'::text, 'Other'::text]))),
    CONSTRAINT accounts_check CHECK (((closed IS NULL) OR (opened IS NULL) OR (closed >= opened))),
    CONSTRAINT accounts_last4_check CHECK ((last4 ~ '^[0-9]{4}$'::text))
);

-- Budget intent: one row per CHANGE, expanded to months by
-- v_budget_months. amount is the cost per PERIOD, not per month.
--

CREATE TABLE public.budget_plans (
    category_id integer NOT NULL,
    valid_from date NOT NULL,
    amount numeric(14,2),
    period text DEFAULT 'monthly'::text NOT NULL,
    carry_forward boolean DEFAULT false NOT NULL,
    opening_balance numeric(14,2),
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT budget_plans_amount_check CHECK (((amount IS NULL) OR (amount >= (0)::numeric))),
    CONSTRAINT budget_plans_period_check CHECK ((period = ANY (ARRAY['monthly'::text, 'quarterly'::text, 'semiannual'::text, 'annual'::text]))),
    CONSTRAINT budget_plans_valid_from_check CHECK ((EXTRACT(day FROM valid_from) = (1)::numeric))
);

-- Single-month overrides that beat the plan for that month only.
--

CREATE TABLE public.budgets (
    category_id integer NOT NULL,
    month date NOT NULL,
    amount numeric(14,2) NOT NULL,
    CONSTRAINT budgets_month_check CHECK ((EXTRACT(day FROM month) = (1)::numeric))
);

-- Self-referencing tree. UNIQUE NULLS NOT DISTINCT lets the top level
-- be unique too, which a plain UNIQUE would not. is_internal marks
-- a branch as movement between your own accounts rather than
-- spending; it is inherited by descendants through v_category_tree.
--

CREATE TABLE public.categories (
    id integer NOT NULL,
    parent_id integer,
    name text NOT NULL,
    is_internal boolean DEFAULT false NOT NULL,
    CONSTRAINT categories_check CHECK ((parent_id IS DISTINCT FROM id))
);

-- One row per file imported.
--

CREATE TABLE public.import_batches (
    id bigint NOT NULL,
    source_file text NOT NULL,
    file_sha256 text NOT NULL,
    mapping text NOT NULL,
    account_id integer,
    rows_read integer DEFAULT 0 NOT NULL,
    rows_inserted integer DEFAULT 0 NOT NULL,
    rows_duplicate integer DEFAULT 0 NOT NULL,
    rows_error integer DEFAULT 0 NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone
);

-- A place. Identity is coordinates AND name: two shops a few metres
-- apart must stay separate, so uq_locations_coord_place keys on both.
--

CREATE TABLE public.locations (
    id integer NOT NULL,
    address text,
    city text,
    state text,
    zip text,
    country text DEFAULT 'US'::text NOT NULL,
    address_key text GENERATED ALWAYS AS (lower(((((((((COALESCE(address, ''::text) || '|'::text) || COALESCE(city, ''::text)) || '|'::text) || COALESCE(state, ''::text)) || '|'::text) || COALESCE(zip, ''::text)) || '|'::text) || country))) STORED,
    latitude numeric(8,6),
    longitude numeric(9,6),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    label text,
    source text,
    coord_key text GENERATED ALWAYS AS (
CASE
    WHEN ((latitude IS NOT NULL) AND (longitude IS NOT NULL)) THEN (((round(latitude, 5))::text || ','::text) || (round(longitude, 5))::text)
    ELSE NULL::text
END) STORED,
    external_ref text,
    popularity numeric(12,9),
    CONSTRAINT locations_coords_paired CHECK (((latitude IS NULL) = (longitude IS NULL))),
    CONSTRAINT locations_latitude_check CHECK (((latitude >= ('-90'::integer)::numeric) AND (latitude <= (90)::numeric))),
    CONSTRAINT locations_longitude_check CHECK (((longitude >= ('-180'::integer)::numeric) AND (longitude <= (180)::numeric))),
    CONSTRAINT locations_not_empty CHECK ((((latitude IS NOT NULL) AND (longitude IS NOT NULL)) OR (address IS NOT NULL) OR (city IS NOT NULL) OR (zip IS NOT NULL))),
    CONSTRAINT locations_popularity_check CHECK ((popularity >= (0)::numeric))
);

-- Description patterns mapped to a merchant, highest priority first.
--

CREATE TABLE public.merchant_aliases (
    id integer NOT NULL,
    merchant_id integer NOT NULL,
    pattern text NOT NULL,
    match_type text DEFAULT 'like'::text NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    category_id integer,
    CONSTRAINT merchant_aliases_match_type_check CHECK ((match_type = ANY (ARRAY['like'::text, 'regex'::text])))
);

-- One row per business, however many ways a bank spells it.
--

CREATE TABLE public.merchants (
    id integer NOT NULL,
    name text NOT NULL,
    description text,
    default_category_id integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    external_ref text
);

-- Named things bought, referenced by transaction_items.
--

CREATE TABLE public.products (
    id bigint NOT NULL,
    name text NOT NULL,
    brand text,
    product_code text,
    description text,
    default_category_id integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

-- Every imported row kept verbatim, which is what makes replay
-- possible when a rule changes.
--

CREATE TABLE public.staging_rows (
    id bigint NOT NULL,
    batch_id bigint NOT NULL,
    line_no integer NOT NULL,
    raw jsonb NOT NULL,
    transaction_id bigint,
    status text NOT NULL,
    error text,
    warning text,
    CONSTRAINT staging_rows_status_check CHECK ((status = ANY (ARRAY['inserted'::text, 'duplicate'::text, 'error'::text])))
);

-- Freeform labels, many per transaction. Names normalise lowercase.
--

CREATE TABLE public.tags (
    id integer NOT NULL,
    name text NOT NULL,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

-- Receipt lines, when an import carries them.
--

CREATE TABLE public.transaction_items (
    id bigint NOT NULL,
    transaction_id bigint NOT NULL,
    product_id bigint,
    description text,
    quantity numeric(12,3) DEFAULT 1 NOT NULL,
    unit_price numeric(14,4),
    amount numeric(14,2) NOT NULL,
    category_id integer,
    CONSTRAINT transaction_items_check CHECK (((product_id IS NOT NULL) OR (description IS NOT NULL)))
);

-- The many-to-many join. See v_transaction_tags.
--

CREATE TABLE public.transaction_tags (
    transaction_id bigint NOT NULL,
    tag_id integer NOT NULL
);

-- amount is signed from your point of view: negative left your
-- accounts, positive arrived. direction records what the source file
-- claimed, and a CHECK keeps the two consistent.
--

CREATE TABLE public.transactions (
    id bigint NOT NULL,
    account_id integer NOT NULL,
    transacted_on date,
    posted_on date NOT NULL,
    amount numeric(14,2) NOT NULL,
    tax numeric(14,2) DEFAULT 0 NOT NULL,
    discount numeric(14,2) DEFAULT 0 NOT NULL,
    merchant_id integer,
    location_id integer,
    category_id integer,
    transfer_group uuid,
    description text,
    raw_description text,
    statement_balance numeric(14,2),
    status text DEFAULT 'posted'::text NOT NULL,
    source text DEFAULT 'manual'::text NOT NULL,
    external_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    import_batch_id bigint,
    direction text,
    source_merchant_ref text,
    source_location_ref text,
    CONSTRAINT transactions_direction_check CHECK ((direction = ANY (ARRAY['Withdraw'::text, 'Deposit'::text, 'Transfer'::text, 'Receive'::text]))),
    CONSTRAINT transactions_direction_sign CHECK (((direction IS NULL) OR ((direction = ANY (ARRAY['Withdraw'::text, 'Transfer'::text])) AND (amount <= (0)::numeric)) OR ((direction = ANY (ARRAY['Deposit'::text, 'Receive'::text])) AND (amount >= (0)::numeric)))),
    CONSTRAINT transactions_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'posted'::text, 'void'::text])))
);

-- =====================================================================
-- Sequences
-- =====================================================================
--

ALTER TABLE public.accounts ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.accounts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
--

ALTER TABLE public.categories ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.categories_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
--

ALTER TABLE public.import_batches ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.import_batches_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
--

ALTER TABLE public.locations ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.locations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
--

ALTER TABLE public.merchant_aliases ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.merchant_aliases_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
--

ALTER TABLE public.merchants ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.merchants_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
--

ALTER TABLE public.products ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.products_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
--

ALTER TABLE public.staging_rows ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.staging_rows_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
--

ALTER TABLE public.tags ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.tags_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
--

ALTER TABLE public.transaction_items ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.transaction_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
--

ALTER TABLE public.transactions ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.transactions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

-- =====================================================================
-- Primary keys, unique keys and checks
-- =====================================================================
--

ALTER TABLE ONLY public.account_balances
    ADD CONSTRAINT account_balances_pkey PRIMARY KEY (account_id, as_of);
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT accounts_name_key UNIQUE (name);
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT accounts_pkey PRIMARY KEY (id);
--

ALTER TABLE ONLY public.budget_plans
    ADD CONSTRAINT budget_plans_pkey PRIMARY KEY (category_id, valid_from);
--

ALTER TABLE ONLY public.budgets
    ADD CONSTRAINT budgets_pkey PRIMARY KEY (category_id, month);
--

ALTER TABLE ONLY public.categories
    ADD CONSTRAINT categories_parent_id_name_key UNIQUE NULLS NOT DISTINCT (parent_id, name);
--

ALTER TABLE ONLY public.categories
    ADD CONSTRAINT categories_pkey PRIMARY KEY (id);
--

ALTER TABLE ONLY public.import_batches
    ADD CONSTRAINT import_batches_pkey PRIMARY KEY (id);
--

ALTER TABLE ONLY public.locations
    ADD CONSTRAINT locations_pkey PRIMARY KEY (id);
--

ALTER TABLE ONLY public.merchant_aliases
    ADD CONSTRAINT merchant_aliases_pattern_key UNIQUE (pattern);
--

ALTER TABLE ONLY public.merchant_aliases
    ADD CONSTRAINT merchant_aliases_pkey PRIMARY KEY (id);
--

ALTER TABLE ONLY public.merchants
    ADD CONSTRAINT merchants_name_key UNIQUE (name);
--

ALTER TABLE ONLY public.merchants
    ADD CONSTRAINT merchants_pkey PRIMARY KEY (id);
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_name_brand_key UNIQUE NULLS NOT DISTINCT (name, brand);
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_pkey PRIMARY KEY (id);
--

ALTER TABLE ONLY public.staging_rows
    ADD CONSTRAINT staging_rows_pkey PRIMARY KEY (id);
--

ALTER TABLE ONLY public.tags
    ADD CONSTRAINT tags_name_key UNIQUE (name);
--

ALTER TABLE ONLY public.tags
    ADD CONSTRAINT tags_pkey PRIMARY KEY (id);
--

ALTER TABLE ONLY public.transaction_items
    ADD CONSTRAINT transaction_items_pkey PRIMARY KEY (id);
--

ALTER TABLE ONLY public.transaction_tags
    ADD CONSTRAINT transaction_tags_pkey PRIMARY KEY (transaction_id, tag_id);
--

ALTER TABLE ONLY public.transactions
    ADD CONSTRAINT transactions_account_id_external_id_key UNIQUE (account_id, external_id);
--

ALTER TABLE ONLY public.transactions
    ADD CONSTRAINT transactions_pkey PRIMARY KEY (id);

-- =====================================================================
-- Foreign keys
-- =====================================================================
--

ALTER TABLE ONLY public.account_balances
    ADD CONSTRAINT account_balances_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id);
--

ALTER TABLE ONLY public.budget_plans
    ADD CONSTRAINT budget_plans_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.categories(id);
--

ALTER TABLE ONLY public.budgets
    ADD CONSTRAINT budgets_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.categories(id);
--

ALTER TABLE ONLY public.categories
    ADD CONSTRAINT categories_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.categories(id);
--

ALTER TABLE ONLY public.import_batches
    ADD CONSTRAINT import_batches_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id);
--

ALTER TABLE ONLY public.merchant_aliases
    ADD CONSTRAINT merchant_aliases_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.categories(id);
--

ALTER TABLE ONLY public.merchant_aliases
    ADD CONSTRAINT merchant_aliases_merchant_id_fkey FOREIGN KEY (merchant_id) REFERENCES public.merchants(id) ON DELETE CASCADE;
--

ALTER TABLE ONLY public.merchants
    ADD CONSTRAINT merchants_default_category_id_fkey FOREIGN KEY (default_category_id) REFERENCES public.categories(id);
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_default_category_id_fkey FOREIGN KEY (default_category_id) REFERENCES public.categories(id);
--

ALTER TABLE ONLY public.staging_rows
    ADD CONSTRAINT staging_rows_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.import_batches(id) ON DELETE CASCADE;
--

ALTER TABLE ONLY public.staging_rows
    ADD CONSTRAINT staging_rows_transaction_id_fkey FOREIGN KEY (transaction_id) REFERENCES public.transactions(id) ON DELETE SET NULL;
--

ALTER TABLE ONLY public.transaction_items
    ADD CONSTRAINT transaction_items_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.categories(id);
--

ALTER TABLE ONLY public.transaction_items
    ADD CONSTRAINT transaction_items_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id);
--

ALTER TABLE ONLY public.transaction_items
    ADD CONSTRAINT transaction_items_transaction_id_fkey FOREIGN KEY (transaction_id) REFERENCES public.transactions(id) ON DELETE CASCADE;
--

ALTER TABLE ONLY public.transaction_tags
    ADD CONSTRAINT transaction_tags_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tags(id) ON DELETE CASCADE;
--

ALTER TABLE ONLY public.transaction_tags
    ADD CONSTRAINT transaction_tags_transaction_id_fkey FOREIGN KEY (transaction_id) REFERENCES public.transactions(id) ON DELETE CASCADE;
--

ALTER TABLE ONLY public.transactions
    ADD CONSTRAINT transactions_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id);
--

ALTER TABLE ONLY public.transactions
    ADD CONSTRAINT transactions_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.categories(id);
--

ALTER TABLE ONLY public.transactions
    ADD CONSTRAINT transactions_import_batch_id_fkey FOREIGN KEY (import_batch_id) REFERENCES public.import_batches(id);
--

ALTER TABLE ONLY public.transactions
    ADD CONSTRAINT transactions_location_id_fkey FOREIGN KEY (location_id) REFERENCES public.locations(id);
--

ALTER TABLE ONLY public.transactions
    ADD CONSTRAINT transactions_merchant_id_fkey FOREIGN KEY (merchant_id) REFERENCES public.merchants(id);


--
-- PostgreSQL database dump complete
--

-- =====================================================================
-- Indexes - Postgres does not index foreign keys for you
-- =====================================================================
--

CREATE INDEX idx_budget_plans_category ON public.budget_plans USING btree (category_id, valid_from);
--

CREATE INDEX idx_categories_parent ON public.categories USING btree (parent_id);
--

CREATE INDEX idx_import_batches_hash ON public.import_batches USING btree (file_sha256);
--

CREATE INDEX idx_items_transaction ON public.transaction_items USING btree (transaction_id);
--

CREATE INDEX idx_locations_city ON public.locations USING btree (city);
--

CREATE INDEX idx_locations_latlon ON public.locations USING btree (latitude, longitude) WHERE (latitude IS NOT NULL);
--

CREATE INDEX idx_locations_popularity ON public.locations USING btree (popularity DESC) WHERE (popularity IS NOT NULL);
--

CREATE INDEX idx_staging_batch ON public.staging_rows USING btree (batch_id);
--

CREATE INDEX idx_staging_status ON public.staging_rows USING btree (status) WHERE (status <> 'inserted'::text);
--

CREATE INDEX idx_staging_warning ON public.staging_rows USING btree (batch_id) WHERE (warning IS NOT NULL);
--

CREATE INDEX idx_transactions_account_posted ON public.transactions USING btree (account_id, posted_on);
--

CREATE INDEX idx_transactions_category ON public.transactions USING btree (category_id);
--

CREATE INDEX idx_transactions_direction ON public.transactions USING btree (direction) WHERE (direction IS NOT NULL);
--

CREATE INDEX idx_transactions_location_ref ON public.transactions USING btree (source_location_ref) WHERE (source_location_ref IS NOT NULL);
--

CREATE INDEX idx_transactions_merchant ON public.transactions USING btree (merchant_id);
--

CREATE INDEX idx_transactions_merchant_ref ON public.transactions USING btree (source_merchant_ref) WHERE (source_merchant_ref IS NOT NULL);
--

CREATE INDEX idx_transactions_no_location ON public.transactions USING btree (account_id, posted_on) WHERE (location_id IS NULL);
--

CREATE INDEX idx_transactions_posted ON public.transactions USING btree (posted_on);
--

CREATE INDEX idx_transactions_transfer ON public.transactions USING btree (transfer_group) WHERE (transfer_group IS NOT NULL);
--

CREATE INDEX idx_transactions_unmatched ON public.transactions USING btree (account_id, posted_on) WHERE (merchant_id IS NULL);
--

CREATE UNIQUE INDEX uq_locations_address ON public.locations USING btree (address_key) WHERE ((latitude IS NULL) AND ((address IS NOT NULL) OR (city IS NOT NULL) OR (zip IS NOT NULL)));
--

CREATE UNIQUE INDEX uq_locations_coord_place ON public.locations USING btree (coord_key, lower(btrim(COALESCE(label, ''::text)))) WHERE (coord_key IS NOT NULL);
--

CREATE UNIQUE INDEX uq_locations_external_ref ON public.locations USING btree (external_ref) WHERE (external_ref IS NOT NULL);
--

CREATE UNIQUE INDEX uq_merchants_external_ref ON public.merchants USING btree (external_ref) WHERE (external_ref IS NOT NULL);
--

CREATE UNIQUE INDEX uq_products_code ON public.products USING btree (product_code) WHERE (product_code IS NOT NULL);

-- =====================================================================
-- Functions
-- =====================================================================

-- monthly/quarterly/semiannual/annual -> 1/3/6/12.
--

CREATE FUNCTION public.budget_period_months(p text) RETURNS integer
    LANGUAGE sql IMMUTABLE STRICT
    AS $$
    SELECT CASE p
               WHEN 'monthly'    THEN 1
               WHEN 'quarterly'  THEN 3
               WHEN 'semiannual' THEN 6
               WHEN 'annual'     THEN 12
           END
$$;

-- Lowercases and trims on the way in, so 'Reimbursable' and
-- 'reimbursable' cannot become two tags that look like one.
--

CREATE FUNCTION public.normalize_tag_name() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.name := lower(btrim(NEW.name));
    IF NEW.name = '' THEN
        RAISE EXCEPTION 'a tag needs a name';
    END IF;
    RETURN NEW;
END
$$;

-- Touches updated_at on write.
--

CREATE FUNCTION public.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

-- Attach a tag, creating it if new. Idempotent, and raises on a
-- transaction id that does not exist rather than doing nothing.
--

CREATE FUNCTION public.tag_transaction(txn bigint, tag_name text) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    resolved integer;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM transactions WHERE id = txn) THEN
        RAISE EXCEPTION 'no transaction with id %', txn;
    END IF;
    INSERT INTO tags (name) VALUES (tag_name)
        ON CONFLICT (name) DO NOTHING;
    SELECT id INTO resolved FROM tags WHERE name = lower(btrim(tag_name));
    INSERT INTO transaction_tags (transaction_id, tag_id)
        VALUES (txn, resolved)
        ON CONFLICT DO NOTHING;
END
$$;

-- Remove one tag from one transaction.
--

CREATE FUNCTION public.untag_transaction(txn bigint, tag_name text) RETURNS void
    LANGUAGE sql
    AS $$
    DELETE FROM transaction_tags tt
    USING tags t
    WHERE t.id = tt.tag_id
      AND tt.transaction_id = txn
      AND t.name = lower(btrim(tag_name));
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

-- =====================================================================
-- Triggers
-- =====================================================================
--

CREATE TRIGGER tags_normalize_name BEFORE INSERT OR UPDATE ON public.tags FOR EACH ROW EXECUTE FUNCTION public.normalize_tag_name();
--

CREATE TRIGGER trg_accounts_updated_at BEFORE UPDATE ON public.accounts FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
--

CREATE TRIGGER trg_transactions_updated_at BEFORE UPDATE ON public.transactions FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- =====================================================================
-- Comments.
-- 
-- These are visible from psql with \d+ and from SQL with
-- obj_description() / col_description(), which is where someone
-- looks before reading this file.
-- =====================================================================

COMMENT ON TABLE budget_plans IS 'Budget intent: one row per change, expanded to months by v_budget_months.';
COMMENT ON TABLE tags IS 'Freeform labels. Many per transaction; the category is the single one.';
COMMENT ON COLUMN categories.is_internal IS 'Spending in this category, and anything beneath it, is movement between your own accounts rather than money leaving your finances.';
COMMENT ON FUNCTION tag_transaction(bigint,text) IS 'Attach a tag, creating it if needed. Idempotent.';
COMMENT ON FUNCTION untag_transaction(bigint,text) IS 'Remove a tag from one transaction. The tag itself survives.';
