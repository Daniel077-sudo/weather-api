# LINE Chat Internal API

All endpoints in this document are server-to-server only. Send the shared
secret in `X-Internal-Key`. The Render service and Supabase Edge Function must
use the same `BACKEND_INTERNAL_KEY` value.

## Parse a LINE message

`POST /api/chat/internal`

```json
{
  "user_id": "f0bda093-41c6-43b9-b88a-d5966cf17f77",
  "channel": "line",
  "contract_version": 2,
  "message": "明天下午三點在高雄前鎮區開會",
  "client_now": "2026-10-22T10:00:00+08:00",
  "current_location": null,
  "intent_hint": null,
  "draft_id": null,
  "draft_event": null
}
```

The response uses the same v2 contract as `POST /api/chat`. The endpoint never
writes an event. A complete `CREATE_EVENT` response is still only a proposal.
The caller must show a confirmation action before calling the confirm endpoint.

For `UPDATE_EVENT` and `DELETE_EVENT`, a unique match includes `event_id` (and
`event_id_to_delete` for delete compatibility). No match or multiple matches
returns `event_id: null`, `missing_fields: ["target"]`, and a numbered
`event_filter.candidates` array. The bridge must still show a confirmation card
before calling an event mutation endpoint.

The endpoint returns within 14.5 seconds. A timeout response has
`status: "error"` and `timing.timeout: true`.

## Confirm a draft

`POST /api/chat/drafts/{draft_id}/confirm`

```json
{
  "user_id": "f0bda093-41c6-43b9-b88a-d5966cf17f77",
  "channel": "line",
  "draft_event": {
    "title": "開會",
    "date": "2026-10-23",
    "start_time": "15:00",
    "end_time": "16:00",
    "city": "高雄市",
    "district": "前鎮區",
    "location": "高雄市前鎮區"
  }
}
```

The same `draft_id` is idempotent for a user. Repeated confirmation returns the
existing event with `idempotent_replay: true` instead of creating another row.
For a non-contiguous multi-date draft, `draft_event.dates` is an array of
`YYYY-MM-DD` values. Confirmation creates one idempotent event per date and
returns them in `data.events`. A continuous all-day range instead uses
`date` plus `end_date` and is stored as one cross-day event.

## Cancel a draft

`POST /api/chat/drafts/{draft_id}/cancel`

```json
{
  "user_id": "f0bda093-41c6-43b9-b88a-d5966cf17f77",
  "channel": "line"
}
```

Cancellation does not write or delete an event. The Edge Function can mark its
own `line_event_drafts` row as cancelled after this endpoint succeeds.

## History and reset

`GET /api/chat/history` includes `channel: "app" | "line"` on every row.
`POST /api/chat/memory/reset` clears the memory profile and chat logs from both
channels.

## Deployment

1. Apply `migrations/017_line_chat_bridge.sql` in Supabase.
2. Set the same random secret as `BACKEND_INTERNAL_KEY` in Render and the
   Supabase Edge Function.
3. Set the Edge Function's `BACKEND_CHAT_URL` to the full internal chat URL.
4. Never put `BACKEND_INTERNAL_KEY` in an iOS or Android build.
