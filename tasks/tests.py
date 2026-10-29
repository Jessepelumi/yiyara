from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from goals.models import Goal, Plan
from .models import Task, TaskDependency


class TaskDependencyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = get_user_model().objects.create_user(email='dependencies@example.com')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.plan = Plan.objects.create(user=self.user, title='Study', raw_input='Study')
        self.goal = Goal.objects.create(user=self.user, plan=self.plan, title='Arrays')
        self.first = Task.objects.create(goal=self.goal, title='Read examples', readiness='ready',
                                         completion_criteria='Notes saved', estimated_minutes=30)
        self.second = Task.objects.create(goal=self.goal, title='Practise', readiness='ready',
                                          completion_criteria='Solutions saved', estimated_minutes=30)

    def update(self, task, parents):
        return self.client.patch(f'/api/tasks/{task.pk}/', {'dependency_ids': [str(pk) for pk in parents]}, format='json')

    def test_cycles_self_links_foreign_links_and_duplicates_roll_back(self):
        self.assertEqual(self.update(self.second, [self.first.pk]).status_code, 200)
        other_plan = Plan.objects.create(user=self.user, title='Other', raw_input='Other')
        other_goal = Goal.objects.create(user=self.user, plan=other_plan, title='Other')
        foreign = Task.objects.create(goal=other_goal, title='Foreign')
        for parents in [[self.second.pk], [self.first.pk], [foreign.pk], [self.second.pk, self.second.pk]]:
            self.plan.refresh_from_db()
            version = self.plan.version
            with self.subTest(parents=parents):
                self.assertEqual(self.update(self.first, parents).status_code, 400)
                self.plan.refresh_from_db()
                self.assertEqual(self.plan.version, version)
                self.assertFalse(self.first.dependencies.exists())
        self.assertTrue(self.second.dependencies.filter(pk=self.first.pk).exists())

    def test_goal_deletion_cannot_silently_remove_a_prerequisite(self):
        other = Goal.objects.create(user=self.user, plan=self.plan, title='Linked')
        self.second.goal = other
        self.second.save()
        TaskDependency.objects.create(task=self.second, prerequisite=self.first)
        response = self.client.delete(f'/api/{self.goal.pk}/')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data['error'], 'task_dependencies')
        self.assertTrue(Task.objects.filter(pk=self.first.pk).exists())
        self.assertEqual(self.update(self.second, []).status_code, 200)
        self.assertEqual(self.client.delete(f'/api/{self.goal.pk}/').status_code, 204)


