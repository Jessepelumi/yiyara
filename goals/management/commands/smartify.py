import os
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from workflow.ai_engine import YiyaraWorkflow

User = get_user_model()

class Command(BaseCommand):
    help = "Smartifies raw input and saves to goals and tasks apps"

    def add_arguments(self, parser):
        parser.add_argument('goal_text', type=str)

    def handle(self, *args, **options):
        # Validation
        user = User.objects.first()
        if not user:
            self.stdout.write(self.style.ERROR("Create a user in admin first!"))
            return

        # Execution
        api_key = os.getenv("GEMINI_API_KEY")
        engine = YiyaraWorkflow(api_key=api_key)
        
        self.stdout.write("Yiyara is thinking...")
        results = engine.create_goals_from_ai(user, options['goal_text'])

        # Output
        if isinstance(results, list) and len(results) > 0:
            self.stdout.write(self.style.SUCCESS(f"Generated {len(results)} Goal(s)"))
            for goal in results:
                self.stdout.write(f"[Goal] {goal.title}")
                # Accessing related tasks from the 'tasks' app
                for task in goal.tasks.all():
                    self.stdout.write(f"   └─ [Task] {task.title}")

                    