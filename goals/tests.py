import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from conversations.models import Conversation, Message
from goals.models import Goal, Plan, PlanRevision
from tasks.models import Task
from ai.providers.gemini_provider import GeminiConfigurationError
from workflow.ai_engine import GoalDecompositionError, YiyaraWorkflow

User = get_user_model()


class StubProvider:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def generate_structured_response(self, prompt):
        if self.error:
            raise self.error
        return json.dumps(self.payload)


VALID_DECOMPOSITION = [
    {
        "title": "Launch portfolio",
        "description": "Publish a portfolio with three case studies.",
        "due_date": "2026-09-30",
        "tasks": [
            {
                "title": "Draft case studies",
                "description": "Write three concise case studies.",
                "due_date": "2026-09-15",
            },
            {
                "title": "Deploy site",
                "description": "Publish the final site.",
                "due_date": "2026-09-30",
            },
        ],
    }
]


class GoalWorkflowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="owner@example.com")

    def test_valid_ai_output_is_saved_atomically(self):
        workflow = YiyaraWorkflow(provider=StubProvider(VALID_DECOMPOSITION))

        plan = workflow.create_plan_from_ai(self.user, "Build my portfolio")

        self.assertEqual(plan.goals.count(), 1)
        goal = plan.goals.get()
        self.assertEqual(plan.raw_input, "Build my portfolio")
        self.assertEqual(goal.raw_input, "Build my portfolio")
        self.assertEqual(goal.tasks.count(), 2)
        self.assertEqual(
            Task.objects.get(title="Draft case studies").due_date.isoformat(),
            "2026-09-15",
        )
        self.assertEqual(
            Message.objects.get(conversation__plan=plan).role,
            Message.Role.ASSISTANT,
        )
        self.assertTrue(PlanRevision.objects.filter(plan=plan, version=1).exists())

    def test_invalid_ai_output_saves_nothing(self):
        invalid = [{**VALID_DECOMPOSITION[0], "tasks": []}]
        workflow = YiyaraWorkflow(provider=StubProvider(invalid))

        with self.assertRaises(GoalDecompositionError):
            workflow.create_plan_from_ai(self.user, "Build my portfolio")

        self.assertFalse(Plan.objects.exists())
        self.assertFalse(Goal.objects.exists())
        self.assertFalse(Task.objects.exists())

    def test_database_error_rolls_back_goal_and_tasks(self):
        workflow = YiyaraWorkflow(provider=StubProvider(VALID_DECOMPOSITION))

        with patch(
            "workflow.ai_engine.Task.objects.bulk_create",
            side_effect=RuntimeError("database write failed"),
        ):
            with self.assertRaises(RuntimeError):
                workflow.create_plan_from_ai(self.user, "Build my portfolio")

        self.assertFalse(Plan.objects.exists())
        self.assertFalse(Goal.objects.exists())
        self.assertFalse(Task.objects.exists())


class GoalApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="owner@example.com")
        self.other_user = User.objects.create_user(email="other@example.com")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_user_can_decompose_then_fetch_nested_tasks(self):
        with patch(
            "workflow.ai_engine.GeminiProvider",
            return_value=StubProvider(VALID_DECOMPOSITION),
        ):
            create_response = self.client.post(
                "/api/decompose/",
                {"text": "Build my portfolio"},
                format="json",
            )

        self.assertEqual(create_response.status_code, 201)
        self.assertEqual(len(create_response.data["goals"]), 1)
        self.assertEqual(len(create_response.data["goals"][0]["tasks"]), 2)
        self.assertIsNotNone(create_response.data["conversation_id"])

        private_plan = Plan.objects.create(
            user=self.other_user,
            title="Private plan",
            raw_input="Private plan",
        )
        Goal.objects.create(
            plan=private_plan,
            user=self.other_user,
            title="Private goal",
        )
        list_response = self.client.get("/api/list/")

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.data), 1)
        self.assertEqual(list_response.data[0]["title"], "Launch portfolio")
        self.assertEqual(len(list_response.data[0]["tasks"]), 2)

    def test_guest_can_preview_decomposition_without_persisting(self):
        self.client.force_authenticate(user=None)

        with patch(
            "workflow.ai_engine.GeminiProvider",
            return_value=StubProvider(VALID_DECOMPOSITION),
        ):
            response = self.client.post(
                "/api/decompose/preview/",
                {"text": "Build my portfolio"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["title"], "Build my portfolio")
        self.assertEqual(response.data["goals"][0]["title"], "Launch portfolio")
        self.assertEqual(len(response.data["goals"][0]["tasks"]), 2)
        self.assertNotIn("id", response.data["goals"][0])
        self.assertFalse(Plan.objects.exists())
        self.assertFalse(Goal.objects.exists())
        self.assertFalse(Task.objects.exists())
        self.assertFalse(Conversation.objects.exists())
        self.assertFalse(Message.objects.exists())

    def test_authenticated_user_can_import_guest_preview_as_one_board(self):
        response = self.client.post(
            "/api/plans/import-preview/",
            {
                "title": "Build my portfolio",
                "raw_input": "Build my portfolio",
                "goals": VALID_DECOMPOSITION,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data["goals"]), 1)
        plan = Plan.objects.get(user=self.user)
        self.assertEqual(plan.goals.count(), 1)
        self.assertTrue(Conversation.objects.filter(plan=plan).exists())
        self.assertTrue(PlanRevision.objects.filter(plan=plan, version=1).exists())

    def test_invalid_ai_output_returns_error_and_does_not_persist(self):
        with patch(
            "workflow.ai_engine.GeminiProvider",
            return_value=StubProvider({"title": "not a list"}),
        ):
            response = self.client.post(
                "/api/decompose/",
                {"text": "Build my portfolio"},
                format="json",
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.data["error"], "goal_decomposition_failed")
        self.assertFalse(Goal.objects.exists())

    def test_missing_ai_configuration_returns_service_unavailable(self):
        with patch(
            "workflow.ai_engine.GeminiProvider",
            side_effect=GeminiConfigurationError("GEMINI_API_KEY is not configured"),
        ):
            response = self.client.post(
                "/api/decompose/",
                {"text": "Build my portfolio"},
                format="json",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["error"], "ai_not_configured")
        self.assertFalse(Goal.objects.exists())

    def test_goal_endpoints_require_authentication(self):
        self.client.force_authenticate(user=None)

        create_response = self.client.post(
            "/api/decompose/",
            {"text": "Build my portfolio"},
            format="json",
        )
        list_response = self.client.get("/api/list/")
        plans_response = self.client.get("/api/plans/")
        import_response = self.client.post(
            "/api/plans/import-preview/",
            {
                "title": "Build my portfolio",
                "raw_input": "Build my portfolio",
                "goals": VALID_DECOMPOSITION,
            },
            format="json",
        )

        self.assertEqual(create_response.status_code, 401)
        self.assertEqual(list_response.status_code, 401)
        self.assertEqual(plans_response.status_code, 401)
        self.assertEqual(import_response.status_code, 401)
