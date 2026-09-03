import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import src.drive_watcher as drive_watcher_module
from src.browser_transport import BrowserResponse, RetryableTransportError
from src.drive_watcher import DriveRequestWatcher
from src.fetcher import MevzuatFetcher as RealMevzuatFetcher
from src.models import ReadyGate, SourceRequest
from src.retry_policy import RetryPolicy


REQUEST_PAYLOAD = {
    "schema_version": 1,
    "request_id": "req-123",
    "report_date": "2026-08-14",
    "requested_at": "2026-09-03T00:00:00Z",
    "requested_by": "atez-mevzuar-rapor-alcn",
}

READY_FILE = {
    "relative_path": "index.html",
    "drive_file_id": "source-1",
    "size_bytes": 13,
    "sha256": "a" * 64,
}


def ready_payload(**overrides):
    payload = {
        "schema_version": 1,
        "status": "READY",
        "report_date": "2026-08-14",
        "resmi_gazete_sayisi": "33299",
        "created_at": "2026-09-03T00:00:00Z",
        "total_files_count": 1,
        "verified": True,
        "files": [READY_FILE],
    }
    payload.update(overrides)
    return payload


class FakeCall:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeFiles:
    def __init__(self, drive):
        self.drive = drive

    def list(self, *, q, **kwargs):
        self.drive.queries.append(q)
        return FakeCall({"files": self.drive.list_files(q)})

    def get_media(self, *, fileId):
        return FakeCall(self.drive.media[fileId])

    def update(self, *, fileId, body=None, media_body=None, **kwargs):
        media = media_body.stream().getvalue() if media_body is not None else None
        update = {"file_id": fileId, "body": body or {}, "media": media}
        self.drive.updates.append(update)
        self.drive.events.append(("update", update["body"].get("name")))
        return FakeCall({"id": fileId})


class FakeDriveService:
    def __init__(self):
        self.events = []
        self.media = {}
        self.processing_files = []
        self.ready_enabled = False
        self.queries = []
        self.updates = []
        self._files = FakeFiles(self)

    def files(self):
        return self._files

    def list_files(self, query):
        if (
            "name contains 'PROCESSING_'" in query
            and "not name contains 'PROCESSING_'" not in query
        ):
            return self.processing_files
        if "name = '2026-08-14'" in query:
            return [{"id": "date-id", "name": "2026-08-14"}] if self.ready_enabled else []
        if "name = 'sources'" in query:
            return [{"id": "sources-id", "name": "sources"}]
        if "'sources-id' in parents" in query:
            return [{"id": "rg-id", "name": "rg-33299"}]
        if "name = '_READY.json'" in query:
            return [{"id": "ready-1", "name": "_READY.json"}]
        return []


class FakeUploader:
    def __init__(self, drive):
        self.service = drive
        self.root_folder_id = "root-id"
        self.drive = drive

    def find_or_create_folder(self, name, parent_id):
        assert (name, parent_id) == ("requests", "root-id")
        return "requests-id"

    def ensure_date_hierarchy(self, report_date):
        return {"sources": "sources-id"}

    def upload_rg_source_tree(self, rg_dir, sources_folder_id):
        self.drive.events.append(("upload", str(rg_dir)))
        self.drive.ready_enabled = True
        gate = ReadyGate.model_validate(ready_payload())
        self.drive.media["ready-1"] = gate.model_dump_json().encode("utf-8")
        return gate, "ready-1"


@pytest.fixture
def drive():
    return FakeDriveService()


@pytest.fixture
def watcher(monkeypatch, drive, tmp_path):
    uploader = FakeUploader(drive)
    monkeypatch.setattr(
        drive_watcher_module,
        "DriveUploader",
        lambda **_kwargs: uploader,
    )
    instance = DriveRequestWatcher(check_interval_seconds=0)
    instance.claim_lock_dir = tmp_path
    return instance


@pytest.fixture
def source_request():
    return SourceRequest.model_validate(REQUEST_PAYLOAD)


