# Individual private beta

Target: **December 14, 2026**. Updated: **October 31, 2026**.
The [README](../../README.md) owns current implemented
capabilities; this document owns release scope and acceptance criteria.

## Success

A person can describe an ambition, clarify what is known or missing, approve
concrete next actions and Calendar sessions, then report work and continue from
accurate saved state. Missing information should produce questions or waiting,
not invented commitments. The same account should support the web interface and
one opted-in messaging channel. No native mobile app is required.

## Release checklist

| Area | Remaining acceptance work |
| --- | --- |
| Web integration | Connect Google sign-in/refresh, Calendar consent, ambition conversation, proposal review, tasks and calendar controls to the tested backend |
| Daily use | Show next actions, upcoming sessions, blockers and waiting items with useful mobile layouts and clear loading/empty/error/retry states |
| Grey periods | Let users manage unavailable time and understand conflicts with already published sessions; do not silently move existing events |
| Planning quality | Test varied ambitions, long-term/mixed goals, missing context and external dependencies; verify useful near-term work rather than vague task lists |
| Calendar trust | Repeat live checks for timezones, conflicts, provider errors, rescheduling, cancellation and external edits; preserve explicit approval and duplicate protection |
| Messaging | Validate the provider/channel, securely link identity, and implement opted-in reminders, acknowledgements and progress replies using the same task/session state |
| Operations | Production deployment, user/data deletion path, backup/restore, sanitized error reporting, tester feedback and measured reliability/cost |

The maintainer completed the live ambition-to-Calendar-and-completion flow in
Postman, including grey-period impact review, keeping an existing session,
rescheduling another and visible completion in Google Calendar. The backend is
pushed to GitHub with 306 automated tests passing. Frontend integration is next;
this does not establish private-beta release readiness.
The [Postman guide](../guides/postman.md) is the repeatable acceptance procedure.

## Scheduling and user-task review — October 31, 2026

The agreed scheduling behavior is implemented and pushed; the
[session guide](../guides/sessions.md#user-selected-times-and-exceptions) owns its
current API contract and [Calendar guide](../guides/calendar.md#impact-review-when-creating-or-editing-a-grey-period)
owns impact review. User additions can receive reviewable AI fit assessments via
[task-list review](../guides/planning.md#review-user-added-tasks).

Release acceptance must cover automatic free slots, user-confirmed grey/busy
exceptions, per-appointment keep/review choices after grey edits, changed-conflict
reconfirmation, independent third-party availability checks and duplicate-safe
recovery. User task input must survive review and edits require approval.
Completion must remain explicit and visible in Calendar, with restoration on
reopening and retryable sync failures; see [completion appearance](../guides/progress.md#calendar-completion-appearance).
Finished sessions remain excluded from grey-period impact review.

## Next order of work

1. Inspect and integrate the existing frontend with the verified backend flow,
   including scheduling exceptions, grey-period review and task-list review.
2. Address recorded planning-quality gaps: jurisdiction and prerequisites,
   explicit deadlines, scoped lesson availability and useful completion criteria.
3. Have testers complete real ambitions; correct planning and usability failures.
4. Add the messaging slice after the web flow is usable. Confirm channel/provider
   feasibility early rather than assuming eligibility or delivery behavior.
5. Complete operational release checks before inviting the private-beta group.

WhatsApp is the preferred candidate, subject to provider eligibility and practical
integration validation. The initial messaging promise is upcoming/start reminders,
acknowledgement and unambiguous progress updates—not access to other private chats.
“Noted” acknowledges a reminder; it does not complete work. Require opt-in/out,
quiet hours, authenticated webhooks, duplicate protection, ambiguity handling and
visible delivery failures. Typing indicators depend on channel support; delivery
must not be promised as instantaneous.

Teams, assignment/blocker graphs, accountability partners, analytics, iMessage,
voice calls and a broader personal-assistant toolset remain later scope. Existing
backend limits are described in the [README](../../README.md#current-boundaries),
not implied to be implemented here.
