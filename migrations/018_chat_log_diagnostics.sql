-- Keep enough structured chat data to diagnose intent and Gemini failures.
alter table public.chat_logs add column if not exists role text;
alter table public.chat_logs add column if not exists content text;
alter table public.chat_logs add column if not exists response_payload jsonb default '{}'::jsonb;
alter table public.chat_logs add column if not exists action_type text;
alter table public.chat_logs add column if not exists event_title text;
alter table public.chat_logs add column if not exists event_start timestamptz;
alter table public.chat_logs add column if not exists event_end timestamptz;
alter table public.chat_logs add column if not exists has_alert boolean default false;
alter table public.chat_logs add column if not exists alert_title text;
alter table public.chat_logs add column if not exists alert_url text;

create index if not exists chat_logs_user_channel_created_idx
  on public.chat_logs(user_id, channel, created_at desc);

notify pgrst, 'reload schema';
