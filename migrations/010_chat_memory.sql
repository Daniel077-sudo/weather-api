-- Persistent chat history and per-user AI memory.

create table if not exists public.chat_messages (
  id bigserial primary key,
  user_id text not null,
  role text not null check (role in ('user', 'assistant', 'system')),
  content text not null,
  response_payload jsonb default '{}'::jsonb,
  action_type text,
  event_title text,
  event_start timestamptz,
  event_end timestamptz,
  has_alert boolean default false,
  alert_title text,
  alert_url text,
  created_at timestamptz default now()
);

alter table public.chat_messages add column if not exists user_id text;
alter table public.chat_messages add column if not exists role text;
alter table public.chat_messages add column if not exists content text;
alter table public.chat_messages add column if not exists response_payload jsonb default '{}'::jsonb;
alter table public.chat_messages add column if not exists action_type text;
alter table public.chat_messages add column if not exists event_title text;
alter table public.chat_messages add column if not exists event_start timestamptz;
alter table public.chat_messages add column if not exists event_end timestamptz;
alter table public.chat_messages add column if not exists has_alert boolean default false;
alter table public.chat_messages add column if not exists alert_title text;
alter table public.chat_messages add column if not exists alert_url text;
alter table public.chat_messages add column if not exists created_at timestamptz default now();

create index if not exists chat_messages_user_created_idx
  on public.chat_messages(user_id, created_at desc);

create index if not exists chat_messages_user_action_idx
  on public.chat_messages(user_id, action_type, created_at desc);

create table if not exists public.user_memory_profiles (
  user_id text primary key,
  memory_markdown text default '',
  summary_json jsonb default '{}'::jsonb,
  last_interaction_at timestamptz,
  updated_at timestamptz default now(),
  created_at timestamptz default now()
);

alter table public.user_memory_profiles add column if not exists memory_markdown text default '';
alter table public.user_memory_profiles add column if not exists summary_json jsonb default '{}'::jsonb;
alter table public.user_memory_profiles add column if not exists last_interaction_at timestamptz;
alter table public.user_memory_profiles add column if not exists updated_at timestamptz default now();
alter table public.user_memory_profiles add column if not exists created_at timestamptz default now();

create index if not exists user_memory_profiles_updated_idx
  on public.user_memory_profiles(updated_at desc);
