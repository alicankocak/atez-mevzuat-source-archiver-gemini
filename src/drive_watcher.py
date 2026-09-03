import io
import json
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from googleapiclient.http import MediaIoBaseUpload
from pydantic import ValidationError

from src.drive_uploader import DriveUploader
from src.fetcher import MevzuatFetcher
from src.models import ReadyGate, SourceRequest, SourceRequestResult

logger = logging.getLogger("atez.watcher")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


class DriveRequestWatcher:
    def __init__(self, check_interval_seconds: int = 15):
        self.interval = check_interval_seconds
        self.uploader = DriveUploader()
        if not self.uploader.service:
            raise RuntimeError("Drive servisi başlatılamadı. .env dosyasını kontrol edin.")

        self.root_id = self.uploader.root_folder_id
        self.requests_folder_id = self.uploader.find_or_create_folder(
            "requests", self.root_id
        )
        logger.info(
            "Drive Request Watcher başlatıldı. Dinlenen klasör ID: %s",
            self.requests_folder_id,
        )

    def list_pending_requests(self) -> List[Dict]:
        """List strict request files that have not entered a terminal state."""
        query = (
            f"'{self.requests_folder_id}' in parents and "
            "mimeType != 'application/vnd.google-apps.folder' and "
            "trashed = false and "
            "name contains 'SOURCE_REQUEST__' and "
            "not name contains 'PROCESSING_' and "
            "not name contains 'DONE_' and "
            "not name contains 'FAILED_' and "
            "not name contains 'processed_'"
        )
        response = (
            self.uploader.service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id, name, createdTime)",
            )
            .execute()
        )
        return response.get("files", [])

    def load_request(self, file_id: str) -> SourceRequest:
        """Parse a request only from its UTF-8 JSON bytes."""
        content = self.uploader.service.files().get_media(fileId=file_id).execute()
        return SourceRequest.model_validate_json(content)

    def claim_request(self, file_id: str, old_name: str) -> str:
        """Expose ownership by changing the Drive file metadata."""
        claimed_name = f"PROCESSING_{old_name}"
        self.uploader.service.files().update(
            fileId=file_id,
            body={"name": claimed_name},
        ).execute()
        logger.info("Talep sahiplenildi: %s", claimed_name)
        return claimed_name

    @staticmethod
    def _terminal_name(prefix: str, request: SourceRequest) -> str:
        return (
            f"{prefix}_SOURCE_REQUEST__{request.report_date.isoformat()}__"
            f"{request.request_id}.json"
        )

    def _write_result(
        self, file_id: str, name: str, result: SourceRequestResult
    ) -> None:
        payload = result.model_dump_json(exclude_none=True, indent=2).encode("utf-8")
        media = MediaIoBaseUpload(
            io.BytesIO(payload), mimetype="application/json", resumable=False
        )
        self.uploader.service.files().update(
            fileId=file_id,
            body={"name": name},
            media_body=media,
        ).execute()

    def complete_request(
        self,
        file_id: str,
        request: SourceRequest,
        result: Optional[SourceRequestResult] = None,
        *,
        rg_number: Optional[str] = None,
        ready_file_id: Optional[str] = None,
    ) -> None:
        if result is None:
            result = SourceRequestResult(
                **request.model_dump(),
                status="DONE",
                completed_at=datetime.now(timezone.utc),
                rg_number=rg_number,
                ready_file_id=ready_file_id,
            )
        self._write_result(file_id, self._terminal_name("DONE", request), result)

    def fail_request(
        self, file_id: str, request: SourceRequest, error: str
    ) -> None:
        result = SourceRequestResult(
            **request.model_dump(),
            status="FAILED",
            completed_at=datetime.now(timezone.utc),
            error=error,
        )
        self._write_result(file_id, self._terminal_name("FAILED", request), result)

    def _fail_invalid_request(self, file_id: str, old_name: str, error: str) -> None:
        payload = json.dumps(
            {
                "schema_version": 1,
                "status": "FAILED",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "error": f"INVALID_REQUEST: {error}",
            },
            indent=2,
        ).encode("utf-8")
        media = MediaIoBaseUpload(
            io.BytesIO(payload), mimetype="application/json", resumable=False
        )
        self.uploader.service.files().update(
            fileId=file_id,
            body={"name": f"FAILED_{old_name}"},
            media_body=media,
        ).execute()

    def _has_processing_request(self, report_date: str) -> bool:
        query = (
            f"'{self.requests_folder_id}' in parents and "
            "mimeType != 'application/vnd.google-apps.folder' and "
            "trashed = false and "
            "name contains 'PROCESSING_' and "
            f"name contains '{report_date}'"
        )
        response = (
            self.uploader.service.files()
            .list(q=query, spaces="drive", fields="files(id, name)")
            .execute()
        )
        return bool(response.get("files", []))

    def _find_folder(self, name: str, parent_id: str) -> Optional[Dict]:
        query = (
            f"name = '{name}' and "
            f"'{parent_id}' in parents and "
            "mimeType = 'application/vnd.google-apps.folder' and "
            "trashed = false"
        )
        response = (
            self.uploader.service.files()
            .list(q=query, spaces="drive", fields="files(id, name)")
            .execute()
        )
        files = response.get("files", [])
        return files[0] if files else None

    def _find_ready_result(self, report_date: str) -> Optional[Tuple[str, str]]:
        date_folder = self._find_folder(report_date, self.root_id)
        if not date_folder:
            return None
        sources_folder = self._find_folder("sources", date_folder["id"])
        if not sources_folder:
            return None

        rg_query = (
            f"'{sources_folder['id']}' in parents and "
            "mimeType = 'application/vnd.google-apps.folder' and "
            "trashed = false"
        )
        rg_folders = (
            self.uploader.service.files()
            .list(q=rg_query, spaces="drive", fields="files(id, name)")
            .execute()
            .get("files", [])
        )
        for rg_folder in rg_folders:
            ready_query = (
                "name = '_READY.json' and "
                f"'{rg_folder['id']}' in parents and "
                "trashed = false"
            )
            ready_files = (
                self.uploader.service.files()
                .list(q=ready_query, spaces="drive", fields="files(id, name)")
                .execute()
                .get("files", [])
            )
            for ready_file in ready_files:
                try:
                    content = (
                        self.uploader.service.files()
                        .get_media(fileId=ready_file["id"])
                        .execute()
                    )
                    gate = ReadyGate.model_validate_json(content)
                except (ValidationError, ValueError, TypeError):
                    logger.warning(
                        "Geçersiz READY kapısı yok sayıldı: %s", ready_file["id"]
                    )
                    continue
                if (
                    gate.status == "READY"
                    and gate.verified
                    and gate.report_date == report_date
                ):
                    rg_number = gate.resmi_gazete_sayisi or rg_folder["name"].removeprefix(
                        "rg-"
                    )
                    return rg_number, ready_file["id"]
        return None

    def _adapt_upload_result(self, report_date: str, upload_result) -> Tuple[str, str]:
        """Bridge the current boolean uploader until it returns the Task 3 tuple."""
        if isinstance(upload_result, tuple) and len(upload_result) == 2:
            gate, ready_file_id = upload_result
            return gate.resmi_gazete_sayisi, ready_file_id
        if upload_result is True:
            ready_result = self._find_ready_result(report_date)
            if ready_result:
                return ready_result
        raise RuntimeError("DRIVE_WRITE_FAILED: geçerli READY kapısı bulunamadı")

    def process_request(self, file_info: Dict) -> None:
        file_id = file_info["id"]
        file_name = file_info["name"]
        logger.info("Yeni talep dosyası tespit edildi: %s (ID: %s)", file_name, file_id)

        try:
            request = self.load_request(file_id)
        except (ValidationError, ValueError, TypeError) as error:
            logger.error("Geçersiz kaynak talebi (%s): %s", file_id, error)
            self.claim_request(file_id, file_name)
            self._fail_invalid_request(file_id, file_name, str(error))
            return

        target_date = request.report_date.isoformat()
        processing_exists = self._has_processing_request(target_date)
        ready_result = self._find_ready_result(target_date)

        if ready_result:
            self.claim_request(file_id, file_name)
            rg_number, ready_file_id = ready_result
            self.complete_request(
                file_id,
                request,
                rg_number=rg_number,
                ready_file_id=ready_file_id,
            )
            return
        if processing_exists:
            logger.info("%s tarihi zaten işleniyor; talep beklemede kaldı.", target_date)
            return

        self.claim_request(file_id, file_name)
        logger.info("==> Hedef Tarih İçin Arşivleme Başlatılıyor: %s <==", target_date)
        try:
            fetcher = MevzuatFetcher(date_str=target_date)
            _, rg_dir = fetcher.run()

            folder_ids = self.uploader.ensure_date_hierarchy(target_date)
            upload_result = self.uploader.upload_rg_source_tree(
                rg_dir, folder_ids["sources"]
            )
            rg_number, ready_file_id = self._adapt_upload_result(
                target_date, upload_result
            )

            self.complete_request(
                file_id,
                request,
                rg_number=rg_number,
                ready_file_id=ready_file_id,
            )
            logger.info(
                "%s tarihli kaynaklar başarıyla Drive'a yüklendi ve doğrulandı.",
                target_date,
            )
        except Exception as error:
            logger.error(
                "Arşivleme işlemi sırasında hata (%s): %s",
                target_date,
                error,
                exc_info=True,
            )
            self.fail_request(file_id, request, str(error))

    def run_forever(self) -> None:
        logger.info("Drive Request Watcher döngüsü başladı. Talepler bekleniyor...")
        while True:
            try:
                pending = self.list_pending_requests()
                if pending:
                    logger.info("%s adet bekleyen talep bulundu.", len(pending))
                    for request in pending:
                        self.process_request(request)
            except Exception as error:
                logger.error("Watcher döngü hatası: %s", error)

            time.sleep(self.interval)


if __name__ == "__main__":
    watcher = DriveRequestWatcher(check_interval_seconds=10)
    watcher.run_forever()
