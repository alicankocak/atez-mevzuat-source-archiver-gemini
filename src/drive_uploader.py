import os
import io
import hashlib
import logging
from pathlib import Path, PurePosixPath
from typing import Dict, Optional, Tuple, List, Set
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from google.auth.transport.requests import Request
from pydantic import ValidationError

from src.config import (
    DRIVE_ROOT_FOLDER_ID,
    DRIVE_ROOT_FOLDER_NAME,
    SUBFOLDERS,
)
from src.models import FileManifest, ReadyFile, ReadyGate, SourceManifest

logger = logging.getLogger("atez.drive")


class DriveVerificationError(RuntimeError):
    pass


class ArchiveValidationError(RuntimeError):
    pass


class DriveUploader:
    FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

    def __init__(self, root_folder_id: Optional[str] = None):
        self.root_folder_id = root_folder_id or DRIVE_ROOT_FOLDER_ID
        self.service = self._init_drive_service()

    def _init_drive_service(self):
        client_id = os.getenv("GOOGLE_CLIENT_ID")
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
        refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN")

        if not (client_id and client_secret and refresh_token):
            logger.warning(
                "Google Drive kimlik bilgileri eksik (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN). "
                "Drive yükleme pasif modda çalışacaktır."
            )
            return None

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        creds.refresh(Request())
        return build("drive", "v3", credentials=creds)

    def find_or_create_folder(self, folder_name: str, parent_id: str) -> str:
        """Finds a folder under parent_id, or creates it if not found."""
        if not self.service:
            raise RuntimeError("Drive servisi başlatılamadı.")

        query = (
            f"name = '{folder_name}' and "
            f"'{parent_id}' in parents and "
            f"mimeType = 'application/vnd.google-apps.folder' and "
            f"trashed = false"
        )
        response = self.service.files().list(q=query, spaces="drive", fields="files(id, name)").execute()
        files = response.get("files", [])

        if files:
            return files[0]["id"]

        folder_metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = self.service.files().create(body=folder_metadata, fields="id").execute()
        logger.info(f"Drive klasörü oluşturuldu: {folder_name} (ID: {folder.get('id')})")
        return folder.get("id")

    def ensure_date_hierarchy(self, iso_date: str) -> Dict[str, str]:
        """
        Creates date folder and all 5 mandatory subfolders:
        requests, sources, analyses, reports, deliveries.
        Returns a map of subfolder_name -> folder_id
        """
        if not self.service:
            raise RuntimeError("Drive servisi bağlı değil.")

        date_folder_id = self.find_or_create_folder(iso_date, self.root_folder_id)
        subfolder_ids = {"date_root": date_folder_id}

        for sub in SUBFOLDERS:
            sub_id = self.find_or_create_folder(sub, date_folder_id)
            subfolder_ids[sub] = sub_id

        return subfolder_ids

    def upload_file(self, local_file_path: Path, parent_folder_id: str) -> Tuple[str, str]:
        """
        Uploads or updates a file in Drive.
        Returns (drive_file_id, web_view_link)
        """
        if not self.service:
            raise RuntimeError("Drive servisi bağlı değil.")

        filename = local_file_path.name
        query = (
            f"name = '{filename}' and "
            f"'{parent_folder_id}' in parents and "
            f"trashed = false"
        )
        existing = self.service.files().list(q=query, spaces="drive", fields="files(id, name)").execute().get("files", [])

        media = MediaFileUpload(str(local_file_path), resumable=True)

        if existing:
            file_id = existing[0]["id"]
            updated = self.service.files().update(
                fileId=file_id,
                media_body=media,
                fields="id, webViewLink",
            ).execute()
            logger.info(f"Drive dosyası güncellendi: {filename} ({file_id})")
            return updated.get("id"), updated.get("webViewLink", "")
        else:
            file_metadata = {
                "name": filename,
                "parents": [parent_folder_id],
            }
            created = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id, webViewLink",
            ).execute()
            logger.info(f"Drive dosyası yüklendi: {filename} ({created.get('id')})")
            return created.get("id"), created.get("webViewLink", "")

    def verify_file_hash(self, drive_file_id: str, expected_sha256: str, expected_size: int) -> bool:
        """
        Downloads the file stream from Drive to verify SHA-256 and size.
        """
        if not self.service:
            return False

        request = self.service.files().get_media(fileId=drive_file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        content = fh.getvalue()
        actual_size = len(content)
        actual_sha256 = hashlib.sha256(content).hexdigest()

        if actual_size != expected_size or actual_sha256 != expected_sha256:
            logger.error(
                f"Drive doğrulama başarısız ({drive_file_id})! "
                f"Boyut: beklenen={expected_size}, gerçek={actual_size}; "
                f"Hash: beklenen={expected_sha256}, gerçek={actual_sha256}"
            )
            return False

        return True

    def _list_children(self, parent_folder_id: str) -> List[Dict]:
        response = (
            self.service.files()
            .list(
                q=f"'{parent_folder_id}' in parents and trashed = false",
                spaces="drive",
                fields="files(id, name, mimeType)",
            )
            .execute()
        )
        return response.get("files", [])

    def _trash_item(self, drive_file_id: str) -> None:
        self.service.files().update(
            fileId=drive_file_id, body={"trashed": True}
        ).execute()

    @staticmethod
    def _manifest_relative_path(
        local_rg_dir: Path, file_manifest: FileManifest
    ) -> Path:
        raw_path = file_manifest.local_relative_path
        if not raw_path or "\\" in raw_path:
            raise ArchiveValidationError("invalid source manifest path")
        manifest_path = PurePosixPath(raw_path)
        if (
            manifest_path.is_absolute()
            or ".." in manifest_path.parts
            or "." in manifest_path.parts
            or not manifest_path.parts
            or manifest_path.parts[0] != local_rg_dir.name
        ):
            raise ArchiveValidationError("invalid source manifest path")
        relative_parts = manifest_path.parts[1:]
        if not relative_parts:
            raise ArchiveValidationError("invalid source manifest path")
        return Path(*relative_parts)

    def _collect_declared_files(self, local_rg_dir: Path) -> List[Path]:
        source_manifest_path = local_rg_dir / "source-manifest.json"
        if not source_manifest_path.exists():
            raise ArchiveValidationError("missing source-manifest.json")
        if source_manifest_path.is_symlink():
            raise ArchiveValidationError("source-manifest.json must not be a symlink")
        try:
            source_manifest = SourceManifest.model_validate_json(
                source_manifest_path.read_bytes()
            )
        except (ValidationError, ValueError) as error:
            raise ArchiveValidationError("invalid source-manifest.json") from error

        expected_files = {Path("index.html"), Path("source-manifest.json")}
        if source_manifest.index_file is None:
            raise ArchiveValidationError("missing index file declaration")
        if (
            self._manifest_relative_path(local_rg_dir, source_manifest.index_file)
            != Path("index.html")
        ):
            raise ArchiveValidationError("invalid daily index path")

        for document in source_manifest.documents:
            document_path = PurePosixPath(document.document_id)
            if (
                document_path.is_absolute()
                or len(document_path.parts) != 1
                or document_path.name in {"", ".", ".."}
            ):
                raise ArchiveValidationError("invalid document manifest path")
            expected_files.add(Path(document.document_id) / "manifest.json")
            if document.main_document is None:
                raise ArchiveValidationError("missing main document declaration")
            expected_files.add(
                self._manifest_relative_path(local_rg_dir, document.main_document)
            )
            for attachment in document.attachments:
                expected_files.add(
                    self._manifest_relative_path(local_rg_dir, attachment)
                )

        expected_directories: Set[Path] = set()
        for relative_path in expected_files:
            expected_directories.update(relative_path.parents)
        expected_directories.discard(Path("."))

        actual_files = set()
        for item in local_rg_dir.rglob("*"):
            relative_path = item.relative_to(local_rg_dir)
            if item.is_symlink():
                raise ArchiveValidationError(
                    f"symlink is not allowed: {relative_path.as_posix()}"
                )
            if item.is_dir():
                if relative_path not in expected_directories:
                    raise ArchiveValidationError(
                        f"undeclared local directory: {relative_path.as_posix()}"
                    )
                continue
            if relative_path == Path("_READY.json"):
                continue
            if not item.is_file() or relative_path not in expected_files:
                raise ArchiveValidationError(
                    f"undeclared local artifact: {relative_path.as_posix()}"
                )
            actual_files.add(relative_path)

        missing_files = expected_files - actual_files
        if missing_files:
            missing = ", ".join(path.as_posix() for path in sorted(missing_files))
            raise ArchiveValidationError(f"missing declared archive files: {missing}")
        return sorted(expected_files, key=lambda path: path.as_posix())

    def _invalidate_existing_ready(self, drive_rg_folder_id: str) -> None:
        for item in self._list_children(drive_rg_folder_id):
            if item.get("name") == "_READY.json":
                self._trash_item(item["id"])

    def _reconcile_remote_tree(
        self,
        drive_folder_id: str,
        expected_files: Set[Path],
        expected_directories: Set[Path],
        relative_directory: Path = Path("."),
    ) -> None:
        seen_names = set()
        for item in self._list_children(drive_folder_id):
            name = item.get("name", "")
            if name in seen_names:
                self._trash_item(item["id"])
                continue
            seen_names.add(name)
            relative_path = (
                Path(name)
                if relative_directory == Path(".")
                else relative_directory / name
            )
            if item.get("mimeType") == self.FOLDER_MIME_TYPE:
                if relative_path not in expected_directories:
                    self._trash_item(item["id"])
                    continue
                self._reconcile_remote_tree(
                    item["id"],
                    expected_files,
                    expected_directories,
                    relative_path,
                )
            elif relative_path not in expected_files:
                self._trash_item(item["id"])

    def upload_rg_source_tree(
        self, local_rg_dir: Path, sources_folder_id: str
    ) -> tuple[ReadyGate, str]:
        """
        Recursively uploads local rg directory to Drive sources/ folder.
        Verifies all uploaded files.
        Writes _READY.json upon full success.
        """
        if not self.service:
            raise RuntimeError("Drive servisi bağlı değil.")

        declared_files = self._collect_declared_files(local_rg_dir)
        rg_folder_name = local_rg_dir.name
        drive_rg_folder_id = self.find_or_create_folder(rg_folder_name, sources_folder_id)
        expected_files = set(declared_files)
        expected_directories: Set[Path] = set()
        for relative_path in expected_files:
            expected_directories.update(relative_path.parents)
        expected_directories.discard(Path("."))

        self._invalidate_existing_ready(drive_rg_folder_id)
        self._reconcile_remote_tree(
            drive_rg_folder_id, expected_files, expected_directories
        )

        uploaded_files: List[Tuple[Path, Path, str]] = []

        # 1. Upload all items in rg directory
        drive_folder_ids = {Path("."): drive_rg_folder_id}

        def ensure_drive_folder(relative_directory: Path) -> str:
            if relative_directory in drive_folder_ids:
                return drive_folder_ids[relative_directory]
            parent_id = ensure_drive_folder(relative_directory.parent)
            folder_id = self.find_or_create_folder(
                relative_directory.name, parent_id
            )
            drive_folder_ids[relative_directory] = folder_id
            return folder_id

        for relative_path in declared_files:
            local_file = local_rg_dir / relative_path
            parent_id = ensure_drive_folder(relative_path.parent)
            file_id, _ = self.upload_file(local_file, parent_id)
            uploaded_files.append((local_file, relative_path, file_id))

        # 2. Verify all files
        logger.info(f"Toplam {len(uploaded_files)} dosya Drive üzerinde doğrulanıyor...")
        ready_files = []
        for local_file, relative_path, drive_id in uploaded_files:
            content = local_file.read_bytes()
            expected_hash = hashlib.sha256(content).hexdigest()
            expected_size = len(content)

            if not self.verify_file_hash(drive_id, expected_hash, expected_size):
                raise DriveVerificationError(
                    f"DRIVE_WRITE_FAILED: {relative_path.as_posix()} doğrulaması başarısız oldu."
                )
            ready_files.append(
                ReadyFile(
                    relative_path=relative_path.as_posix(),
                    drive_file_id=drive_id,
                    size_bytes=expected_size,
                    sha256=expected_hash,
                )
            )

        logger.info("Tüm dosyalar SHA-256 ve boyut yönünden doğrulandı.")

        # 3. Create & upload _READY.json
        ready_gate = ReadyGate(
            status="READY",
            report_date=local_rg_dir.parent.parent.name,
            resmi_gazete_sayisi=rg_folder_name.replace("rg-", ""),
            total_files_count=len(uploaded_files),
            verified=True,
            files=ready_files,
        )
        ready_path = local_rg_dir / "_READY.json"
        with open(ready_path, "w", encoding="utf-8") as f:
            f.write(ready_gate.model_dump_json(indent=2))

        ready_file_id, _ = self.upload_file(ready_path, drive_rg_folder_id)
        logger.info(f"_READY.json başarıyla oluşturuldu ve yüklendi: {ready_path}")
        return ready_gate, ready_file_id
