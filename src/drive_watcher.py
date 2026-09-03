import os
import re
import time
import json
import logging
from pathlib import Path
from typing import List, Dict, Optional

from src.config import DRIVE_ROOT_FOLDER_ID, DRIVE_ROOT_FOLDER_NAME
from src.fetcher import MevzuatFetcher, normalize_date_formats
from src.drive_uploader import DriveUploader

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
        
        # Ensure root requests folder exists
        self.root_id = self.uploader.root_folder_id
        self.requests_folder_id = self.uploader.find_or_create_folder("requests", self.root_id)
        logger.info(f"Drive Request Watcher başlatıldı. Dinlenen klasör ID: {self.requests_folder_id}")

    def list_pending_requests(self) -> List[Dict]:
        """Lists unhandled request files inside requests/ folder."""
        query = (
            f"'{self.requests_folder_id}' in parents and "
            f"mimeType != 'application/vnd.google-apps.folder' and "
            f"trashed = false and "
            f"not name contains 'processed_' and "
            f"not name contains 'DONE_'"
        )
        response = self.uploader.service.files().list(
            q=query,
            spaces="drive",
            fields="files(id, name, createdTime)",
        ).execute()
        return response.get("files", [])

    def extract_date_from_request(self, file_id: str, file_name: str) -> Optional[str]:
        """Extracts date (YYYY-MM-DD) from file name or its JSON/text content."""
        # 1. Try file name regex: e.g. 2026-07-03.json or 03.07.2026.txt
        match = re.search(r"(\d{4}-\d{2}-\d{2})", file_name)
        if match:
            return match.group(1)

        match_tr = re.search(r"(\d{2}\.\d{2}\.\d{4})", file_name)
        if match_tr:
            iso_d, _ = normalize_date_formats(match_tr.group(1))
            return iso_d

        # 2. Try reading file content
        try:
            content_bytes = self.uploader.service.files().get_media(fileId=file_id).execute()
            content_str = content_bytes.decode("utf-8", errors="ignore")
            try:
                data = json.loads(content_str)
                if "report_date" in data:
                    iso_d, _ = normalize_date_formats(data["report_date"])
                    return iso_d
            except Exception:
                pass

            # Search in raw text
            match_txt = re.search(r"(\d{4}-\d{2}-\d{2})", content_str)
            if match_txt:
                return match_txt.group(1)
        except Exception as e:
            logger.warning(f"Talep dosyası içeriği okunamadı ({file_id}): {e}")

        return None

    def mark_request_processed(self, file_id: str, old_name: str):
        """Renames file to processed_<old_name>"""
        new_name = f"processed_{old_name}"
        self.uploader.service.files().update(
            fileId=file_id,
            body={"name": new_name},
        ).execute()
        logger.info(f"Talep işlendi olarak işaretlendi: {new_name}")

    def process_request(self, file_info: Dict):
        file_id = file_info["id"]
        file_name = file_info["name"]
        logger.info(f"Yeni talep dosyası tespit edildi: {file_name} (ID: {file_id})")

        target_date = self.extract_date_from_request(file_id, file_name)
        if not target_date:
            logger.error(f"Dosyadan geçerli tarih çıkarılamadı: {file_name}")
            self.mark_request_processed(file_id, file_name)
            return

        logger.info(f"==> Hedef Tarih İçin Arşivleme Başlatılıyor: {target_date} <==")
        try:
            # 1. Fetch Resmî Gazete sources
            fetcher = MevzuatFetcher(date_str=target_date)
            source_manifest, rg_dir = fetcher.run()

            # 2. Upload to Drive
            folder_ids = self.uploader.ensure_date_hierarchy(target_date)
            self.uploader.upload_rg_source_tree(rg_dir, folder_ids["sources"])

            logger.info(f"✅ {target_date} tarihli kaynaklar başarıyla Drive'a yüklendi ve doğrulandı.")
            self.mark_request_processed(file_id, file_name)
        except Exception as e:
            logger.error(f"Arşivleme işlemi sırasında hata ({target_date}): {e}", exc_info=True)
            self.mark_request_processed(file_id, f"FAILED_{file_name}")

    def run_forever(self):
        logger.info("Drive Request Watcher döngüsü başladı. Talepler bekleniyor...")
        while True:
            try:
                pending = self.list_pending_requests()
                if pending:
                    logger.info(f"{len(pending)} adet bekleyen talep bulundu.")
                    for req in pending:
                        self.process_request(req)
            except Exception as e:
                logger.error(f"Watcher döngü hatası: {e}")

            time.sleep(self.interval)


if __name__ == "__main__":
    watcher = DriveRequestWatcher(check_interval_seconds=10)
    watcher.run_forever()
