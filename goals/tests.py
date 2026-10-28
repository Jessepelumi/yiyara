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
