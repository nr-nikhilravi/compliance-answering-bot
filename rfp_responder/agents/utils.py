import time
import logging
import litellm

logger = logging.getLogger(__name__)

def call_llm_with_retries(max_retries=3, backoff_factor=2, **kwargs):
    """Call litellm.completion with retry logic for connection errors.
    
    Includes dedicated handling for OSError (e.g., [Errno 22] Invalid argument)
    which can occur on Windows due to socket/SSL issues under concurrent load.
    """
    total_attempts = max_retries + 2  # Extra attempts for OS-level errors
    for attempt in range(total_attempts):
        try:
            return litellm.completion(**kwargs)
        except OSError as e:
            # Windows socket errors (Errno 22, connection reset, etc.)
            if attempt == total_attempts - 1:
                logger.error("LLM call failed after %d attempts (OS error): %s", total_attempts, e)
                raise
            sleep_time = backoff_factor ** (attempt + 1)
            logger.warning(
                "OS-level connection error (attempt %d/%d): %s. Retrying in %ds...",
                attempt + 1, total_attempts, e, sleep_time
            )
            time.sleep(sleep_time)
        except Exception as e:
            if attempt == max_retries - 1:
                logger.error("LLM call failed after %d attempts: %s", max_retries, e)
                raise
            
            sleep_time = backoff_factor ** attempt
            logger.warning("LLM connection error (attempt %d/%d): %s. Retrying in %ds...", 
                           attempt + 1, max_retries, e, sleep_time)
            time.sleep(sleep_time)

