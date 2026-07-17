-- Unified official disaster alert feed.

create table if not exists public.disaster_alerts (
  id bigserial primary key,
  alert_hash text unique not null,
  source text not null,
  type text not null,
  city text,
  district text,
  title text not null,
  description text,
  severity text default 'low',
  started_at timestamptz,
  expires_at timestamptz,
  source_url text,
  raw_payload jsonb default '{}'::jsonb,
  updated_at timestamptz default now(),
  created_at timestamptz default now()
);

alter table public.disaster_alerts add column if not exists alert_hash text;
alter table public.disaster_alerts add column if not exists source text;
alter table public.disaster_alerts add column if not exists type text;
alter table public.disaster_alerts add column if not exists city text;
alter table public.disaster_alerts add column if not exists district text;
alter table public.disaster_alerts add column if not exists title text;
alter table public.disaster_alerts add column if not exists description text;
alter table public.disaster_alerts add column if not exists severity text default 'low';
alter table public.disaster_alerts add column if not exists started_at timestamptz;
alter table public.disaster_alerts add column if not exists expires_at timestamptz;
alter table public.disaster_alerts add column if not exists source_url text;
alter table public.disaster_alerts add column if not exists raw_payload jsonb default '{}'::jsonb;
alter table public.disaster_alerts add column if not exists updated_at timestamptz default now();
alter table public.disaster_alerts add column if not exists created_at timestamptz default now();

create unique index if not exists disaster_alerts_alert_hash_idx
  on public.disaster_alerts(alert_hash);

create index if not exists disaster_alerts_area_active_idx
  on public.disaster_alerts(city, district, expires_at desc);

create index if not exists disaster_alerts_source_type_idx
  on public.disaster_alerts(source, type, started_at desc);
