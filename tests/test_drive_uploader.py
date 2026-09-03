import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.drive_uploader import DriveUploader, DriveVerificationError
from src.models import ReadyGate


class RecordingUploader(DriveUploader):
    def __init__(self):
        self.service = object()
        self.upload_names = []
        self.uploaded_bytes = {}
        self.fail_verification_for = None
        self._folder_ids = {}
        self.remote_children = {}
        self.trashed_ids = []
        self.events = []

    def find_or_create_folder(self, folder_name: str, parent_id: str) -> str:
        for item in self.remote_children.get(parent_id, []):
            if (
                item["name"] == folder_name
                and item["mimeType"] == "application/vnd.google-apps.folder"
                and not item.get("trashed", False)
            ):
                return item["id"]
        key = (parent_id, folder_name)
        folder_id = self._folder_ids.setdefault(
            key, f"folder-{len(self._folder_ids) + 1}"
        )
        self.remote_children.setdefault(parent_id, []).append(
            {
                "id": folder_id,
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder",
            }
        )
        return folder_id

    def _list_children(self, parent_folder_id: str):
        return [
            dict(item)
            for item in self.remote_children.get(parent_folder_id, [])
            if not item.get("trashed", False)
        ]

    def _trash_item(self, drive_file_id: str) -> None:
        self.trashed_ids.append(drive_file_id)
        self.events.append(("trash", drive_file_id))
        for children in self.remote_children.values():
            for item in children:
                if item["id"] == drive_file_id:
                    item["trashed"] = True

    def upload_file(self, local_file_path: Path, parent_folder_id: str):
        relative_path = self._relative_path(local_file_path)
        self.upload_names.append(local_file_path.name)
        self.events.append(("upload", relative_path))
        drive_file_id = f"file-{len(self.upload_names)}"
        self.uploaded_bytes[drive_file_id] = (
            relative_path,
            local_file_path.read_bytes(),
        )
        self.remote_children.setdefault(parent_folder_id, []).append(
            {
                "id": drive_file_id,
                "name": local_file_path.name,
                "mimeType": "application/octet-stream",
            }
        )
        return drive_file_id, f"https://drive.example/{drive_file_id}"

    def verify_file_hash(
        self, drive_file_id: str, expected_sha256: str, expected_size: int
    ) -> bool:
        relative_path, content = self.uploaded_bytes[drive_file_id]
        if relative_path == self.fail_verification_for:
            return False
        return (
            len(content) == expected_size
            and hashlib.sha256(content).hexdigest() == expected_sha256
        )

    def _relative_path(self, local_file_path: Path) -> str:
        if local_file_path.name == "_READY.json":
            return "_READY.json"
        return local_file_path.relative_to(self.rg_dir).as_posix()


class PageCall:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class PaginatedFiles:
    def __init__(self, pages):
        self.pages = pages
        self.page_tokens = []

    def list(self, **kwargs):
        page_token = kwargs.get("pageToken")
        self.page_tokens.append(page_token)
        return PageCall(self.pages[page_token])


class PaginatedService:
    def __init__(self, pages):
        self._files = PaginatedFiles(pages)

    def files(self):
        return self._files


@pytest.fixture
def uploader():
    return RecordingUploader()


@pytest.fixture
def archive_tree(tmp_path, uploader):
    rg_dir = tmp_path / "2026-08-14" / "sources" / "rg-33299"
    files = {
        "index.html": b"gazette index",
        "doc-01/source.pdf": b"main document",
        "doc-01/attachments/ek-1.pdf": b"nested attachment",
    }
    for relative_path, content in files.items():
        path = rg_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    source_manifest = {
        "schema_version": 1,
        "report_date": "2026-08-14",
        "resmi_gazete_sayisi": "33299",
        "fihrist_url": "https://www.resmigazete.gov.tr/fihrist",
        "index_file": _source_file(
            "rg-33299/index.html", "daily_index", files["index.html"]
        ),
        "documents": [
            {
                "document_id": "doc-01",
                "title": "Test Tebliği",
                "source_url": "https://www.resmigazete.gov.tr/source.pdf",
                "main_document": _source_file(
                    "rg-33299/doc-01/source.pdf",
                    "main_document",
                    files["doc-01/source.pdf"],
                    "doc-01",
                ),
                "attachments": [
                    _source_file(
                        "rg-33299/doc-01/attachments/ek-1.pdf",
                        "attachment",
                        files["doc-01/attachments/ek-1.pdf"],
                        "doc-01",
                    )
                ],
            }
        ],
    }
    (rg_dir / "doc-01" / "manifest.json").write_text(
        json.dumps(source_manifest["documents"][0]), encoding="utf-8"
    )
    files["doc-01/manifest.json"] = (
        rg_dir / "doc-01" / "manifest.json"
    ).read_bytes()
    (rg_dir / "source-manifest.json").write_text(
        json.dumps(source_manifest), encoding="utf-8"
    )
    files["source-manifest.json"] = (rg_dir / "source-manifest.json").read_bytes()
    (rg_dir / "_READY.json").write_text("stale", encoding="utf-8")
    uploader.rg_dir = rg_dir
    return SimpleNamespace(rg_dir=rg_dir, relative_paths=set(files))


