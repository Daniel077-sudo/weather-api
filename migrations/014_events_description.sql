alter table public.events
  add column if not exists description text;

notify pgrst, 'reload schema';
