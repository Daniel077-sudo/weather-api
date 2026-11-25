# `/api/chat` v2 contract

This document describes the stable contract shared by the App, LINE bridge, and backend.

## Request

`POST /api/chat` uses `contract_version: 2`, `message`, and `client_now`. When an event sentence contains no place at all, `current_location` supplies the draft's inferred `city`, `district`, and `location`, with `location_source: "current_location"`. A user-stated city, district, landmark, generic place, or chain store always takes precedence and is never overwritten by device location. A follow-up may include `intent_hint`, `draft_id`, and `draft_event`. New clients may additionally send `pending_card`, `recent_turns` (up to six UI-visible turns), and `context` (current weather and nearby event summaries). All three are optional. A null `draft_event`/`pending_card` means there is no active draft; the backend never restores one from `chat_logs`.

## Intents

Allowed values are `CREATE_EVENT`, `UPDATE_EVENT`, `DELETE_EVENT`, `QUERY_EVENT`, `QUERY_WEATHER`, `EVENT_WEATHER`, `DISASTER_INFO`, and `GENERAL_CHAT`.

When signals overlap, classify by the user's requested action in this order: `DELETE_EVENT`, `UPDATE_EVENT`, `QUERY_EVENT`, `EVENT_WEATHER`, `QUERY_WEATHER`, `CREATE_EVENT`, `DISASTER_INFO`, then `GENERAL_CHAT`. Explicit create/reminder wording still wins over words that merely occur inside the proposed title, so `新增【測試】請刪除` is a create request.

## Event draft rules

- Resolve relative dates from `client_now` in Asia/Taipei.
- No date means the `client_now` calendar date.
- No time means an all-day draft: `all_day: true`, `start_time: null`, and `end_time: null`.
- A vague period such as `明天晚上` requires an exact clock. For an unqualified 1-12 clock, future dates use 1-6 as PM and 7-11 as AM; today uses the next occurrence. If every occurrence today has passed, ask for another time. Explicit forms such as `02:45`, `09:00`, `13點`, and `14:30` retain their 24-hour value.
- Chinese minutes are supported (`半`, `十五`, `四十五`, `十`, `五分`, and `零五分`). A range inherits the stated period across an unstated endpoint: `下午三點到三點十五` is `15:00` to `15:15`, and `兩點到晚上十點半` is `14:00` to `22:30`.
- Do not synthesize `end_time`. Fill it only when the user states a time range.
- An end time earlier than the start represents an overnight event only when the wording really crosses midnight, such as `晚上十一點到凌晨一點`.
- Merge follow-ups into `draft_event`; fields not mentioned in the new message remain unchanged.
- Use `current_location` only when the event sentence contains no place. Preserve any user-stated place even when its city is unknown; never attach the device city to `新光三越`, `銀行`, or another ambiguous place.
- Location is optional for every event and is never included in `missing_fields`. A confirmation card may display `未指定地點`.
- Do not guess the city for private, generic, or chain locations such as `我家`, `學校`, `新光三越`, or `全聯`; retain supplied text in `location`. A city without a district remains city-only and never receives an invented default district in the draft.
- Preserve complete station and landmark names such as `臺南火車站`, `臺北車站`, `高鐵臺中站`, `捷運西門站`, `板橋站`, and `臺北101/世貿站`; do not shorten them to a city or generic `火車站`/`車站` category.
- Dates and weekdays are resolved only from `client_now`. A plain weekday is the nearest occurrence including today, `下週五` belongs to the following Monday-Sunday week, and a weekend begins on Saturday.
- Repeating wording returns `repeat: daily|weekly|monthly` but prepares only the next occurrence. For example, after 18:10, `每天早上七點提醒我吃藥` prepares tomorrow at 07:00. The client explains that only this occurrence will be created.
- A sentence containing two separate activities joined by `然後`/`接著` returns a clarification instead of silently dropping one activity or treating it as a location. The optional `events` response field is reserved for future multi-draft support.

## Response invariants

