-- Backend-only tables. Does not delete rows, force RLS on the owner, or change
-- service_role privileges. Apply inside a transaction after auditing the target.
-- Includes legacy/checkpointer tables created outside db/schema.sql.
do $$
declare t record;
begin
  for t in
    select c.relname
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind in ('r', 'p')
      and c.relname = any(array[
        'pricing_tiers', 'customers', 'products', 'square_orders',
        'square_invoices', 'invoice_logs', 'invoice_chat_turns', 'sync_state',
        'agent_cases', 'trace_events', 'failure_labels', 'reservations',
        'availability_claims', 'reservation_events', 'reservation_action_requests',
        'system_heartbeat', 'chat_pending_actions', 'raw_email_events',
        'unresolved_reservation_events', 'case_judgments', 'validation_results',
        'execution_results', 'workflow_records', 'checkpoint_migrations',
        'checkpoints', 'checkpoint_blobs', 'checkpoint_writes'
      ])
  loop
    execute format('alter table public.%I enable row level security', t.relname);
    execute format('revoke all on table public.%I from anon, authenticated, public', t.relname);
  end loop;
end $$;
