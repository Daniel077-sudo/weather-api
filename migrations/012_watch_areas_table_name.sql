-- Restore the backend table name to public.watch_areas.
-- If old data exists in public.user_watch_areas, copy it forward once.

create table if not exists public.watch_areas (
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

alter table public.watch_areas add column if not exists user_id text;
alter table public.watch_areas add column if not exists label text;
alter table public.watch_areas add column if not exists city text;
alter table public.watch_areas add column if not exists district text;
alter table public.watch_areas add column if not exists lat double precision;
alter table public.watch_areas add column if not exists lng double precision;
alter table public.watch_areas add column if not exists is_active boolean default true;
alter table public.watch_areas add column if not exists created_at timestamptz default now();
alter table public.watch_areas add column if not exists updated_at timestamptz default now();

do $$
begin
  if to_regclass('public.user_watch_areas') is not null then
    insert into public.watch_areas (
      user_id,
      label,
      city,
      district,
      lat,
      lng,
      is_active,
      created_at,
      updated_at
    )
    select
      old_area.user_id,
      old_area.label,
      old_area.city,
      old_area.district,
      old_area.lat,
      old_area.lng,
      coalesce(old_area.is_active, true),
      coalesce(old_area.created_at, now()),
      coalesce(old_area.updated_at, now())
    from public.user_watch_areas old_area
    where not exists (
      select 1
      from public.watch_areas existing
      where existing.user_id = old_area.user_id
        and existing.city = old_area.city
        and coalesce(existing.district, '') = coalesce(old_area.district, '')
        and coalesce(existing.label, '') = coalesce(old_area.label, '')
    );
  end if;
end $$;

create index if not exists watch_areas_user_active_idx
  on public.watch_areas(user_id, is_active, updated_at desc);

create index if not exists watch_areas_area_idx
  on public.watch_areas(city, district);
