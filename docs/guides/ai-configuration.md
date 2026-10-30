# AI configuration and recovery

## Provider configuration

The current settings configure Gemini through `GEMINI_API_KEY` and `GEMINI_MODEL`.
Select a model available to your account. Restart/recreate the service as described
in [setup](setup.md) after configuration changes.

`AI_PROVIDER_ORDER` supplies the default route order. `AI_PLAN_ITERATION_PROVIDERS`
controls clarification/conversations and `AI_TASK_PLANNING_PROVIDERS` controls task
generation/replanning. Each is a comma-separated provider-name list.

Changing the order alone does not add a provider. Register its backend class and
options in `AI_PROVIDERS` in [settings](../../config/settings.py). Only Gemini is
registered there currently. `OpenAICompatibleProvider` is an available adapter,
not an automatically enabled integration. Providers implement
`generate(GenerationRequest) → GenerationResult`; workflows use the shared gateway.

## Execution

Calls are synchronous, schema/domain validated and bounded by per-attempt and total
timeouts. Transient failures receive jittered retries, followed by configured
fallback when available. Defaults and environment names are defined once in
[settings](../../config/settings.py): see `AI_*`, `PLAN_*`, `CONVERSATION_HISTORY_LIMIT`
and `OUTCOME_HISTORY_LIMIT`. Prompts consume the shared policy rather than separate
limits. `GET /api/policy/` exposes the subset needed by clients.

Generation produces reviewable changes; it never authorizes Calendar writes.
Validation rejects malformed or unsafe operations but cannot prove that every task
is useful. Review the action, effort, prerequisites and completion criteria.

## Request identity and recovery

Use a UUID `client_id` per logical AI request. Preserve it, content and focus for an
exact retry; use a new UUID for new work. A live attempt returns 409. After its
lease expires the saved request can be reclaimed with the same ID and current plan
version. Completed requests replay the saved result instead of calling AI again;
late superseded attempts cannot commit.

`AI_REQUEST_LEASE_SECONDS` must exceed the total AI timeout. There is no durable job
queue or background worker; process interruption requires a client retry.

A 502 `plan_iteration_failed` or `task_planning_failed` can mean provider failure,
timeout or validation failure. The saved message is retained. Retry the same ID;
if repeated, inspect `docker compose logs backend`. A 503 `ai_not_configured` means
no usable configured route/provider. Do not log raw prompts, tokens or credentials.