@pytest.fixture
def pending_file():
    return {
        "id": "file-1",
        "name": "SOURCE_REQUEST__2026-08-14__req-123.json",
    }


def test_request_requires_exact_schema_and_iso_date():
    request = SourceRequest.model_validate(
        {
            "schema_version": 1,
            "request_id": "req-123",
            "report_date": "2026-08-14",
            "requested_at": "2026-09-03T00:00:00Z",
            "requested_by": "atez-mevzuar-rapor-alcn",
        }
    )
    assert request.report_date.isoformat() == "2026-08-14"


def test_request_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        SourceRequest.model_validate(
            {
                "schema_version": 1,
                "request_id": "req-123",
                "report_date": "2026-08-14",
                "requested_at": "2026-09-03T00:00:00Z",
                "requested_by": "atez-mevzuar-rapor-alcn",
                "target_url": "https://example.com",
            }
        )


def test_request_parses_utf8_json_bytes():
    payload = json.dumps(REQUEST_PAYLOAD).encode("utf-8")

    request = SourceRequest.model_validate_json(payload)

    assert request.request_id == "req-123"
    assert request.report_date.isoformat() == "2026-08-14"
    assert request.requested_at.utcoffset().total_seconds() == 0


def test_request_accepts_explicit_rfc3339_utc_offset():
    payload = dict(REQUEST_PAYLOAD, requested_at="2026-09-03T00:00:00+00:00")

    request = SourceRequest.model_validate(payload)

    assert request.requested_at.utcoffset().total_seconds() == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("request_id", "   "),
        ("report_date", "14.08.2026"),
        ("requested_at", "2026-09-03T00:00:00"),
        ("requested_at", "2026-09-03T03:00:00+03:00"),
        ("requested_at", 0),
        ("request_id", "a"),
        ("request_id", "req-" + "a" * 129),
        ("requested_by", "another-client"),
    ],
)
def test_request_rejects_values_outside_the_strict_contract(field, value):
    payload = dict(REQUEST_PAYLOAD)
    payload[field] = value

    with pytest.raises(ValidationError):
        SourceRequest.model_validate(payload)


def test_claim_renames_before_fetch(watcher, drive, pending_file):
    claimed_name = watcher.claim_request(pending_file["id"], pending_file["name"])

    assert claimed_name.startswith("PROCESSING_")
    assert drive.updates[-1]["body"]["name"] == claimed_name


def test_success_replaces_json_and_renames_done(watcher, drive, source_request):
    watcher.complete_request(
        "file-1", source_request, rg_number="33299", ready_file_id="ready-1"
    )

    update = drive.updates[-1]
    assert update["body"]["name"].startswith("DONE_")
    result = json.loads(update["media"])
    assert result["status"] == "DONE"
    assert result["rg_number"] == "33299"
    assert result["ready_file_id"] == "ready-1"


def test_failure_records_error_and_does_not_reenter_pending(
    watcher, drive, source_request
):
    watcher.fail_request("file-1", source_request, "network unavailable")

    update = drive.updates[-1]
    assert update["body"]["name"].startswith("FAILED_")
    result = json.loads(update["media"])
    assert result["status"] == "FAILED"
    assert result["error"] == "network unavailable"

    watcher.list_pending_requests()
    pending_query = next(
        query
        for query in drive.queries
        if "not name contains 'PROCESSING_'" in query
    )
    assert "not name contains 'PROCESSING_'" in pending_query
    assert "not name contains 'DONE_'" in pending_query
    assert "not name contains 'FAILED_'" in pending_query


