import time
import logging
import litellm

logger = logging.getLogger(__name__)

def call_llm_with_retries(max_retries=3, backoff_factor=2, **kwargs):
    """Call litellm.completion with retry logic for connection errors."""
    for attempt in range(max_retries):
        try:
            return litellm.completion(**kwargs)
        except Exception as e:
            if attempt == max_retries - 1:
                logger.error("LLM call failed after %d attempts: %s", max_retries, e)
                raise
            
            sleep_time = backoff_factor ** attempt
            logger.warning("LLM connection error (attempt %d/%d): %s. Retrying in %ds...", 
                           attempt + 1, max_retries, e, sleep_time)
            time.sleep(sleep_time)
