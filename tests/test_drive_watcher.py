import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import src.drive_watcher as drive_watcher_module
from src.drive_watcher import DriveRequestWatcher
from src.models import SourceRequest


REQUEST_PAYLOAD = {
    "schema_version": 1,
    "request_id": "req-123",
    "report_date": "2026-08-14",
    "requested_at": "2026-09-03T00:00:00Z",
    "requested_by": "atez-mevzuar-rapor-alcn",
}


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
        if "name contains 'PROCESSING_'" in query:
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
        self.drive.media["ready-1"] = json.dumps(
            {
                "schema_version": 1,
                "status": "READY",
                "report_date": "2026-08-14",
                "resmi_gazete_sayisi": "33299",
                "created_at": "2026-09-03T00:00:00Z",
                "total_files_count": 3,
                "verified": True,
            }
        ).encode("utf-8")
        return True


@pytest.fixture
def drive():
    return FakeDriveService()


@pytest.fixture
def watcher(monkeypatch, drive):
    uploader = FakeUploader(drive)
    monkeypatch.setattr(drive_watcher_module, "DriveUploader", lambda: uploader)
    return DriveRequestWatcher(check_interval_seconds=0)


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("request_id", "   "),
        ("report_date", "14.08.2026"),
        ("requested_at", "2026-09-03T00:00:00"),
        ("requested_at", "2026-09-03T03:00:00+03:00"),
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
    pending_query = drive.queries[-1]
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
        }
    ]

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
    drive.media["ready-1"] = json.dumps(
        {
            "schema_version": 1,
            "status": "READY",
            "report_date": "2026-08-14",
            "resmi_gazete_sayisi": "33299",
            "created_at": "2026-09-03T00:00:00Z",
            "total_files_count": 3,
            "verified": True,
        }
    ).encode("utf-8")

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
        def __init__(self, date_str):
            assert date_str == "2026-08-14"

        def run(self):
            drive.events.append(("fetch", "2026-08-14"))
            manifest = SimpleNamespace(resmi_gazete_sayisi="33299")
            return manifest, Path(tmp_path) / "rg-33299"

    monkeypatch.setattr(drive_watcher_module, "MevzuatFetcher", FakeFetcher)

    watcher.process_request(pending_file)

    event_names = [event[0] for event in drive.events]
    assert event_names.index("update") < event_names.index("fetch")
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
