# Yiyara Backend Architecture

This document explains the backend architecture of the Yiyara project, including how each part of the system connects and what each component does.

## Overview

Yiyara is an AI-powered life planning and goal management platform. The backend is built with Django and Django REST Framework, and it provides APIs for user authentication, goal creation, task management, and chat-based AI interaction.

## Project Structure

```
yiyara-backend/
├── manage.py
├── Dockerfile
├── README.md
├── backend.md
├── requirements.txt
├── memory_test.py
├── config/
├── users/
├── goals/
├── tasks/
├── conversations/
├── workflow/
└── ai/
```

## Core Components

### `config/`

This is the main Django project configuration.

- `settings.py` — Defines installed apps, authentication, database configuration, REST framework settings, JWT settings, CORS settings, and static file configuration.
- `urls.py` — Routes incoming requests to the admin interface and to app-level URL configurations.
- `views.py` — Contains a simple health check view that verifies the database connection.
- `asgi.py` / `wsgi.py` — Entry points for ASGI and WSGI servers.

### `users/`

This app manages user authentication and account creation.

- `models.py` — Defines a custom `User` model with UUID primary key, email authentication, optional username generation, and verification fields.
- `serializers.py` — Contains serializers for user-related API responses.
- `views.py` — Includes `InternalAuthView`, which accepts verified email data from an external auth provider and returns JWT tokens.
- `urls.py` — Exposes the internal auth bridge endpoint.
- `admin.py` — Registers user models for the Django admin.
- `tests.py` — Contains tests for user functionality.

### `goals/`

This app manages goals and the AI-driven decomposition of raw user input.

- `models.py` — Defines the `Goal` model with fields for user, title, description, due date, completion status, and raw input.
- `serializers.py` — Serializes goals and nests related tasks.
- `views.py` — Contains three main views:
  - `DecomposeGoalView` — Receives raw text, runs AI decomposition, and creates goals and tasks.
  - `GoalListView` — Lists the authenticated user's goals with nested tasks.
  - `DeleteGoalView` — Deletes a specific goal by UUID for the authenticated user.
- `urls.py` — Registers goal-related endpoints.
- `management/commands/smartify.py` — CLI command that also uses the AI workflow to create goals from text.
- `migrations/` — Database migration files for goal models.

### `tasks/`

This app stores tasks that are created as part of goal decomposition.

- `models.py` — Defines the `Task` model with a foreign key to `Goal`, title, description, due date, and completion status.
- `serializers.py` — Serializes task data for use in goal responses.
- `views.py` — Present but currently empty, as tasks are surfaced through goal endpoints.
- `admin.py` — Registers task models for admin management.
- `migrations/` — Database migration files for task models.

### `conversations/`

This app manages chat conversations between users and the AI assistant.

- `models.py` — Defines `Conversation` and `Message` models; conversations link to a `Goal` and a `User`, and messages store content and role.
- `serializers.py` — Serializes conversations and nested messages.
- `views.py` — Contains two main views:
  - `ChatAPIView` — Receives chat messages, ensures a conversation exists, and routes the message to AI handling logic.
  - `ConversationHistoryView` — Retrieves the message history for a specific goal's conversation.
- `services.py` — Contains `handle_yiyara_logic`, which classifies intent, persists chat messages, and generates AI responses.
- `urls.py` — Registers the chat and history endpoints.
- `migrations/` — Database migration files for conversation and message models.

### `workflow/`

This package contains the shared AI orchestration logic.

- `ai_engine.py` — Defines `YiyaraWorkflow`, the class responsible for converting raw user input into structured goals and tasks using AI.

### `ai/`

This package contains provider wrappers and prompt definitions for AI.

- `providers/gemini_provider.py` — Wraps Google Gemini AI calls and provides methods for intent classification, free-form response generation, and structured JSON generation.
- `providers/chatgpt_provider.py` — Placeholder for OpenAI ChatGPT integration.
- `prompts/goal_decomposition_prompt.py` — Defines the system prompt used to instruct the AI to decompose user input into SMART goals and tasks.

## How Components Connect

### Workflow Chart

