from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from src.browser_transport import (
    BrowserResponse,
    InvalidSourceResponse,
    RetryableTransportError,
)
from src.drive_uploader import DriveUploader
from src.retry_policy import (
    RetryPolicy,
    is_retryable_drive_error,
    is_retryable_source_error,
)


def _http_error(status):
    return HttpError(Response({"status": str(status)}), b"{}")


def test_retry_policy_recovers_with_bounded_backoff():
    attempts = []
    delays = []
    policy = RetryPolicy(
        max_attempts=3,
        initial_delay_seconds=0.25,
        backoff_multiplier=2,
        sleep=delays.append,
    )

    def operation():
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise RetryableTransportError("temporary")
        return "recovered"

    result = policy.run(operation, is_retryable=is_retryable_source_error)

    assert result == "recovered"
    assert attempts == [1, 2, 3]
    assert delays == [0.25, 0.5]


def test_retry_policy_stops_after_the_finite_attempt_limit():
    attempts = 0
    delays = []
    policy = RetryPolicy(
        max_attempts=3,
        initial_delay_seconds=1,
        backoff_multiplier=2,
        sleep=delays.append,
    )

    def operation():
        nonlocal attempts
        attempts += 1
        raise RetryableTransportError("still unavailable")

    with pytest.raises(RetryableTransportError, match="still unavailable"):
        policy.run(operation, is_retryable=is_retryable_source_error)

    assert attempts == 3
    assert delays == [1, 2]


def test_retry_policy_does_not_retry_validation_or_permanent_failures():
    attempts = 0
    delays = []
    response = BrowserResponse(
        400,
        "https://resmigazete.gov.tr/bad.html",
        "text/html",
        b"bad request",
    )
    policy = RetryPolicy(sleep=delays.append)

    def operation():
        nonlocal attempts
        attempts += 1
        raise InvalidSourceResponse("permanent", response=response)

    with pytest.raises(InvalidSourceResponse, match="permanent"):
        policy.run(operation, is_retryable=is_retryable_source_error)

    assert attempts == 1
    assert delays == []


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_drive_http_transients_are_retryable(status):
    assert is_retryable_drive_error(_http_error(status)) is True


@pytest.mark.parametrize("status", [400, 401, 404])
def test_drive_permanent_http_errors_are_not_retryable(status):
    assert is_retryable_drive_error(_http_error(status)) is False


class _SequenceCall:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.attempts = 0

    def execute(self):
        outcome = self.outcomes[min(self.attempts, len(self.outcomes) - 1)]
        self.attempts += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _ListingFiles:
    def __init__(self, call):
        self.call = call

    def list(self, **_kwargs):
        return self.call


def test_drive_uploader_retries_a_retryable_list_failure():
    call = _SequenceCall(
        [
            _http_error(503),
            {"files": [{"id": "folder-1", "name": "sources"}]},
        ]
    )
    delays = []
    uploader = object.__new__(DriveUploader)
    uploader.service = SimpleNamespace(files=lambda: _ListingFiles(call))
    uploader.retry_policy = RetryPolicy(
        max_attempts=3,
        initial_delay_seconds=0.1,
        sleep=delays.append,
    )

    folder_id = uploader.find_or_create_folder("sources", "date-1")

    assert folder_id == "folder-1"
    assert call.attempts == 2
    assert delays == [0.1]


def test_drive_uploader_does_not_retry_a_permanent_list_failure():
    call = _SequenceCall(
        [
            _http_error(404),
            {"files": [{"id": "must-not-be-used", "name": "sources"}]},
        ]
    )
    delays = []
    uploader = object.__new__(DriveUploader)
    uploader.service = SimpleNamespace(files=lambda: _ListingFiles(call))
    uploader.retry_policy = RetryPolicy(sleep=delays.append)

    with pytest.raises(HttpError):
        uploader.find_or_create_folder("sources", "date-1")

    assert call.attempts == 1
    assert delays == []


class _CallbackCall:
    def __init__(self, callback):
        self.callback = callback

    def execute(self):
        return self.callback()


class _AmbiguousFolderCreateFiles:
    def __init__(self):
        self.folder_exists = False
        self.list_calls = 0
        self.create_calls = 0

    def list(self, **_kwargs):
        def execute_list():
            self.list_calls += 1
            files = (
                [{"id": "folder-1", "name": "sources"}]
                if self.folder_exists
                else []
            )
            return {"files": files}

        return _CallbackCall(execute_list)

    def create(self, **_kwargs):
        def execute_create():
            self.create_calls += 1
            if self.create_calls == 1:
                self.folder_exists = True
                raise _http_error(503)
            return {"id": f"folder-{self.create_calls}"}

        return _CallbackCall(execute_create)


def test_ambiguous_drive_create_is_relisted_instead_of_duplicated():
    files = _AmbiguousFolderCreateFiles()
    uploader = object.__new__(DriveUploader)
    uploader.service = SimpleNamespace(files=lambda: files)
    uploader.retry_policy = RetryPolicy(sleep=lambda _delay: None)

    folder_id = uploader.find_or_create_folder("sources", "date-1")

    assert folder_id == "folder-1"
    assert files.list_calls == 2
    assert files.create_calls == 1
