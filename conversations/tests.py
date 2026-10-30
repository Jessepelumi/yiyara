import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from ai.providers.base import AIUnavailableError
from conversations.models import Conversation, Message
from goals.models import Goal, Plan, PlanChange, PlanRevision
from tasks.models import Task


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


class StubGateway:
    def __init__(self, plan_response=None, error=None):
        self.plan_response = plan_response
        self.error = error

    def generate_json(self, *, task, **kwargs):
        if self.error:
            raise self.error
        payload = self.plan_response
        validator = kwargs.get("validator")
        return validator(payload) if validator else payload


class PlanConversationApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="owner@example.com")
        self.other_user = User.objects.create_user(email="other@example.com")
        # Stored legacy boards remain readable and editable after route retirement.
        self.plan = Plan.objects.create(user=self.user, title='Freelance business', raw_input='Build a freelance business')
        for item in DECOMPOSITION:
            goal = Goal.objects.create(plan=self.plan, user=self.user,
                                      title=item['title'], description=item['description'],
                                      due_date=item['due_date'], raw_input=self.plan.raw_input)
            for task in item['tasks']:
                Task.objects.create(goal=goal, **task)
        conversation = Conversation.objects.create(plan=self.plan, user=self.user)
        Message.objects.create(conversation=conversation, role='assistant', content='Saved legacy board.')
        from goals.services import create_plan_revision
        create_plan_revision(self.plan, 'Initial legacy board')
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
            "conversations.services.get_ai_gateway",
            return_value=StubGateway(plan_response=response_payload),
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
            "conversations.services.get_ai_gateway",
            return_value=StubGateway(plan_response=response_payload),
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

    def test_failed_idempotent_message_can_be_retried(self):
        client_id = uuid.uuid4()
        url = f"/api/conversations/plans/{self.plan.id}/messages/"
        request_data = {
            "content": "Help me connect these goals",
            "plan_version": self.plan.version,
            "client_id": str(client_id),
        }

        with patch(
            "conversations.services.get_ai_gateway",
            return_value=StubGateway(
                error=AIUnavailableError("all providers unavailable")
            ),
        ):
            failed_response = self.client.post(url, request_data, format="json")

        self.assertEqual(failed_response.status_code, 502)
        user_message = Message.objects.get(
            conversation__plan=self.plan,
            role=Message.Role.USER,
            client_id=client_id,
        )
        self.assertEqual(user_message.metadata["ai_status"], "failed")

        successful_payload = {
            "reply": "The portfolio supports your client outreach.",
            "summary": "No board changes",
            "changes": [],
        }
        with patch(
            "conversations.services.get_ai_gateway",
            return_value=StubGateway(plan_response=successful_payload),
        ):
            retry_response = self.client.post(url, request_data, format="json")

        self.assertEqual(retry_response.status_code, 201)
        self.assertEqual(
            Message.objects.filter(
                conversation__plan=self.plan,
                role=Message.Role.USER,
                client_id=client_id,
            ).count(),
            1,
        )
        user_message.refresh_from_db()
        self.assertEqual(user_message.metadata["ai_status"], "completed")
        self.assertTrue(
            Message.objects.filter(
                conversation__plan=self.plan,
                role=Message.Role.ASSISTANT,
                metadata__request_client_id=str(client_id),
            ).exists()
        )


class AIRequestRecoveryTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        from goals.planning import create_ambition
        cache.clear()
        self.user = User.objects.create_user(email='ai-recovery@example.com')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.plan = create_ambition(self.user, 'Learn arrays')
        self.url = f'/api/conversations/plans/{self.plan.pk}/messages/'
        self.data = {'content': 'I already know Python loops.', 'client_id': str(uuid.uuid4()), 'plan_version': self.plan.version}

    def pending(self, **fields):
        from django.utils import timezone
        from datetime import timedelta
        return Message.objects.create(conversation=self.plan.conversation, role='user', content=self.data['content'],
            client_id=self.data['client_id'], metadata={'ai_status': 'pending'},
            ai_attempt=uuid.uuid4(), ai_lease_until=timezone.now() - timedelta(seconds=1), **fields)

    def gateway(self):
        from unittest.mock import Mock
        gateway = Mock()
        gateway.generate_json.return_value = {'reply': 'Please confirm your desired outcome.', 'summary': 'Clarify', 'changes': []}
        return gateway

    def test_expired_request_retries_same_message_and_replays_once(self):
        message = self.pending()
        gateway = self.gateway()
        with patch('conversations.clarification.get_ai_gateway', return_value=gateway):
            result = self.client.post(self.url, self.data, format='json')
            self.assertEqual(result.status_code, 201, result.data)
            replay = self.client.post(self.url, self.data, format='json')
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(gateway.generate_json.call_count, 1)
        self.assertEqual(Message.objects.filter(client_id=self.data['client_id']).count(), 1)
        message.refresh_from_db()
        self.assertEqual(message.metadata['ai_status'], 'completed')
        self.assertIsNone(message.ai_lease_until)

    def test_live_request_returns_409_instead_of_an_empty_success(self):
        from django.utils import timezone
        from datetime import timedelta
        message = self.pending()
        message.ai_lease_until = timezone.now() + timedelta(minutes=1)
        message.save()
        with patch('conversations.clarification.get_ai_gateway') as gateway:
            result = self.client.post(self.url, self.data, format='json')
        self.assertEqual(result.status_code, 409)
        gateway.assert_not_called()

    def test_pre_migration_pending_message_can_recover_after_lease_duration(self):
        from django.utils import timezone
        from datetime import timedelta
        message = self.pending()
        Message.objects.filter(pk=message.pk).update(ai_attempt=None, ai_lease_until=None, created_at=timezone.now() - timedelta(days=1))
        with patch('conversations.clarification.get_ai_gateway', return_value=self.gateway()):
            self.assertEqual(self.client.post(self.url, self.data, format='json').status_code, 201)

    def test_superseded_attempt_cannot_save_output_or_mark_new_attempt_failed(self):
        from django.utils import timezone
        from datetime import timedelta
        message = self.pending()
        new_attempt = uuid.uuid4()
        gateway = self.gateway()
        def supersede(**kwargs):
            Message.objects.filter(pk=message.pk).update(ai_attempt=new_attempt,
                ai_lease_until=timezone.now() + timedelta(minutes=1), metadata={'ai_status': 'pending', 'new_attempt': True})
            return {'reply': 'Old response', 'summary': 'Old attempt', 'changes': []}
        gateway.generate_json.side_effect = supersede
        with patch('conversations.clarification.get_ai_gateway', return_value=gateway):
            result = self.client.post(self.url, self.data, format='json')
        self.assertEqual(result.status_code, 409, result.data)
        message.refresh_from_db()
        self.assertEqual(message.ai_attempt, new_attempt)
        self.assertTrue(message.metadata['new_attempt'])
        self.assertEqual(message.metadata['ai_status'], 'pending')
        self.assertFalse(self.plan.conversation.messages.filter(role='assistant').exists())

    def test_replayed_change_uses_current_status(self):
        import json
        from unittest.mock import Mock
        gateway = Mock()
        def generate(**kwargs):
            source = json.loads(kwargs['prompt'])['current_user_message_id']
            return {'reply': 'Review starting point', 'summary': 'Starting point', 'changes': [
                {'action': 'update_context', 'starting_point': self.data['content'], 'source_message_id': source}]}
        gateway.generate_json.side_effect = generate
        with patch('conversations.clarification.get_ai_gateway', return_value=gateway):
            result = self.client.post(self.url, self.data, format='json')
            self.assertEqual(result.status_code, 201, result.data)
            applied = self.client.post(f"/api/conversations/plans/{self.plan.pk}/changes/{result.data['change']['id']}/apply/")
            self.assertEqual(applied.status_code, 200)
            replay = self.client.post(self.url, self.data, format='json')
        self.assertEqual(replay.data['change']['status'], 'applied')

    def test_legacy_message_processing_also_recovers_expired_requests(self):
        from goals.models import Plan
        self.plan.planning_mode = 'legacy'
        self.plan.save()
        message = self.pending()
        gateway = StubGateway(plan_response={'reply': 'Review the board.', 'summary': 'Advice', 'changes': []})
        with patch('conversations.services.get_ai_gateway', return_value=gateway):
            result = self.client.post(self.url, self.data, format='json')
        self.assertEqual(result.status_code, 201, result.data)
        message.refresh_from_db()
        self.assertEqual(message.metadata['ai_status'], 'completed')
