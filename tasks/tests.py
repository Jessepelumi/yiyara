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
