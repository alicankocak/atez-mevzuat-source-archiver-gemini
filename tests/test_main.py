import multiprocessing

import pytest

import src.main as main_module
from src.date_lease import date_archive_lease
from src.drive_watcher import DriveRequestWatcher
from src.retry_policy import RetryPolicy


def _hold_date_lease(lock_dir, report_date, ready_queue, release_event):
    with date_archive_lease(report_date, lock_dir=lock_dir) as acquired:
        ready_queue.put(acquired)
        release_event.wait(timeout=10)


def test_date_archive_lease_has_single_interprocess_owner(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    release_event = context.Event()
    process = context.Process(
        target=_hold_date_lease,
        args=(tmp_path, "2026-08-14", ready_queue, release_event),
    )
    process.start()

    try:
        assert ready_queue.get(timeout=10) is True
        with date_archive_lease("2026-08-14", lock_dir=tmp_path) as acquired:
            assert acquired is False
    finally:
        release_event.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)

    assert process.exitcode == 0


def test_main_does_not_fetch_while_watcher_owns_the_date_lease(
    monkeypatch, tmp_path
):
    watcher = object.__new__(DriveRequestWatcher)
    watcher.claim_lock_dir = tmp_path

    class UnexpectedFetcher:
        def __init__(self, *args, **kwargs):
            raise AssertionError("main must not fetch while watcher owns the date")

    monkeypatch.setattr(main_module, "MevzuatFetcher", UnexpectedFetcher)

    with watcher._date_claim_lock("2026-08-14") as acquired:
        assert acquired is True
        result = main_module.archive_date(
            "2026-08-14",
            skip_drive=True,
            lease_dir=tmp_path,
        )

    assert result == main_module.ArchiveRunStatus.DATE_BUSY


class _StateOnlyUploader:
    service = object()

    def __init__(self, *, retry_policy):
        self.retry_policy = retry_policy

    def ensure_date_hierarchy(self, _report_date):
        raise AssertionError("an existing archive must not be mutated")

    def upload_rg_source_tree(self, *_args):
        raise AssertionError("an existing archive must not be mutated")


class _UnexpectedFetcher:
    def __init__(self, *args, **kwargs):
        raise AssertionError("an existing/processing archive must not be fetched")


@pytest.mark.parametrize(
    ("ready_result", "processing_exists", "expected_status"),
    [
        (("33299", "ready-1"), False, "ready_exists"),
        (None, True, "processing_exists"),
    ],
)
def test_main_rechecks_drive_state_before_mutating_an_edition(
    tmp_path, ready_result, processing_exists, expected_status
):
    class ArchiveState:
        def __init__(self, **_kwargs):
            pass

        def find_ready_result(self, report_date):
            assert report_date == "2026-08-14"
            return ready_result

        def has_processing_request(self, report_date):
            assert report_date == "2026-08-14"
            return processing_exists

    result = main_module.archive_date(
        "2026-08-14",
        lease_dir=tmp_path,
        fetcher_factory=_UnexpectedFetcher,
        uploader_factory=_StateOnlyUploader,
        watcher_factory=ArchiveState,
    )

    assert result.value == expected_status


def test_main_injects_one_retry_policy_into_source_and_drive(tmp_path):
    seen = {}
    policy = RetryPolicy(max_attempts=2, sleep=lambda _delay: None)

    class CapturingUploader:
        def __init__(self, *, retry_policy):
            seen["uploader_policy"] = retry_policy
            self.service = None

    class CapturingFetcher:
        def __init__(self, *, date_str, retry_policy):
            assert date_str == "2026-08-14"
            seen["fetcher_policy"] = retry_policy

        def run(self):
            manifest = type(
                "Manifest",
                (),
                {"resmi_gazete_sayisi": "33299", "documents": []},
            )()
            return manifest, tmp_path / "rg-33299"

    result = main_module.archive_date(
        "2026-08-14",
        lease_dir=tmp_path / "locks",
        retry_policy=policy,
        fetcher_factory=CapturingFetcher,
        uploader_factory=CapturingUploader,
    )

    assert result == main_module.ArchiveRunStatus.COMPLETED
    assert seen == {"uploader_policy": policy, "fetcher_policy": policy}
