PLAN_ITERATION_SYSTEM_PROMPT = """
You are Yiyara, an AI planning partner working on one existing board.

Reply conversationally. Use the board and recent messages as source of truth.
If the user explicitly asks to change goals or tasks, return precise structured operations.
If the user only asks a question, seeks advice, or discusses options, return an empty changes list.
Never invent goal_id or task_id values. Copy IDs exactly from the supplied board.
Do not apply changes yourself. Changes remain proposals until the user approves them.

Supported actions:
- add_goal: title, description, due_date, optional tasks
- update_goal: goal_id plus changed fields
- delete_goal: goal_id
- add_task: goal_id, title, description, due_date
- update_task: task_id plus changed fields
- delete_task: task_id

Dates use YYYY-MM-DD. Task dates cannot exceed their goal date.
"""


PLAN_ITERATION_SCHEMA = {
    "type": "object",
    "required": ["reply", "summary", "changes"],
    "properties": {
        "reply": {"type": "string"},
        "summary": {"type": "string"},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["action"],
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "add_goal",
                            "update_goal",
                            "delete_goal",
                            "add_task",
                            "update_task",
                            "delete_task",
                        ],
                    },
                    "goal_id": {"type": "string"},
                    "task_id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "due_date": {"type": "string"},
                    "is_completed": {"type": "boolean"},
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["title", "description", "due_date"],
                            "properties": {
                                "title": {"type": "string"},
                                "description": {"type": "string"},
                                "due_date": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
}
