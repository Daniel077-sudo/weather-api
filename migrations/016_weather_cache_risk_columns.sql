alter table public.weather_cache
  add column if not exists city text,
  add column if not exists district text,
  add column if not exists risk_level text default 'low',
  add column if not exists risk_tags jsonb default '[]'::jsonb,
  add column if not exists has_weather_risk boolean default false;

update public.weather_cache
set
  city = coalesce(
    nullif(city, ''),
    substring(
      city_name from
      '^(臺北市|新北市|桃園市|臺中市|臺南市|高雄市|基隆市|新竹市|嘉義市|新竹縣|苗栗縣|彰化縣|南投縣|雲林縣|嘉義縣|屏東縣|宜蘭縣|花蓮縣|臺東縣|澎湖縣|金門縣|連江縣)'
    )
  ),
  district = coalesce(
    nullif(district, ''),
    nullif(
      regexp_replace(
        city_name,
        '^(臺北市|新北市|桃園市|臺中市|臺南市|高雄市|基隆市|新竹市|嘉義市|新竹縣|苗栗縣|彰化縣|南投縣|雲林縣|嘉義縣|屏東縣|宜蘭縣|花蓮縣|臺東縣|澎湖縣|金門縣|連江縣)',
        ''
      ),
      ''
    )
  ),
  risk_level = coalesce(nullif(weather_data ->> 'risk_level', ''), risk_level, 'low'),
  risk_tags = case
    when jsonb_typeof(weather_data -> 'risk_tags') = 'array'
      then weather_data -> 'risk_tags'
    else coalesce(risk_tags, '[]'::jsonb)
  end,
  has_weather_risk = case
    when weather_data ->> 'has_weather_risk' in ('true', 'false')
      then (weather_data ->> 'has_weather_risk')::boolean
    else coalesce(has_weather_risk, false)
  end;

create index if not exists weather_cache_city_district_idx
  on public.weather_cache(city, district);

notify pgrst, 'reload schema';