```mermaid
flowchart TD
  A[Frontend Request] -->|POST /api/decompose/| B[goals.views.DecomposeGoalView]
  B --> C[YiyaraWorkflow.create_goals_from_ai()]
  C --> D[GeminiProvider.generate_structured_response()]
  D --> E[Parse AI JSON into goals/tasks]
  E --> F[Save Goal and Task models to DB]
  F --> G[Return created goals/tasks JSON]

  A2[Frontend Request] -->|GET /api/list/| H[goals.views.GoalListView]
  H --> I[Query Goal.objects.filter(user)]
  I --> J[Prefetch related tasks]
  J --> K[Return goals + tasks JSON]

  A3[Frontend Request] -->|POST /api/conversations/chat/| L[conversations.views.ChatAPIView]
  L --> M[Get or create Conversation]
  M --> N[Save user Message]
  N --> O[GeminiProvider.classify_intent()]
  O -->|DECOMPOSE| C
  O -->|CHAT| P[GeminiProvider.generate_response()]
  O -->|QUERY| Q[Return placeholder response]
  P --> R[Save AI Message]
  R --> S[Return chat response JSON]

  A4[Frontend Request] -->|GET /api/conversations/history/<uuid>/| T[conversations.views.ConversationHistoryView]
  T --> U[Query latest conversation for goal/user]
  U --> V[Return serialized message history]
```

### Authentication and User Flow

1. External auth provider verifies the user and sends a verified email to the backend.
2. `users.views.InternalAuthView` receives the request.
3. It validates the request using `INTERNAL_AUTH_SECRET`.
4. It creates or updates the `User` record.
5. It returns JWT access and refresh tokens.
6. The frontend uses the JWT token for authenticated API calls.

### Goal Creation and AI Decomposition

1. The frontend sends raw text to `POST /api/decompose/`.
2. `goals.views.DecomposeGoalView` validates the request and instantiates `YiyaraWorkflow`.
3. `YiyaraWorkflow.create_goals_from_ai()` builds a prompt using `DECOMPOSITION_SYSTEM_PROMPT`.
4. It calls `GeminiProvider.generate_structured_response()` to get JSON output from Gemini AI.
5. The response is parsed into goal and task objects.
6. Goals and tasks are stored in the database using Django models.
7. The API returns newly created goals with nested tasks.

### Goal Listing

1. The frontend requests `GET /api/list/`.
2. `goals.views.GoalListView` fetches the authenticated user's goals.
3. It prefetches related `tasks` to avoid extra queries.
4. It serializes goals and nested tasks and returns them.

### Goal Deletion

1. The frontend sends `DELETE /api/<uuid>/` with the goal UUID.
2. `goals.views.DeleteGoalView` verifies the goal belongs to the authenticated user.
3. It deletes the goal and associated tasks (via cascade).
4. Returns a 204 No Content response.

### Conversation and Chat Flow

1. The frontend sends a chat message to `POST /api/conversations/chat/` with either `goal_id` or `conversation_id`.
2. `conversations.views.ChatAPIView` retrieves or creates the `Conversation`.
3. `conversations.services.handle_yiyara_logic()` stores the user message.
4. It classifies the intent using `GeminiProvider.classify_intent()`.
5. If the intent is `DECOMPOSE`, it uses `YiyaraWorkflow` to create goals from the chat text.
6. If the intent is `QUERY`, it returns a placeholder response.
7. If the intent is `CHAT`, it builds chat history and calls `GeminiProvider.generate_response()`.
8. The AI response is saved as a `Message` and returned.

### Conversation History Retrieval

1. The frontend requests `GET /api/conversations/history/<uuid>/` with a goal UUID.
2. `conversations.views.ConversationHistoryView` finds the latest conversation for that goal and user.
3. It retrieves all messages in chronological order.
4. Returns the serialized message history.

## Data Relationships

- A `User` can own many `Goals`.
- Each `Goal` can have many `Tasks`.
- Each `Goal` can have many `Conversations`.
- Each `Conversation` has many `Messages`.

## Important Notes

- Tasks do not currently have their own exposed API routes; they are managed through goals.
- `chatgpt_provider.py` is present but not actively used.
- The AI workflow is shared between the goal decomposition endpoint and the conversational intent handler.
- The current conversation query handling is a placeholder and can be extended with retrieval-augmented generation (RAG) logic.

## Endpoint Summary

- `POST /api/users/auth/bridge/` — Authenticate internal users and return JWT.
- `POST /api/decompose/` — Convert raw input into goals and tasks.
- `GET /api/list/` — Retrieve authenticated user's goals and tasks.
- `DELETE /api/<uuid>/` — Delete a specific goal by UUID.
- `POST /api/conversations/chat/` — Send chat messages and receive AI responses.
- `GET /api/conversations/history/<uuid>/` — Retrieve conversation history for a goal.

## Running the Backend

1. Create and activate a Python virtual environment.
2. Install dependencies from `requirements.txt`.
3. Set environment variables for PostgreSQL, `GEMINI_API_KEY`, and `INTERNAL_AUTH_SECRET`.
4. Run Django migrations.
5. Start the development server with `python manage.py runserver`.

## Summary

This document is intended to help any developer understand how the Yiyara backend is organized, what each app does, and how the components interact to support AI-driven goal decomposition, task management, and conversations.