from ai.providers.gemini_provider import GeminiProvider
from workflow.ai_engine import ZimnaWorkflow
from .models import Message
from django.db import transaction


def handle_zimna_logic(user, conversation, raw_text):
    provider = GeminiProvider()
    workflow = ZimnaWorkflow(provider=provider)

    # Save User Message immediately (No transaction here - save it immediately)
    Message.objects.create(conversation=conversation, role='user', content=raw_text)

    # Route the intent (External API Call - keep outside transaction)
    intent = provider.classify_intent(raw_text)

    # Handle logic branches
    if intent == "DECOMPOSE":
        result = workflow.create_goals_from_ai(user, raw_text)
        
        if isinstance(result, list) and len(result) > 0:
            response_text = f"I've broken that down into {len(result)} goals. Check your path!"
        else:
            # Handle the case where AI returned an error or couldn't parse JSON
            response_text = "I couldn't quite turn that into a roadmap. Could you be more specific?"
        
    elif intent == "QUERY":
        response_text = "I'm checking your current goals... (RAG logic pending)"
        
    else:
        # Provide context so Zimna has 'memory'
        history = get_chat_history(conversation, limit=6)
        response_text = provider.generate_response(raw_text, history=history)

    # Save AI Response
    ai_message = Message.objects.create(
        conversation=conversation, 
        role='assistant',
        content=response_text
    )

    return ai_message

def get_chat_history(conversation, limit=10):
    messages = conversation.messages.order_by('-created_at')[:limit]
    
    history = []
    for m in reversed(messages):
        # Gemini SDK uses 'user' and 'model'
        role = "user" if m.role == "user" else "model"
        history.append({"role": role, "parts": [m.content]}) 
    
    return history
