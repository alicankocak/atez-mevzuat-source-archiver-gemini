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

from src.browser_transport import UnsafeSourceUrl, validate_official_url
from src.config import (
    DRIVE_ROOT_FOLDER_ID,
    DRIVE_ROOT_FOLDER_NAME,
    SUBFOLDERS,
)
from src.models import (
    DocumentItem,
    FileManifest,
    ReadyFile,
    ReadyGate,
    SourceManifest,
)
from src.retry_policy import RetryPolicy, is_retryable_drive_error

logger = logging.getLogger("atez.drive")


class DriveVerificationError(RuntimeError):
    pass


class ArchiveValidationError(RuntimeError):
    pass


class DriveUploader:
    FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

    def __init__(
        self,
        root_folder_id: Optional[str] = None,
        *,
        retry_policy: Optional[RetryPolicy] = None,
    ):
        self.root_folder_id = root_folder_id or DRIVE_ROOT_FOLDER_ID
        self.retry_policy = retry_policy or RetryPolicy()
        self.service = self._init_drive_service()

    def execute_with_retry(self, operation, *, operation_name: str):
        return self.retry_policy.run(
            operation,
            is_retryable=is_retryable_drive_error,
            operation_name=operation_name,
        )

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
        self.execute_with_retry(
            lambda: creds.refresh(Request()),
            operation_name="Drive credential refresh",
        )
        return self.execute_with_retry(
            lambda: build("drive", "v3", credentials=creds),
            operation_name="Drive service initialization",
        )

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
        folder_metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }

        def find_or_create():
            response = (
                self.service.files()
                .list(q=query, spaces="drive", fields="files(id, name)")
                .execute()
            )
            files = response.get("files", [])
            if files:
                return files[0]["id"], False
            folder = (
                self.service.files()
                .create(body=folder_metadata, fields="id")
                .execute()
            )
            return folder.get("id"), True

        folder_id, created = self.execute_with_retry(
            find_or_create,
            operation_name=f"find or create Drive folder {folder_name}",
        )
        if created:
            logger.info(f"Drive klasörü oluşturuldu: {folder_name} (ID: {folder_id})")
        return folder_id

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

        def upsert_file():
            existing = (
                self.service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="files(id, name, webViewLink)",
                )
                .execute()
                .get("files", [])
            )
            if existing:
                file_id = existing[0]["id"]
                updated = (
                    self.service.files()
                    .update(
                        fileId=file_id,
                        media_body=MediaFileUpload(
                            str(local_file_path), resumable=True
                        ),
                        fields="id, webViewLink",
                    )
                    .execute()
                )
                return updated, True
            file_metadata = {
                "name": filename,
                "parents": [parent_folder_id],
            }
            created = (
                self.service.files()
                .create(
                    body=file_metadata,
                    media_body=MediaFileUpload(
                        str(local_file_path), resumable=True
                    ),
                    fields="id, webViewLink",
                )
                .execute()
            )
            return created, False

        uploaded, updated_existing = self.execute_with_retry(
            upsert_file,
            operation_name=f"upload Drive file {filename}",
        )
        if updated_existing:
            logger.info(f"Drive dosyası güncellendi: {filename} ({uploaded.get('id')})")
        else:
            logger.info(f"Drive dosyası yüklendi: {filename} ({uploaded.get('id')})")
        return uploaded.get("id"), uploaded.get("webViewLink", "")

    def verify_file_hash(self, drive_file_id: str, expected_sha256: str, expected_size: int) -> bool:
        """
        Downloads the file stream from Drive to verify SHA-256 and size.
        """
        if not self.service:
            return False

        def download_bytes() -> bytes:
            request = self.service.files().get_media(fileId=drive_file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return fh.getvalue()

        content = self.execute_with_retry(
            download_bytes,
            operation_name=f"download Drive file {drive_file_id} for verification",
        )
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
        children = []
        page_token = None
        while True:
            request_args = {
                "q": f"'{parent_folder_id}' in parents and trashed = false",
                "spaces": "drive",
                "fields": "nextPageToken, files(id, name, mimeType)",
            }
            if page_token:
                request_args["pageToken"] = page_token
            response = self.execute_with_retry(
                lambda: self.service.files().list(**request_args).execute(),
                operation_name=f"list Drive children of {parent_folder_id}",
            )
            children.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return children

    def _trash_item(self, drive_file_id: str) -> None:
        self.execute_with_retry(
            lambda: self.service.files()
            .update(fileId=drive_file_id, body={"trashed": True})
            .execute(),
            operation_name=f"trash Drive item {drive_file_id}",
        )

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

    @staticmethod
    def _require_official_url(url: str) -> None:
        try:
            validate_official_url(url)
        except UnsafeSourceUrl as error:
            raise ArchiveValidationError(
                f"manifest URL is not an official HTTPS source: {url}"
            ) from error

    def _validate_file_declaration(
        self,
        local_rg_dir: Path,
        file_manifest: FileManifest,
        *,
        expected_role: str,
        expected_parent: Optional[str],
    ) -> Path:
        if file_manifest.role != expected_role:
            raise ArchiveValidationError(
                f"invalid {expected_role} role: {file_manifest.role}"
            )
        if file_manifest.parent_document_id != expected_parent:
            raise ArchiveValidationError(
                f"invalid {expected_role} parent_document_id"
            )
        self._require_official_url(file_manifest.source_url)
        self._require_official_url(file_manifest.final_url)
        relative_path = self._manifest_relative_path(local_rg_dir, file_manifest)
        if expected_parent and (
            not relative_path.parts or relative_path.parts[0] != expected_parent
        ):
            raise ArchiveValidationError(
                f"invalid {expected_role} path association"
            )
        return relative_path

    @staticmethod
    def _validate_declared_bytes(
        local_file: Path, relative_path: Path, file_manifest: FileManifest
    ) -> None:
        content = local_file.read_bytes()
        if len(content) != file_manifest.size_bytes:
            raise ArchiveValidationError(
                f"declared size mismatch: {relative_path.as_posix()}"
            )
        if hashlib.sha256(content).hexdigest() != file_manifest.sha256:
            raise ArchiveValidationError(
                f"declared sha256 mismatch: {relative_path.as_posix()}"
            )

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

        expected_report_date = local_rg_dir.parent.parent.name
        if source_manifest.report_date != expected_report_date:
            raise ArchiveValidationError(
                "source manifest report_date does not match archive path"
            )
        if not local_rg_dir.name.startswith("rg-"):
            raise ArchiveValidationError("invalid issue folder name")
        expected_issue_number = local_rg_dir.name.removeprefix("rg-")
        if source_manifest.resmi_gazete_sayisi != expected_issue_number:
            raise ArchiveValidationError(
                "source manifest issue number does not match archive path"
            )
        self._require_official_url(source_manifest.fihrist_url)

        expected_files = {Path("index.html"), Path("source-manifest.json")}
        source_records = {}

        def add_source_record(
            relative_path: Path, file_manifest: FileManifest
        ) -> None:
            if relative_path in source_records:
                raise ArchiveValidationError(
                    f"duplicate declared source path: {relative_path.as_posix()}"
                )
            source_records[relative_path] = file_manifest

        if source_manifest.index_file is None:
            raise ArchiveValidationError("missing index file declaration")
        index_relative_path = self._validate_file_declaration(
            local_rg_dir,
            source_manifest.index_file,
            expected_role="daily_index",
            expected_parent=None,
        )
        if index_relative_path != Path("index.html"):
            raise ArchiveValidationError("invalid daily index path")
        add_source_record(index_relative_path, source_manifest.index_file)

        for document in source_manifest.documents:
            document_path = PurePosixPath(document.document_id)
            if (
                document_path.is_absolute()
                or len(document_path.parts) != 1
                or document_path.name in {"", ".", ".."}
            ):
                raise ArchiveValidationError("invalid document manifest path")
            self._require_official_url(document.source_url)
            expected_files.add(Path(document.document_id) / "manifest.json")
            if document.main_document is None:
                raise ArchiveValidationError("missing main document declaration")
            main_relative_path = self._validate_file_declaration(
                local_rg_dir,
                document.main_document,
                expected_role="main_document",
                expected_parent=document.document_id,
            )
            expected_files.add(main_relative_path)
            add_source_record(main_relative_path, document.main_document)
            for attachment in document.attachments:
                attachment_relative_path = self._validate_file_declaration(
                    local_rg_dir,
                    attachment,
                    expected_role="attachment",
                    expected_parent=document.document_id,
                )
                expected_files.add(attachment_relative_path)
                add_source_record(attachment_relative_path, attachment)

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

        for document in source_manifest.documents:
            document_manifest_path = (
                local_rg_dir / document.document_id / "manifest.json"
            )
            try:
                stored_document = DocumentItem.model_validate_json(
                    document_manifest_path.read_bytes()
                )
            except (ValidationError, ValueError) as error:
                raise ArchiveValidationError("invalid document manifest") from error
            if stored_document != document:
                raise ArchiveValidationError(
                    f"document manifest does not match source manifest: {document.document_id}"
                )

        for relative_path, file_manifest in source_records.items():
            self._validate_declared_bytes(
                local_rg_dir / relative_path, relative_path, file_manifest
            )
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
        matching_rg_folders = [
            item
            for item in self._list_children(sources_folder_id)
            if item.get("name") == rg_folder_name
            and item.get("mimeType") == self.FOLDER_MIME_TYPE
        ]
        if matching_rg_folders:
            drive_rg_folder_id = matching_rg_folders[0]["id"]
        else:
            drive_rg_folder_id = self.find_or_create_folder(
                rg_folder_name, sources_folder_id
            )
        expected_files = set(declared_files)
        expected_directories: Set[Path] = set()
        for relative_path in expected_files:
            expected_directories.update(relative_path.parents)
        expected_directories.discard(Path("."))

        for folder in matching_rg_folders:
            self._invalidate_existing_ready(folder["id"])
        for duplicate_folder in matching_rg_folders[1:]:
            self._trash_item(duplicate_folder["id"])
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
