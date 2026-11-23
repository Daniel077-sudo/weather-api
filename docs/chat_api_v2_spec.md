# `/api/chat` v2 contract

This document describes the stable contract shared by the App, LINE bridge, and backend.

## Request

`POST /api/chat` uses `contract_version: 2`, `message`, and `client_now`. `current_location` is query context only and must never be copied into an event draft. A follow-up may include `intent_hint`, `draft_id`, and `draft_event`.

## Intents

Allowed values are `CREATE_EVENT`, `UPDATE_EVENT`, `DELETE_EVENT`, `QUERY_EVENT`, `QUERY_WEATHER`, `EVENT_WEATHER`, `DISASTER_INFO`, and `GENERAL_CHAT`.

When signals overlap, classify by the user's requested action in this order: `DELETE_EVENT`, `UPDATE_EVENT`, `QUERY_EVENT`, `EVENT_WEATHER`, `QUERY_WEATHER`, `CREATE_EVENT`, `DISASTER_INFO`, then `GENERAL_CHAT`. Explicit create/reminder wording still wins over words that merely occur inside the proposed title, so `新增【測試】請刪除` is a create request.

## Event draft rules

- Resolve relative dates from `client_now` in Asia/Taipei.
- No date means the `client_now` calendar date.
- No time means an all-day draft: `all_day: true`, `start_time: null`, and `end_time: null`.
- A vague period such as `明天晚上` requires clarification. A 1-12 clock without a period is never guessed: `三點` and `2:45` return `start_time: null`, include `start_time` in `missing_fields`, and ask whether it is AM or PM. Explicit 24-hour forms such as `09:00`, `13點`, and `14:30` are unambiguous.
- Chinese minutes are supported (`半`, `十五`, `四十五`, `十`, `五分`, and `零五分`). A range inherits the stated period across an unstated endpoint: `下午三點到三點十五` is `15:00` to `15:15`, and `兩點到晚上十點半` is `14:00` to `22:30`.
- Do not synthesize `end_time`. Fill it only when the user states a time range.
- An end time earlier than the start represents an overnight event only when the wording really crosses midnight, such as `晚上十一點到凌晨一點`.
- Merge follow-ups into `draft_event`; fields not mentioned in the new message remain unchanged.
- Do not copy `current_location` into `city`.
- Location is optional only for activities that clearly do not need one, such as washing clothes, submitting an assignment, paying a bill, or taking medicine. Other event drafts without a city include `city` in `missing_fields`.
- Do not guess the city for private, generic, or chain locations such as `我家`, `學校`, `新光三越`, or `全聯`; retain the supplied place text in `location` and ask for the city. A city without a district may include optional `district` clarification, but must never invent a default district.
- Preserve complete named landmarks such as `臺南火車站`; do not shorten them to a city or category name.
- Dates and weekdays are resolved only from `client_now`. A plain weekday is the nearest occurrence including today, `下週五` belongs to the following Monday-Sunday week, and a weekend begins on Saturday.

## Response invariants

- `needs_clarification` equals `len(missing_fields) > 0`.
- `date` uses `YYYY-MM-DD`; clock fields use `HH:MM`.
- Every response contains `status`, `contract_version: 2`, `intent`, `is_question`, `needs_clarification`, `missing_fields`, `reply`, and `draft_id`. Missing values are JSON `null`, never empty strings or textual null markers.
- Only intent-specific attachments are populated: create uses `draft_event`; update uses `event_filter` and `changes`; delete uses `event_filter`; query intents use `entities`; disaster/general chat uses none. The v2 response does not expose `event_id`.
- `city`, when present, must be one of the configured Taiwan cities/counties and uses `臺`.
- `QUERY_EVENT` queries the authenticated user's events, returns a factual `reply`, and adds `matched_events/query_status/query_source` under `entities`. `QUERY_WEATHER` and `EVENT_WEATHER` return `entities` and a factual answer from `weather_cache_own`/`weather_daily_own`. Missing data produces an explicit `目前讀不到…` reply and never falls through to Gemini. Other intents return a non-empty reply unless `ai_failed` is true.
- Weather rows are selected by the combined `city_name` (`臺南市中西區`). A city-only query uses that city/county government's configured district (for example, `臺南市安平區`) unless a same-city `current_location` supplies a district; `entities.district` remains null when the user did not state one, while `weather_summary.data_district` identifies the row used. Summaries preserve fresh `weather_data.obs`, `weather_data.astro`, and `hourly_3h`; stale observations older than 90 minutes are excluded. `current.fetched_at` is the cache write time (`updated_at`), while the actual station observation time is `obs.time/current.observed_at`; `current.time` must not be described as a forecast time. `current_hourly` is selected with `hourly_3h[].time <= now < time + 3 hours` and is the authoritative active forecast period. Cache rows past `valid_until` and daily rows older than seven hours are marked stale and not presented as current data. Daily items are expanded from the eight-day `weather_daily_own.daily[]`, normalize source `min_t/max_t` to `temp_min/temp_max`, and allow later-day `pop: null`. Empty `astro.tomorrow.moonrise/moonset` values mean that event does not occur on that date and are reported as such.
- `/api/chat` only prepares a draft. It does not write events before explicit confirmation.
- Gemini failures return `status: success`, `ai_failed: true`, `gemini_status: timeout|error`, and a non-empty temporary-failure reply; they must not produce an HTTP 500 or a false claim that an action was completed.
- Draft, create, update, delete, and query replies describe preparation or lookup only. They never claim an event was added, changed, deleted, remembered, or scheduled before the user confirms it.
- `/api/chat` and event mutation endpoints require `Authorization: Bearer <Supabase access_token>`. LINE uses `/api/chat/internal` with `X-Internal-Key`; authenticated user identity is never trusted from the request body.

Text generation uses `GEMINI_TEXT_MODEL` (default `gemini-3.5-flash`). A transient 429/5xx or request timeout retries once with `GEMINI_TEXT_FALLBACK_MODEL` (default `gemini-3.5-flash-lite`).

## Examples

- `明天辦護照` -> `CREATE_EVENT`, tomorrow, all day.
- `明天下午三點到五點開會` -> start `15:00`, end `17:00`, ask only for genuinely missing fields.
- `明天三點開會` -> keep the time null and ask `是上午還是下午？`.
- `明天下午三點去全聯` -> keep `全聯` as the described place and ask for city; never use `current_location`.
- Follow-up `下午四點` with an existing draft -> update only `start_time`, preserving title, date, and location.
