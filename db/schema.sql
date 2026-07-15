-- Winefornia Agent — full Supabase schema
-- Run this in Supabase SQL Editor.
-- Safe to re-run (all statements are idempotent).

-- ============================================================
-- UTILITY
-- ============================================================

create or replace function update_updated_at()
returns trigger language plpgsql
set search_path = ''                       -- linter 0011: pin search_path
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;


-- ============================================================
-- PRICING TIERS
-- ============================================================

create table if not exists pricing_tiers (
    id                          uuid primary key default gen_random_uuid(),
    tier_number                 integer unique,
    name                        text unique not null,
    channel                     text,                    -- B2B | DTC
    msrp_multiplier             numeric not null,
    discount_percent            numeric not null,
    requires_human_confirmation boolean default false,
    notes                       text,
    created_at                  timestamptz default now()
);

create index if not exists pricing_tiers_name_idx on pricing_tiers (lower(name));


-- ============================================================
-- CUSTOMERS
-- ============================================================

create table if not exists customers (
    id                  uuid primary key default gen_random_uuid(),
    square_customer_id  text unique,
    full_name           text,
    company             text,
    email               text,
    phone               text,
    tier_name           text references pricing_tiers (name),
    customer_type       text,                            -- wholesale | retail | restaurant | export | employee
    notes               text,
    square_created_at   timestamptz,
    created_at          timestamptz default now(),
    updated_at          timestamptz default now(),
    synced_at           timestamptz
);

create index if not exists customers_email_idx          on customers (lower(email));
create index if not exists customers_square_id_idx      on customers (square_customer_id);
create index if not exists customers_full_name_idx      on customers (lower(full_name));
create index if not exists customers_company_idx        on customers (lower(company));
create index if not exists customers_tier_idx           on customers (tier_name);

create or replace trigger customers_updated_at
    before update on customers
    for each row execute function update_updated_at();


-- ============================================================
-- PRODUCTS (wine catalog)
-- ============================================================

create table if not exists products (
    id                  uuid primary key default gen_random_uuid(),
    sku                 text unique,
    name                text not null,
    vintage             integer,
    size                text default '750ml',
    bottles_per_case    integer default 12,
    msrp_bottle_cents   integer,
    variable_pricing    boolean default false,
    tier_unavailable    text[] default '{}',             -- tiers that cannot order this product
    tier_prices         jsonb,                           -- per-tier bottle prices from the pricing sheet;
                                                         -- the pricing engine (product_service.py) reads this directly
    created_at          timestamptz default now(),
    updated_at          timestamptz default now()
);

alter table products add column if not exists tier_prices jsonb;

create index if not exists products_name_idx    on products (lower(name));
create index if not exists products_vintage_idx on products (vintage);
create unique index if not exists products_name_vintage_size_idx
    on products (lower(name), vintage, size)
    where vintage is not null;

create or replace trigger products_updated_at
    before update on products
    for each row execute function update_updated_at();


-- ============================================================
-- SQUARE ORDERS (historical + ongoing)
-- ============================================================

create table if not exists square_orders (
    id                  uuid primary key default gen_random_uuid(),
    square_order_id     text unique not null,
    square_customer_id  text,
    customer_id         uuid references customers (id),
    location_id         text,
    state               text,                            -- OPEN | COMPLETED | CANCELED
    total_money_cents   integer,
    currency            text default 'USD',
    line_items          jsonb,
    fulfillments        jsonb,
    order_created_at    timestamptz,
    order_updated_at    timestamptz,
    synced_at           timestamptz default now()
);

create index if not exists square_orders_customer_idx   on square_orders (square_customer_id);
create index if not exists square_orders_created_idx    on square_orders (order_created_at desc);
create index if not exists square_orders_customer_id_idx on square_orders (customer_id);
create index if not exists square_orders_state_idx      on square_orders (state);


-- ============================================================
-- SQUARE INVOICES (historical + ongoing)
-- ============================================================

create table if not exists square_invoices (
    id                  uuid primary key default gen_random_uuid(),
    square_invoice_id   text unique not null,
    square_order_id     text,
    square_customer_id  text,
    customer_id         uuid references customers (id),
    invoice_number      text,
    title               text,
    status              text,                            -- DRAFT | UNPAID | SCHEDULED | PAID | CANCELED | REFUNDED
    delivery_method     text,
    payment_schedule    text,
    total_money_cents   integer,
    due_date            date,
    paid_at             timestamptz,
    line_items          jsonb,
    invoice_created_at  timestamptz,
    invoice_updated_at  timestamptz,
    synced_at           timestamptz default now()
);

