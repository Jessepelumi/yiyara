# API Documentation

This guide documents the Yiyara REST API endpoints.

## Base URL

```
http://localhost:8000/api/
```

## Authentication

The API uses JWT (JSON Web Token) authentication. Include the token in the Authorization header:

```
Authorization: Bearer <your-jwt-token>
```

### Obtaining a Token

Currently, authentication is handled through Django's admin interface or custom implementation. JWT tokens are issued upon successful login.

## Endpoints

### Goals

#### Decompose Goal

Convert natural language goal into structured SMART goals and tasks using AI.

**Endpoint:** `POST /api/decompose/`

**Request Body:**

```json
{
  "text": "I want to learn Python programming and build a web application"
}
```

**Response (Success):**

```json
[
  {
    "id": "uuid",
    "title": "Learn Python Fundamentals",
    "description": "Master basic Python concepts and syntax",
    "due_date": "2026-06-01",
    "is_completed": false,
    "tasks": [
      {
        "id": "uuid",
        "title": "Complete Python tutorial",
        "description": "Work through an online Python tutorial",
        "due_date": "2026-03-20",
        "is_completed": false
      }
    ]
  }
]
```

**Response (Clarification Needed):**

```json
{
  "error": "clarification_needed",
  "message": "Please provide more specific details about your goal."
}
```

**Error Response:**

```json
{
  "error": "AI Processing Failed",
  "details": "Specific error message"
}
```

#### List Goals

Retrieve all goals and their associated tasks for the authenticated user.

**Endpoint:** `GET /api/list/`

**Response:**

```json
[
  {
    "id": "uuid",
    "title": "Learn Python Fundamentals",
    "description": "Master basic Python concepts and syntax",
    "due_date": "2026-06-01",
    "is_completed": false,
    "tasks": [
      {
        "id": "uuid",
        "title": "Complete Python tutorial",
        "description": "Work through an online Python tutorial",
        "due_date": "2026-03-20",
        "is_completed": false
      }
    ]
  }
]
```

## Data Models

### Goal

| Field        | Type    | Description                    |
| ------------ | ------- | ------------------------------ |
| id           | UUID    | Unique identifier              |
| title        | string  | Goal title                     |
| description  | string  | Goal description               |
| due_date     | date    | Optional due date (YYYY-MM-DD) |
| is_completed | boolean | Completion status              |
| tasks        | Task[]  | Associated tasks               |

### Task

| Field        | Type    | Description                    |
| ------------ | ------- | ------------------------------ |
| id           | UUID    | Unique identifier              |
| title        | string  | Task title                     |
| description  | string  | Task description               |
| due_date     | date    | Optional due date (YYYY-MM-DD) |
| is_completed | boolean | Completion status              |

## Error Codes

| Code | Description                                          |
| ---- | ---------------------------------------------------- |
| 400  | Bad Request - Missing or invalid parameters          |
| 401  | Unauthorized - Invalid or missing authentication     |
| 500  | Internal Server Error - Server-side processing error |

## Rate Limiting

Currently, no rate limiting is implemented. AI processing may be subject to provider limits.

## SDK

The frontend includes a TypeScript client in `src/lib/api/`. Example usage:

```typescript
import { goalsApi } from "@/lib/api/goals";

// Decompose a goal
const goals = await goalsApi.decompose("Learn React");

// List all goals
const allGoals = await goalsApi.list();
```

## Testing

Use the test endpoint to verify database connectivity: `GET /test/`

## Future Endpoints

Planned additions:

- Task CRUD operations
- Goal update/completion
- User authentication endpoints
- Progress tracking</content>
  <parameter name="filePath">/Users/jeolad/Documents/yiyara/docs/guides/api.md