class TaskOutcomeTests(TestCase):
    def setUp(self):
        import uuid
        from goals.services import create_plan_revision

        cache.clear()
        self.addCleanup(cache.clear)
        self.user = get_user_model().objects.create_user(email='outcomes@example.com')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.plan = Plan.objects.create(user=self.user, title='DSA', raw_input='Learn DSA')
        self.goal = Goal.objects.create(user=self.user, plan=self.plan, title='Arrays')
        self.task = Task.objects.create(goal=self.goal, title='Write traversal loop', readiness='ready',
                                        estimated_minutes=60, completion_criteria='Print five values in order')
        self.child = Task.objects.create(goal=self.goal, title='Write search function', readiness='ready',
                                         estimated_minutes=30, completion_criteria='Find a matching index')
        TaskDependency.objects.create(task=self.child, prerequisite=self.task)
        create_plan_revision(self.plan, 'Initial work')
        self.url = f'/api/tasks/{self.task.pk}/outcomes/'
        self.body = {'client_id': str(uuid.uuid4()), 'plan_version': 1, 'status': 'partial',
                     'remaining_minutes': 20, 'spent_minutes': 45, 'notes': 'Loop works; still need edge cases.'}

    def report(self, **changes):
        import uuid

        self.plan.refresh_from_db()
        body = {**self.body, 'plan_version': self.plan.version, 'client_id': str(uuid.uuid4()), **changes}
        return self.client.post(self.url, body, format='json')

    def test_partial_keeps_original_estimate_and_reports_remaining_work(self):
        from .models import TaskOutcome

        response = self.client.post(self.url, self.body, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        task = response.data['task']
        self.assertEqual(task['progress_status'], 'partial')
        self.assertFalse(task['is_completed'])
        self.assertEqual(task['estimated_minutes'], 60)
        self.assertEqual(task['remaining_minutes'], 20)
        self.assertEqual(task['effective_minutes'], 20)
        self.assertTrue(task['is_schedulable'])
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.version, 2)
        self.assertEqual(self.plan.context_version, 1)
        self.assertEqual(TaskOutcome.objects.get().notes, self.body['notes'])
        self.assertFalse(self.client.get(f'/api/tasks/{self.child.pk}/').data['is_schedulable'])

    def test_completion_unlocks_dependencies_and_reopening_blocks_them(self):
        response = self.report(status='completed', remaining_minutes=0)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data['task']['is_completed'])
        self.assertEqual(response.data['task']['remaining_minutes'], 0)
        self.assertTrue(self.client.get(f'/api/tasks/{self.child.pk}/').data['is_schedulable'])
        self.assertEqual(self.report().status_code, 400)
        response = self.report(status='reopened', remaining_minutes=15)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(response.data['task']['is_completed'])
        self.assertFalse(self.client.get(f'/api/tasks/{self.child.pk}/').data['is_schedulable'])
        history = self.client.get(self.url)
        self.assertEqual(history.status_code, 200)
        self.assertEqual([item['status'] for item in history.data], ['reopened', 'completed'])

    def test_blocked_and_skipped_require_explicit_resolution(self):
        response = self.report(status='blocked', remaining_minutes=None, blocker_reason='Course access not granted.')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(response.data['task']['is_schedulable'])
        self.assertEqual(response.data['task']['readiness'], 'blocked')
        response = self.report(status='skipped', remaining_minutes=None)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(response.data['task']['is_schedulable'])
        response = self.client.patch(f'/api/tasks/{self.task.pk}/', {'readiness': 'ready'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['is_schedulable'])
        response = self.report(status='reopened', remaining_minutes=30)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data['task']['is_schedulable'])

    def test_exact_replay_does_not_duplicate_or_rewind_progress(self):
        from .models import TaskOutcome

        first = self.client.post(self.url, self.body, format='json')
        self.assertEqual(first.status_code, 201)
        self.report(status='completed', remaining_minutes=0)
        replay = self.client.post(self.url, self.body, format='json')
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertTrue(replay.data['replayed'])
        self.assertEqual(replay.data['outcome']['id'], first.data['outcome']['id'])
        self.assertTrue(replay.data['task']['is_completed'])
        self.assertEqual(TaskOutcome.objects.count(), 2)
        conflict = self.client.post(self.url, {**self.body, 'notes': 'Different report'}, format='json')
        self.assertEqual(conflict.status_code, 409)
        # IDs are scoped to the plan, so another task cannot reuse one.
        conflict = self.client.post(f'/api/tasks/{self.child.pk}/outcomes/', self.body, format='json')
        self.assertEqual(conflict.status_code, 409)

    def test_stale_versions_validation_and_unsupported_fields_do_not_mutate(self):
        from .models import TaskOutcome

        for change in [
            {'plan_version': 99}, {'status': 'unknown'}, {'status': 'completed', 'remaining_minutes': 20},
            {'remaining_minutes': None}, {'remaining_minutes': 0}, {'remaining_minutes': 481},
            {'spent_minutes': -1}, {'status': 'blocked', 'blocker_reason': ''},
            {'blocker_reason': 'Not allowed on partial'}, {'extra': 'ignored?'},
        ]:
            with self.subTest(change=change):
                response = self.client.post(self.url, {**self.body, **change}, format='json')
                self.assertIn(response.status_code, [400, 409], response.data)
                self.assertFalse(TaskOutcome.objects.exists())
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.version, 1)
        self.assertEqual(self.plan.revisions.count(), 1)
        self.assertEqual(self.client.post(self.url, [], format='json').status_code, 400)

    def test_ownership_auth_and_immutable_history(self):
        other = get_user_model().objects.create_user(email='other-outcomes@example.com')
        self.assertEqual(self.report().status_code, 201)
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url, self.body, format='json').status_code, 404)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.patch(self.url, self.body, format='json').status_code, 405)
        self.assertEqual(self.client.delete(self.url).status_code, 405)

    def test_restore_keeps_real_progress_and_history_even_after_task_deletion(self):
        from goals.models import PlanRevision
        from goals.services import restore_plan_revision

        initial = self.plan.revisions.get(version=1)
        self.assertEqual(self.report(status='completed', remaining_minutes=0).status_code, 201)
        restore_plan_revision(initial, self.user)
        self.task.refresh_from_db()
        self.assertTrue(self.task.is_completed)
        self.assertEqual(self.task.progress_status, 'completed')
        self.assertEqual(self.client.get(self.url).data[0]['status'], 'completed')
        # A contextual, goal-less restore may delete the task without deleting its report.
        empty = PlanRevision.objects.create(plan=self.plan, version=100, snapshot={
            'planning_mode': 'contextual', 'planning_state': 'clarifying', 'goals': [], 'questions': []})
        restore_plan_revision(empty, self.user)
        self.assertFalse(Task.objects.filter(pk=self.task.pk).exists())
        self.assertEqual(self.client.get(self.url).status_code, 200)
        restore_plan_revision(initial, self.user)
        self.task.refresh_from_db()
        self.assertTrue(self.task.is_completed)

    def test_legacy_completion_control_is_logged_and_not_reverted_by_restore(self):
        from goals.services import restore_plan_revision
        from .models import TaskOutcome

        initial = self.plan.revisions.get(version=1)
        url = f'/api/tasks/{self.task.pk}/'
        response = self.client.patch(url, {'is_completed': True}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['progress_status'], 'completed')
        self.assertEqual(TaskOutcome.objects.get().status, 'completed')
        self.client.patch(url, {'is_completed': True}, format='json')
        self.assertEqual(TaskOutcome.objects.count(), 1)
        restore_plan_revision(initial, self.user)
        self.task.refresh_from_db()
        self.assertTrue(self.task.is_completed)
        self.client.patch(url, {'is_completed': False}, format='json')
        self.assertEqual(TaskOutcome.objects.count(), 2)
        self.task.refresh_from_db()
        self.assertEqual(self.task.progress_status, 'not_started')
        self.assertEqual(self.task.remaining_minutes, 60)

    def test_revision_failure_rolls_back_report_and_progress(self):
        from unittest.mock import patch
        from .models import TaskOutcome

        with patch('goals.services.create_plan_revision', side_effect=RuntimeError('revision failed')):
            with self.assertRaises(RuntimeError):
                self.client.post(self.url, self.body, format='json')
        self.assertFalse(TaskOutcome.objects.exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.progress_version, 0)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.version, 1)

    def test_outcomes_are_in_planning_snapshot_without_automatically_changing_context(self):
        from goals.services import serialize_plan_snapshot

        response = self.report(notes='I booked driving lessons every Tuesday at 10am.')
        self.assertEqual(response.status_code, 201)
        self.plan.refresh_from_db()
        snapshot = serialize_plan_snapshot(self.plan)
        self.assertEqual(snapshot['recent_task_outcomes'][0]['notes'], 'I booked driving lessons every Tuesday at 10am.')
        self.assertEqual(snapshot['goals'][0]['tasks'][0]['remaining_minutes'], 20)
        self.assertEqual(self.plan.context, {})

    def test_progress_does_not_reaffirm_changed_planning_context(self):
        self.plan.context_version = 2
        self.plan.save()
        self.task.planning_context_version = 1
        self.task.save()
        response = self.report()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(response.data['task']['is_schedulable'])
        self.assertEqual(response.data['task']['planning_context_version'], 1)

    def test_inactive_plan_rejects_new_reports(self):
        self.plan.status = 'archived'
        self.plan.save()
        self.assertEqual(self.report().status_code, 400)