def test_processing_same_date_leaves_second_request_pending(
    monkeypatch, watcher, drive, pending_file
):
    drive.media["file-1"] = json.dumps(REQUEST_PAYLOAD).encode("utf-8")
    drive.processing_files = [
        {
            "id": "other-file",
            "name": "PROCESSING_SOURCE_REQUEST__2026-08-14__other.json",
            "appProperties": {
                "claimed_at": datetime.now(timezone.utc).isoformat()
            },
        }
    ]
    other_payload = dict(REQUEST_PAYLOAD, request_id="req-456")
    drive.media["other-file"] = json.dumps(other_payload).encode("utf-8")

    class UnexpectedFetcher:
        def __init__(self, **kwargs):
            raise AssertionError("a duplicate request must not start a collector")

    monkeypatch.setattr(drive_watcher_module, "MevzuatFetcher", UnexpectedFetcher)

    watcher.process_request(pending_file)

    assert drive.updates == []


def test_existing_ready_gate_is_reused_without_collecting(
    monkeypatch, watcher, drive, pending_file
):
    drive.media["file-1"] = json.dumps(REQUEST_PAYLOAD).encode("utf-8")
    drive.ready_enabled = True
    drive.media["ready-1"] = json.dumps(ready_payload()).encode("utf-8")

    class UnexpectedFetcher:
        def __init__(self, **kwargs):
            raise AssertionError("an existing READY gate must be reused")

    monkeypatch.setattr(drive_watcher_module, "MevzuatFetcher", UnexpectedFetcher)

    watcher.process_request(pending_file)

    assert drive.updates[0]["body"]["name"].startswith("PROCESSING_")
    result = json.loads(drive.updates[-1]["media"])
    assert result["status"] == "DONE"
    assert result["ready_file_id"] == "ready-1"


def test_process_uses_request_bytes_and_claims_before_fetch(
    monkeypatch, watcher, drive, pending_file, tmp_path
):
    pending_file["name"] = "SOURCE_REQUEST__2099-01-01__req-123.json"
    drive.media["file-1"] = json.dumps(REQUEST_PAYLOAD).encode("utf-8")

    class FakeFetcher:
        def __init__(self, date_str, retry_policy):
            assert date_str == "2026-08-14"
            assert retry_policy is watcher.retry_policy

        def run(self):
            drive.events.append(("fetch", "2026-08-14"))
            manifest = SimpleNamespace(resmi_gazete_sayisi="33299")
            return manifest, Path(tmp_path) / "rg-33299"

    monkeypatch.setattr(drive_watcher_module, "MevzuatFetcher", FakeFetcher)

    watcher.process_request(pending_file)

    event_names = [event[0] for event in drive.events]
    assert event_names.index("update") < event_names.index("fetch")
    claimed_name = drive.updates[0]["body"]["name"]
    assert "2026-08-14" in claimed_name
    assert "2099-01-01" not in claimed_name
    assert json.loads(drive.updates[-1]["media"])["status"] == "DONE"


def test_invalid_request_is_failed_without_date_inference(
    monkeypatch, watcher, drive, pending_file
):
    drive.media["file-1"] = b"archive 2026-08-14 please"

    class UnexpectedFetcher:
        def __init__(self, **kwargs):
            raise AssertionError("free text must not start a collector")

    monkeypatch.setattr(drive_watcher_module, "MevzuatFetcher", UnexpectedFetcher)

    watcher.process_request(pending_file)

    assert drive.updates[0]["body"]["name"].startswith("PROCESSING_")
    assert drive.updates[-1]["body"]["name"].startswith("FAILED_")
    result = json.loads(drive.updates[-1]["media"])
    assert result["status"] == "FAILED"
    assert "INVALID_REQUEST" in result["error"]


