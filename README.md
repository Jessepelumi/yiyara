# Yiyara Backend

Yiyara is an AI-powered life planning and goal management platform designed to help users turn long-term goals into clear, actionable steps.

This repository contains the backend service for Yiyara, built with Django and Django REST Framework. It provides REST APIs for authentication, goal management, task planning, scheduling, and AI-powered features.

For a detailed architectural overview of how the backend components connect and work together, see [backend.md](backend.md).

---

## 🚀 Features

- User authentication and authorization via JWT
- Goal creation and management with AI-powered SMART goal breakdown
- Task generation and tracking
- Smart planning and scheduling using Google Gemini AI
- Progress tracking
- PostgreSQL database integration
- CORS support for frontend integration
- Conversational AI chat interface for goal refinement

---

## 🛠 Tech Stack

- **Python** - Backend language
- **Django** - Web framework
- **Django REST Framework** - API framework
- **PostgreSQL** - Database
- **Google Generative AI (Gemini)** - AI for goal decomposition and chat
- **JWT Authentication** - Token-based auth via `rest_framework_simplejwt`
- **Docker** - Containerization

---

For a complete backend architecture and integration guide, see [backend.md](backend.md).

Task (tasks.Task)
├── id (UUID)
├── goal (ForeignKey → Goal)
├── title, description
├── due_date, is_completed
└── created_at, updated_at

Conversation (conversations.Conversation)
├── id (UUID)
├── goal (ForeignKey → Goal)
├── user (ForeignKey → User)
├── created_at, updated_at
└── messages (related_name)

Message (conversations.Message)
├── id (UUID)
├── conversation (ForeignKey → Conversation)
├── role (user/assistant/system)
├── content (text)
└── created_at
```

### API Endpoints

#### Authentication (`/api/users/`)
- `POST /api/users/auth/bridge/` — Exchange verified email for JWT tokens

#### Goals (`/api/`)
- `POST /api/decompose/` — AI-powered goal creation from raw text
- `GET /api/list/` — Fetch user's goals with nested tasks

#### Conversations (`/api/conversations/`)
- `POST /api/conversations/chat/` — Send message and get AI response

#### Admin
- `/admin/` — Django admin interface

### Key Workflows

#### 1. User Authentication Flow
1. Frontend authenticates user (NextAuth)
2. Frontend calls `POST /api/users/auth/bridge/` with verified email
3. Backend creates/finds `User` record
4. Backend returns JWT access + refresh tokens
5. Frontend uses JWT for subsequent API calls

#### 2. Goal Creation Flow
1. User submits raw text (e.g., "I want to get fit and lose weight")
2. Frontend calls `POST /api/decompose/` with JWT auth
3. `DecomposeGoalView` receives request
4. Calls `YiyaraWorkflow.create_goals_from_ai(user, raw_text)`
5. `YiyaraWorkflow` builds prompt using `DECOMPOSITION_SYSTEM_PROMPT`
6. Calls `GeminiProvider.generate_structured_response()` for JSON parsing
7. Parses AI response into goal/task data structures
8. Creates `Goal` and `Task` records in database transaction
9. Returns created goals with nested tasks

#### 3. Goal Listing Flow
1. Frontend calls `GET /api/list/` with JWT auth
2. `GoalListView` fetches user's goals with `prefetch_related('tasks')`
3. `GoalSerializer` nests `TaskSerializers` for each goal
4. Returns goals + tasks in single API response

#### 4. Chat/Conversation Flow
1. User sends message with `goal_id` or `conversation_id`
2. Frontend calls `POST /api/conversations/chat/`
3. `ChatAPIView` ensures `Conversation` exists for goal/user
4. Calls `handle_yiyara_logic(user, conversation, raw_text)`
5. Saves user message to database
6. Calls `GeminiProvider.classify_intent()` to determine: DECOMPOSE/QUERY/CHAT
7. **If DECOMPOSE:** Calls `YiyaraWorkflow.create_goals_from_ai()` (same as above)
8. **If QUERY:** Returns placeholder (RAG logic pending)
9. **If CHAT:** Builds conversation history, calls `GeminiProvider.generate_response()`
10. Saves AI message and returns it

### AI Integration Details

#### `workflow/ai_engine.py` - YiyaraWorkflow
- Main AI orchestration class
- `create_goals_from_ai()`: Core method for goal decomposition
- Uses `GeminiProvider` for structured JSON responses
- Handles database persistence in atomic transactions

#### `ai/providers/gemini_provider.py` - GeminiProvider
- Google Gemini API wrapper
- `generate_structured_response()`: Returns JSON for goal parsing
- `generate_response()`: Free-form chat responses
- `classify_intent()`: Classifies user input as DECOMPOSE/QUERY/CHAT

#### `ai/prompts/goal_decomposition_prompt.py`
- System prompt for SMART goal breakdown
- Defines JSON schema for AI responses

### Configuration (`config/settings.py`)
- Enables apps: `users`, `goals`, `tasks`, `conversations`
- Sets `AUTH_USER_MODEL = 'users.User'`
- Configures DRF with JWT authentication
- Enables CORS for frontend origins
- PostgreSQL database via environment variables

### URL Routing (`config/urls.py`)
- `/admin/` → Django admin
- `/test/` → Health check
- `/api/users/` → User auth endpoints
- `/api/` → Goal endpoints
- `/api/conversations/` → Chat endpoints

---

## 🔧 Setup & Development

### Prerequisites
- Python 3.10+
- PostgreSQL
- Google Gemini API key

### Environment Variables
```bash
# Database
PGHOST=your_postgres_host
PGDATABASE=your_database_name
PGUSER=your_db_user
PGPASSWORD=your_db_password
PGPORT=5432

