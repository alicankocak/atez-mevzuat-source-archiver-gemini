import fcntl
import io
import json
import logging
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
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
    CLAIM_LEASE_SECONDS = 15 * 60

    def __init__(self, check_interval_seconds: int = 15):
        self.interval = check_interval_seconds
        self.claim_lock_dir = (
            Path(tempfile.gettempdir()) / "atez-mevzuat-source-archiver-gemini-locks"
        )
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
        pending = response.get("files", [])
        return pending + self._recover_stale_claims()

    def load_request(self, file_id: str) -> SourceRequest:
        """Parse a request only from its UTF-8 JSON bytes."""
        content = self.uploader.service.files().get_media(fileId=file_id).execute()
        return SourceRequest.model_validate_json(content)

    def claim_request(
        self,
        file_id: str,
        old_name: str,
        request: Optional[SourceRequest] = None,
    ) -> str:
        """Expose ownership by changing the Drive file metadata."""
        body = {"name": f"PROCESSING_{old_name}"}
        if request is not None:
            report_date = request.report_date.isoformat()
            body = {
                "name": (
                    f"PROCESSING_SOURCE_REQUEST__{report_date}__"
                    f"{request.request_id}.json"
                ),
                "appProperties": {
                    "report_date": report_date,
                    "request_id": request.request_id,
                    "claimed_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        self.uploader.service.files().update(
            fileId=file_id,
            body=body,
        ).execute()
        claimed_name = body["name"]
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
                **request.model_dump(mode="json"),
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
            **request.model_dump(mode="json"),
            status="FAILED",
            completed_at=datetime.now(timezone.utc),
            error=error,
        )
        self._write_result(file_id, self._terminal_name("FAILED", request), result)

    def _fail_invalid_request(self, file_id: str, old_name: str, error: str) -> None:
        terminal_source_name = old_name.removeprefix("PROCESSING_")
        payload = json.dumps(
            {
                "schema_version": 1,
                "status": "FAILED",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "error": f"INVALID_REQUEST: {error}",
                "request_file_id": file_id,
                "request_file_name": terminal_source_name,
            },
            indent=2,
        ).encode("utf-8")
        media = MediaIoBaseUpload(
            io.BytesIO(payload), mimetype="application/json", resumable=False
        )
        self.uploader.service.files().update(
            fileId=file_id,
            body={"name": f"FAILED_{terminal_source_name}"},
            media_body=media,
        ).execute()

    @contextmanager
    def _date_claim_lock(self, report_date: str):
        self.claim_lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.claim_lock_dir / f"{report_date}.lock"
        lock_file = lock_path.open("a+")
        acquired = False
        try:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            if acquired:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def _list_processing_requests(self) -> List[Dict]:
        query = (
            f"'{self.requests_folder_id}' in parents and "
            "mimeType != 'application/vnd.google-apps.folder' and "
            "trashed = false and "
            "name contains 'PROCESSING_'"
        )
        response = (
            self.uploader.service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id, name, modifiedTime, appProperties)",
            )
            .execute()
        )
        return response.get("files", [])

    def _processing_claim_is_stale(self, file_info: Dict) -> bool:
        claimed_at = file_info.get("appProperties", {}).get("claimed_at")
        claimed_at = claimed_at or file_info.get("modifiedTime")
        if not claimed_at:
            return True
        try:
            claimed_time = datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return True
        if claimed_time.tzinfo is None:
            return True
        age = datetime.now(timezone.utc) - claimed_time.astimezone(timezone.utc)
        return age.total_seconds() >= self.CLAIM_LEASE_SECONDS

    def _return_claim_to_pending(
        self, file_info: Dict, request: SourceRequest
    ) -> Dict:
        pending_name = (
            f"SOURCE_REQUEST__{request.report_date.isoformat()}__"
            f"{request.request_id}.json"
        )
        self.uploader.service.files().update(
            fileId=file_info["id"],
            body={
                "name": pending_name,
                "appProperties": {
                    "report_date": None,
                    "request_id": None,
                    "claimed_at": None,
                },
            },
        ).execute()
        logger.warning("Süresi dolan talep yeniden kuyruğa alındı: %s", pending_name)
        return {"id": file_info["id"], "name": pending_name}

    def _recover_stale_claims(self) -> List[Dict]:
        recovered = []
        for file_info in self._list_processing_requests():
            if not self._processing_claim_is_stale(file_info):
                continue
            try:
                request = self.load_request(file_info["id"])
            except (ValidationError, ValueError, TypeError) as error:
                self._fail_invalid_request(
                    file_info["id"], file_info["name"], str(error)
                )
                logger.warning(
                    "Süresi dolan geçersiz talep FAILED durumuna alındı: %s",
                    file_info["id"],
                )
                continue
            report_date = request.report_date.isoformat()
            with self._date_claim_lock(report_date) as acquired:
                if acquired:
                    recovered.append(self._return_claim_to_pending(file_info, request))
        return recovered

    def _has_processing_request(self, report_date: str) -> bool:
        for file_info in self._list_processing_requests():
            try:
                request = self.load_request(file_info["id"])
            except (ValidationError, ValueError, TypeError):
                continue
            if request.report_date.isoformat() != report_date:
                continue
            if self._processing_claim_is_stale(file_info):
                self._return_claim_to_pending(file_info, request)
                continue
            return True
        return False

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
                    raw_gate = json.loads(content)
                    if not isinstance(raw_gate, dict):
                        raise ValueError("READY kapısı JSON nesnesi olmalı")
                    required_fields = {
                        "schema_version",
                        "status",
                        "report_date",
                        "resmi_gazete_sayisi",
                        "created_at",
                        "total_files_count",
                        "verified",
                        "files",
                    }
                    created_at = raw_gate.get("created_at")
                    if (
                        not required_fields.issubset(raw_gate)
                        or type(raw_gate["schema_version"]) is not int
                        or raw_gate["schema_version"] != 1
                        or raw_gate["status"] != "READY"
                        or raw_gate["report_date"] != report_date
                        or not isinstance(raw_gate["resmi_gazete_sayisi"], str)
                        or not raw_gate["resmi_gazete_sayisi"].strip()
                        or type(raw_gate["total_files_count"]) is not int
                        or raw_gate["total_files_count"] < 1
                        or raw_gate["verified"] is not True
                        or not isinstance(created_at, str)
                        or not re.fullmatch(
                            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
                            r"(?:Z|[+-]\d{2}:\d{2})",
                            created_at,
                        )
                    ):
                        raise ValueError("READY kapısı zorunlu doğrulama alanlarını taşımıyor")
                    gate = ReadyGate.model_validate(raw_gate)
                except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
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
        with self._date_claim_lock(target_date) as acquired:
            if not acquired:
                logger.info(
                    "%s tarihi başka bir watcher tarafından işleniyor; talep beklemede kaldı.",
                    target_date,
                )
                return
            self._process_claimed_date(file_id, file_name, request, target_date)

    def _process_claimed_date(
        self,
        file_id: str,
        file_name: str,
        request: SourceRequest,
        target_date: str,
    ) -> None:
        processing_exists = self._has_processing_request(target_date)
        ready_result = self._find_ready_result(target_date)

        if ready_result:
            self.claim_request(file_id, file_name, request)
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

        self.claim_request(file_id, file_name, request)
        logger.info("==> Hedef Tarih İçin Arşivleme Başlatılıyor: %s <==", target_date)
        try:
            fetcher = MevzuatFetcher(date_str=target_date)
            _, rg_dir = fetcher.run()

            folder_ids = self.uploader.ensure_date_hierarchy(target_date)
            gate, ready_file_id = self.uploader.upload_rg_source_tree(
                rg_dir, folder_ids["sources"]
            )
            rg_number = gate.resmi_gazete_sayisi
            if not rg_number or not ready_file_id:
                raise RuntimeError(
                    "DRIVE_WRITE_FAILED: yükleyici geçerli READY sonucu döndürmedi"
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
