# Scheduling and Google events

Requires an eligible [task](planning.md#tasks-and-review) and configured
[Calendar availability](calendar.md). API authentication is described
[here](authentication.md). For executable examples use the [Postman collection](postman.md).

## Propose, review and publish

`POST /api/tasks/{task_id}/sessions/propose/` takes time_min/time_max (future,
offset-bearing timestamps), optional preferred_start and conflict_policy.
`GET /api/policy/` reports current duration/range limits; additional policy defaults
are in [settings](../../config/settings.py).

The scheduler finds one continuous block using remaining_minutes when reported,
otherwise estimated_minutes. It does not split the task or schedule every task in
a plan. Task/goal due dates end at the next local midnight. Tentative proposals and
in-flight writes reserve time; replacing a proposal releases its predecessor only
after finding a replacement.

Expect 201 with status=proposed, starts_at/ends_at, destination, task snapshot and
expiry. No Google write occurs. Inspect it with `GET /api/sessions/{session_id}/`.
No fit returns 409. Approval must occur before expires_at and the session start.

| Request | Effect |
| --- | --- |
| `POST /api/sessions/{session_id}/approve/` | Publish the reviewed block; 200 with status=published and google_event_id |
| `POST /api/sessions/{session_id}/reject/` | Reject a tentative session, releasing its reservation |
| `GET /api/tasks/{task_id}/sessions/` | Session history for the task |

Approval rechecks task/context, deadlines, calendar access, grey periods and busy
windows. Events are private busy blocks with instructions/completion criteria and
no attendees. Repeating approval reuses the same event ID. Publication is separate
from [finishing work](progress.md#finish-and-continue).

## User-selected times and exceptions

Without preferred_start, every proposal is automatic and uses only free time outside
all grey periods. No occupied-slot fallback exists, even with conflict_policy=ask.
A 409 means no suitable free slot fits the supplied range.

Providing preferred_start explicitly requests that exact time. It must fit the range,
duration, deadlines, task/activity rules and in-flight reservations; the scheduler
will not silently substitute a different time. With ask it can return conflicts for
busy events and grey periods. With avoid, any conflict returns 409.

Inspect time_selection, conflicts and requires_conflict_approval. Conflicts identify
calendar busy windows or kind=grey_period with period ID/version and protected times.
To confirm a reviewed exception, send:

```json
{"allow_overlap": true, "conflict_token": "<token from the reviewed proposal>"}
```

The exception belongs to that session/time and the reviewed conflicts only. Existing
events and grey periods remain intact; new/changed conflicts require fresh review.
User-approved parallel sessions are supported. Automatic proposals never inherit
another session's exception. Without conflicts approve with no body or `{}`.
Personal exceptions cannot establish third-party availability, complete prerequisites
or bypass an in-flight Calendar operation. The same rules apply to rescheduling.

## Reschedule or cancel

| Request | Body / result |
| --- | --- |
| `POST /api/sessions/{session_id}/reschedule/` | UUID client_id, time_min/time_max, optional preferred_start/conflict_policy/reason → change proposal |
| `POST /api/sessions/{session_id}/cancel/` | UUID client_id, optional reason → cancellation proposal |
| `GET /api/sessions/changes/{change_id}/` | Inspect management change |
| `POST /api/sessions/changes/{change_id}/approve/` | Apply reviewed move/delete; overlap consent as above when needed |
| `POST /api/sessions/changes/{change_id}/reject/` | Reject management proposal |

These change IDs identify SessionChange, not planning proposals. Move preserves
duration and the Google event ID. Cancellation removes the event and releases the
commitment without completing the task. Management uses conditional event versions
and rechecks constraints. Only supported future, unfinished, unchanged ready work
can be rescheduled; cancellation remains the route for removing an unsuitable block.
Recurring, all-day and guest events are not managed.

## Recovery and reconciliation

After an uncertain publication, retry approval of the **same session**. After an
uncertain move/cancel, retry approval of the **same management change**. Preserve
client_id for retries of a management request. Active leases return 409; abandoned
leases expire. Do not generate replacement writes to recover uncertainty.

`POST /api/sessions/{session_id}/refresh/` takes no body and reads the linked Google
event. GET detail reads only saved state. Refresh adopts supported external
moves/renames, invalidates stale management proposals and records history. Violated
rules or unsupported formats are flagged for review, not silently repaired. Refresh
also retries [completion appearance updates](progress.md#calendar-completion-appearance).

Verified event deletion cancels the local session without completing work or
recreating Google events. Access failures retain the last known state. A restored
event cannot create a second unfinished commitment for the same task. Fix unsupported
formats in Google, then refresh. Uncertain writes must be recovered through their
original approval first. There is no background sync or calendar-transfer discovery.
