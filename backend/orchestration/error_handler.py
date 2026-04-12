"""
backend.orchestration.error_handler

Centralised error handling and retry logic for the multi-agent LangGraph workflow engine.

Retry strategy — exponential backoff with jitter:
    Each retryable failure waits progressively longer before the next attempt:
        delay = base_delay * (backoff_multiplier ** attempt)
    A ±20% random jitter is applied on top (when RetryConfig.jitter=True) to prevent
    the thundering-herd problem — multiple agents hitting a rate-limited API at exactly
    the same moment and amplifying load spikes.

Why error classification matters in agentic systems:
    In a normal web request you can usually retry any failure safely. In a multi-step
    agent graph the cost of a spurious retry is much higher:
        - LLM calls cost money and latency.
        - Some failures (bad API key, malformed input) will NEVER succeed no matter
          how many times you retry — looping wastes resources and delays the user.
        - Misclassifying a non-retryable error as retryable can cause a cascade where
          every downstream node also retries, multiplying costs by max_retries.
    Classification lets us fail fast on deterministic errors and be resilient only
    where resilience is meaningful (transient network / rate-limit issues).

on_failure callback:
    execute_with_retry accepts an optional async on_failure(exc, task_id) hook.
    This decouples persistence (writing ExecutionError rows to Supabase and appending
    the ID to WorkflowState.error_ids) from retry logic, keeping each concern in the
    layer that owns it.
"""

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom sentinel exception types referenced by the classifier
# ---------------------------------------------------------------------------

class RateLimitError(Exception):
    """Raised when an upstream API returns a 429 / rate-limit response."""


class AuthenticationError(Exception):
    """Raised when credentials are rejected by an upstream service (401 / 403)."""


class InvalidInputError(Exception):
    """Raised when agent input is structurally invalid and cannot be corrected by retrying."""


# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------

@dataclass
class RetryConfig:
    """
    Configures the exponential-backoff retry behaviour for a single agent call.

    Attributes:
        max_retries:        Maximum number of retry attempts after the first failure.
                            A value of 3 means the function is called up to 4 times total.
        base_delay:         Seconds to wait before the first retry.
        backoff_multiplier: Factor by which delay grows on each subsequent attempt.
                            Delay for attempt n = base_delay * (backoff_multiplier ** n).
        jitter:             When True, applies ±20% random noise to each delay to prevent
                            thundering-herd collisions when multiple agents retry together.
    """
    max_retries: int = 2
    base_delay: float = 1.0
    backoff_multiplier: float = 2.0
    jitter: bool = True

    def compute_delay(self, attempt: int) -> float:
        """
        Return the sleep duration (seconds) for a given retry attempt index (0-based).

        Args:
            attempt: Zero-based index of the retry attempt (0 = first retry).

        Returns:
            Sleep duration in seconds, with optional jitter applied.
        """
        delay = self.base_delay * (self.backoff_multiplier ** attempt)
        if self.jitter:
            # Apply ±20% random noise to stagger concurrent retries
            delay *= random.uniform(0.8, 1.2)
        return delay


# ---------------------------------------------------------------------------
# Error classifier
# ---------------------------------------------------------------------------

# Exception types where retrying is meaningful (transient / recoverable)
_RETRYABLE_TYPES: tuple[type[Exception], ...] = (
    TimeoutError,
    ConnectionError,
    RateLimitError,
    httpx.TimeoutException,
    httpx.NetworkError,
)

# Exception types that are deterministic — retrying will never help
_NON_RETRYABLE_TYPES: tuple[type[Exception], ...] = (
    ValueError,
    AuthenticationError,
    InvalidInputError,
    NotImplementedError,
    TypeError,
)


class ErrorClassifier:
    """
    Classifies exceptions as retryable or non-retryable to drive the retry strategy.

    Classification order:
        1. Non-retryable types are checked first — if matched, we fail fast immediately.
        2. Retryable types are checked next — if matched, exponential backoff applies.
        3. Unknown exceptions default to "retryable" (fail-safe): in production systems
           it is usually safer to attempt recovery on an unknown error than to
           hard-abort a long-running workflow.
    """

    @staticmethod
    def classify(exc: Exception) -> str:
        """
        Determine whether an exception should trigger a retry or an immediate abort.

        Args:
            exc: The caught exception instance.

        Returns:
            "non_retryable" if the error is deterministic and retrying would be futile.
            "retryable"     if the error is transient and the call may succeed on retry.
        """
        # Check non-retryable first — fail fast on deterministic errors
        if isinstance(exc, _NON_RETRYABLE_TYPES):
            return "non_retryable"

        # Retryable: transient network, timeout, or rate-limit errors
        if isinstance(exc, _RETRYABLE_TYPES):
            return "retryable"

        # Unknown error type — default to retryable (fail-safe for long workflows)
        logger.warning(
            "ErrorClassifier: unknown exception type '%s' defaulting to retryable.",
            type(exc).__name__,
        )
        return "retryable"


