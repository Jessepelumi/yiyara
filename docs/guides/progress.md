# Progress, continuation and replanning

A Google event's end time does not prove completion. Outcomes are user reports,
independent of Calendar publication. Start from the current plan version and
[task details](planning.md#tasks-and-review).

## Report work

`POST /api/tasks/{task_id}/outcomes/` records status, notes and effort. For example:

```json
{
  "client_id": "278cc88d-c92a-4f5f-8183-52de6e95b8a5",
  "plan_version": 4,
  "status": "partial",
  "remaining_minutes": 20,
  "spent_minutes": 40,
  "notes": "One exercise remains."
}
```

Use a new UUID per report and preserve it for retries. The response contains task,
immutable outcome, updated plan_version and replayed. Optional session_id associates
a report with a session; reporting alone does not finish that session.

| Status | Meaning / requirement |
| --- | --- |
| completed | remaining_minutes is zero or omitted |
| partial | Positive supported remaining_minutes required |
| blocked | blocker_reason required; remaining effort may be unknown |
| skipped | Work is set aside; explicit reopening is needed before scheduling |
| reopened | Positive remaining_minutes required to resume work |

Estimated effort remains unchanged. effective_minutes uses reported remaining work
when known. Null remaining work means unknown, not automatic subtraction of time
spent. The supported effort bounds come from the [shared policy](ai-configuration.md#execution).

`GET /api/tasks/{task_id}/outcomes/` returns outcome history, including retained
reports after a task is removed by plan restoration.

## Finish and continue

`POST /api/sessions/{session_id}/finish/` accepts the same report fields except
session_id: the URL selects the published session. Allowed statuses are completed,
partial, blocked and skipped. It records the outcome and finished_at atomically.
The historical Google event and publication status remain; a partial finish does
not complete the task.

After a partial finish, propose another session for the same task through the
[session flow](sessions.md#propose-review-and-publish). It uses remaining effort,
links continuation history and needs separate approval. Blocked/skipped work must
first be explicitly resolved/reopened and eligible. One task cannot have two
unfinished committed sessions. Outcome reporting is blocked while a relevant
Calendar management write is uncertain; recover that write first.

## Replan

`POST /api/plans/{plan_id}/replan/` accepts plan_version, UUID client_id and optional
focus. Save outcomes and context corrections first. The AI can propose eligible
unfinished-task updates and additions. Inspect before/changes/reason and dependencies;
apply/reject through the [shared plan-change routes](planning.md#intake-and-clarification).

A null change can mean no justified revision or missing context. Completed/skipped
work requires explicit reopening. The model cannot silently clear an existing
external blocker or revise work with an unfinished committed session. It preserves
reported progress and never moves/cancels Google events. Stale versions or changes
during generation require refresh/review. Approved task edits invalidate affected
tentative sessions; Calendar changes still use their own approval flow.
