import os
import re
import hashlib
import logging
from pathlib import Path
from typing import Tuple, List, Optional, Dict
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import requests
from playwright.sync_api import sync_playwright

from src.config import (
    PRIMARY_DOMAIN,
    ALIAS_DOMAIN,
    PRIMARY_FIHRIST_TEMPLATE,
    ALIAS_FIHRIST_TEMPLATE,
    ALLOWED_ATTACHMENT_EXTENSIONS,
    DEFAULT_USER_AGENT,
    DOWNLOADS_DIR,
)
from src.models import (
    FileManifest,
    DocumentItem,
    SourceManifest,
)

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("atez.fetcher")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def compute_sha256(file_path: Path) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def normalize_date_formats(date_input: str) -> Tuple[str, str]:
    """
    Returns (YYYY-MM-DD, DD.MM.YYYY)
    Accepts either format as input.
    """
    date_input = date_input.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_input):
        parts = date_input.split("-")
        return f"{parts[0]}-{parts[1]}-{parts[2]}", f"{parts[2]}.{parts[1]}.{parts[0]}"
    elif re.match(r"^\d{2}\.\d{2}\.\d{4}$", date_input):
        parts = date_input.split(".")
        return f"{parts[2]}-{parts[1]}-{parts[0]}", f"{parts[0]}.{parts[1]}.{parts[2]}"
    else:
        raise ValueError(f"Geçersiz tarih formatı: {date_input}. YYYY-MM-DD veya DD.MM.YYYY olmalıdır.")


