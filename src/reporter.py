import os
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional
from playwright.sync_api import sync_playwright

from src.config import DOWNLOADS_DIR, DRIVE_ROOT_FOLDER_NAME
from src.drive_uploader import DriveUploader

logger = logging.getLogger("atez.reporter")


class ReportGenerator:
    def __init__(self, iso_date: str, template_path: Optional[Path] = None):
        self.iso_date = iso_date
        self.template_path = template_path or (Path(__file__).resolve().parent.parent / "template" / "ATEZ Mevzuat Radarı — Açıklamalı Bülten Şablonu.html")
        self.output_dir = DOWNLOADS_DIR / self.iso_date / "reports" / "r01"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.uploader = DriveUploader()

    def generate_html(self, report_data: Dict) -> Path:
        """
        Populates HTML template with real report_data and saves it.
        """
        # Read base CSS and structure
        with open(self.template_path, "r", encoding="utf-8") as f:
            template_raw = f.read()

        # Extract CSS block
        css_start = template_raw.find("<style>")
        css_end = template_raw.find("</style>") + len("</style>")
        css_block = template_raw[css_start:css_end]

        # Extract Logo & Assets Base64 or standard URLs
        report_id = report_data.get("report_id", f"{self.iso_date.replace('-', '')[2:]}-01")
        rg_sayisi = report_data.get("resmi_gazete_sayisi", "")
        formatted_date = report_data.get("formatted_date", self.iso_date)
        overview_text = report_data.get("overview", "")
        
        stat_new = report_data.get("stat_new", 0)
        stat_changed = report_data.get("stat_changed", 0)
        stat_removed = report_data.get("stat_removed", 0)

        # Build Cards HTML
        cards = report_data.get("cards", [])
        cards_html = ""

        if not cards:
            # Empty state
            cards_html = f"""
            <article class="card empty-state">
              <div class="badge">Tür: Bilgi</div>
              <div class="empty-title"><h2>Kapsam içi değişiklik yok</h2><span class="info-mark">i</span></div>
              <p>{formatted_date} tarihli Resmî Gazete incelendi; gümrük, dış ticaret ve ithalat/ihracat operasyonlarını doğrudan etkileyen bir düzenleme tespit edilmemiştir.</p>
              <div class="info-box"><h3>Bilgi</h3><p>Bir sonraki bülten yayımlandığında bilgilendirileceksiniz.</p></div>
              <div class="source"><span class="source-label">Kaynak:</span><a href="https://resmigazete.gov.tr" target="_blank">Resmî Gazete</a></div>
            </article>
            """
        else:
            for c in cards:
                card_type = c.get("type", "Yeni Tebliğ")
                title = c.get("title", "")
                teblig_no = c.get("teblig_no", "")
                summary = c.get("summary", "")
                who_affected = "".join([f"<li>{item}</li>" for item in c.get("who_affected", [])])
                dates = "".join([f"<li><strong>{item.get('label', 'Tarih')}:</strong> {item.get('text', '')}</li>" for item in c.get("dates", [])])
                note = c.get("note", "")
                source_url = c.get("source_url", "https://resmigazete.gov.tr")
                source_label = c.get("source_label", "Resmî Gazete")

                # Optional table
                table_html = ""
                if c.get("table"):
                    tbl = c["table"]
                    headers = "".join([f"<th>{h}</th>" for h in tbl.get("headers", [])])
                    rows = ""
                    for row in tbl.get("rows", []):
                        row_tds = "".join([f"<td>{td}</td>" for td in row])
                        rows += f"<tr>{row_tds}</tr>"
                    table_html = f"""
                    <section class="table-section">
                      <h2>İlgili Tablo</h2>
                      <div class="table-scroll">
                        <table class="data-table">
                          <thead><tr>{headers}</tr></thead>
                          <tbody>{rows}</tbody>
                        </table>
                      </div>
                    </section>
                    """

                cards_html += f"""
                <article class="card">
                  <div class="badge">Tür: {card_type}</div>
                  <div class="metadata">
                    <div class="label">Tebliğ başlığı</div>
                    <div class="title-value">{title}<br><span style="color:#6b7280;font-size:12px;">({teblig_no})</span></div>
                  </div>
                  <div class="rule"></div>

                  <section class="block">
                    <h2>Tebliğ kısa özeti</h2>
                    <p>{summary}</p>
                  </section>

                  <section class="block">
                    <h2>Kimleri etkiliyor?</h2>
                    <ul>{who_affected}</ul>
                  </section>

                  <section class="block">
                    <h2>Tebliğe dair dikkat edilecek tarihler</h2>
                    <ul>{dates}</ul>
                  </section>
                  <div class="rule"></div>

                  {f'<aside class="note"><h2>Tebliğe dair not</h2><p>{note}</p></aside>' if note else ''}
                  {table_html}

                  <div class="source">
                    <span class="source-label">Kaynak:</span>
                    <a href="{source_url}" target="_blank" rel="noreferrer">{source_label}</a>
                  </div>
                </article>
                """

        html_content = f"""<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ATEZ Mevzuat Radarı — {formatted_date} Raporu</title>
  {css_block}
</head>
<body>
  <main class="report">
    <header>
      <div class="topbar">
        <span style="color:#fff;font-weight:800;font-size:18px;letter-spacing:1px;">ATEZ</span>
        <span class="report-id">Rapor: {report_id}</span>
      </div>
      <div class="header">
        <div class="header-grid">
          <h1 class="report-title">ATEZ Mevzuat Radarı Günlük Raporu</h1>
          <div class="issue">
            <div>{rg_sayisi} Sayılı Resmî Gazete</div>
            <div class="date"><span>{formatted_date}</span></div>
          </div>
        </div>
      </div>
    </header>

    <section class="overview">
      <h2 class="section-heading">Günün özeti ve değerlendirme</h2>
      <p>{overview_text}</p>
    </section>

    <section class="stats" aria-label="Günlük mevzuat özeti">
      <div class="stat"><strong>{stat_new}</strong><span>Yeni tebliğ</span></div>
      <div class="stat"><strong>{stat_changed}</strong><span>Değişiklik / düzenleme</span></div>
      <div class="stat"><strong>{stat_removed}</strong><span>Yürürlükten kaldırılan</span></div>
    </section>

    <section class="feed">
      {cards_html}
    </section>

    <section class="fine">
      <div class="rule"></div>
      <h2>Kaynak sınıflandırma sistemi</h2>
      <p>ATEZ Mevzuat Bülteni’ndeki tüm veriler doğrudan Resmî Gazete, Ticaret Bakanlığı ve diğer ilgili gümrük idarelerinin resmî web sitelerinden derlenmektedir.</p>
      <h2>Yasal uyarı / sorumluluk reddi</h2>
      <p>Bu bültende yer alan gümrük ve dış ticaret mevzuatı bildirimleri yalnızca bilgilendirme amaçlı olup, hukuki bir tavsiye veya nihai karar niteliği taşımamaktadır.</p>
    </section>

    <footer class="footer">
      <div class="footer-top">
        <h2 class="footer-head">İletişim &amp; destek</h2>
        <p>mevzuat@atezyazilim.com &nbsp;|&nbsp; +90 (212) 000 0000</p>
      </div>
      <div class="footer-rule"></div>
      <div class="footer-bottom">
        <div class="links"><span>Web Sitemiz</span><span>Gizlilik Politikası</span><span>Abonelikten Çık</span></div>
        <p class="copyright">© 2026 Atez Yazılım Teknolojileri A.Ş. Tüm Hakları Saklıdır.</p>
      </div>
    </footer>
  </main>
</body>
</html>
"""
        html_path = self.output_dir / f"gunluk-mevzuat-bulteni-{self.iso_date}.html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        # Also write mailing.html
        mailing_path = self.output_dir / "mailing.html"
        with open(mailing_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        logger.info(f"HTML Rapor oluşturuldu: {html_path}")
        return html_path

    def generate_pdf(self, html_path: Path) -> Path:
        """
        Renders HTML to a single-page pixel-perfect PDF using Playwright.
        """
        pdf_path = self.output_dir / f"gunluk-mevzuat-bulteni-{self.iso_date}.pdf"
        logger.info(f"PDF oluşturuluyor: {pdf_path}")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"file://{html_path.resolve()}", wait_until="networkidle")
            
            # Calculate height for single long page
            height = page.evaluate("() => document.documentElement.scrollHeight")
            page.pdf(
                path=str(pdf_path),
                width="760px",
                height=f"{height + 40}px",
                print_background=True,
                margin={"top": "0px", "right": "0px", "bottom": "0px", "left": "0px"},
            )
            browser.close()

        logger.info(f"PDF Rapor başarıyla oluşturuldu: {pdf_path}")
        return pdf_path

    def upload_reports_to_drive(self) -> Dict[str, str]:
        """
        Uploads generated HTML and PDF to Drive reports/r01/
        """
        if not self.uploader.service:
            logger.warning("Drive servisi bağlı değil.")
            return {}

        folder_ids = self.uploader.ensure_date_hierarchy(self.iso_date)
        reports_parent_id = folder_ids["reports"]
        r01_id = self.uploader.find_or_create_folder("r01", reports_parent_id)

        uploaded = {}
        for file in self.output_dir.iterdir():
            if file.is_file():
                file_id, web_link = self.uploader.upload_file(file, r01_id)
                uploaded[file.name] = web_link

        logger.info(f"Raporlar Drive'a yüklendi: {uploaded}")
        return uploaded
