-- User-subscribed areas for proactive local monitoring.

create table if not exists public.user_watch_areas (
  id bigserial primary key,
  user_id text not null,
  label text,
  city text not null,
  district text,
  lat double precision,
  lng double precision,
  is_active boolean default true,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

alter table public.user_watch_areas add column if not exists user_id text;
alter table public.user_watch_areas add column if not exists label text;
alter table public.user_watch_areas add column if not exists city text;
alter table public.user_watch_areas add column if not exists district text;
alter table public.user_watch_areas add column if not exists lat double precision;
alter table public.user_watch_areas add column if not exists lng double precision;
alter table public.user_watch_areas add column if not exists is_active boolean default true;
alter table public.user_watch_areas add column if not exists created_at timestamptz default now();
alter table public.user_watch_areas add column if not exists updated_at timestamptz default now();

create index if not exists user_watch_areas_user_active_idx
  on public.user_watch_areas(user_id, is_active, updated_at desc);

create index if not exists user_watch_areas_area_idx
  on public.user_watch_areas(city, district);
