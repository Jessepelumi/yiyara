from google import genai
from google.genai import types
import os
import logging

from ai.prompts.goal_decomposition_prompt import GOAL_DECOMPOSITION_SCHEMA
from ai.prompts.plan_iteration_prompt import PLAN_ITERATION_SCHEMA

logger = logging.getLogger(__name__)


class GeminiConfigurationError(RuntimeError):
    pass


class GeminiProvider:
    def __init__(self, api_key=None):
        api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise GeminiConfigurationError("GEMINI_API_KEY is not configured")

        self.client = genai.Client(api_key=api_key)
        self.model_name = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

    def generate_response(self, prompt, history=None):
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction="You are Yiyara, a supportive AI life coach."
                )
            )
            return response.text
        except Exception as e:
            logger.error(f"Gemini API Error: {e}")
            return "I'm having trouble thinking right now."

    def generate_structured_response(self, prompt):
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=GOAL_DECOMPOSITION_SCHEMA,
                )
            )
            return response.text
        except Exception as e:
            logger.error(f"Gemini Structured Error: {e}")
            raise e

    def generate_plan_response(self, prompt):
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=PLAN_ITERATION_SCHEMA,
                ),
            )
            return response.text
        except Exception as e:
            logger.error(f"Gemini Plan Iteration Error: {e}")
            raise
        
    def classify_intent(self, user_input):
        """
        Determines if the user wants to create a goal, ask a question, or just chat.
        """
        prompt = f"""
        Analyze the following user input and classify it into ONE of these categories:
        - DECOMPOSE: User wants to start a new goal, project, or task list.
        - QUERY: User is asking for information about their existing goals or progress.
        - CHAT: General conversation, greetings, or follow-up questions.

        Input: "{user_input}"
        
        Return ONLY the word: DECOMPOSE, QUERY, or CHAT.
        """
        try:
            # Use self.client.models.generate_content (New SDK style)
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1, # Low temperature for strict classification
                )
            )
            
            intent = response.text.strip().upper()
            
            # Clean up the response in case it returned "Category: DECOMPOSE" or similar
            for valid_intent in ['DECOMPOSE', 'QUERY', 'CHAT']:
                if valid_intent in intent:
                    return valid_intent
                    
            return 'CHAT' # Final fallback
            
        except Exception as e:
            logger.error(f"Classification Error: {e}")
            return 'CHAT' # Fallback to chat so the user isn't stuck
