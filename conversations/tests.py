import json
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from conversations.models import Conversation, Message
from goals.models import Goal, PlanChange, PlanRevision
from tasks.models import Task
from workflow.ai_engine import YiyaraWorkflow


User = get_user_model()


DECOMPOSITION = [
    {
        "title": "Launch portfolio",
        "description": "Publish three case studies.",
        "due_date": "2026-09-30",
        "tasks": [
            {
                "title": "Draft case studies",
                "description": "Write three case studies.",
                "due_date": "2026-09-15",
            }
        ],
    },
    {
        "title": "Find clients",
        "description": "Start outreach.",
        "due_date": "2026-10-15",
        "tasks": [
            {
                "title": "Build prospect list",
                "description": "Find twenty prospects.",
                "due_date": "2026-10-01",
            }
        ],
    },
]


class StubProvider:
    def __init__(self, decomposition=None, plan_response=None):
        self.decomposition = decomposition
        self.plan_response = plan_response

    def generate_structured_response(self, prompt):
        return json.dumps(self.decomposition)

    def generate_plan_response(self, prompt):
        return json.dumps(self.plan_response)


class PlanConversationApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="owner@example.com")
        self.other_user = User.objects.create_user(email="other@example.com")
        self.plan = YiyaraWorkflow(
            provider=StubProvider(decomposition=DECOMPOSITION)
        ).create_plan_from_ai(self.user, "Build a freelance business")
        self.goal = self.plan.goals.order_by("created_at").first()
        self.task = self.goal.tasks.first()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_board_uses_one_conversation_for_all_goals(self):
        response_payload = {
            "reply": "Both goals share one board.",
            "summary": "No board changes",
            "changes": [],
        }
        with patch(
            "conversations.services.GeminiProvider",
            return_value=StubProvider(plan_response=response_payload),
        ):
            response = self.client.post(
                f"/api/conversations/plans/{self.plan.id}/messages/",
                {
                    "content": "How do these goals connect?",
                    "scope_goal_id": str(self.plan.goals.last().id),
                    "plan_version": self.plan.version,
                    "client_id": str(uuid.uuid4()),
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Conversation.objects.filter(plan=self.plan).count(), 1)
        self.assertEqual(len(response.data["messages"]), 2)
        self.assertIsNone(response.data["change"])

    def test_change_is_proposed_then_applied(self):
        response_payload = {
            "reply": "I prepared a clearer task title.",
            "summary": "Clarify drafting task",
            "changes": [
                {
                    "action": "update_task",
                    "task_id": str(self.task.id),
                    "title": "Draft three polished case studies",
                }
            ],
        }
        with patch(
            "conversations.services.GeminiProvider",
            return_value=StubProvider(plan_response=response_payload),
        ):
            propose_response = self.client.post(
                f"/api/conversations/plans/{self.plan.id}/messages/",
                {
                    "content": "Make the drafting task more specific",
                    "scope_goal_id": str(self.goal.id),
                    "plan_version": self.plan.version,
                },
                format="json",
            )

        self.assertEqual(propose_response.status_code, 201)
        change = PlanChange.objects.get(plan=self.plan)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Draft case studies")
        self.assertEqual(change.status, PlanChange.Status.PROPOSED)

        apply_response = self.client.post(
            f"/api/conversations/plans/{self.plan.id}/changes/{change.id}/apply/"
        )

        self.assertEqual(apply_response.status_code, 200)
        self.task.refresh_from_db()
        self.plan.refresh_from_db()
        self.assertEqual(self.task.title, "Draft three polished case studies")
        self.assertEqual(self.plan.version, 2)
        self.assertTrue(
            PlanRevision.objects.filter(plan=self.plan, version=2).exists()
        )

    def test_stale_change_cannot_overwrite_newer_board(self):
        change = PlanChange.objects.create(
            plan=self.plan,
            scope_goal=self.goal,
            summary="Stale update",
            operations=[
                {
                    "action": "update_task",
                    "task_id": str(self.task.id),
                    "title": "Stale title",
                }
            ],
            base_version=1,
        )
        self.plan.version = 2
        self.plan.save(update_fields=["version"])

        response = self.client.post(
            f"/api/conversations/plans/{self.plan.id}/changes/{change.id}/apply/"
        )

        self.assertEqual(response.status_code, 409)
        self.task.refresh_from_db()
        self.assertNotEqual(self.task.title, "Stale title")

    def test_user_can_restore_earlier_board_revision(self):
        change = PlanChange.objects.create(
            plan=self.plan,
            scope_goal=self.goal,
            summary="Rename task",
            operations=[
                {
                    "action": "update_task",
                    "task_id": str(self.task.id),
                    "title": "Temporary task title",
                }
            ],
            base_version=1,
        )
        apply_response = self.client.post(
            f"/api/conversations/plans/{self.plan.id}/changes/{change.id}/apply/"
        )
        self.assertEqual(apply_response.status_code, 200)

        restore_response = self.client.post(
            f"/api/plans/{self.plan.id}/revisions/1/restore/"
        )

        self.assertEqual(restore_response.status_code, 200)
        self.task.refresh_from_db()
        self.plan.refresh_from_db()
        self.assertEqual(self.task.title, "Draft case studies")
        self.assertEqual(self.plan.version, 3)
        self.assertTrue(
            PlanRevision.objects.filter(plan=self.plan, version=3).exists()
        )

    def test_other_user_cannot_open_board_messages(self):
        self.client.force_authenticate(self.other_user)
        response = self.client.get(
            f"/api/conversations/plans/{self.plan.id}/messages/"
        )
        self.assertEqual(response.status_code, 404)
