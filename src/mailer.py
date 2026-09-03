import os
import json
import base64
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import List, Dict, Optional, Union
from datetime import datetime, timezone
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

from src.drive_uploader import DriveUploader

logger = logging.getLogger("atez.mailer")

# Predefined mail groups
MAIL_GROUPS = {
    "test1": ["alicankocak7@gmail.com", "alican.kocak@atez.com"],
    "test": ["alicankocak7@gmail.com", "alican.kocak@atez.com"],
    "yonetim": ["alican.kocak@atez.com"],
}

SENDER_EMAIL = os.getenv("GMAIL_SENDER", "atezmevzuat@gmail.com")


class ReportMailer:
    def __init__(self):
        self.service = self._init_gmail_service()
        self.uploader = DriveUploader()

    def _init_gmail_service(self):
        client_id = os.getenv("GOOGLE_CLIENT_ID")
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
        refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN")

        if not (client_id and client_secret and refresh_token):
            logger.warning("Gmail gönderme için Google OAuth kimlikleri eksik.")
            return None

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=[
                "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        try:
            creds.refresh(Request())
            return build("gmail", "v1", credentials=creds)
        except Exception as e:
            logger.warning(f"Gmail servisi başlatılamadı: {e}")
            return None

    def resolve_recipients(self, target: Union[str, List[str]]) -> List[str]:
        """Resolves group names (e.g. test1) or raw emails to a list of emails."""
        if isinstance(target, list):
            emails = []
            for item in target:
                emails.extend(self.resolve_recipients(item))
            return list(set(emails))

        target_clean = target.strip().lower()
        if target_clean in MAIL_GROUPS:
            return MAIL_GROUPS[target_clean]
        
        # Split by comma or semicolon
        if "," in target or ";" in target:
            parts = [p.strip() for p in target.replace(";", ",").split(",") if p.strip()]
            return list(set(parts))

        return [target.strip()]

    def send_report_email(
        self,
        iso_date: str,
        html_content: str,
        recipients_input: Union[str, List[str]],
        resmi_gazete_sayisi: Optional[str] = None,
    ) -> Dict:
        """
        Sends embedded HTML email to recipients and logs delivery to Drive deliveries/
        """
        recipients = self.resolve_recipients(recipients_input)
        subject = f"{iso_date} — ATEZ Mevzuat Radarı Günlük Raporu"
        if resmi_gazete_sayisi:
            subject = f"{iso_date} ({resmi_gazete_sayisi} Sayılı Resmî Gazete) — ATEZ Mevzuat Radarı"

        logger.info(f"E-posta gönderiliyor -> Alıcılar: {recipients}")

        delivery_record = {
            "report_date": iso_date,
            "resmi_gazete_sayisi": resmi_gazete_sayisi,
            "revision": "r01",
            "recipients": recipients,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "status": "DELIVERED",
            "message_ids": [],
        }

        if not self.service:
            logger.warning("Gmail servisi aktif değil (OAuth gmail.send yetkisi gerekebilir). Test log kaydı oluşturuldu.")
            delivery_record["status"] = "SIMULATED_SUCCESS"
            delivery_record["note"] = "Gmail API yetkilendirmesi bekleniyor"
            self._save_delivery_log(iso_date, delivery_record)
            return delivery_record

        try:
            for email in recipients:
                message = MIMEMultipart("alternative")
                message["to"] = email
                message["from"] = SENDER_EMAIL
                message["subject"] = subject

                html_part = MIMEText(html_content, "html", "utf-8")
                message.attach(html_part)

                raw_msg = base64.urlsafe_b64encode(message.as_bytes()).decode()
                sent = self.service.users().messages().send(
                    userId="me",
                    body={"raw": raw_msg},
                ).execute()

                msg_id = sent.get("id", "")
                logger.info(f"E-posta başarıyla iletildi: {email} (Message ID: {msg_id})")
                delivery_record["message_ids"].append({"email": email, "id": msg_id})

            self._save_delivery_log(iso_date, delivery_record)
            return delivery_record

        except Exception as e:
            logger.error(f"E-posta gönderme hatası: {e}", exc_info=True)
            delivery_record["status"] = "DELIVERY_FAILED"
            delivery_record["error"] = str(e)
            self._save_delivery_log(iso_date, delivery_record)
            raise

    def _save_delivery_log(self, iso_date: str, record: Dict):
        """Saves delivery record into Drive deliveries/d01-r01.json"""
        if not self.uploader.service:
            return

        try:
            folder_ids = self.uploader.ensure_date_hierarchy(iso_date)
            deliveries_folder_id = folder_ids["deliveries"]

            log_name = f"d01-r01.json"
            temp_log_path = Path(f"/tmp/{log_name}")
            with open(temp_log_path, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2, ensure_ascii=False)

            self.uploader.upload_file(temp_log_path, deliveries_folder_id)
            logger.info(f"Teslim kaydı Drive'a işlendi: deliveries/{log_name}")
        except Exception as e:
            logger.warning(f"Teslim kaydı Drive'a yazılamadı: {e}")
