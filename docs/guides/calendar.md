# Calendar connection and availability

[Google sign-in](authentication.md) and Calendar consent are separate. This guide
owns connection and availability settings; event publication is in [sessions](sessions.md).

## Google configuration

Enable Google Calendar API for the OAuth project. Configure a web OAuth client and
register the exact `GOOGLE_CALENDAR_REDIRECT_URI`; locally it is
`http://localhost:8000/api/calendar/google/callback/`. Keep any separate sign-in
callback registered too. Configure consent/test-user access for your project.

Set these in `.env`:

- `GOOGLE_CALENDAR_CLIENT_ID` and `GOOGLE_CALENDAR_CLIENT_SECRET`.
- `GOOGLE_CALENDAR_REDIRECT_URI`.
- `GOOGLE_CALENDAR_ENCRYPTION_KEY`: a stable Fernet key for stored tokens.

Generate the encryption key inside the installed runtime if needed:

```sh
docker compose exec backend python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep it private and preserve it with backups. Environment changes require
[container recreation](setup.md#run).

The backend requests openid/email plus Calendar scopes `calendar.calendarlist.readonly`,
`calendar.events.freebusy`, and `calendar.events.owned`, each prefixed by
`https://www.googleapis.com/auth/`.

## Connect and choose calendars

| Request | Action |
| --- | --- |
| `POST /api/calendar/google/connect/` | Return authorization_url; open it in the browser |
| `GET /api/calendar/google/callback/` | Google invokes it after consent; do not construct requests manually |
| `GET /api/calendar/connection/` | Connection/reconnect state and selected calendars |
| `GET /api/calendar/calendars/` | Accessible calendar IDs and accessRole |
| `PUT /api/calendar/preferences/` | Save source_calendars list and destination_calendar ID |
| `DELETE /api/calendar/connection/` | Remove local connection/authorization attempts; 204 |

Consent must use the same Google identity as Yiyara sign-in. Attempts expire and
are single-use; restart connect after failed consent. A configured fixed FRONTEND_URL
receives the successful callback redirect; otherwise the browser gets JSON.
Provider tokens remain server-side.

Use actual IDs from the calendar list, not the literal primary alias. Select
accessible availability sources and an owned destination. The destination is
checked for conflicts even if it is not a source. Unsupported sources such as some
holiday calendars must be removed; failed availability is not interpreted as free.

Google token refresh happens server-side. A 409 reconnect requirement needs new
consent; application JWT refresh does not repair Calendar authorization. Disconnect
leaves Google events untouched and does not log the user out of Yiyara.

## Availability rules

`GET/PUT /api/calendar/scheduling-preferences/` reads/replaces the IANA `timezone`
and optional `preferred_session_minutes`. There is no global allowed-hours timetable:
all time outside the user's grey periods is eligible, including across midnight.
Users choose their own unavailable time; no office/sleep hours are assumed.

```json
{"timezone": "Europe/London", "preferred_session_minutes": 60}
```

The removed global `working_hours` field is rejected with a 400 directing clients
to protected periods. Existing timezone/grey periods are preserved by migration;
old allowed hours are removed, not converted into guessed grey periods.

`GET/POST /api/calendar/protected-periods/` lists/creates grey periods.
`GET/PATCH/DELETE /api/calendar/protected-periods/{id}/` manages one period.

- Weekly: kind=weekly, weekdays, and either all_day=true or start_time/end_time.
  Optional start_date/end_date bound recurrence.
- One-off: kind=one_off and offset-bearing starts_at/ends_at; omit recurrence fields.

Grey periods exclude automatic proposals. Explicit user-selected exceptions follow
[session conflict review](sessions.md#user-selected-times-and-exceptions). DST is
interpreted in the saved timezone; no edit automatically moves Google events.

### Impact review when creating or editing a grey period

POST/PATCH checks saved future unfinished published sessions. A conflicting edit
returns 409 with error=protected_period_review_required, affected_sessions and
impact_token; nothing is saved yet. Resend the same period fields with that token
and conflict_resolution:

- keep: save the period and keep existing appointments as session-specific exceptions.
- review: save the period and flag affected sessions for rescheduling review.
- Optional keep_session_ids selects specific appointments to keep; remaining ones
  are flagged for review. Only IDs in the current impact list are accepted.

The response's impact_review lists kept/review session IDs. No Google write happens.
Changed sessions/rules invalidate the review token. An unconfirmed Calendar write
must be recovered first. Existing tentative management proposals are invalidated.

`GET /api/calendar/protected-periods/{id}/impacts/` inspects current saved overlaps
and kept exceptions. A later PATCH can review keep choices again. Impacts use local
session state and include synced_at; explicitly refresh stale Calendar sessions
before deciding. Keeping an appointment does not permit other tasks in that grey
period. Rescheduling it requires its own independent review.

`POST /api/calendar/scheduling-windows/` previews all time minus grey periods;
it explicitly does **not** check Google busy time or ambition-specific rules.
`POST /api/calendar/availability/` returns selected calendars' Google busy intervals.
Both take time_min/time_max; scheduling intersects these with confirmed
[ambition rules](planning.md#context-corrections-and-calendar-rules) and local reservations.