def test_incomplete_ready_gate_does_not_skip_collection(
    monkeypatch, watcher, drive, pending_file, tmp_path
):
    drive.media["file-1"] = json.dumps(REQUEST_PAYLOAD).encode("utf-8")
    drive.ready_enabled = True
    drive.media["ready-1"] = json.dumps(
        {"report_date": "2026-08-14", "total_files_count": 0}
    ).encode("utf-8")
    fetch_count = 0

    class FakeFetcher:
        def __init__(self, date_str, **_kwargs):
            assert date_str == "2026-08-14"

        def run(self):
            nonlocal fetch_count
            fetch_count += 1
            manifest = SimpleNamespace(resmi_gazete_sayisi="33299")
            return manifest, Path(tmp_path) / "rg-33299"

    monkeypatch.setattr(drive_watcher_module, "MevzuatFetcher", FakeFetcher)

    watcher.process_request(pending_file)

    assert fetch_count == 1
    assert json.loads(drive.updates[-1]["media"])["status"] == "DONE"


def test_count_only_legacy_ready_gate_is_rejected(watcher, drive):
    drive.ready_enabled = True
    legacy_gate = ready_payload()
    legacy_gate.pop("files")
    drive.media["ready-1"] = json.dumps(legacy_gate).encode("utf-8")

    assert watcher._find_ready_result("2026-08-14") is None


@pytest.mark.parametrize(
    "files,total_files_count",
    [
        ([READY_FILE], 2),
        ([dict(READY_FILE, drive_file_id="")], 1),
        ([dict(READY_FILE, size_bytes=-1)], 1),
        ([dict(READY_FILE, sha256="not-a-sha256")], 1),
    ],
)
def test_malformed_file_complete_ready_gate_is_rejected(
    watcher, drive, files, total_files_count
):
    drive.ready_enabled = True
    drive.media["ready-1"] = json.dumps(
        ready_payload(files=files, total_files_count=total_files_count)
    ).encode("utf-8")

    assert watcher._find_ready_result("2026-08-14") is None


def test_boolean_upload_result_fails_request(
    monkeypatch, watcher, drive, pending_file, tmp_path
):
    drive.media["file-1"] = json.dumps(REQUEST_PAYLOAD).encode("utf-8")

    class FakeFetcher:
        def __init__(self, date_str, **_kwargs):
            assert date_str == "2026-08-14"

        def run(self):
            return SimpleNamespace(resmi_gazete_sayisi="33299"), tmp_path / "rg-33299"

    monkeypatch.setattr(drive_watcher_module, "MevzuatFetcher", FakeFetcher)
    drive.media["ready-1"] = json.dumps(ready_payload()).encode("utf-8")

    def boolean_upload(*_args):
        drive.ready_enabled = True
        return True

    watcher.uploader.upload_rg_source_tree = boolean_upload

    watcher.process_request(pending_file)

    result = json.loads(drive.updates[-1]["media"])
    assert result["status"] == "FAILED"


def test_stale_processing_claim_returns_to_pending(watcher, drive):
    stale_file = {
        "id": "stale-file",
        "name": "PROCESSING_SOURCE_REQUEST__2099-01-01__wrong.json",
        "appProperties": {"claimed_at": "2000-01-01T00:00:00Z"},
    }
    drive.processing_files = [stale_file]
    drive.media["stale-file"] = json.dumps(REQUEST_PAYLOAD).encode("utf-8")

    pending = watcher.list_pending_requests()

    assert pending == [
        {
            "id": "stale-file",
            "name": "SOURCE_REQUEST__2026-08-14__req-123.json",
        }
    ]
    assert drive.updates[-1]["body"]["name"] == pending[0]["name"]


def test_stale_invalid_processing_claim_is_terminalized(watcher, drive):
    stale_file = {
        "id": "stale-invalid",
        "name": "PROCESSING_SOURCE_REQUEST__untrusted-name.json",
        "appProperties": {"claimed_at": "2000-01-01T00:00:00Z"},
    }
    drive.processing_files = [stale_file]
    drive.media["stale-invalid"] = b"archive 2026-08-14 please"

    pending = watcher.list_pending_requests()

    assert pending == []
    update = drive.updates[-1]
    assert update["file_id"] == "stale-invalid"
    assert update["body"]["name"] == "FAILED_SOURCE_REQUEST__untrusted-name.json"
    result = json.loads(update["media"])
    assert result["status"] == "FAILED"
    assert "INVALID_REQUEST" in result["error"]
    assert result["request_file_id"] == "stale-invalid"
    assert result["request_file_name"] == "SOURCE_REQUEST__untrusted-name.json"
    assert "request_id" not in result
    assert "report_date" not in result


