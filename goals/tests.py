from unittest.mock import Mock, patch

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from conversations.models import Conversation, Message
from goals.models import Goal, Plan, PlanRevision
from tasks.models import Task
from ai.gateway import AIGateway, AITask, RetryPolicy
from ai.providers.base import (
    AIConfigurationError,
    AIRateLimitError,
    AIRetryableProviderError,
    AIUnavailableError,
    GenerationRequest,
    GenerationResult,
)
from ai.providers.gemini_provider import GeminiProvider
from ai.providers.openai_compatible_provider import OpenAICompatibleProvider
from workflow.ai_engine import GoalDecompositionError, YiyaraWorkflow

User = get_user_model()


class StubGateway:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def generate_json(self, **kwargs):
        if self.error:
            raise self.error
        validator = kwargs.get("validator")
        return validator(self.payload) if validator else self.payload


class SequencedProvider:
    model = "test-model"

    def __init__(self, name, outcomes):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return GenerationResult(
            content=outcome,
            provider=self.name,
            model=self.model,
            latency_ms=1,
        )


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
        workflow = YiyaraWorkflow(gateway=StubGateway(VALID_DECOMPOSITION))

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
        workflow = YiyaraWorkflow(gateway=StubGateway(invalid))

        with self.assertRaises(GoalDecompositionError):
            workflow.create_plan_from_ai(self.user, "Build my portfolio")

        self.assertFalse(Plan.objects.exists())
        self.assertFalse(Goal.objects.exists())
        self.assertFalse(Task.objects.exists())

    def test_database_error_rolls_back_goal_and_tasks(self):
        workflow = YiyaraWorkflow(gateway=StubGateway(VALID_DECOMPOSITION))

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
            "workflow.ai_engine.get_ai_gateway",
            return_value=StubGateway(VALID_DECOMPOSITION),
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
            "workflow.ai_engine.get_ai_gateway",
            return_value=StubGateway(VALID_DECOMPOSITION),
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
            "workflow.ai_engine.get_ai_gateway",
            return_value=StubGateway({"title": "not a list"}),
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
            "workflow.ai_engine.get_ai_gateway",
            side_effect=AIConfigurationError("No AI provider is configured"),
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


class AIGatewayTests(TestCase):
    def make_gateway(self, providers, route=("primary", "backup"), **policy):
        return AIGateway(
            providers=providers,
            routes={AITask.GOAL_DECOMPOSITION.value: route},
            retry_policy=RetryPolicy(
                max_attempts=policy.get("max_attempts", 2),
                min_delay_seconds=0,
                max_delay_seconds=policy.get("max_delay_seconds", 1),
                total_timeout_seconds=10,
            ),
            sleep=lambda _: None,
            random_uniform=lambda _start, _end: 0,
        )

    def generate(self, gateway):
        return gateway.generate_json(
            task=AITask.GOAL_DECOMPOSITION,
            prompt="test",
            system_prompt="test",
            response_schema={"type": "object"},
            schema_name="test",
        )

    def test_retries_transient_failure_before_succeeding(self):
        primary = SequencedProvider(
            "primary",
            [AIRetryableProviderError("primary", "temporary"), '{"ok": true}'],
        )
        gateway = self.make_gateway({"primary": primary}, route=("primary",))

        self.assertEqual(self.generate(gateway), {"ok": True})
        self.assertEqual(primary.calls, 2)

    def test_long_rate_limit_falls_back_without_repeated_attempts(self):
        primary = SequencedProvider(
            "primary",
            [AIRateLimitError("primary", "limited", retry_after=60)],
        )
        backup = SequencedProvider("backup", ['{"provider": "backup"}'])
        gateway = self.make_gateway({"primary": primary, "backup": backup})

        self.assertEqual(self.generate(gateway), {"provider": "backup"})
        self.assertEqual(primary.calls, 1)
        self.assertEqual(backup.calls, 1)

    def test_malformed_json_retries_then_reports_unavailable(self):
        primary = SequencedProvider("primary", ["not-json", "still-not-json"])
        gateway = self.make_gateway({"primary": primary}, route=("primary",))

        with self.assertRaises(AIUnavailableError):
            self.generate(gateway)
        self.assertEqual(primary.calls, 2)

    def test_semantically_invalid_output_falls_back(self):
        primary = SequencedProvider(
            "primary",
            ['{"ok": false}', '{"ok": false}'],
        )
        backup = SequencedProvider("backup", ['{"ok": true}'])
        gateway = self.make_gateway({"primary": primary, "backup": backup})

        def require_ok(payload):
            if payload.get("ok") is not True:
                raise ValueError("not acceptable")
            return payload

        result = gateway.generate_json(
            task=AITask.GOAL_DECOMPOSITION,
            prompt="test",
            system_prompt="test",
            response_schema={"type": "object"},
            schema_name="test",
            validator=require_ok,
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(primary.calls, 2)
        self.assertEqual(backup.calls, 1)


class OpenAICompatibleProviderTests(TestCase):
    def test_sends_schema_and_normalizes_response(self):
        provider = OpenAICompatibleProvider(
            name="groq",
            api_key="test-key",
            base_url="https://api.example.test/v1",
            model="test-model",
        )
        provider.client = Mock()
        provider.client.post.return_value = httpx.Response(
            200,
            request=httpx.Request(
                "POST", "https://api.example.test/v1/chat/completions"
            ),
            json={
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"total_tokens": 12},
            },
        )

        result = provider.generate(
            GenerationRequest(
                prompt="prompt",
                system_prompt="system",
                response_schema={"type": "object"},
                schema_name="test_schema",
                timeout_seconds=3,
            )
        )

        self.assertEqual(result.content, '{"ok": true}')
        self.assertEqual(result.provider, "groq")
        self.assertEqual(result.usage, {"total_tokens": 12})
        request_payload = provider.client.post.call_args.kwargs["json"]
        self.assertEqual(
            request_payload["response_format"]["json_schema"]["schema"],
            {"type": "object"},
        )
        self.assertEqual(
            request_payload["messages"],
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "prompt"},
            ],
        )


