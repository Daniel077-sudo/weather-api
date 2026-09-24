update public.user_memory_profiles
set
  summary_json = coalesce(summary_json, '{}'::jsonb)
    - 'pending_event'
    - 'last_event_title'
    - 'last_event_start',
  updated_at = now()
where coalesce(summary_json, '{}'::jsonb) ?| array[
  'pending_event',
  'last_event_title',
  'last_event_start'
];

notify pgrst, 'reload schema';