# AI
GEMINI_API_KEY=your_gemini_api_key

# Auth
INTERNAL_AUTH_SECRET=your_internal_secret
```

### Installation
```bash
# Clone repository
git clone <repository-url>
cd yiyara-backend

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run migrations
python manage.py migrate

# Create superuser
python manage.py createsuperuser

# Run development server
python manage.py runserver
```

### Docker
```bash
# Build and run with Docker
docker build -t yiyara-backend .
docker run -p 8000:8000 yiyara-backend
```

### Testing AI Features
```bash
# Test goal decomposition via management command
python manage.py smartify "I want to learn Python and build a web app"
```

---

## 📊 API Usage Examples

### Authentication
```bash
# Get JWT tokens
curl -X POST http://localhost:8000/api/users/auth/bridge/ \
  -H "Content-Type: application/json" \
  -H "X-Internal-Secret: your_secret" \
  -d '{"email": "user@example.com"}'
```

### Goal Creation
```bash
# Decompose raw text into SMART goals
curl -X POST http://localhost:8000/api/decompose/ \
  -H "Authorization: Bearer <jwt_token>" \
  -H "Content-Type: application/json" \
  -d '{"text": "I want to get fit and lose 10 pounds"}'
```

### Chat Interaction
```bash
# Send message in goal context
curl -X POST http://localhost:8000/api/conversations/chat/ \
  -H "Authorization: Bearer <jwt_token>" \
  -H "Content-Type: application/json" \
  -d '{"content": "How should I start?", "goal_id": "uuid-here"}'