create index if not exists square_invoices_customer_idx    on square_invoices (square_customer_id);
create index if not exists square_invoices_customer_id_idx on square_invoices (customer_id);
create index if not exists square_invoices_status_idx      on square_invoices (status);
create index if not exists square_invoices_created_idx     on square_invoices (invoice_created_at desc);
create index if not exists square_invoices_order_idx       on square_invoices (square_order_id);


-- ============================================================
-- INVOICE LOGS (agent-created drafts)
-- ============================================================

create table if not exists invoice_logs (
    id                      uuid primary key default gen_random_uuid(),
    thread_id               text not null unique,
    sender_id               text,
    raw_message             text,
    customer_id             text,
    customer_name           text,
    customer_email          text,
    tier_name               text,
    line_items              jsonb,
    subtotal_cents          integer,
    discount_cents          integer,
    total_before_tax_cents  integer,
    shipping_cents          integer,
    payment_schedule        text,
    payment_methods         jsonb,
    approval                text,
    square_order_id         text,
    square_invoice_id       text,
    square_invoice_url      text,
    square_invoice_number   text,               -- Square's human number (#202468) — matches notification emails
    -- Validation loop: Square's own notification emails confirm the process
    -- actually worked. 'pending' = case open awaiting confirmation;
    -- 'created_confirmed' / 'paid_confirmed' = verified; 'legacy' = predates loop.
    verification_status     text not null default 'pending',
    verified_created_at     timestamptz,
    verified_paid_at        timestamptz,
    errors                  jsonb,
    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now()
);

create index if not exists invoice_logs_thread_id_idx    on invoice_logs (thread_id);
create index if not exists invoice_logs_square_invoice_number_idx on invoice_logs (square_invoice_number);
create index if not exists invoice_logs_customer_name_idx on invoice_logs (customer_name);
create index if not exists invoice_logs_created_at_idx   on invoice_logs (created_at desc);

create or replace trigger invoice_logs_updated_at
    before update on invoice_logs
    for each row execute function update_updated_at();


-- ============================================================
-- INVOICE CHAT TURNS (durable transcript of the invoice chat assistant)
-- Written best-effort by vertex_agent/invoice_chat_memory.py; read for
-- restart-safe case rehydration and months-later recall (past_conversations).
-- Applied to Supabase 2026-07-15 (migration invoice_chat_turns).
-- ============================================================

create table if not exists invoice_chat_turns (
    id          uuid primary key default gen_random_uuid(),
    case_key    text not null,               -- space|sender, see invoice_chat_memory.case_key
    user_id     text,
    role        text not null,               -- staff | assistant
    text        text not null,
    created_at  timestamptz not null default now()
);

create index if not exists invoice_chat_turns_case_idx    on invoice_chat_turns (case_key, created_at desc);
create index if not exists invoice_chat_turns_created_idx on invoice_chat_turns (created_at desc);


-- ============================================================
-- SYNC STATE (tracks last successful sync cursor per entity)
-- ============================================================

create table if not exists sync_state (
    entity      text primary key,                        -- customers | orders | invoices
    last_synced timestamptz,
    cursor      text,
    notes       text
);


-- ============================================================
-- CONTROL LAYER — agent_cases, trace_events, failure_labels
-- ============================================================

-- One row per user intent → outcome lifecycle (a "case")
create table if not exists agent_cases (
    case_id         text primary key,
    sender_id       text not null,
    user_id         text not null,
    thread_id       text,                               -- LangGraph thread_id
    raw_input       text,
    intent          text,
    agent           text,
    risk_level      text default 'low',                 -- low | medium | high | critical
    status          text default 'running',             -- running | completed | failed | escalated | abandoned
    final_response  text,
    outcome         text,                               -- success | failure | rejected | escalated | refused
    error_summary   text,
    created_at      timestamptz default now(),
    closed_at       timestamptz
);

create index if not exists agent_cases_sender_idx     on agent_cases (sender_id);
create index if not exists agent_cases_status_idx     on agent_cases (status);
create index if not exists agent_cases_created_idx    on agent_cases (created_at desc);
create index if not exists agent_cases_risk_idx       on agent_cases (risk_level);


