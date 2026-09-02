import os
from pathlib import Path
from dotenv import load_dotenv

# Load local .env if present
load_dotenv()

# Base URLs
PRIMARY_DOMAIN = "https://resmigazete.gov.tr"
ALIAS_DOMAIN = "https://www.resmigazete.gov.tr"

PRIMARY_FIHRIST_TEMPLATE = "https://resmigazete.gov.tr/{date_str}" # DD.MM.YYYY
ALIAS_FIHRIST_TEMPLATE = "https://www.resmigazete.gov.tr/{date_str}"

# GitHub & Drive config
GITHUB_REPO = os.getenv("GITHUB_REPO", "alicankocak/atez-mevzuat-radari-fetcher")
DRIVE_ROOT_FOLDER_NAME = "ATEZ-Mevzuat-Radari-V2"
DRIVE_ROOT_FOLDER_ID = os.getenv("DRIVE_ROOT_FOLDER_ID", "1TONr3xXlRbOfClJKPZJ_Q3mbFqzBEiTE")

# Subfolder names
SUBFOLDERS = ["requests", "sources", "analyses", "reports", "deliveries"]

# Timezone
TIMEZONE = "Europe/Istanbul"

# Allowed attachment extensions as specified in doc
ALLOWED_ATTACHMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".html"}

# User Agent
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Output directory for temporary downloads
BASE_DIR = Path(__file__).resolve().parent.parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