def test_same_mac_watchers_cannot_collect_same_date_concurrently(
    monkeypatch, watcher, drive, tmp_path
):
    second_watcher = DriveRequestWatcher(check_interval_seconds=0)
    second_watcher.claim_lock_dir = watcher.claim_lock_dir
    first_file = {
        "id": "file-1",
        "name": "SOURCE_REQUEST__2026-08-14__req-123.json",
    }
    second_file = {
        "id": "file-2",
        "name": "SOURCE_REQUEST__2099-01-01__req-456.json",
    }
    drive.media["file-1"] = json.dumps(REQUEST_PAYLOAD).encode("utf-8")
    second_payload = dict(REQUEST_PAYLOAD, request_id="req-456")
    drive.media["file-2"] = json.dumps(second_payload).encode("utf-8")
    fetch_count = 0

    class ReentrantFetcher:
        def __init__(self, date_str, **_kwargs):
            assert date_str == "2026-08-14"

        def run(self):
            nonlocal fetch_count
            fetch_count += 1
            if fetch_count == 1:
                second_watcher.process_request(second_file)
            manifest = SimpleNamespace(resmi_gazete_sayisi="33299")
            return manifest, Path(tmp_path) / "rg-33299"

    monkeypatch.setattr(drive_watcher_module, "MevzuatFetcher", ReentrantFetcher)

    watcher.process_request(first_file)

    assert fetch_count == 1
    processing_updates = [
        update
        for update in drive.updates
        if update["body"].get("name", "").startswith("PROCESSING_")
    ]
    assert [update["file_id"] for update in processing_updates] == ["file-1"]


def test_watcher_writes_failed_only_after_retryable_source_attempts_exhaust(
    monkeypatch, watcher, drive, pending_file, tmp_path
):
    drive.media["file-1"] = json.dumps(REQUEST_PAYLOAD).encode("utf-8")
    fihrist_url = "https://resmigazete.gov.tr/14.08.2026"
    document_url = "https://resmigazete.gov.tr/document.html"
    fihrist_body = (
        "<!doctype html>"
        "<title>14 Ağustos 2026 Tarihli ve 33299 Sayılı Resmî Gazete</title>"
        "<span id='spanGazeteTarih'>33299 Sayılı Resmî Gazete</span>"
        "<h2 class='html-subtitle'>TEBLİĞLER</h2>"
        "<div class='fihrist-item mb-1'>"
        "<a href='/document.html'>Fixture Tebliğ</a>"
        "</div>"
    ).encode("utf-8")
    document_attempts = 0

    class Transport:
        def fetch(self, url):
            nonlocal document_attempts
            if url == fihrist_url:
                return BrowserResponse(200, url, "text/html", fihrist_body)
            assert url == document_url
            document_attempts += 1
            raise RetryableTransportError("temporary document timeout")

    delays = []
    watcher.retry_policy = RetryPolicy(
        max_attempts=3,
        initial_delay_seconds=0.1,
        backoff_multiplier=2,
        sleep=delays.append,
    )

    def fetcher_factory(date_str, retry_policy):
        return RealMevzuatFetcher(
            date_str,
            output_base_dir=tmp_path / "sources",
            transport=Transport(),
            retry_policy=retry_policy,
        )

    monkeypatch.setattr(drive_watcher_module, "MevzuatFetcher", fetcher_factory)

    watcher.process_request(pending_file)

    assert document_attempts == 3
    assert delays == [0.1, 0.2]
    assert not any(event[0] == "upload" for event in drive.events)
    result = json.loads(drive.updates[-1]["media"])
    assert result["status"] == "FAILED"
    assert "temporary document timeout" in result["error"]
