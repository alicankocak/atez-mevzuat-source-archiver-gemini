import os
import io
import hashlib
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple, List
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from google.auth.transport.requests import Request

from src.config import (
    DRIVE_ROOT_FOLDER_ID,
    DRIVE_ROOT_FOLDER_NAME,
    SUBFOLDERS,
)
from src.models import ReadyFile, ReadyGate

logger = logging.getLogger("atez.drive")


class DriveVerificationError(RuntimeError):
    pass


class DriveUploader:
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

        rg_folder_name = local_rg_dir.name
        drive_rg_folder_id = self.find_or_create_folder(rg_folder_name, sources_folder_id)

        uploaded_files: List[Tuple[Path, Path, str]] = []

        # 1. Upload all items in rg directory
        def upload_directory(local_dir: Path, drive_folder_id: str) -> None:
            for item in sorted(local_dir.iterdir(), key=lambda path: path.name):
                if item.is_file():
                    if item.name == "_READY.json":
                        continue
                    file_id, _ = self.upload_file(item, drive_folder_id)
                    uploaded_files.append(
                        (item, item.relative_to(local_rg_dir), file_id)
                    )
                elif item.is_dir():
                    sub_drive_id = self.find_or_create_folder(
                        item.name, drive_folder_id
                    )
                    upload_directory(item, sub_drive_id)

        upload_directory(local_rg_dir, drive_rg_folder_id)

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