-- One row per discrete event within a case (full audit trail)
create table if not exists trace_events (
    event_id    text primary key,
    case_id     text not null references agent_cases (case_id),
    event_type  text not null,                          -- input_received | intent_classified | guardrail_check
                                                        -- | tool_call | tool_result | interrupt_issued
                                                        -- | human_decision | output_generated | failure
    layer       text not null,                          -- supervisor | invoice_agent | guardrail | human | square | llm
    data        jsonb,
    latency_ms  integer,
    error       text,
    ts          timestamptz default now()
);

create index if not exists trace_events_case_id_idx   on trace_events (case_id);
create index if not exists trace_events_type_idx      on trace_events (event_type);
create index if not exists trace_events_ts_idx        on trace_events (ts desc);


-- One row per labeled failure (auto-created by control layer or manually labeled)
create table if not exists failure_labels (
    failure_id        text primary key,
    case_id           text not null references agent_cases (case_id),
    failure_type      text not null,                    -- see failure taxonomy in control_layer.py
    severity          text not null,                    -- low | medium | high | critical
    source            text,                             -- which node / service raised it
    responsible_layer text,
    description       text,
    suggested_patch   text,                             -- prompt | tool | guardrail | schema | routing | workflow
    patch_applied     boolean default false,
    eval_case_id      text,                             -- set when regression case created
    confidence        float default 1.0,
    created_at        timestamptz default now()
);

create index if not exists failure_labels_case_id_idx   on failure_labels (case_id);
create index if not exists failure_labels_type_idx      on failure_labels (failure_type);
create index if not exists failure_labels_severity_idx  on failure_labels (severity);
create index if not exists failure_labels_patch_idx     on failure_labels (patch_applied);


-- ============================================================
-- TASTING ROOM RESERVATIONS — email-native coordination state
-- ============================================================

create table if not exists reservations (
    reservation_id          text primary key,
    client_name             text,
    client_email            text,
    phone                   text,
    requested_date          date,
    requested_time          time,
    guest_count             integer,
    experience_type         text,
    price_per_person_cents  integer,
    current_state           text not null default 'REQUEST_RECEIVED',
    payment_status          text not null default 'not_sent',
    booking_status          text not null default 'not_booked',
    square_customer_id      text,
    square_order_id         text,
    square_invoice_id       text,
    square_invoice_number   text,
    square_invoice_url      text,
    square_invoice_total_cents integer,
    square_invoice_status   text,
    square_invoice_verified_at timestamptz,
    calendar_event_id       text,
    calendar_event_url      text,
    gmail_thread_ids        text[] not null default '{}',
    active_slot             jsonb not null default '{}'::jsonb,
    candidate_slots         jsonb not null default '[]'::jsonb,
    recommended_action      text,
    confidence              numeric default 1.0,
    notes                   text,
    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now()
);

create index if not exists reservations_client_email_idx on reservations (client_email);
create index if not exists reservations_state_idx        on reservations (current_state);
create index if not exists reservations_date_idx         on reservations (requested_date);
create index if not exists reservations_updated_idx      on reservations (updated_at desc);

create or replace trigger reservations_updated_at
    before update on reservations
    for each row execute function update_updated_at();

alter table reservations add column if not exists square_customer_id text;
alter table reservations add column if not exists square_order_id text;
alter table reservations add column if not exists square_invoice_id text;
alter table reservations add column if not exists square_invoice_number text;
alter table reservations add column if not exists square_invoice_url text;
alter table reservations add column if not exists square_invoice_total_cents integer;
alter table reservations add column if not exists square_invoice_status text;
alter table reservations add column if not exists square_invoice_verified_at timestamptz;
alter table reservations add column if not exists calendar_event_id text;
alter table reservations add column if not exists calendar_event_url text;


create table if not exists availability_claims (
    id                    uuid primary key default gen_random_uuid(),
    reservation_id         text not null references reservations (reservation_id),
    actor                  text not null,             -- client | josh | internal_staff
    actor_email            text,
    claim_type             text not null,             -- requested_slot | facility_availability | internal_availability | facility_booking_confirmation
    claim_status           text not null,             -- available | unavailable | alternative_offered | ambiguous | confirmed
    date                   date,
    start_time             time,
    end_time               time,
    time_description       text,
    guest_count            integer,
    experience_type        text,
    source_channel         text not null,             -- email | google_chat | manual
    source_message_id      text,
    raw_text               text,
    confidence             numeric default 1.0,
    expires_at             timestamptz,
    reviewed_by_human      boolean default false,
    created_at             timestamptz default now()
);

create index if not exists availability_claims_reservation_idx on availability_claims (reservation_id);
create index if not exists availability_claims_actor_idx       on availability_claims (actor);
create index if not exists availability_claims_date_idx        on availability_claims (date);
create index if not exists availability_claims_created_idx     on availability_claims (created_at desc);


