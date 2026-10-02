# Yiyara Backend

Yiyara turns an ambition into a persistent drawing board: the model first
decomposes the ambition into goals, then breaks every goal into actionable
tasks. An authenticated user can continue refining the whole board, or one
selected goal, through a single plan conversation.

The service is built with Django, Django REST Framework, PostgreSQL, JWT, and a
provider-agnostic AI gateway. See [backend.md](backend.md) for the broader
architecture and [docs/ai-workflow.md](docs/ai-workflow.md) for provider
routing, retries, fallbacks, and the proposed background-job migration.

## Current capabilities

- Anonymous decomposition preview without database persistence.
- Authenticated decomposition persisted atomically as one plan, its goals,
  tasks, initial conversation, and revision.
- Goal and plan retrieval scoped to the authenticated user.
- One drawing-board conversation per plan, with optional goal scope.
- AI-proposed board changes that require explicit apply or reject actions.
- Plan version checks and restorable board revisions.
- Idempotent console messages through client-generated message IDs.
- Bounded AI calls with structured output, retries, provider fallback, and
  domain validation before writes.
- Gemini plus an OpenAI-compatible adapter for Groq, OpenRouter, local Ollama,
  and similar APIs.

## Core API

Authentication:

- `POST /api/users/auth/bridge/` exchanges a verified frontend identity for
  backend JWTs.

Decomposition and plans:

- `POST /api/decompose/preview/` returns a guest preview and writes nothing.
- `POST /api/decompose/` creates an authenticated user's plan.
- `POST /api/plans/import-preview/` persists a prior guest preview.
- `GET /api/plans/` lists the user's drawing boards.
- `GET /api/plans/{plan_id}/` returns one board with nested goals and tasks.
- `GET /api/list/` provides the legacy flat goal list.

Drawing-board conversation:

- `GET /api/conversations/plans/{plan_id}/messages/` returns its messages.
- `POST /api/conversations/plans/{plan_id}/messages/` discusses or proposes
  changes to the board. `scope_goal_id` may focus the prompt on one goal.
- `POST /api/conversations/plans/{plan_id}/changes/{change_id}/apply/` applies
  a proposal.
- `POST /api/conversations/plans/{plan_id}/changes/{change_id}/reject/` rejects
  a proposal.
- `GET /api/plans/{plan_id}/revisions/` lists restorable revisions.
- `POST /api/plans/{plan_id}/revisions/{version}/restore/` restores a snapshot
  as a new plan version.

Except for the preview route, these endpoints require a bearer token. Object
queries are scoped to `request.user`.

## AI execution model

The existing endpoints intentionally remain synchronous for frontend
compatibility. The AI gateway keeps them bounded with:

- per-attempt and total timeouts;
- transient-error retries with exponential full jitter;
- ordered fallback configured separately for each AI task;
- JSON Schema output where supported;
- application and domain validation before any plan data is persisted;
- sanitized operational logs containing provider, model, attempt, latency, and
  token usage, but not prompts or credentials.

Making a Django view asynchronous would not make model work durable. The next
stage is a persistent job model plus a Redis-backed worker, while reusing the
same gateway. That migration is detailed in
[docs/ai-workflow.md](docs/ai-workflow.md).

## Local setup

Requirements:

- Python 3.10+
- PostgreSQL
- credentials for at least one hosted provider, or a local Ollama model

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py runserver
```

Configure provider order in `.env`. Only providers named in a route are loaded;
providers with missing credentials are skipped.

```dotenv
AI_PROVIDER_ORDER=groq,gemini
AI_DECOMPOSITION_PROVIDERS=groq,gemini
AI_PLAN_ITERATION_PROVIDERS=gemini,groq

GROQ_API_KEY=your_key
GROQ_MODEL=openai/gpt-oss-20b

GEMINI_API_KEY=your_key
GEMINI_MODEL=gemini-2.5-flash-lite
```

For local Ollama, add `ollama` to the desired route and set
`OLLAMA_BASE_URL`. When Django runs in Docker, the default example uses
`host.docker.internal` to reach Ollama on the host.

The full configuration surface is documented in [.env.example](.env.example).

## Usage examples

Preview without authentication:

```bash
curl -X POST http://localhost:8000/api/decompose/preview/ \
  -H "Content-Type: application/json" \
  -d '{"text":"Build a freelance design business"}'
```

Persist a decomposition:

```bash
curl -X POST http://localhost:8000/api/decompose/ \
  -H "Authorization: Bearer <jwt_token>" \
  -H "Content-Type: application/json" \
  -d '{"text":"Build a freelance design business"}'
```

Continue on its drawing board:

```bash
curl -X POST http://localhost:8000/api/conversations/plans/<plan_id>/messages/ \
  -H "Authorization: Bearer <jwt_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "content":"Make the first task more specific",
    "scope_goal_id":"<goal_id>",
    "plan_version":1,
    "client_id":"<client_generated_uuid>"
  }'
```

AI changes are returned as proposals and do not mutate the board until the
apply endpoint is called.

## Development checks

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

To exercise configured AI routing manually:

```bash
python manage.py smartify "Learn Python and build a web application"
```

## Deployment notes

- Do not use free hosted model tiers for sensitive production data without
  reviewing their data-use terms.
- PostgreSQL is required for normal deployment.
- Redis and a worker are planned for durable background AI jobs; they are not
  required by the current synchronous endpoints.
- The project is private and proprietary.

Built by Jesse Adesina as part of the Yiyara platform.
