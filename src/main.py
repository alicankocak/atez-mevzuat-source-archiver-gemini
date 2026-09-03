import sys
import argparse
import logging
from datetime import datetime
from enum import Enum
import zoneinfo
from pathlib import Path

from src.config import TIMEZONE
from src.date_lease import date_archive_lease
from src.drive_watcher import DriveRequestWatcher
from src.fetcher import MevzuatFetcher, normalize_date_formats
from src.drive_uploader import DriveUploader
from src.retry_policy import RetryPolicy

logger = logging.getLogger("atez.main")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def get_current_date_istanbul() -> str:
    tz = zoneinfo.ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    return now.strftime("%Y-%m-%d")


class ArchiveRunStatus(str, Enum):
    COMPLETED = "completed"
    DATE_BUSY = "date_busy"
    READY_EXISTS = "ready_exists"
    PROCESSING_EXISTS = "processing_exists"


def archive_date(
    target_date: str,
    *,
    skip_drive: bool = False,
    lease_dir: Path | str | None = None,
    fetcher_factory=None,
    uploader_factory=None,
    watcher_factory=None,
    retry_policy: RetryPolicy | None = None,
) -> ArchiveRunStatus:
    fetcher_factory = fetcher_factory or MevzuatFetcher
    uploader_factory = uploader_factory or DriveUploader
    watcher_factory = watcher_factory or DriveRequestWatcher
    retry_policy = retry_policy or RetryPolicy()

    iso_date, display_date = normalize_date_formats(target_date)
    logger.info("=== ATEZ Mevzuat Arşivleme Başlatıldı ===")
    logger.info("Hedef Tarih: %s (%s)", iso_date, display_date)

    with date_archive_lease(iso_date, lock_dir=lease_dir) as acquired:
        if not acquired:
            logger.info(
                "%s tarihi watcher veya başka bir arşivleyici tarafından işleniyor; "
                "doğrudan çalışma değişiklik yapmadan sonlandırıldı.",
                iso_date,
            )
            return ArchiveRunStatus.DATE_BUSY

        uploader = None
        if not skip_drive:
            uploader = uploader_factory(retry_policy=retry_policy)
            if uploader.service:
                archive_state = watcher_factory(
                    check_interval_seconds=0,
                    uploader=uploader,
                    claim_lock_dir=lease_dir,
                    retry_policy=retry_policy,
                )
                if archive_state.find_ready_result(iso_date):
                    logger.info(
                        "%s tarihi için geçerli READY arşivi zaten var; "
                        "yayınlanmış arşiv değiştirilmedi.",
                        iso_date,
                    )
                    return ArchiveRunStatus.READY_EXISTS
                if archive_state.has_processing_request(iso_date):
                    logger.info(
                        "%s tarihi için etkin PROCESSING talebi var; "
                        "doğrudan çalışma değişiklik yapmadan sonlandırıldı.",
                        iso_date,
                    )
                    return ArchiveRunStatus.PROCESSING_EXISTS

        fetcher = fetcher_factory(
            date_str=target_date,
            retry_policy=retry_policy,
        )
        source_manifest, rg_dir = fetcher.run()

        logger.info(
            "İndirme Başarılı! Resmî Gazete Sayısı: %s",
            source_manifest.resmi_gazete_sayisi,
        )
        logger.info("İndirilen Tebliğ Sayısı: %s", len(source_manifest.documents))

        if not skip_drive:
            logger.info("Google Drive yükleme ve doğrulama başlatılıyor...")
            if uploader.service:
                folder_ids = uploader.ensure_date_hierarchy(iso_date)
                uploader.upload_rg_source_tree(rg_dir, folder_ids["sources"])
                logger.info("Google Drive işlemi başarıyla tamamlandı.")
            else:
                logger.warning(
                    "Google Drive kimlikleri tanımlı olmadığı için Drive yüklemesi atlandı."
                )

        logger.info("=== ATEZ Mevzuat Arşivleme Başarıyla Tamamlandı ===")
        return ArchiveRunStatus.COMPLETED


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ATEZ Mevzuat Radarı - Resmî Gazete Tebliğler Fetcher")
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="İndirilecek tarih (YYYY-MM-DD veya DD.MM.YYYY formatında). Varsayılan: Bugün",
    )
    parser.add_argument(
        "--skip-drive",
        action="store_true",
        help="Google Drive yüklemesini atla (yalnızca yerel indirme)",
    )
    parser.add_argument(
        "--retry",
        action="store_true",
        help="Eski uyumluluk seçeneği; sınırlı geri deneme artık her zaman etkindir",
    )

    args = parser.parse_args(argv)

    target_date = args.date or get_current_date_istanbul()

    try:
        if args.retry:
            logger.info(
                "--retry artık gerekli değildir; sınırlı geri deneme tüm çalışmalarda etkindir."
            )
        archive_date(target_date, skip_drive=args.skip_drive)
        return 0
    except Exception as error:
        logger.error("HATA: %s", error, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
