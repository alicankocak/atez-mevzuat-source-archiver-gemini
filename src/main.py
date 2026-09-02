import sys
import argparse
import logging
from datetime import datetime, timezone
import zoneinfo
from pathlib import Path

from src.config import TIMEZONE
from src.fetcher import MevzuatFetcher, normalize_date_formats
from src.drive_uploader import DriveUploader

logger = logging.getLogger("atez.main")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def get_current_date_istanbul() -> str:
    tz = zoneinfo.ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    return now.strftime("%Y-%m-%d")


def main():
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
        help="Hata durumunda 10 dakika sonra tek tekrar yap",
    )

    args = parser.parse_args()

    target_date = args.date or get_current_date_istanbul()
    iso_date, display_date = normalize_date_formats(target_date)

    logger.info(f"=== ATEZ Mevzuat Arşivleme Başlatıldı ===")
    logger.info(f"Hedef Tarih: {iso_date} ({display_date})")

    try:
        # 1. Fetch Resmî Gazete Tebliğler
        fetcher = MevzuatFetcher(date_str=target_date)
        source_manifest, rg_dir = fetcher.run()

        logger.info(f"İndirme Başarılı! Resmî Gazete Sayısı: {source_manifest.resmi_gazete_sayisi}")
        logger.info(f"İndirilen Tebliğ Sayısı: {len(source_manifest.documents)}")

        # 2. Upload to Google Drive if not skipped
        if not args.skip_drive:
            logger.info("Google Drive yükleme ve doğrulama başlatılıyor...")
            uploader = DriveUploader()
            if uploader.service:
                folder_ids = uploader.ensure_date_hierarchy(iso_date)
                uploader.upload_rg_source_tree(rg_dir, folder_ids["sources"])
                logger.info("Google Drive işlemi başarıyla tamamlandı.")
            else:
                logger.warning("Google Drive kimlikleri tanımlı olmadığı için Drive yüklemesi atlandı.")

        logger.info("=== ATEZ Mevzuat Arşivleme Başarıyla Tamamlandı ===")

    except Exception as e:
        logger.error(f"HATA: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
