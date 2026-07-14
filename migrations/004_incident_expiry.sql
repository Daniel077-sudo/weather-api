alter table public.incident_reports
  add column if not exists expires_at timestamptz;

create index if not exists incident_reports_expires_at_idx
  on public.incident_reports(expires_at);

update public.incident_reports
set expires_at = coalesce(expires_at, last_seen_at + interval '90 minutes')
where expires_at is null;