def _source_file(relative_path, role, content, parent_document_id=None):
    return {
        "source_url": f"https://www.resmigazete.gov.tr/{Path(relative_path).name}",
        "final_url": f"https://www.resmigazete.gov.tr/{Path(relative_path).name}",
        "http_status": 200,
        "content_type": "application/octet-stream",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "downloaded_at": "2026-09-03T00:00:00+00:00",
        "role": role,
        "parent_document_id": parent_document_id,
        "local_relative_path": relative_path,
    }


def _read_source_manifest(rg_dir):
    return json.loads((rg_dir / "source-manifest.json").read_text(encoding="utf-8"))


def _write_source_manifest(rg_dir, manifest, *, sync_documents=True):
    (rg_dir / "source-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    if sync_documents:
        for document in manifest["documents"]:
            (rg_dir / document["document_id"] / "manifest.json").write_text(
                json.dumps(document), encoding="utf-8"
            )


def test_ready_contains_every_verified_file(uploader, archive_tree):
    gate, ready_file_id = uploader.upload_rg_source_tree(
        archive_tree.rg_dir, "sources-id"
    )

    assert {item.relative_path for item in gate.files} == archive_tree.relative_paths
    assert all(item.drive_file_id for item in gate.files)
    assert ready_file_id


def test_ready_is_last_upload(uploader, archive_tree):
    uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")

    assert uploader.upload_names[-1] == "_READY.json"


def test_verification_failure_never_uploads_ready(uploader, archive_tree):
    uploader.fail_verification_for = "doc-01/source.pdf"

    with pytest.raises(DriveVerificationError):
        uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")

    assert "_READY.json" not in uploader.upload_names


@pytest.mark.parametrize(
    "gate_data",
    [
        {
            "report_date": "2026-08-14",
            "resmi_gazete_sayisi": "33299",
            "total_files_count": 1,
        },
        {
            "report_date": "2026-08-14",
            "resmi_gazete_sayisi": "33299",
            "total_files_count": 0,
            "files": [],
        },
        {
            "report_date": "2026-08-14",
            "resmi_gazete_sayisi": "33299",
            "total_files_count": 2,
            "files": [
                {
                    "relative_path": "index.html",
                    "drive_file_id": "source-1",
                    "size_bytes": 13,
                    "sha256": "a" * 64,
                }
            ],
        },
    ],
)
def test_ready_gate_rejects_incomplete_inventory(gate_data):
    with pytest.raises(ValidationError):
        ReadyGate.model_validate(gate_data)


def test_existing_ready_is_trashed_before_reused_files_are_uploaded(
    uploader, archive_tree
):
    uploader.remote_children["sources-id"] = [
        {
            "id": "rg-existing",
            "name": "rg-33299",
            "mimeType": "application/vnd.google-apps.folder",
        }
    ]
    uploader.remote_children["rg-existing"] = [
        {
            "id": "old-ready",
            "name": "_READY.json",
            "mimeType": "application/json",
        },
        {
            "id": "old-index",
            "name": "index.html",
            "mimeType": "text/html",
        },
    ]
    uploader.fail_verification_for = "doc-01/source.pdf"

    with pytest.raises(DriveVerificationError):
        uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")

    first_upload = next(
        index for index, event in enumerate(uploader.events) if event[0] == "upload"
    )
    assert uploader.events.index(("trash", "old-ready")) < first_upload
    assert "old-ready" in uploader.trashed_ids
    assert "_READY.json" not in uploader.upload_names


def test_stale_remote_files_and_folders_are_trashed_before_new_ready(
    uploader, archive_tree
):
    uploader.remote_children["sources-id"] = [
        {
            "id": "rg-existing",
            "name": "rg-33299",
            "mimeType": "application/vnd.google-apps.folder",
        }
    ]
    uploader.remote_children["rg-existing"] = [
        {
            "id": "old-ready",
            "name": "_READY.json",
            "mimeType": "application/json",
        },
        {
            "id": "stale-file",
            "name": ".DS_Store",
            "mimeType": "application/octet-stream",
        },
        {
            "id": "obsolete-folder",
            "name": "doc-99",
            "mimeType": "application/vnd.google-apps.folder",
        },
    ]

    gate, _ = uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")

    assert {"old-ready", "stale-file", "obsolete-folder"}.issubset(
        uploader.trashed_ids
    )
    assert {item.relative_path for item in gate.files} == archive_tree.relative_paths
    assert uploader.events[-1][0:2] == ("upload", "_READY.json")


def test_ready_on_later_drive_page_is_invalidated(uploader):
    service = PaginatedService(
        {
            None: {
                "files": [
                    {
                        "id": "first-page",
                        "name": "index.html",
                        "mimeType": "text/html",
                    }
                ],
                "nextPageToken": "page-2",
            },
            "page-2": {
                "files": [
                    {
                        "id": "second-page",
                        "name": "_READY.json",
                        "mimeType": "application/json",
                    }
                ]
            },
        }
    )
    uploader.service = service
    uploader._list_children = DriveUploader._list_children.__get__(
        uploader, RecordingUploader
    )

    uploader._invalidate_existing_ready("rg-id")

    assert uploader.trashed_ids == ["second-page"]
    assert service._files.page_tokens == [None, "page-2"]


def test_duplicate_rg_folders_are_invalidated_before_primary_upload(
    uploader, archive_tree
):
    uploader.remote_children["sources-id"] = [
        {
            "id": "rg-primary",
            "name": "rg-33299",
            "mimeType": "application/vnd.google-apps.folder",
        },
        {
            "id": "rg-duplicate",
            "name": "rg-33299",
            "mimeType": "application/vnd.google-apps.folder",
        },
    ]
    uploader.remote_children["rg-primary"] = [
        {
            "id": "ready-primary",
            "name": "_READY.json",
            "mimeType": "application/json",
        }
    ]
    uploader.remote_children["rg-duplicate"] = [
        {
            "id": "ready-duplicate",
            "name": "_READY.json",
            "mimeType": "application/json",
        }
    ]

    uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")

    first_upload = next(
        index for index, event in enumerate(uploader.events) if event[0] == "upload"
    )
    assert uploader.events.index(("trash", "ready-primary")) < first_upload
    assert uploader.events.index(("trash", "ready-duplicate")) < first_upload
    assert "rg-duplicate" in uploader.trashed_ids


def test_undeclared_local_artifact_is_rejected(uploader, archive_tree):
    (archive_tree.rg_dir / ".DS_Store").write_bytes(b"stale metadata")

    with pytest.raises(RuntimeError, match="undeclared"):
        uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")

    assert uploader.upload_names == []


def test_declared_symlink_is_rejected(uploader, archive_tree, tmp_path):
    outside_file = tmp_path / "outside.pdf"
    outside_file.write_bytes(b"outside archive")
    declared_file = archive_tree.rg_dir / "doc-01" / "source.pdf"
    declared_file.unlink()
    declared_file.symlink_to(outside_file)

    with pytest.raises(RuntimeError, match="symlink"):
        uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")

    assert uploader.upload_names == []


def test_manifest_path_escape_is_rejected(uploader, archive_tree):
    manifest_path = archive_tree.rg_dir / "source-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["documents"][0]["main_document"]["local_relative_path"] = (
        "rg-33299/../outside.pdf"
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="path"):
        uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")

    assert uploader.upload_names == []