# ---------------------------------------------------------------------------
# Core retry executor
# ---------------------------------------------------------------------------

async def execute_with_retry(
    func: Callable[[], Awaitable[Any]],
    retry_config: RetryConfig,
    task_id: str,
    agent_name: str,
    on_failure: Optional[Callable[[Exception, str], Awaitable[None]]] = None,
) -> Any:
    """
    Execute an async callable with exponential backoff, skipping retries on non-retryable errors.

    Retry policy:
        - On each failure, ErrorClassifier decides whether to retry or abort immediately.
        - Retryable errors sleep for `base_delay * (backoff_multiplier ** attempt)` seconds
          (with optional ±20% jitter) before the next attempt.
        - Non-retryable errors bypass the retry loop entirely and raise at once.
        - After max_retries exhausted retryable attempts, the final exception is re-raised.

    Args:
        func:         Zero-argument async callable wrapping the agent operation to run.
                      Use `functools.partial` or a lambda to bind arguments beforehand.
        retry_config: RetryConfig instance controlling max attempts, delays and jitter.
        task_id:      UUID string of the task being executed — included in all log lines.
        agent_name:   Human-readable agent identifier — used in log context.
        on_failure:   Optional async callback invoked after all retries are exhausted
                      (or immediately on non-retryable error). Signature:
                          async def on_failure(exc: Exception, task_id: str) -> None
                      Use this hook to persist ExecutionError rows to Supabase and
                      append their IDs to WorkflowState.error_ids without coupling
                      this module to the database layer.

    Returns:
        The return value of `func` on a successful invocation.

    Raises:
        The final exception from `func` after all retry attempts are exhausted,
        or immediately if the error is classified as non-retryable.
    """
    classifier = ErrorClassifier()
    last_exc: Optional[Exception] = None

    # Total attempts = 1 initial call + max_retries
    for attempt in range(retry_config.max_retries + 1):
        try:
            if attempt == 0:
                logger.info(
                    "[%s] task_id=%s — starting (attempt 1/%d).",
                    agent_name, task_id, retry_config.max_retries + 1,
                )
            else:
                logger.info(
                    "[%s] task_id=%s — retry %d/%d.",
                    agent_name, task_id, attempt, retry_config.max_retries,
                )

            result = await func()
            logger.info(
                "[%s] task_id=%s — succeeded on attempt %d.",
                agent_name, task_id, attempt + 1,
            )
            return result

        except Exception as exc:
            last_exc = exc
            classification = classifier.classify(exc)

            logger.warning(
                "[%s] task_id=%s — attempt %d failed with %s (%s): %s",
                agent_name,
                task_id,
                attempt + 1,
                classification,
                type(exc).__name__,
                exc,
            )

            # ----------------------------------------------------------------
            # Non-retryable: abort immediately, no point sleeping
            # ----------------------------------------------------------------
            if classification == "non_retryable":
                logger.error(
                    "[%s] task_id=%s — non-retryable error, aborting immediately.",
                    agent_name, task_id,
                )
                if on_failure is not None:
                    await on_failure(exc, task_id)
                raise

            # ----------------------------------------------------------------
            # Retryable: check if we have attempts remaining
            # ----------------------------------------------------------------
            if attempt >= retry_config.max_retries:
                # No more attempts left
                logger.error(
                    "[%s] task_id=%s — exhausted all %d retry attempts. Giving up.",
                    agent_name, task_id, retry_config.max_retries,
                )
                if on_failure is not None:
                    await on_failure(exc, task_id)
                raise

            # Sleep with exponential backoff before next attempt
            delay = retry_config.compute_delay(attempt)
            logger.info(
                "[%s] task_id=%s — waiting %.2fs before retry %d/%d.",
                agent_name, task_id, delay, attempt + 1, retry_config.max_retries,
            )
            await asyncio.sleep(delay)

    # Unreachable, but satisfies type checkers
    raise RuntimeError("execute_with_retry exited loop without returning or raising.")