create table if not exists reservation_events (
    id                  uuid primary key default gen_random_uuid(),
    reservation_id       text not null references reservations (reservation_id),
    event_type           text not null,
    actor                text,
    source_channel       text not null,
    source_message_id    text,
    summary              text,
    raw_payload          jsonb not null default '{}'::jsonb,
    created_at           timestamptz default now()
);

create index if not exists reservation_events_reservation_idx on reservation_events (reservation_id);
create index if not exists reservation_events_type_idx        on reservation_events (event_type);
create index if not exists reservation_events_created_idx     on reservation_events (created_at desc);


create table if not exists reservation_action_requests (
    action_id             text primary key,
    reservation_id         text not null references reservations (reservation_id),
    action_type            text not null,
    status                 text not null default 'pending', -- pending | approved | rejected | sent | failed | escalated
    risk_level             text not null default 'medium',
    recipient_email        text,
    email_subject          text,
    email_body             text,
    recommendation         text,
    source_message_id      text,
    decided_by             text,
    decided_at             timestamptz,
    idempotency_key        text,              -- dedupe guard: same key can't create a second action card
    created_at             timestamptz default now(),
    updated_at             timestamptz default now()
);

alter table reservation_action_requests add column if not exists idempotency_key text;

create index if not exists reservation_action_requests_reservation_idx on reservation_action_requests (reservation_id);
create index if not exists reservation_action_requests_status_idx      on reservation_action_requests (status);
create index if not exists reservation_action_requests_created_idx     on reservation_action_requests (created_at desc);
create unique index if not exists reservation_action_requests_idempotency_key_idx
    on reservation_action_requests (idempotency_key)
    where idempotency_key is not null;

create or replace trigger reservation_action_requests_updated_at
    before update on reservation_action_requests
    for each row execute function update_updated_at();


-- ============================================================
-- SYSTEM HEARTBEAT (liveness) + CHAT PENDING ACTIONS (durability)
-- ============================================================
-- system_heartbeat: the always-on tasting-room watcher stamps its name each poll
-- so the web app's monitor can detect "watcher went silent" and alert.
create table if not exists system_heartbeat (
    name          text primary key,
    last_beat_at  timestamptz not null,
    meta          jsonb default '{}'::jsonb,
    updated_at    timestamptz default now()
);

create or replace trigger system_heartbeat_updated_at
    before update on system_heartbeat
    for each row execute function update_updated_at();

-- chat_pending_actions: durable store for confirm-first chat actions (send email,
-- cancel, revoke) so a staged-but-unconfirmed action survives a web restart.
-- One pending action per chat user (PK), 10-min TTL enforced in application code.
create table if not exists chat_pending_actions (
    chat_user   text primary key,
    kind        text not null,            -- send_email | cancel_case | revoke
    params      jsonb not null default '{}'::jsonb,
    summary     text,
    created_at  timestamptz not null default now()
);


-- ============================================================
-- ROW LEVEL SECURITY
-- ============================================================
-- Every table in this schema is reached only through the backend using the
-- service_role key (SUPABASE_SERVICE_KEY), which bypasses RLS. Enabling RLS
-- with NO policies therefore denies all anon / authenticated access via
-- PostgREST while leaving the backend untouched — the secure default, and
-- what the Supabase linter (0013_rls_disabled_in_public) requires.
-- These statements are idempotent.

alter table pricing_tiers               enable row level security;
alter table customers                   enable row level security;
alter table products                    enable row level security;
alter table square_orders               enable row level security;
alter table square_invoices             enable row level security;
alter table invoice_logs                enable row level security;
alter table sync_state                  enable row level security;
alter table agent_cases                 enable row level security;
alter table trace_events                enable row level security;
alter table failure_labels              enable row level security;
alter table reservations                enable row level security;
alter table availability_claims         enable row level security;
alter table reservation_events          enable row level security;
alter table reservation_action_requests enable row level security;
alter table system_heartbeat             enable row level security;
alter table chat_pending_actions         enable row level security;

-- Tables created outside this file (control/eval layer + LangGraph checkpointer
-- orphans) also have RLS enabled directly on the project; see migration
-- enable_rls_on_public_tables:
--   unresolved_reservation_events, case_judgments, raw_email_events,
--   validation_results, execution_results, workflow_records,
--   checkpoint_migrations, checkpoints, checkpoint_blobs, checkpoint_writes
