# Test the complete flow in Postman

Import the [collection](../postman/yiyara-ambition-to-calendar.postman_collection.json).
It contains request URLs, bodies and scripts; this guide explains how to use it.
Run requests individually with **Send**, not Run collection: AI clarification and
all approvals need human review. The requests retain their imported order.

## Initial configuration

1. Select **No environment** in the top-right dropdown, so environment variables do
   not override collection values.
2. Collection → Authorization: **Bearer Token**, value `{{access_token}}`.
3. Collection → Variables: keep base_url at your backend URL; enter a fresh Google
   ID token in google_id_token. Leave access_token and refresh_token blank initially.
   Use [authentication](authentication.md) for token types and sign-in setup.
4. Google sign-in → Authorization: **No Auth**. Save and Send. Its post-response
   script saves the returned Yiyara tokens. Other protected requests inherit the
   collection authorization.
5. Send Confirm signed-in user. Expect your account details.

Use local variable values; do not export or share filled credentials. Changing
request names or importing a newer collection should not involve copying secrets
into the JSON file.

## Run sequence

| Requests | What to do |
| --- | --- |
| Start Calendar connection → Check Calendar connection | Skip start if connected; otherwise open authorization_url in the browser and complete consent |
| List calendars → Select source and destination calendar | Put an actual owned calendar ID in calendar_id; include other busy calendars as sources if needed |
| Save timezone and working hours → Add lunch grey period | Review before sending; these persist real settings. Send grey-period creation once or skip an existing rule |
| Save ambition | Creates a new test plan; its script resets this run's IDs |
| Ask AI to clarify → Apply reviewed context/questions | Inspect reply and change.operations; apply only a correct non-null change |
| Inspect full plan and questions | Answer actual open required questions with Optional — Answer a required question; otherwise skip it |
| Confirm explicit context and calendar rules | A manual confirmation for the sample; skip if approved context already has the correct starting point and rules |
| Ask AI for concrete tasks → Apply reviewed task proposal | Review actionability before applying; creates tasks and selects a schedulable task_id |
| Inspect chosen task → Propose a free time block | Confirm eligibility, then review time, duration, destination and conflicts |
| Approve reviewed session (REAL CALENDAR WRITE) | Creates the actual event. Check Google Calendar, then repeat approval and verify the same event ID with no duplicate |
| Refresh linked Calendar event | Reads the Google event and reconciliation state |
| Fetch latest plan version → Finish test session | Optional simulated completion for test work; preserves the historical Google event |

The sample ambition is deliberately detailed; real users can begin with a short
ambition and answer clarification. Do not treat the manual context-confirmation
sample as a required step when the AI-approved context is already sufficient.

For detailed behavior, follow [planning](planning.md), [Calendar](calendar.md),
[sessions](sessions.md) and [progress](progress.md). These guides own the API rules;
the collection owns the executable end-to-end examples.

## Variables and recovery

- Scripts save plan_id, latest plan_version and returned proposal IDs. If change_id
  is empty, inspect the generation response: null change or an error means there is
  nothing to apply. For a valid proposal, manually copying change.id is safe after
  reviewing it and checking its plan_id.
- chat_client_id, task_client_id and finish_client_id are generated UUIDs. Preserve
  them for exact retries; clear the relevant variable before sending new content
  or focus. Updated scripts use Postman's `{{$guid}}` and repair the old `{$guid}`
  placeholder. If using an earlier import, re-import this collection.
- If an access token expires, send Optional — Refresh application JWT, then retry
  the original request. This collection does not automatically refresh. If refresh
  fails, sign in again. Do not reset an AI request ID just because access expired.
- On a plan-version conflict, fetch the latest plan and review it before retrying.
  Model failure recovery is covered in [AI recovery](ai-configuration.md#request-identity-and-recovery).
- time_min/time_max are generated as a future seven-day search range once. Clear
  both for a later run, or enter appropriate future offset-bearing timestamps.
- Optional — Delete test grey period removes only this run's saved grey period.
  To remove a Google test event, use [session cancellation](sessions.md#reschedule-or-cancel)
  or delete it in Google and refresh. No automatic event cleanup is performed.