class GeminiProviderTests(TestCase):
    @patch("ai.providers.gemini_provider.genai.Client")
    def test_uses_remaining_gateway_timeout(self, client_class):
        default_client = Mock()
        deadline_client = Mock()
        deadline_client.models.generate_content.return_value = Mock(
            text='{"ok": true}',
            usage_metadata=None,
        )
        client_class.side_effect = [default_client, deadline_client]
        provider = GeminiProvider(
            api_key="test-key",
            model="test-model",
            timeout_seconds=30,
        )

        result = provider.generate(
            GenerationRequest(prompt="prompt", timeout_seconds=5)
        )

        self.assertEqual(result.content, '{"ok": true}')
        self.assertEqual(client_class.call_count, 2)
        deadline_http_options = client_class.call_args.kwargs["http_options"]
        self.assertEqual(deadline_http_options.timeout, 5000)
        deadline_client.close.assert_called_once_with()


class ContextualPlanningTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(email='clarification@example.com')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        response = self.client.post('/api/plans/', {'text': 'Learn to drive'}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.plan = Plan.objects.get(pk=response.data['id'])
        self.question = self.plan.questions.get()
        self.chat_url = f'/api/conversations/plans/{self.plan.pk}/messages/'

    def chat(self, content, operations, client_id=None, version=None):
        import json
        import uuid

        gateway = Mock()
        def generate(**kwargs):
            current = json.loads(kwargs['prompt'])
            source = current['current_user_message_id']
            changes = operations(source) if callable(operations) else operations
            return {'reply': 'Please review these proposed context updates.', 'summary': 'Clarification', 'changes': changes}
        gateway.generate_json.side_effect = generate
        data = {'content': content, 'client_id': str(client_id or uuid.uuid4())}
        if version is not None:
            data['plan_version'] = version
        with patch('conversations.clarification.get_ai_gateway', return_value=gateway):
            response = self.client.post(self.chat_url, data, format='json')
        return response, gateway, data

    def apply(self, response):
        return self.client.post(f"/api/conversations/plans/{self.plan.pk}/changes/{response.data['change']['id']}/apply/")

    def test_intake_is_saved_without_ai_and_without_goals(self):
        with patch('workflow.ai_engine.get_ai_gateway', side_effect=AssertionError('Must not call AI')):
            response = self.client.post('/api/plans/', {'text': 'Learn DSA'}, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['planning_state'], 'clarifying')
        self.assertEqual(response.data['planning_mode'], 'contextual')
        self.assertEqual(response.data['goals'], [])
        self.assertEqual(len(response.data['questions']), 1)
        self.assertEqual(self.plan.goals.count(), 0)
        self.assertTrue(self.plan.revisions.filter(version=1).exists())
        self.assertEqual(self.client.get(f'/api/plans/{self.plan.pk}/').data['context']['desired_outcome'], 'Learn to drive')

    def test_answer_persists_without_ai_and_replay_is_idempotent(self):
        url = f'/api/plans/{self.plan.pk}/questions/{self.question.pk}/answer/'
        data = {'answer': 'I have not chosen a driving school.', 'plan_version': 1}
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['context']['starting_point'], data['answer'])
        self.assertEqual(response.data['questions'][0]['status'], 'answered')
        self.assertEqual(response.data['version'], 2)
        replay = self.client.post(url, data, format='json')
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.data['version'], 2)
        self.assertEqual(self.client.post(url, {**data, 'answer': 'Different answer'}, format='json').status_code, 409)

    def test_reviewed_ai_changes_save_answers_and_follow_up_questions(self):
        content = 'I have never driven and have not found a school.'
        response, _, _ = self.chat(content, lambda source: [
            {'action': 'answer_question', 'question_id': str(self.question.pk), 'answer': content, 'source_message_id': source},
            {'action': 'ask_question', 'field': 'timing', 'text': 'When would you like to start lessons?', 'required': False},
        ])
        self.assertEqual(response.status_code, 201, response.data)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.context['starting_point'], '')
        self.assertEqual(self.plan.questions.count(), 1)
        applied = self.apply(response)
        self.assertEqual(applied.status_code, 200, applied.data)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.context['starting_point'], content)
        self.assertEqual(self.plan.questions.count(), 2)
        self.assertEqual(self.plan.goals.count(), 0)

    def test_unverified_facts_are_rejected_and_message_survives(self):
        response, _, _ = self.chat('I want to start in June.', lambda source: [
            {'action': 'update_context', 'timing': '2027-06-01', 'source_message_id': source},
        ])
        self.assertEqual(response.status_code, 502)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.context['timing'], '')
        self.assertFalse(self.plan.changes.exists())
        self.assertEqual(self.plan.conversation.messages.latest('created_at').metadata['ai_status'], 'failed')

    def test_assumptions_remain_distinct_and_context_accumulates(self):
        first = 'I can study on weekdays.'
        response, _, _ = self.chat(first, lambda source: [
            {'action': 'update_context', 'constraints': [first], 'source_message_id': source,
             'assumptions': ['Weekends may be unavailable.']},
        ])
        self.assertEqual(self.apply(response).status_code, 200)
        second = 'My budget is limited.'
        response, _, _ = self.chat(second, lambda source: [
            {'action': 'update_context', 'constraints': [second], 'source_message_id': source},
        ])
        self.assertEqual(self.apply(response).status_code, 200)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.context['constraints'], [first, second])
        self.assertEqual(self.plan.context['assumptions'], ['Weekends may be unavailable.'])
        self.assertEqual(self.plan.context_provenance['assumptions']['kind'], 'assumption')

    def test_context_corrections_and_waiting_state_validation(self):
        url = f'/api/plans/{self.plan.pk}/context/'
        self.assertEqual(self.client.patch(url, {'plan_version': 1, 'planning_state': 'waiting'}, format='json').status_code, 400)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.version, 1)
        response = self.client.patch(url, {'plan_version': 1, 'starting_point': 'I already have an instructor.'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['context']['starting_point'], 'I already have an instructor.')
        self.assertEqual(self.client.patch(url, {'plan_version': 1, 'timing': 'Tomorrow'}, format='json').status_code, 409)

    def test_deferred_question_waits_without_fabricating_answer(self):
        response, _, _ = self.chat('I am waiting to hear back.', [
            {'action': 'defer_question', 'question_id': str(self.question.pk), 'reason': 'Waiting for a booking response.'},
            {'action': 'update_context', 'planning_state': 'waiting', 'waiting_reason': 'Need school booking details.'},
        ])
        self.assertEqual(self.apply(response).status_code, 200)
        self.plan.refresh_from_db()
        self.question.refresh_from_db()
        self.assertEqual(self.plan.planning_state, 'waiting')
        self.assertEqual(self.question.status, 'deferred')
        self.assertEqual(self.question.answer, '')

    def test_required_questions_prevent_actionable_state_and_roll_back(self):
        response, _, _ = self.chat('Can we proceed?', [{'action': 'update_context', 'planning_state': 'actionable'}])
        self.assertEqual(self.apply(response).status_code, 400)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.version, 1)
        self.assertEqual(self.plan.planning_state, 'clarifying')

    def test_goal_less_revision_restores_context_and_questions(self):
        url = f'/api/plans/{self.plan.pk}/questions/{self.question.pk}/answer/'
        self.client.post(url, {'answer': 'No school yet.', 'plan_version': 1}, format='json')
        response = self.client.post(f'/api/plans/{self.plan.pk}/revisions/1/restore/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['context']['starting_point'], '')
        self.assertEqual(response.data['questions'][0]['status'], 'open')
        self.assertEqual(response.data['goals'], [])
        self.assertEqual(response.data['version'], 3)

    def test_durable_context_in_prompt_outlives_recent_messages(self):
        import json

        self.client.patch(f'/api/plans/{self.plan.pk}/context/', {
            'plan_version': 1, 'constraints': ['Only evenings'],
        }, format='json')
        for index in range(15):
            Message.objects.create(conversation=self.plan.conversation, role='assistant', content=f'Later discussion {index}')
        response, gateway, _ = self.chat('What do you remember?', [])
        self.assertEqual(response.status_code, 201)
        prompt = json.loads(gateway.generate_json.call_args.kwargs['prompt'])
        self.assertEqual(prompt['persistent_plan']['context']['constraints'], ['Only evenings'])
        self.assertLessEqual(len(prompt['recent_messages']), 10)
        self.assertNotIn('Only evenings', str(prompt['recent_messages']))

    def test_idempotent_message_replay_and_content_conflict(self):
        import uuid

        response, _, data = self.chat('I have no school yet.', [], client_id=uuid.uuid4(), version=1)
        self.assertEqual(response.status_code, 201)
        count = self.plan.conversation.messages.count()
        self.client.patch(f'/api/plans/{self.plan.pk}/context/', {'plan_version': 1, 'timing': 'Later'}, format='json')
        with patch('conversations.clarification.get_ai_gateway', side_effect=AssertionError('Must not repeat AI')):
            replay = self.client.post(self.chat_url, data, format='json')
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(self.plan.conversation.messages.count(), count + 1)
        response = self.client.post(self.chat_url, {**data, 'content': 'Different content'}, format='json')
        self.assertEqual(response.status_code, 409)

    def test_model_failure_retry_keeps_ambition_and_user_message(self):
        import uuid

        data = {'content': 'No school yet.', 'client_id': str(uuid.uuid4())}
        with patch('conversations.clarification.get_ai_gateway', side_effect=AIConfigurationError('Unavailable')):
            self.assertEqual(self.client.post(self.chat_url, data, format='json').status_code, 503)
        count = self.plan.conversation.messages.count()
        response, _, _ = self.chat(data['content'], [], client_id=data['client_id'])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.plan.conversation.messages.count(), count + 1)
        self.assertTrue(Plan.objects.filter(pk=self.plan.pk).exists())

    def test_old_state_and_cross_plan_evidence_cannot_be_applied(self):
        response, _, _ = self.chat('I have not started.', lambda source: [
            {'action': 'update_context', 'starting_point': 'I have not started.', 'source_message_id': source},
        ])
        self.client.patch(f'/api/plans/{self.plan.pk}/context/', {'plan_version': 1, 'timing': 'Later'}, format='json')
        self.assertEqual(self.apply(response).status_code, 409)
        from goals.planning import normalize_context_operation
        from rest_framework.exceptions import ValidationError

        other_plan = Plan.objects.create(user=self.user, title='Other', raw_input='Other')
        other_chat = Conversation.objects.create(plan=other_plan, user=self.user)
        source = Message.objects.create(conversation=other_chat, role='user', content='I have not started.')
        self.plan.refresh_from_db()
        with self.assertRaises(ValidationError):
            normalize_context_operation(self.plan, {'action': 'update_context', 'starting_point': source.content,
                                                   'source_message_id': str(source.pk)})

    def test_no_tasks_or_excess_questions_can_be_generated_in_stage_one(self):
        for changes in ([{'action': 'add_goal', 'title': 'Invented work'}],
                        [{'action': 'ask_question', 'field': 'other', 'text': str(n)} for n in range(3)]):
            response, _, _ = self.chat('Tell me more.', changes)
            self.assertEqual(response.status_code, 502)
        self.assertFalse(self.plan.goals.exists())

    def test_ownership_and_authentication(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post('/api/plans/', {'text': 'Ambition'}, format='json').status_code, 401)
        self.client.force_authenticate(User.objects.create_user(email='outsider-context@example.com'))
        self.assertEqual(self.client.get(f'/api/plans/{self.plan.pk}/').status_code, 404)
        self.assertEqual(self.client.patch(f'/api/plans/{self.plan.pk}/context/', {'plan_version': 1, 'timing': 'Later'}, format='json').status_code, 404)
        self.assertEqual(self.client.post(f'/api/plans/{self.plan.pk}/questions/{self.question.pk}/answer/', {
            'plan_version': 1, 'answer': 'Different answer',
        }, format='json').status_code, 404)
        self.assertEqual(self.client.post(self.chat_url, {'content': 'Read private context'}, format='json').status_code, 404)