def test_missing_document_manifest_is_rejected(uploader, archive_tree):
    (archive_tree.rg_dir / "doc-01" / "manifest.json").unlink()

    with pytest.raises(RuntimeError, match="missing"):
        uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")

    assert uploader.upload_names == []


def test_zero_teblig_archive_publishes_control_files(uploader, tmp_path):
    rg_dir = tmp_path / "2026-08-14" / "sources" / "rg-33299"
    rg_dir.mkdir(parents=True)
    (rg_dir / "index.html").write_bytes(b"gazette without teblig")
    (rg_dir / "source-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "report_date": "2026-08-14",
                "resmi_gazete_sayisi": "33299",
                "fihrist_url": "https://www.resmigazete.gov.tr/fihrist",
                "index_file": _source_file(
                    "rg-33299/index.html",
                    "daily_index",
                    b"gazette without teblig",
                ),
                "documents": [],
            }
        ),
        encoding="utf-8",
    )
    uploader.rg_dir = rg_dir

    gate, _ = uploader.upload_rg_source_tree(rg_dir, "sources-id")

    assert {item.relative_path for item in gate.files} == {
        "index.html",
        "source-manifest.json",
    }
    assert gate.total_files_count == 2


@pytest.mark.parametrize(
    "field,value,error_fragment",
    [
        ("report_date", "2026-08-13", "report_date"),
        ("resmi_gazete_sayisi", "99999", "issue"),
    ],
)
def test_archive_identity_must_match_its_date_and_issue_path(
    uploader, archive_tree, field, value, error_fragment
):
    manifest = _read_source_manifest(archive_tree.rg_dir)
    manifest[field] = value
    _write_source_manifest(archive_tree.rg_dir, manifest)

    with pytest.raises(RuntimeError, match=error_fragment):
        uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")

    assert uploader.upload_names == []


