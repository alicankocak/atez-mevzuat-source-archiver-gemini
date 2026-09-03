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
        "doc-01/manifest.json": b'{"document_id": "doc-01"}',
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
        "index_file": _source_file("rg-33299/index.html", "daily_index"),
        "documents": [
            {
                "document_id": "doc-01",
                "title": "Test Tebliği",
                "source_url": "https://www.resmigazete.gov.tr/source.pdf",
                "main_document": _source_file(
                    "rg-33299/doc-01/source.pdf", "main_document", "doc-01"
                ),
                "attachments": [
                    _source_file(
                        "rg-33299/doc-01/attachments/ek-1.pdf",
                        "attachment",
                        "doc-01",
                    )
                ],
            }
        ],
    }
    (rg_dir / "source-manifest.json").write_text(
        json.dumps(source_manifest), encoding="utf-8"
    )
    files["source-manifest.json"] = (rg_dir / "source-manifest.json").read_bytes()
    (rg_dir / "_READY.json").write_text("stale", encoding="utf-8")
    uploader.rg_dir = rg_dir
    return SimpleNamespace(rg_dir=rg_dir, relative_paths=set(files))


def _source_file(relative_path, role, parent_document_id=None):
    return {
        "source_url": f"https://www.resmigazete.gov.tr/{Path(relative_path).name}",
        "final_url": f"https://www.resmigazete.gov.tr/{Path(relative_path).name}",
        "http_status": 200,
        "content_type": "application/octet-stream",
        "size_bytes": 1,
        "sha256": "a" * 64,
        "role": role,
        "parent_document_id": parent_document_id,
        "local_relative_path": relative_path,
    }


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
                    "rg-33299/index.html", "daily_index"
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