class MevzuatFetcher:
    def __init__(self, date_str: str, output_base_dir: Optional[Path] = None):
        self.iso_date, self.display_date = normalize_date_formats(date_str)
        self.output_base_dir = output_base_dir or (DOWNLOADS_DIR / self.iso_date / "sources")
        self.output_base_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
        self.session.verify = False

    def fetch_fihrist_page(self) -> Tuple[str, str, int]:
        """
        Loads fihrist page using Playwright.
        Tries primary URL, falls back to alias URL on network/5xx errors.
        Returns: (html_content, final_url, status_code)
        """
        urls_to_try = [
            PRIMARY_FIHRIST_TEMPLATE.format(date_str=self.display_date),
            ALIAS_FIHRIST_TEMPLATE.format(date_str=self.display_date),
        ]

        last_error = None
        for target_url in urls_to_try:
            logger.info(f"Fihrist sayfası deneniyor: {target_url}")
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    context = browser.new_context(user_agent=DEFAULT_USER_AGENT, ignore_https_errors=True)
                    page = context.new_page()
                    response = page.goto(target_url, wait_until="networkidle", timeout=30000)

                    if response is None:
                        raise Exception(f"Boş yanıt alındı: {target_url}")

                    status = response.status
                    logger.info(f"HTTP Yanıt Kodu: {status} ({target_url})")

                    if status == 404:
                        logger.warning(f"Resmî Gazete fihristi bulunamadı (404): {target_url}")
                        browser.close()
                        return "", target_url, 404

                    if 500 <= status < 600:
                        logger.warning(f"5xx Sunucu hatası ({status}), alias denenecek.")
                        browser.close()
                        continue

                    content = page.content()
                    final_url = page.url
                    browser.close()
                    return content, final_url, status

            except Exception as e:
                logger.warning(f"Fihrist yükleme hatası ({target_url}): {e}")
                last_error = e

        raise RuntimeError(f"Fihrist sayfası alınamadı. Son hata: {last_error}")

    def extract_resmi_gazete_number(self, soup: BeautifulSoup) -> str:
        """Extracts the issue number from page, e.g. 32547 or 32500"""
        # 1. Check spanGazeteTarih specifically
        span_tarih = soup.find(id="spanGazeteTarih")
        if span_tarih:
            text = span_tarih.get_text()
            match = re.search(r"(\d{4,6})\s*Sayılı", text, re.IGNORECASE)
            if match:
                return match.group(1)
            match_num = re.search(r"Sayı\s*[:：]?\s*(\d{4,6})", text, re.IGNORECASE)
            if match_num:
                return match_num.group(1)

        # 2. Look for '32547 Sayılı' or 'Sayı : 32500' across page text
        text = soup.get_text()
        match_sayili = re.search(r"(\d{4,6})\s*Sayılı\s*Resmî\s*Gazete", text, re.IGNORECASE)
        if match_sayili:
            return match_sayili.group(1)

        match = re.search(r"Sayı\s*[:：]?\s*(\d{4,6})", text, re.IGNORECASE)
        if match:
            return match.group(1)
            
        # 3. Look in badges / headers
        for tag in soup.find_all(["span", "div", "h1", "h2", "h3", "p", "a"]):
            tag_text = tag.get_text(strip=True)
            match_tag = re.search(r"(\d{4,6})\s*Sayılı", tag_text, re.IGNORECASE)
            if match_tag:
                return match_tag.group(1)
            match_tag2 = re.search(r"Sayı\s*[:：]?\s*(\d{4,6})", tag_text, re.IGNORECASE)
            if match_tag2:
                return match_tag2.group(1)
        
        # 4. Look in title
        title = soup.title.string if soup.title else ""
        match_title = re.search(r"(\d{4,6})", title)
        if match_title:
            return match_title.group(1)

        # 5. Fallback to date without dashes if unknown
        return self.iso_date.replace("-", "")

    def extract_tebligler(self, soup: BeautifulSoup, base_url: str) -> List[Dict[str, str]]:
        """
        Parses HTML according to Section 5.3:
        1. Finds .html-subtitle where text contains TEBLİĞ or TEBLİĞLER.
        2. Collects all .fihrist-item.mb-1 links until next section subtitle.
        """
        teblig_items = []
        subtitles = soup.find_all(class_=re.compile(r"html-subtitle", re.I))
        
        teblig_header = None
        for sub in subtitles:
            sub_text = sub.get_text(strip=True).upper()
            if "TEBLİĞ" in sub_text:
                teblig_header = sub
                break

        if not teblig_header:
            logger.info("Fihristte 'TEBLİĞ' veya 'TEBLİĞLER' başlığı bulunamadı.")
            return []

        # Traverse siblings until next section header
        curr = teblig_header.find_next_sibling()
        while curr:
            # Check if this sibling is or contains a new section header
            classes = curr.get("class", [])
            if any("html-subtitle" in c for c in classes) or curr.find(class_=re.compile(r"html-subtitle", re.I)):
                break

            # Check if it's a fihrist item
            fihrist_items = curr.find_all(class_=re.compile(r"fihrist-item", re.I))
            if not fihrist_items and "fihrist-item" in " ".join(classes):
                fihrist_items = [curr]

            for item in fihrist_items:
                link_tag = item.find("a") if item.name != "a" else item
                if link_tag and link_tag.get("href"):
                    href = link_tag.get("href").strip()
                    full_url = urljoin(base_url, href)
                    title = link_tag.get_text(strip=True)
                    if not title and item.get_text(strip=True):
                        title = item.get_text(strip=True)
                    teblig_items.append({
                        "title": title or "Başlıksız Tebliğ",
                        "url": full_url,
                    })

            curr = curr.find_next_sibling()

        logger.info(f"Toplam {len(teblig_items)} adet Tebliğ bulundu.")
        return teblig_items

    def download_file(self, url: str, target_path: Path, role: str, parent_doc_id: Optional[str] = None) -> FileManifest:
        """Downloads a single file, saves locally, computes sha256 and metadata."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"İndiriliyor [{role}]: {url} -> {target_path}")

        try:
            resp = self.session.get(url, timeout=30, allow_redirects=True)
            status_code = resp.status_code
            content_type = resp.headers.get("Content-Type", "application/octet-stream")
            final_url = resp.url

            with open(target_path, "wb") as f:
                f.write(resp.content)

            size_bytes = target_path.stat().st_size
            sha256_hash = compute_sha256(target_path)

            return FileManifest(
                source_url=url,
                final_url=final_url,
                http_status=status_code,
                content_type=content_type,
                size_bytes=size_bytes,
                sha256=sha256_hash,
                role=role, # type: ignore
                parent_document_id=parent_doc_id,
                local_relative_path=str(target_path.relative_to(self.output_base_dir)),
            )
        except Exception as e:
            logger.error(f"Dosya indirme hatası ({url}): {e}")
            raise

    def process_teblig_document(self, teblig_info: Dict[str, str], doc_index: int, rg_dir: Path) -> DocumentItem:
        """
        Downloads a Tebliğ, inspects its HTML for attachments, and saves everything in doc folder.
        """
        doc_id = f"doc-{doc_index:02d}"
        doc_dir = rg_dir / doc_id
        doc_dir.mkdir(parents=True, exist_ok=True)

        url = teblig_info["url"]
        title = teblig_info["title"]

        # Determine file type
        parsed_url = urlparse(url)
        path_lower = parsed_url.path.lower()
        is_pdf = path_lower.endswith(".pdf")
        main_filename = "source.pdf" if is_pdf else "source.html"
        main_file_path = doc_dir / main_filename

        main_manifest = self.download_file(
            url=url,
            target_path=main_file_path,
            role="main_document",
            parent_doc_id=doc_id,
        )

        attachments: List[FileManifest] = []

        # If it's HTML, search for attachments on the same domain
        if not is_pdf and main_file_path.exists():
            try:
                with open(main_file_path, "r", encoding="utf-8", errors="ignore") as f:
                    doc_soup = BeautifulSoup(f.read(), "html.parser")

                # Find all links and images
                attachment_urls = set()
                for tag in doc_soup.find_all(["a", "img", "embed", "iframe"]):
                    href = tag.get("href") or tag.get("src")
                    if not href:
                        continue
                    abs_url = urljoin(url, href)
                    parsed_att = urlparse(abs_url)

                    # Only allow attachments from same official domain
                    if parsed_att.netloc in ["resmigazete.gov.tr", "www.resmigazete.gov.tr"]:
                        ext = Path(parsed_att.path).suffix.lower()
                        if ext in ALLOWED_ATTACHMENT_EXTENSIONS and abs_url != url:
                            attachment_urls.add(abs_url)

                for att_url in sorted(attachment_urls):
                    att_name = Path(urlparse(att_url).path).name
                    if not att_name:
                        att_name = f"attachment_{len(attachments)+1}.dat"
                    att_path = doc_dir / att_name

                    att_manifest = self.download_file(
                        url=att_url,
                        target_path=att_path,
                        role="attachment",
                        parent_doc_id=doc_id,
                    )
                    attachments.append(att_manifest)
            except Exception as e:
                logger.warning(f"Ek ayrıştırma uyarısı ({doc_id}): {e}")

        # Write doc manifest.json
        doc_manifest_path = doc_dir / "manifest.json"
        doc_item = DocumentItem(
            document_id=doc_id,
            title=title,
            source_url=url,
            decision="unclassified",
            main_document=main_manifest,
            attachments=attachments,
        )
        with open(doc_manifest_path, "w", encoding="utf-8") as f:
            f.write(doc_item.model_dump_json(indent=2))

        return doc_item

    def run(self) -> Tuple[SourceManifest, Path]:
        """
        Executes full fetch workflow:
        1. Fetch index.html
        2. Extract RG number
        3. Parse & download all Tebliğler + attachments
        4. Save source-manifest.json
        Returns: (SourceManifest, rg_dir_path)
        """
        html_content, final_url, status = self.fetch_fihrist_page()
        if status == 404 or not html_content:
            raise FileNotFoundError(f"{self.display_date} tarihli Resmî Gazete fihristi bulunamadı (HTTP {status}).")

        soup = BeautifulSoup(html_content, "html.parser")
        rg_number = self.extract_resmi_gazete_number(soup)
        rg_folder_name = f"rg-{rg_number}"
        rg_dir = self.output_base_dir / rg_folder_name
        rg_dir.mkdir(parents=True, exist_ok=True)

        # Save index.html
        index_file_path = rg_dir / "index.html"
        with open(index_file_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        index_manifest = FileManifest(
            source_url=final_url,
            final_url=final_url,
            http_status=status,
            content_type="text/html; charset=utf-8",
            size_bytes=index_file_path.stat().st_size,
            sha256=compute_sha256(index_file_path),
            role="daily_index",
            local_relative_path=str(index_file_path.relative_to(self.output_base_dir)),
        )

        # Extract & Process Tebliğler
        teblig_items = self.extract_tebligler(soup, final_url)
        documents = []
        for idx, teblig in enumerate(teblig_items, start=1):
            doc_item = self.process_teblig_document(teblig, idx, rg_dir)
            documents.append(doc_item)

        source_manifest = SourceManifest(
            report_date=self.iso_date,
            resmi_gazete_sayisi=rg_number,
            fihrist_url=final_url,
            index_file=index_manifest,
            documents=documents,
        )

        source_manifest_path = rg_dir / "source-manifest.json"
        with open(source_manifest_path, "w", encoding="utf-8") as f:
            f.write(source_manifest.model_dump_json(indent=2))

        logger.info(f"Kaynak arşivleme tamamlandı: {rg_dir}")
        return source_manifest, rg_dir
