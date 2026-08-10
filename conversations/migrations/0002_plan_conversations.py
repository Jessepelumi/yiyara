import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


def move_conversations_to_plans(apps, schema_editor):
    Conversation = apps.get_model("conversations", "Conversation")
    Message = apps.get_model("conversations", "Message")

    for conversation in Conversation.objects.select_related("goal").order_by(
        "created_at"
    ):
        plan_id = conversation.goal.plan_id
        existing = Conversation.objects.filter(plan_id=plan_id).exclude(
            id=conversation.id
        ).first()
        if existing:
            Message.objects.filter(conversation_id=conversation.id).update(
                conversation_id=existing.id
            )
            conversation.delete()
        else:
            conversation.plan_id = plan_id
            conversation.save(update_fields=["plan"])


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0001_initial"),
        ("goals", "0002_plan_boards"),
    ]

    operations = [
        migrations.AddField(
            model_name="conversation",
            name="plan",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="conversation_rows",
                to="goals.plan",
            ),
        ),
        migrations.AddField(
            model_name="message",
            name="client_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="message",
            name="metadata",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="message",
            name="scope_goal",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="scoped_messages",
                to="goals.goal",
            ),
        ),
        migrations.RunPython(move_conversations_to_plans, migrations.RunPython.noop),
        migrations.RemoveField(model_name="conversation", name="goal"),
        migrations.AlterField(
            model_name="conversation",
            name="plan",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="conversation",
                to="goals.plan",
            ),
        ),
        migrations.AddConstraint(
            model_name="message",
            constraint=models.UniqueConstraint(
                condition=Q(client_id__isnull=False),
                fields=("conversation", "client_id"),
                name="unique_conversation_client_message",
            ),
        ),
    ]
