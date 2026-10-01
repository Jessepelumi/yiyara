from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("goals", "0002_plan_boards"),
        ("tasks", "0001_initial"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="task",
            options={"ordering": ["created_at"]},
        ),
    ]
