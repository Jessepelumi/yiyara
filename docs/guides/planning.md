# Ambitions and task planning

For request authentication, see [authentication](authentication.md). For a live
walkthrough with prepared request bodies, use [Postman](postman.md).

## Intake and clarification

`POST /api/plans/` accepts required `text` and optional `title`. Omitting title uses
the first 255 characters of text. It saves a contextual plan and conversation,
initially with empty goals/questions; it does not call AI.

`POST /api/conversations/plans/{plan_id}/messages/` accepts `content`, `client_id`
and `plan_version`, plus optional `scope_goal_id`. This calls AI and returns messages
and a proposed `change`, or null if no mutation is needed. Request identity/retry
rules are defined in [AI recovery](ai-configuration.md#request-identity-and-recovery).

Review `change.operations` before using the shared approval routes:

- `POST /api/conversations/plans/{plan_id}/changes/{change_id}/apply/`
- `POST /api/conversations/plans/{plan_id}/changes/{change_id}/reject/`

These take no body. Apply returns the updated `plan` and change; it does not create
Calendar events. Proposed questions become answerable only after applying them.

`POST /api/plans/{plan_id}/questions/{question_id}/answer/` takes `answer` and
`plan_version`. Skip it when there are no open questions. Context records outcome,
starting point, timing, constraints and assumptions, with user-statement provenance.
It distinguishes what the user said from independently verified facts.

Planning states are `clarifying`, `actionable`, `waiting` and `completed`. Waiting
requires `waiting_reason`. Deferred required questions must retain a waiting state;
they are not answered by guessing. Independent next actions may still be proposed.

## Context corrections and calendar rules

`PATCH /api/plans/{plan_id}/context/` takes `plan_version` and the context fields to
replace. Supported fields are defined in
[ContextUpdateSerializer](../../goals/planning.py). Direct edits replace supplied
fields; approved AI context additions preserve earlier confirmed statements.

Timing prose alone is not executable. Confirm its interpretation through
`scheduling_rules` in that PATCH or an approved AI context change:

```json
{
  "plan_version": 2,
  "timing": "Weekdays after 15:00; start November 2, 2026.",
  "scheduling_rules": {
    "not_before": "2026-11-02T00:00:00Z",
    "deadline": null,
    "working_hours": [
      {"weekday": 0, "start_time": "15:00", "end_time": "17:00"}
    ]
  }
}
```

This example permits **Monday only**; include all intended weekdays. Hours use the
saved [Calendar timezone](calendar.md#availability-rules), and timestamp boundaries
require offsets. `deadline` is exclusive. Null working_hours adds no restriction;
`[]` permits no time. Rules narrow global availability; they never override it.
Confirm `{}` only when the prose adds no calendar restriction. Clarify unsupported
constraints rather than pretending they are enforced.

Changing timing/constraint prose invalidates its previous rules confirmation.
`scheduling_rules_review_required` exposes this. Context edits increment
`context_version`; affected tasks need review and an explicit ready update before
scheduling again, even after rules are reconfirmed.

## Tasks and review

| Request | Purpose / body |
| --- | --- |
| `POST /api/plans/{plan_id}/tasks/propose/` | AI additions: plan_version, UUID client_id, optional focus |
| `POST /api/tasks/` | Manual task under goal_id, with task fields |
| `GET /api/tasks/{task_id}/` | Task details and scheduling blockers |
| `PATCH /api/tasks/{task_id}/` | Explicit task edits; dependency_ids replaces prerequisites |

Task generation requires a confirmed desired outcome and starting point and no
unresolved open required questions. Inspect the reply and proposed operations;
a null change means clarification or no justified next action, not an empty plan
ready to approve. Apply/reject uses the shared routes above.

A useful task has specific instructions, effort and an observable completion
criterion. `readiness` is draft/ready/blocked. Dependencies must belong to the same
plan and cannot form cycles. Use `is_schedulable` and `scheduling_blockers`, not
readiness alone: completion, prerequisites, external blockers, stale context,
unconfirmed timing and unfinished commitments can all prevent scheduling.

The default proposal limits apply **per batch**, not to the whole ambition; see
[AI policy configuration](ai-configuration.md#execution). Additional proposals can
add work under existing goals without duplicating unfinished tasks.

## Retrieval and history

| Request | Result |
| --- | --- |
| `GET /api/plans/` | Plan summaries |
| `GET /api/plans/{plan_id}/` | Context, questions, goals and all nested tasks |
| `GET /api/plans/{plan_id}/changes/` | Proposed/applied/rejected changes |
| `GET /api/conversations/plans/{plan_id}/messages/` | Conversation history |
| `GET /api/plans/{plan_id}/revisions/` | Saved revisions |
| `POST /api/plans/{plan_id}/revisions/{version}/restore/` | Restore as a new version, preserving later reported work |
| `POST /api/plans/{plan_id}/upgrade/` | Upgrade an existing legacy plan; tasks require review |
| `GET /api/list/` / `DELETE /api/{goal_id}/` | Flat goal list / guarded goal deletion |

Mutations advance plan versions. Refresh the plan after a 409 rather than blindly
reapplying stale proposals. Unfinished committed sessions protect task/goal edits;
finish or cancel the relevant session before changing its work. Restoring a plan
never recreates, moves or deletes Google events. The old decomposition, preview,
import-preview endpoints and smartify command have been removed.
