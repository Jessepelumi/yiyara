# AI workflow

## Current execution model

Yiyara keeps the existing HTTP response contract synchronous for now, but every
model call is bounded by:

- a per-attempt timeout;
- a total request deadline shared by every provider;
- retry of transient failures only;
- exponential backoff with full jitter;
- ordered provider fallback;
- application-level JSON and domain validation before database writes.

Making a Django view `async def` would not make the operation durable: the user
would still wait for the model, and a process restart would still lose the work.
True asynchronous execution requires a persistent job plus a worker.

## Provider routing

Routes are configured per AI task. The first configured provider is preferred;
the next provider is used when the first is unavailable, rate-limited for longer
than the retry window, returns invalid structured output, or rejects the request.

```dotenv
AI_PROVIDER_ORDER=groq,gemini,ollama
AI_DECOMPOSITION_PROVIDERS=groq,gemini,ollama
AI_PLAN_ITERATION_PROVIDERS=gemini,groq,ollama
```

Only providers named by a route are initialized. A provider with missing
credentials is skipped. If no provider in a route is usable, the API returns its
existing `ai_not_configured` response.

The default policy makes at most two attempts per provider and never exceeds the
shared 45-second deadline. A long `Retry-After` value causes immediate fallback
instead of making an interactive request sleep for a minute.

## Available adapters

- `GeminiProvider` uses Google's native structured-output API.
- `OpenAICompatibleProvider` supports Groq, OpenRouter, local Ollama, and other
  compatible `/chat/completions` services.
- A new provider can be added without changing a workflow: implement
  `generate(GenerationRequest) -> GenerationResult`, add its dotted class path
  to `AI_PROVIDERS`, then place its name in a task route.

System instructions and user input are sent as separate roles. Provider errors
are sanitized before logging; prompts and credentials are never logged. Logs do
include task, provider, model, attempt, latency, and token usage when available.

## Free development options

- **Local Ollama** is the most repeatable option because it has no hosted quota.
  It supports JSON Schema structured output locally. Model quality and speed
  depend on the development machine.
- **Groq Free** is a strong hosted fallback for development. Its GPT-OSS and Qwen
  models support structured output, but free rate and token limits still apply.
- **Gemini Flash-Lite** remains useful as another free route. Free-tier prompts
  may be used to improve Google's products, so do not send sensitive production
  data through that tier.
- **OpenRouter Free** provides model diversity, but its request allowance is
  small and the available free models change. It is best used as a development
  fallback rather than a production dependency.
- **Cloudflare Workers AI** has a daily free allocation and can be added as
  another adapter when needed.

Provider availability and free quotas change. Confirm them in the official
provider documentation before relying on them:

- https://console.groq.com/docs/rate-limits
- https://ai.google.dev/gemini-api/docs/pricing
- https://openrouter.ai/pricing
- https://docs.ollama.com/capabilities/structured-outputs
- https://developers.cloudflare.com/workers-ai/platform/pricing/

## Background-job migration

The next reliability step should be additive rather than changing the current
endpoints in place:

1. Add an `AIJob` model with `queued`, `running`, `succeeded`, `failed`, and
   `cancelled` states, an idempotency key, attempt count, timestamps, sanitized
   error code, and result reference.
2. Add `POST /api/ai/jobs/decomposition/` returning `202 Accepted` and a job ID.
3. Add `GET /api/ai/jobs/{id}/` for polling; streaming or server-sent events can
   be added later.
4. Execute jobs in a worker such as Celery or Dramatiq backed by Redis. The
   worker calls the same `AIGateway`; provider policy stays in one place.
5. Persist plans and revisions only after a validated model result. Use the job
   idempotency key to guarantee that worker retries cannot create duplicate
   plans or messages.
6. Keep the existing synchronous endpoints until the frontend switches to the
   job API, then remove them deliberately.

For an MVP, this separation avoids introducing Redis and worker operations before
they solve an observed latency or reliability problem, while keeping the domain
workflow ready for that migration.
