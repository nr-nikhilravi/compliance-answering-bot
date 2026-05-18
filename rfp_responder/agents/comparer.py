import logging
import litellm
from .utils import call_llm_with_retries

logger = logging.getLogger(__name__)

COMPARER_SYSTEM_PROMPT = """You are a comparison agent tasked with evaluating two answers to an RFP question.
Answer 1 (Existing Answer): This is the current answer provided by the user.
Answer 2 (Generated Answer): This is the newly generated answer by the system.

Your task is to determine whether Answer 1 is sufficient, accurate, and covers the same points as Answer 2.
- If Answer 1 is mostly fine, accurate, and does not miss critical information present in Answer 2, output EXACTLY "No Change".
- If Answer 1 is significantly worse, inaccurate, or missing key information present in Answer 2, output EXACTLY "Rework".

Do not output any additional text, just "No Change" or "Rework"."""

class ComparerAgent:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash", provider: str = "gemini"):
        self.api_key = api_key
        
        # Add provider prefix for litellm
        if provider == "gemini" and not model.startswith("gemini/"):
            self.model = f"gemini/{model}"
        elif provider == "openrouter" and not model.startswith("openrouter/"):
            self.model = f"openrouter/{model}"
        elif provider == "claude" and not model.startswith("anthropic/"):
            self.model = f"anthropic/{model}"
        elif provider == "openai" and not model.startswith("openai/"):
            self.model = f"openai/{model}"
        else:
            self.model = model

    def compare(self, existing_answer: str, generated_answer: str) -> tuple[str, int]:
        """
        Compare two answers and return ('No Change' | 'Rework', tokens_used)
        """
        if not existing_answer or not existing_answer.strip():
            return "", 0

        prompt = f"Answer 1 (Existing Answer):\n{existing_answer}\n\nAnswer 2 (Generated Answer):\n{generated_answer}\n\nDecision:"
        
        try:
            response = call_llm_with_retries(
                model=self.model,
                api_key=self.api_key,
                messages=[
                    {"role": "system", "content": COMPARER_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
            )
            text = (response.choices[0].message.content or "").strip()
            if "rework" in text.lower():
                result = "Rework"
            else:
                result = "No Change"
            
            tokens = response.usage.total_tokens if hasattr(response, "usage") and response.usage else 0

            return result, tokens
        except Exception as e:
            logger.error(f"Comparer agent failed: {e}")
            return "Rework", 0
