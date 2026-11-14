# `/api/chat` v2 contract

This document describes the stable contract shared by the App, LINE bridge, and backend.

## Request

`POST /api/chat` uses `contract_version: 2`, `message`, and `client_now`. `current_location` is query context only and must never be copied into an event draft. A follow-up may include `intent_hint`, `draft_id`, and `draft_event`.

## Intents

Allowed values are `CREATE_EVENT`, `UPDATE_EVENT`, `DELETE_EVENT`, `QUERY_EVENT`, `QUERY_WEATHER`, `EVENT_WEATHER`, `DISASTER_INFO`, and `GENERAL_CHAT`.

## Event draft rules

- Resolve relative dates from `client_now` in Asia/Taipei.
- No date means the `client_now` calendar date.
- No time means an all-day draft: `all_day: true`, `start_time: null`, and `end_time: null`.
- A vague period such as `明天晚上` requires clarification. A numeric clock without AM/PM follows the App/LINE convention: today uses the next occurrence; on another date, 1-6 means PM and 7-11 means AM; two-digit colon time is 24-hour time.
- Do not synthesize `end_time`. Fill it only when the user states a time range.
- In a stated range, inherit a period across both ends: `下午三點到五點` is `15:00` to `17:00`; `兩點到晚上十點半` is `14:00` to `22:30`.
- Merge follow-ups into `draft_event`; fields not mentioned in the new message remain unchanged.
- Do not copy `current_location` into `city`.
- Location is optional. Do not add `city`, `district`, or `location` to `missing_fields`.
- Do not guess the city for generic or chain locations such as `學校` or `全聯`; retain the supplied place text in `location` when useful.
- A clearly named venue may be retained in `location` without inventing `city` or `district`.

## Response invariants

- `needs_clarification` equals `len(missing_fields) > 0`.
- `date` uses `YYYY-MM-DD`; clock fields use `HH:MM`.
- `city`, when present, must be one of the configured Taiwan cities/counties and uses `臺`.
- `QUERY_EVENT`, `QUERY_WEATHER`, and `EVENT_WEATHER` may return an empty `reply`; App and LINE answer from `entities`. Other intents return a non-empty reply unless `ai_failed` is true.
- `/api/chat` only prepares a draft. It does not write events before explicit confirmation.
- Gemini failures return `ai_failed: true`, `gemini_status: timeout|error`, and an empty `reply`; they must not produce an HTTP 500 or a false claim that an action was completed.

Text generation uses `GEMINI_TEXT_MODEL` (default `gemini-3.5-flash`). A transient 429/5xx or request timeout retries once with `GEMINI_TEXT_FALLBACK_MODEL` (default `gemini-3.5-flash-lite`).

## Examples

- `明天辦護照` -> `CREATE_EVENT`, tomorrow, all day.
- `明天下午三點到五點開會` -> start `15:00`, end `17:00`, ask only for genuinely missing fields.
- `明天下午三點去全聯` -> keep `全聯` as the described place and ask for city; never use `current_location`.
- Follow-up `下午四點` with an existing draft -> update only `start_time`, preserving title, date, and location.
