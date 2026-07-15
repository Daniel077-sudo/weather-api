-- Performance indexes and cache helpers.
-- Run this after 002_integrations_and_observability.sql.

alter table public.emergency_kit_scans
  add column if not exists image_hash text;

create index if not exists events_user_start_time_idx
  on public.events(user_id, start_time);

create index if not exists events_start_time_idx
  on public.events(start_time);

create index if not exists weather_cache_city_valid_until_idx
  on public.weather_cache(city_name, valid_until);

create index if not exists event_weather_alerts_user_status_created_idx
  on public.event_weather_alerts(user_id, status, created_at desc);

create index if not exists ai_suggestion_cache_prompt_subject_idx
  on public.ai_suggestion_cache(prompt_type, subject, created_at desc);

create index if not exists emergency_kit_scans_user_created_idx
  on public.emergency_kit_scans(user_id, created_at desc);

create index if not exists emergency_kit_scans_image_hash_idx
  on public.emergency_kit_scans(image_hash);
