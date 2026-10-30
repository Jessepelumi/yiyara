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

`GET/PUT /api/calendar/scheduling-preferences/` reads/replaces `timezone`,
`working_hours` and optional `preferred_session_minutes`. Use an IANA timezone;
weekday 0 is Monday, 6 Sunday. Each working window has weekday/start_time/end_time.
Times are local minute-precision values; windows cannot overlap or run overnight.
Split overnight periods. Empty working hours permits no scheduling.

`GET/POST /api/calendar/protected-periods/` lists/creates grey periods.
`GET/PATCH/DELETE /api/calendar/protected-periods/{id}/` manages one period.

- Weekly: kind=weekly, weekdays, and either all_day=true or start_time/end_time.
  Optional start_date/end_date bound recurrence.
- One-off: kind=one_off and offset-bearing starts_at/ends_at; omit recurrence fields.

Grey periods are hard exclusions, including when overlap with ordinary events is
approved. DST is interpreted in the saved timezone. A preference/grey-period edit
affects future eligibility checks; it does not automatically move published events.

`POST /api/calendar/scheduling-windows/` previews global hours minus grey periods;
it explicitly does **not** check Google busy time or ambition-specific rules.
`POST /api/calendar/availability/` returns selected calendars' Google busy intervals.
Both take time_min/time_max; scheduling intersects these with confirmed
[ambition rules](planning.md#context-corrections-and-calendar-rules) and local reservations.
