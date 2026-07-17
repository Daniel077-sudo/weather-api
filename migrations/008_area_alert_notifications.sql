-- Notifications generated from user watch areas and active disaster alerts.

create table if not exists public.area_alert_notifications (
  id bigserial primary key,
  user_id text not null,
  watch_area_id bigint,
  alert_hash text not null,
  city text,
  district text,
  title text not null,
  message text,
  severity text default 'low',
  source text,
  source_url text,
  status text default 'unread',
  created_at timestamptz default now(),
  read_at timestamptz
);

alter table public.area_alert_notifications add column if not exists user_id text;
alter table public.area_alert_notifications add column if not exists watch_area_id bigint;
alter table public.area_alert_notifications add column if not exists alert_hash text;
alter table public.area_alert_notifications add column if not exists city text;
alter table public.area_alert_notifications add column if not exists district text;
alter table public.area_alert_notifications add column if not exists title text;
alter table public.area_alert_notifications add column if not exists message text;
alter table public.area_alert_notifications add column if not exists severity text default 'low';
alter table public.area_alert_notifications add column if not exists source text;
alter table public.area_alert_notifications add column if not exists source_url text;
alter table public.area_alert_notifications add column if not exists status text default 'unread';
alter table public.area_alert_notifications add column if not exists created_at timestamptz default now();
alter table public.area_alert_notifications add column if not exists read_at timestamptz;

create unique index if not exists area_alert_notifications_dedupe_idx
  on public.area_alert_notifications(user_id, watch_area_id, alert_hash);

create index if not exists area_alert_notifications_user_status_idx
  on public.area_alert_notifications(user_id, status, created_at desc);
