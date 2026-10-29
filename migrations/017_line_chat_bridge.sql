alter table public.chat_logs
  add column if not exists channel text not null default 'app';

update public.chat_logs
set channel = 'app'
where channel is null or channel not in ('app', 'line');

alter table public.chat_logs
  drop constraint if exists chat_logs_channel_check;

alter table public.chat_logs
  add constraint chat_logs_channel_check check (channel in ('app', 'line'));

-- chat_logs belongs to an authenticated account, not to an optional profile
-- row. Drop any FK that points at public.profiles so LINE can persist messages
-- even when the user has not created a profile record yet.
do $$
declare
  constraint_row record;
begin
  for constraint_row in
    select constraint_def.conname
    from pg_constraint constraint_def
    where constraint_def.contype = 'f'
      and constraint_def.conrelid = 'public.chat_logs'::regclass
      and constraint_def.confrelid = to_regclass('public.profiles')
  loop
    execute format(
      'alter table public.chat_logs drop constraint %I',
      constraint_row.conname
    );
  end loop;
end
$$;

-- If user_id is UUID, enforce future rows against the actual Supabase Auth
-- account. NOT VALID skips legacy rows but still validates every new insert.
do $$
declare
  user_id_type text;
begin
  select data_type
    into user_id_type
  from information_schema.columns
  where table_schema = 'public'
    and table_name = 'chat_logs'
    and column_name = 'user_id';

  if user_id_type = 'uuid'
     and to_regclass('auth.users') is not null
     and not exists (
       select 1
       from pg_constraint constraint_def
       where constraint_def.contype = 'f'
         and constraint_def.conrelid = 'public.chat_logs'::regclass
         and constraint_def.confrelid = 'auth.users'::regclass
     ) then
    alter table public.chat_logs
      add constraint chat_logs_user_id_auth_fkey
      foreign key (user_id)
      references auth.users(id)
      on delete cascade
      not valid;
  end if;
end
$$;

create index if not exists chat_logs_user_channel_created_idx
  on public.chat_logs(user_id, channel, created_at desc);

create unique index if not exists events_chat_draft_idempotency_idx
  on public.events(user_id, external_source, external_event_id)
  where external_source = 'chat_draft' and external_event_id is not null;
