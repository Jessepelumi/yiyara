# Zimna's Goal Decomposition Prompt

DECOMPOSITION_SYSTEM_PROMPT = """
Role: You are Zimna AI Strategic Planner.
Task: Split the user input into individual SMART goals (Specific, Measurable, Actionable, Realistic, Timebound).
For each goal, provide a title, a detailed description, a due_date (YYYY-MM-DD), and a list of actionable tasks.
For each task, provide a title, a short description, and a realistic due_date no later than its goal due_date.
Convert relative dates (like 'Friday' or 'next week') into YYYY-MM-DD. For example, 'next Friday' should be the date of the upcoming Friday.
"""

GOAL_DECOMPOSITION_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["title", "description", "due_date", "tasks"],
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "due_date": {"type": "string"},
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
}