@pytest.mark.parametrize(
    "target,field,url",
    [
        ("index", "source_url", "http://www.resmigazete.gov.tr/index.html"),
        ("index", "final_url", "https://example.com/index.html"),
        ("document", "source_url", "https://example.com/document"),
        ("main", "source_url", "http://resmigazete.gov.tr/source.pdf"),
        ("main", "final_url", "https://example.com/source.pdf"),
        ("attachment", "source_url", "https://example.com/ek-1.pdf"),
        ("attachment", "final_url", "http://resmigazete.gov.tr/ek-1.pdf"),
    ],
)
def test_every_manifest_url_must_be_https_and_official(
    uploader, archive_tree, target, field, url
):
    manifest = _read_source_manifest(archive_tree.rg_dir)
    if target == "index":
        record = manifest["index_file"]
    elif target == "document":
        record = manifest["documents"][0]
    elif target == "main":
        record = manifest["documents"][0]["main_document"]
    else:
        record = manifest["documents"][0]["attachments"][0]
    record[field] = url
    _write_source_manifest(archive_tree.rg_dir, manifest)

    with pytest.raises(RuntimeError, match="official"):
        uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")

    assert uploader.upload_names == []


@pytest.mark.parametrize(
    "replacement,error_fragment",
    [
        (b"main document changed length", "size"),
        (b"other content", "sha256"),
    ],
)
def test_declared_payload_must_match_recorded_bytes(
    uploader, archive_tree, replacement, error_fragment
):
    (archive_tree.rg_dir / "doc-01" / "source.pdf").write_bytes(replacement)

    with pytest.raises(RuntimeError, match=error_fragment):
        uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")

    assert uploader.upload_names == []


@pytest.mark.parametrize(
    "target,field,value,error_fragment",
    [
        ("main", "role", "attachment", "role"),
        ("attachment", "parent_document_id", "doc-99", "parent"),
    ],
)
def test_declared_payload_role_and_parent_must_match_document(
    uploader, archive_tree, target, field, value, error_fragment
):
    manifest = _read_source_manifest(archive_tree.rg_dir)
    document = manifest["documents"][0]
    record = (
        document["main_document"]
        if target == "main"
        else document["attachments"][0]
    )
    record[field] = value
    _write_source_manifest(archive_tree.rg_dir, manifest)

    with pytest.raises(RuntimeError, match=error_fragment):
        uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")

    assert uploader.upload_names == []


def test_document_manifest_must_match_source_manifest(uploader, archive_tree):
    document_manifest_path = archive_tree.rg_dir / "doc-01" / "manifest.json"
    document_manifest = json.loads(document_manifest_path.read_text(encoding="utf-8"))
    document_manifest["title"] = "Stale title"
    document_manifest_path.write_text(
        json.dumps(document_manifest), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="document manifest"):
        uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")

    assert uploader.upload_names == []


def test_two_source_records_cannot_declare_the_same_local_path(
    uploader, archive_tree
):
    manifest = _read_source_manifest(archive_tree.rg_dir)
    main_record = manifest["documents"][0]["main_document"]
    attachment_record = manifest["documents"][0]["attachments"][0]
    attachment_record["local_relative_path"] = main_record["local_relative_path"]
    attachment_record["size_bytes"] = main_record["size_bytes"]
    attachment_record["sha256"] = main_record["sha256"]
    (archive_tree.rg_dir / "doc-01" / "attachments" / "ek-1.pdf").unlink()
    (archive_tree.rg_dir / "doc-01" / "attachments").rmdir()
    _write_source_manifest(archive_tree.rg_dir, manifest)

    with pytest.raises(RuntimeError, match="duplicate"):
        uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")

    assert uploader.upload_names == []
