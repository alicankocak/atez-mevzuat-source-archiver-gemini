import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.drive_uploader import DriveUploader, DriveVerificationError


class RecordingUploader(DriveUploader):
    def __init__(self):
        self.service = object()
        self.upload_names = []
        self.uploaded_bytes = {}
        self.fail_verification_for = None
        self._folder_ids = {}

    def find_or_create_folder(self, folder_name: str, parent_id: str) -> str:
        key = (parent_id, folder_name)
        return self._folder_ids.setdefault(key, f"folder-{len(self._folder_ids) + 1}")

    def upload_file(self, local_file_path: Path, parent_folder_id: str):
        relative_path = self._relative_path(local_file_path)
        self.upload_names.append(local_file_path.name)
        drive_file_id = f"file-{len(self.upload_names)}"
        self.uploaded_bytes[drive_file_id] = (
            relative_path,
            local_file_path.read_bytes(),
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
    }
    for relative_path, content in files.items():
        path = rg_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (rg_dir / "_READY.json").write_text("stale", encoding="utf-8")
    uploader.rg_dir = rg_dir
    return SimpleNamespace(rg_dir=rg_dir, relative_paths=set(files))


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