class ActionableTaskPlanningTests(TestCase):
    def setUp(self):
        import uuid
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(email='planner@example.com')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        response = self.client.post('/api/plans/', {'text': 'Learn DSA'}, format='json')
        self.plan = Plan.objects.get(pk=response.data['id'])
        question = self.plan.questions.get()
        response = self.client.post(f'/api/plans/{self.plan.pk}/questions/{question.pk}/answer/', {
            'plan_version': self.plan.version, 'answer': 'I know Python but have never studied arrays.'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.plan.refresh_from_db()
        self.url = f'/api/plans/{self.plan.pk}/tasks/propose/'
        self.request = {'plan_version': self.plan.version, 'client_id': str(uuid.uuid4())}
        self.payload = {'reply': 'Review these two sessions.', 'summary': 'Begin arrays', 'goals': [{
            'title': 'Understand arrays', 'description': 'Practise indexing and traversal.', 'tasks': [
                {'key': 'read', 'title': 'Trace array indexing in Python', 'description': 'Create a five-element list; print its first and last elements.',
                 'completion_criteria': 'A script prints both values and includes a note explaining zero-based indexing.',
                 'estimated_minutes': 30, 'readiness': 'ready', 'blocker_reason': '', 'depends_on': []},
                {'key': 'practise', 'title': 'Write a list traversal loop', 'description': 'Use the list from the previous session to print every item.',
                 'completion_criteria': 'A loop prints all five items in order.',
                 'estimated_minutes': 30, 'readiness': 'ready', 'blocker_reason': '', 'depends_on': ['read']},
            ]}]}

    def propose(self, payload=None, data=None):
        with patch('goals.task_planning.get_ai_gateway', return_value=StubGateway(payload if payload is not None else self.payload)) as gateway:
            response = self.client.post(self.url, data or self.request, format='json')
        return response, gateway

    def apply(self, response):
        return self.client.post(f"/api/conversations/plans/{self.plan.pk}/changes/{response.data['change']['id']}/apply/")

    def test_review_apply_dependencies_completion_and_replay(self):
        response, _ = self.propose()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(Task.objects.exists())
        self.assertFalse(self.plan.goals.exists())
        applied = self.apply(response)
        self.assertEqual(applied.status_code, 200, applied.data)
        self.assertEqual(Task.objects.count(), 2)
        first = Task.objects.get(title='Trace array indexing in Python')
        second = Task.objects.get(title='Write a list traversal loop')
        detail = self.client.get(f'/api/tasks/{second.pk}/').data
        self.assertFalse(detail['is_schedulable'])
        self.assertEqual([str(pk) for pk in detail['dependencies']], [str(first.pk)])
        self.assertTrue(self.client.get(f'/api/tasks/{first.pk}/').data['is_schedulable'])
        result = self.client.patch(f'/api/tasks/{first.pk}/', {'is_completed': True}, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        self.assertTrue(self.client.get(f'/api/tasks/{second.pk}/').data['is_schedulable'])
        replay, gateway = self.propose()
        self.assertEqual(replay.status_code, 200, replay.data)
        gateway.assert_not_called()
        self.assertEqual(replay.data['change']['status'], 'applied')
        self.assertEqual(Task.objects.count(), 2)

    def test_context_change_requires_explicit_task_review(self):
        response, _ = self.propose()
        self.assertEqual(self.apply(response).status_code, 200)
        self.plan.refresh_from_db()
        original_context_version = self.plan.context_version
        response = self.client.patch(f'/api/plans/{self.plan.pk}/context/', {
            'plan_version': self.plan.version, 'constraints': ['Only twenty minutes available per day.']}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        task = Task.objects.get(title='Trace array indexing in Python')
        self.assertFalse(self.client.get(f'/api/tasks/{task.pk}/').data['is_schedulable'])
        response = self.client.patch(f'/api/tasks/{task.pk}/', {'readiness': 'ready', 'estimated_minutes': 20}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['is_schedulable'])
        self.assertEqual(response.data['planning_context_version'], original_context_version + 1)

    def test_invalid_graphs_dates_and_invented_resources_save_no_tasks(self):
        import copy
        import uuid

        for mutation in ['cycle', 'self', 'unknown', 'foreign', 'date', 'url']:
            payload = copy.deepcopy(self.payload)
            tasks = payload['goals'][0]['tasks']
            if mutation == 'cycle':
                tasks[0]['depends_on'] = ['practise']
            elif mutation == 'self':
                tasks[0]['depends_on'] = ['read']
            elif mutation == 'unknown':
                tasks[0]['depends_on'] = ['missing']
            elif mutation == 'foreign':
                tasks[0]['depends_on'] = [str(uuid.uuid4())]
            elif mutation == 'date':
                tasks[0]['due_date'] = '2026-12-31'
            else:
                tasks[0]['description'] = 'Use https://invented.example/course'
            with self.subTest(mutation=mutation):
                response, _ = self.propose(payload, {**self.request, 'client_id': str(uuid.uuid4())})
                self.assertEqual(response.status_code, 502, response.data)
                self.assertFalse(self.plan.changes.exists())
                self.assertFalse(Task.objects.exists())

    def test_external_blocker_is_retained_and_prevents_scheduling(self):
        self.payload['goals'][0]['tasks'] = [self.payload['goals'][0]['tasks'][0]]
        task = self.payload['goals'][0]['tasks'][0]
        task.update(readiness='blocked', estimated_minutes=None, blocker_reason='Awaiting course access.')
        response, _ = self.propose()
        self.assertEqual(response.status_code, 201, response.data)
        applied = self.apply(response)
        self.assertEqual(applied.status_code, 200, applied.data)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.planning_state, 'waiting')
        detail = self.client.get(f'/api/tasks/{Task.objects.get().pk}/').data
        self.assertFalse(detail['is_schedulable'])
        self.assertEqual(detail['blocker_reason'], 'Awaiting course access.')

    def test_missing_context_does_not_call_ai(self):
        self.plan.context['starting_point'] = ''
        self.plan.save()
        response, gateway = self.propose()
        self.assertEqual(response.status_code, 400, response.data)
        gateway.assert_not_called()

    def test_empty_result_and_failed_request_retries_are_idempotent(self):
        response, _ = self.propose({'reply': 'Which course can you access?', 'summary': 'More context needed', 'goals': []})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data['change'])
        replay, gateway = self.propose()
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.data['message_id'], response.data['message_id'])
        gateway.assert_not_called()
        self.assertFalse(Task.objects.exists())

    def test_failed_request_can_retry_and_client_id_cannot_change_focus(self):
        with patch('goals.task_planning.get_ai_gateway', return_value=StubGateway(error=RuntimeError('provider unavailable'))):
            response = self.client.post(self.url, self.request, format='json')
        self.assertEqual(response.status_code, 502, response.data)
        response, _ = self.propose()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Message.objects.filter(client_id=self.request['client_id']).count(), 1)
        conflict, gateway = self.propose(data={**self.request, 'focus': 'Something different'})
        self.assertEqual(conflict.status_code, 409)
        gateway.assert_not_called()

    def test_stale_proposal_cannot_be_applied(self):
        response, _ = self.propose()
        edit = self.client.patch(f'/api/plans/{self.plan.pk}/context/', {
            'plan_version': self.plan.version, 'timing': 'Next month'}, format='json')
        self.assertEqual(edit.status_code, 200, edit.data)
        self.assertEqual(self.apply(response).status_code, 409)
        self.assertFalse(Task.objects.exists())

    def test_context_change_during_generation_discards_result(self):
        def generate(**kwargs):
            Plan.objects.filter(pk=self.plan.pk).update(version=self.plan.version + 1)
            return self.payload
        gateway = Mock()
        gateway.generate_json.side_effect = generate
        with patch('goals.task_planning.get_ai_gateway', return_value=gateway):
            response = self.client.post(self.url, self.request, format='json')
        self.assertEqual(response.status_code, 409, response.data)
        self.assertFalse(self.plan.changes.exists())

    def test_owner_only_and_authentication_required(self):
        other = User.objects.create_user(email='another-planner@example.com')
        self.client.force_authenticate(other)
        self.assertEqual(self.client.post(self.url, self.request, format='json').status_code, 404)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post(self.url, self.request, format='json').status_code, 401)

    def test_restore_preserves_dependencies_and_requires_context_review(self):
        response, _ = self.propose()
        self.assertEqual(self.apply(response).status_code, 200)
        self.plan.refresh_from_db()
        saved_version = self.plan.version
        first = Task.objects.get(title='Trace array indexing in Python')
        second = Task.objects.get(title='Write a list traversal loop')
        self.client.patch(f'/api/tasks/{second.pk}/', {'dependency_ids': []}, format='json')
        response = self.client.post(f'/api/plans/{self.plan.pk}/revisions/{saved_version}/restore/')
        self.assertEqual(response.status_code, 200, response.data)
        second.refresh_from_db()
        self.assertTrue(second.dependencies.filter(pk=first.pk).exists())
        self.assertFalse(self.client.get(f'/api/tasks/{first.pk}/').data['is_schedulable'])

    def test_followup_reuses_goal_and_rejects_duplicate_unfinished_work(self):
        import uuid

        response, _ = self.propose()
        self.assertEqual(self.apply(response).status_code, 200)
        self.plan.refresh_from_db()
        goal = self.plan.goals.get()
        self.payload['goals'][0]['goal_id'] = str(goal.pk)
        duplicate, _ = self.propose(data={'plan_version': self.plan.version, 'client_id': str(uuid.uuid4())})
        self.assertEqual(duplicate.status_code, 502, duplicate.data)
        task = self.payload['goals'][0]['tasks'][0]
        task.update(key='search', title='Implement linear search',
                    completion_criteria='A function returns the index of an existing element and handles a missing element.',
                    depends_on=[str(Task.objects.get(title='Write a list traversal loop').pk)])
        self.payload['goals'][0]['tasks'] = [task]
        response, _ = self.propose(data={'plan_version': self.plan.version, 'client_id': str(uuid.uuid4())})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.apply(response).status_code, 200)
        self.assertEqual(self.plan.goals.count(), 1)
        self.assertEqual(goal.tasks.count(), 3)
        self.assertTrue(Task.objects.get(title='Implement linear search').dependencies.exists())

    def test_waiting_plan_can_propose_independent_work_without_erasing_wait_reason(self):
        from goals.models import PlanQuestion

        PlanQuestion.objects.create(plan=self.plan, text='When does the course begin?', field='timing',
                                    required=True, status='deferred', deferred_reason='Waiting for the provider.')
        self.plan.planning_state = 'waiting'
        self.plan.waiting_reason = 'Waiting for the course timetable.'
        self.plan.save()
        response, _ = self.propose()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.apply(response).status_code, 200)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.planning_state, 'waiting')
        self.assertEqual(self.plan.waiting_reason, 'Waiting for the course timetable.')
        task = Task.objects.get(title='Trace array indexing in Python')
        self.assertTrue(self.client.get(f'/api/tasks/{task.pk}/').data['is_schedulable'])

    def test_rejection_saves_no_tasks(self):
        response, _ = self.propose()
        response = self.client.post(f"/api/conversations/plans/{self.plan.pk}/changes/{response.data['change']['id']}/reject/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(Task.objects.exists())
        self.assertFalse(self.plan.goals.exists())
