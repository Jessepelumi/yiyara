# Yiyara

Yiyara is a personal planning assistant that turns ambitions into concrete tasks
and approved Google Calendar sessions. It asks for missing context, preserves
waiting states, and adapts the plan to user-reported progress.

This repository contains the Django REST backend. The current individual flow has
been tested end to end through Postman with live AI and Google Calendar. Connecting
the frontend to that flow is the next priority; it is not yet a verified web experience.

## What works

- Google sign-in and separate Calendar consent, with encrypted provider credentials.
- Persistent ambitions, contextual clarification, questions and user-approved task proposals.
- Actionable tasks with effort, completion criteria, prerequisites and blockers.
- Calendar preferences, timezones and protected grey periods; reviewed session publication.
- Explicit overlap decisions, duplicate-safe publishing, rescheduling, cancellation
  and refresh after external Google edits.
- Partial/completed/blocked/skipped progress, continuation sessions, AI replanning
  and plan revisions that preserve reported work.

Creating an ambition saves it first. AI clarification and task generation are
separate requests. Accepting a task plan does not publish Calendar events; each
session needs its own approval.

## Start here

[Local setup](docs/guides/setup.md) →
[Postman walkthrough](docs/guides/postman.md) →
[December MVP checklist](docs/product/individual-mvp.md).

| Guide | Owns |
| --- | --- |
| [Authentication](docs/guides/authentication.md) | Google identity, application JWTs, refresh and account linking |
| [Planning](docs/guides/planning.md) | Ambitions, clarification, task proposals, context and revision rules |
| [Calendar configuration](docs/guides/calendar.md) | OAuth, calendar selection, working hours and grey periods |
| [Sessions](docs/guides/sessions.md) | Scheduling, publication, conflicts, management and reconciliation |
| [Progress](docs/guides/progress.md) | Outcomes, finishing sessions, continuation and replanning |
| [AI configuration](docs/guides/ai-configuration.md) | Provider routing, limits, failures and request recovery |
| [Deployment](docs/guides/deployment.md) | Production entry point and outstanding operational setup |

## System

Python 3.12, Django, Django REST Framework, PostgreSQL, JWT and a configurable AI
gateway. Gemini is the configured provider; an OpenAI-compatible adapter is also
available but requires explicit provider configuration. Google Calendar writes
are performed by backend services, not by the model.

`Plan → Goal → Task` stores the work; conversations, proposed changes and revisions
store planning history. `WorkSession` represents a task's Calendar block, with
separate publication, management, synchronization and completion state.

| Source | Responsibility |
| --- | --- |
| `users/` | Identity and JWT lifecycle |
| `goals/`, `conversations/` | Context, questions, proposals and revisions |
| `tasks/` | Task readiness, dependencies and outcomes |
| `calendars/` | Credentials, availability, sessions and Google event lifecycle |
| `ai/` | Prompts, structured output, provider adapters and retry policy |
| `config/` | Application settings, shared policy, routes and server configuration |

## Current boundaries

Scheduling handles one continuous task session at a time. There is no automatic
session splitting, bulk ambition scheduling, resource research, cross-ambition
priority optimization, background Calendar sync or messaging delivery yet. Team
assignment and project management are later product scope.

The December 14, 2026 target is an individual private beta, including one supported
messaging channel. See the [MVP checklist](docs/product/individual-mvp.md) for the
remaining release requirements.

Licensed under [MIT](LICENSE).
