from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

import httplib2
from google.auth.exceptions import TransportError as GoogleAuthTransportError
from googleapiclient.errors import HttpError

from src.browser_transport import InvalidSourceResponse, RetryableTransportError


logger = logging.getLogger("atez.retry")
T = TypeVar("T")


def is_retryable_source_error(error: BaseException) -> bool:
    if isinstance(error, RetryableTransportError):
        return True
    if isinstance(error, InvalidSourceResponse) and error.response is not None:
        return (
            error.response.status in {408, 425, 429}
            or 500 <= error.response.status < 600
        )
    return False


def _drive_error_reasons(error: HttpError) -> set[str]:
    try:
        payload = json.loads(error.content or b"{}")
    except (TypeError, ValueError, UnicodeDecodeError):
        return set()
    reasons = set()
    for item in payload.get("error", {}).get("errors", []):
        if isinstance(item, dict) and isinstance(item.get("reason"), str):
            reasons.add(item["reason"])
    return reasons


def is_retryable_drive_error(error: BaseException) -> bool:
    if isinstance(error, HttpError):
        try:
            status = int(error.resp.status)
        except (AttributeError, TypeError, ValueError):
            return False
        if status in {408, 429} or 500 <= status < 600:
            return True
        if status == 403:
            return bool(
                _drive_error_reasons(error)
                & {"backendError", "rateLimitExceeded", "userRateLimitExceeded"}
            )
        return False
    return isinstance(
        error,
        (
            GoogleAuthTransportError,
            httplib2.HttpLib2Error,
            ConnectionError,
            TimeoutError,
        ),
    )


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    sleep: Callable[[float], None] = field(
        default=time.sleep,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds must not be negative")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be at least 1")

    def run(
        self,
        operation: Callable[[], T],
        *,
        is_retryable: Callable[[BaseException], bool],
        operation_name: str = "operation",
    ) -> T:
        for attempt in range(1, self.max_attempts + 1):
            try:
                return operation()
            except Exception as error:
                if attempt >= self.max_attempts or not is_retryable(error):
                    raise
                delay = self.initial_delay_seconds * (
                    self.backoff_multiplier ** (attempt - 1)
                )
                logger.warning(
                    "%s failed transiently on attempt %s/%s; retrying in %.2fs: %s",
                    operation_name,
                    attempt,
                    self.max_attempts,
                    delay,
                    error,
                )
                self.sleep(delay)
        raise AssertionError("retry loop exhausted without returning or raising")