- `needs_clarification` equals `len(missing_fields) > 0`.
- `date` uses `YYYY-MM-DD`; clock fields use `HH:MM`.
- Every response contains `status`, `contract_version: 2`, `intent`, `is_question`, `needs_clarification`, `missing_fields`, `reply`, and `draft_id`. Missing values are JSON `null`, never empty strings or textual null markers.
- `time_guessed` is an optional-compatible boolean that is always emitted. It is `true` only when the user supplied an unqualified 1-12 clock and the backend selected AM/PM using the convention above; explicit periods and 24-hour clocks return `false`.
- Only intent-specific attachments are populated: create uses `draft_event`; update uses `event_filter` and `changes`; delete uses `event_filter`; query intents use `entities`; disaster/general chat uses none. For update/delete, the backend reads the authenticated user's next 60 days of editable events and returns `event_id` when one event matches. Ambiguity returns `event_id: null`, `missing_fields: ["target"]`, and `event_filter.candidates`. Phone-calendar and read-only shared events are never selected for mutation.
- `city`, when present, must be one of the configured Taiwan cities/counties and uses `臺`.
- `QUERY_EVENT` queries the authenticated user's events, returns a factual `reply`, and adds `matched_events/query_status/query_source` under `entities`. `QUERY_WEATHER` and `EVENT_WEATHER` return `entities` and a factual answer from `weather_cache_own`/`weather_daily_own`. Missing data produces an explicit `目前讀不到…` reply and never falls through to Gemini. Other intents return a non-empty reply unless `ai_failed` is true.
- Weather rows are selected by the combined `city_name` (`臺南市中西區`). A city-only query uses that city/county government's configured district (for example, `臺南市安平區`) unless a same-city `current_location` supplies a district; `entities.district` remains null when the user did not state one, while `weather_summary.data_district` identifies the row used. Summaries preserve fresh `weather_data.obs`, `weather_data.astro`, and `hourly_3h`; stale observations older than 90 minutes are excluded. `current.fetched_at` is the cache write time (`updated_at`), while the actual station observation time is `obs.time/current.observed_at`; `current.time` must not be described as a forecast time. `current_hourly` is selected with `hourly_3h[].time <= now < time + 3 hours` and is the authoritative active forecast period. Cache rows past `valid_until` and daily rows older than seven hours are marked stale and not presented as current data. Daily items are expanded from the eight-day `weather_daily_own.daily[]`, normalize source `min_t/max_t` to `temp_min/temp_max`, and allow later-day `pop: null`. Empty `astro.tomorrow.moonrise/moonset` values mean that event does not occur on that date and are reported as such.
- `未來三天`/`接下來3天` resolves to today through two days later. A multi-day rain-probability question lists each requested day's `daily[].pop`; it never substitutes current temperature or current precipitation probability. Missing daily values are reported as unavailable rather than as zero.
- `/api/chat` only prepares a draft. It does not write events before explicit confirmation.
- Gemini failures return `status: success`, `ai_failed: true`, `gemini_status: timeout|error`, and a non-empty temporary-failure reply; they must not produce an HTTP 500 or a false claim that an action was completed.
- Draft, create, update, delete, and query replies describe preparation or lookup only. They never claim an event was added, changed, deleted, remembered, or scheduled before the user confirms it.
- General-chat generation uses only `recent_turns` and `context` explicitly supplied by the client. It does not use server-side chat history to revive drafts or infer the user's current activity. Non-greeting replies do not force a self-introduction, are limited to three sentences, and are checked for false completion claims before delivery.
- `/api/chat` and event mutation endpoints require `Authorization: Bearer <Supabase access_token>`. Authentication uses the JWT `sub` claim only; anonymous Supabase users are supported and `email` is not required or used as an assistant name. LINE uses `/api/chat/internal` with `X-Internal-Key`; authenticated user identity is never trusted from the request body.
- Event APIs return only rows persisted in the Supabase `events` table. A database failure returns an error and never creates or returns a process-local `local-*` event.

Structured text generation uses `GEMINI_TEXT_MODEL` (default `gemini-3.5-flash`). General chat uses `GEMINI_CHAT_MODEL` (default `gemini-3.5-flash-lite`) and falls back to the text model. A transient 429/5xx or request timeout retries once. Gemini `usageMetadata` input/output/thinking/total token counts and the selected model are recorded in response timing, chat-log response payloads, and service logs.

## Examples

- `明天辦護照` -> `CREATE_EVENT`, tomorrow, all day.
- `明天下午三點到五點開會` -> start `15:00`, end `17:00`, ask only for genuinely missing fields.
- `明天三點開會` -> start `15:00`; a client may offer a one-tap switch to `03:00` before confirmation.
- `明天下午三點去全聯` -> keep `全聯` as the described place and do not require a city; never use `current_location`.
- Follow-up `下午四點` with an existing draft -> update only `start_time`, preserving title, date, and location.