```

---

## 🔄 Development Workflow

1. **Authentication**: Users authenticate via external service, backend provides JWT
2. **Goal Creation**: Raw text → AI decomposition → SMART goals + tasks
3. **Task Management**: Tasks are created automatically, tracked via goals
4. **Conversations**: Goal-specific chat with AI intent classification
5. **Progress Tracking**: Goals and tasks have completion status

---

## 🚀 Deployment

The application is containerized with Docker and designed for deployment to cloud platforms supporting Django applications.

### Key Dependencies
- PostgreSQL for data persistence
- Google Gemini API for AI features
- Redis (planned) for caching and session management

---

## 🤝 Contributing

1. Follow Django best practices
2. Write tests for new features
3. Update documentation
4. Use meaningful commit messages

---

## 📝 Notes

- Tasks are currently managed through goals (no direct task API endpoints)
- ChatGPT provider is scaffolded but not implemented
- RAG (Retrieval-Augmented Generation) for goal queries is planned
- Progress tracking UI and advanced scheduling features are in development
│   ├── serializers.py
│   ├── tests.py
│   ├── urls.py             # Goal API endpoints
│   ├── views.py            # Goal views including AI decomposition
│   ├── management/
│   │   └── commands/
│   │       └── smartify.py # Management command for AI goal processing
│   └── migrations/         # Database migrations
│
├── tasks/                  # Task management app
│   ├── __init__.py
│   ├── admin.py
│   ├── apps.py
│   ├── models.py           # Task model linked to goals
│   ├── serializers.py
│   ├── tests.py
│   ├── views.py
│   └── migrations/
│
├── users/                  # Custom user management app
│   ├── __init__.py
│   ├── admin.py
│   ├── apps.py
│   ├── models.py           # Custom User model with email auth
│   ├── serializers.py
│   ├── tests.py
│   ├── views.py
│   └── migrations/
│
├── workflow/               # AI workflow module
│   ├── __init__.py
│   └── ai_engine.py        # Google Gemini integration for goal processing
│
├── templates/              # HTML templates
│   └── test.html
│
├── manage.py               # Django management script
├── requirements.txt        # Python dependencies
├── Dockerfile              # Docker configuration
├── memory_test.py          # Memory testing script
└── README.md               # This file
```

---

## ⚙️ Local Development Setup

Follow the steps below to set up the backend locally.

### Prerequisites

- Python 3.8+
- PostgreSQL database
- Google Gemini API key

### 1. Clone and Navigate to Project Directory

```bash
git clone <repository-url>
cd yiyara-backend
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Environment Variables

Create a `.env` file in the project root with the following variables:

```env
SECRET_KEY=your-django-secret-key
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1

# Database Configuration
PGHOST=localhost
PGDATABASE=yiyara_db
PGUSER=your_db_user
PGPASSWORD=your_db_password
PGPORT=5432

# AI Configuration
GEMINI_API_KEY=your-google-gemini-api-key
```

### 4. Database Setup

Ensure PostgreSQL is running and create the database:

```bash
createdb yiyara_db
```

### 5. Run Migrations

```bash
python manage.py migrate
```

### 6. Create Superuser

```bash
python manage.py createsuperuser
```

### 7. Run Development Server

```bash
python manage.py runserver
```

The API will be available at `http://localhost:8000/`

### 8. Test AI Functionality

You can test the AI goal decomposition using the management command:

```bash
python manage.py smartify "I want to learn Python and build a web app"
```

---

## 🐳 Docker Setup (Alternative)

If you prefer using Docker:

```bash
# Build the image
docker build -t yiyara-backend .

# Run the container
docker run -p 8000:8000 --env-file .env yiyara-backend
```

---

## 📡 API Endpoints

### Goals API

- `POST /api/decompose/` - Decompose user input into SMART goals and tasks using AI

### Authentication

- Basic authentication is currently implemented
- JWT authentication planned for future releases

### Admin Interface

- Access Django admin at `http://localhost:8000/admin/`

---

## 🧪 Testing

Run tests for all apps:

```bash
python manage.py test
```

---

## 📈 Roadmap

### Phase 1 — Core Backend (Current)

- ✅ User authentication
- ✅ Goals API with AI decomposition
- ✅ Task management
- ✅ PostgreSQL integration
- ✅ Google Gemini AI integration
- Basic planner functionality

### Phase 2 — Intelligence (In Progress)

- Enhanced AI task breakdown
- Smart rescheduling
- Productivity profiling
- Advanced analytics

### Phase 3 — Platform (Planned)

- JWT authentication
- Team collaboration
- Accountability features
- Analytics dashboard
- Mobile app API

---

## 🤝 Contribution

This project is currently under active development. Feedback, issues, and suggestions are welcome.

### Development Guidelines

1. Follow Django best practices
2. Write tests for new features
3. Use meaningful commit messages
4. Update documentation for API changes

---

## 📄 License

This project is private and proprietary. All rights reserved.

---

## ✨ Author

Built by Jesse Adesina as part of Yiyara AI platform.
