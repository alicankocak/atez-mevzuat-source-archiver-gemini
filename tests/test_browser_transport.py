import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
from playwright.sync_api import Browser, Page

import src.browser_transport as browser_transport_module
import src.fetcher as fetcher_module
from src.browser_transport import (
    BrowserResponse,
    InvalidSourceResponse,
    OfficialBrowserTransport,
    RetryableTransportError,
    UnsafeSourceUrl,
    validate_browser_response,
    validate_official_url,
)
from src.fetcher import MevzuatFetcher


PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\nfixture"
IFRAME_HTML_BYTES = (
    b'<!doctype html><title>main source</title><iframe src="/document.pdf"></iframe>'
)


@pytest.fixture
def local_server():
    state = {"outside_hits": 0, "websocket_hits": 0}

    class WebSocketHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["websocket_hits"] += 1
            self.send_response(400)
            self.end_headers()

        def log_message(self, _format, *_args):
            pass

    websocket_server = ThreadingHTTPServer(("127.0.0.1", 0), WebSocketHandler)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/escape":
                self.send_response(302)
                self.send_header(
                    "Location", f"http://localhost:{self.server.server_port}/outside"
                )
                self.end_headers()
                return

            if self.path == "/outside":
                state["outside_hits"] += 1

            if self.path == "/slow-frame.html":
                time.sleep(0.4)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<!doctype html><title>slow frame</title>")
                return

            if self.path == "/websocket.html":
                body = f"""
                    <!doctype html><title>source</title>
                    <script>
                      new WebSocket('ws://127.0.0.1:{websocket_server.server_port}/socket');
                    </script>
                    <iframe src="/slow-frame.html"></iframe>
                """.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path == "/iframe.html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(IFRAME_HTML_BYTES)
                return

            if self.path == "/document.pdf":
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.end_headers()
                self.wfile.write(PDF_BYTES)
                return

            if self.path == "/download":
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.end_headers()
                self.wfile.write(PDF_BYTES)
                return

            if self.path == "/redirect-to-pdf.html":
                self.send_response(302)
                self.send_header("Location", "/document.pdf")
                self.end_headers()
                return

            if self.path == "/attachment.png":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                self.wfile.write(PNG_BYTES)
                return

            if self.path == "/image-download":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                self.wfile.write(PNG_BYTES)
                return

            if self.path == "/redirect-to-image.html":
                self.send_response(302)
                self.send_header("Location", "/attachment.png")
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<!doctype html><title>fixture</title>")

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    websocket_thread = Thread(target=websocket_server.serve_forever, daemon=True)
    thread.start()
    websocket_thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"

    try:
        yield SimpleNamespace(
            origins={origin},
            escape_url=f"{origin}/escape",
            html_url=f"{origin}/index.html",
            pdf_url=f"{origin}/document.pdf",
            extensionless_pdf_url=f"{origin}/download",
            redirected_pdf_url=f"{origin}/redirect-to-pdf.html",
            image_url=f"{origin}/attachment.png",
            extensionless_image_url=f"{origin}/image-download",
            redirected_image_url=f"{origin}/redirect-to-image.html",
            websocket_url=f"{origin}/websocket.html",
            iframe_url=f"{origin}/iframe.html",
            state=state,
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        websocket_server.shutdown()
        websocket_thread.join()
        websocket_server.server_close()


@pytest.mark.parametrize(
    "url",
    [
        "http://resmigazete.gov.tr/14.08.2026",
        "https://example.com/file.pdf",
        "https://resmigazete.gov.tr.evil.test/file.pdf",
    ],
)
def test_rejects_non_official_urls(url):
    with pytest.raises(UnsafeSourceUrl):
        validate_official_url(url)


def test_rejects_redirect_leaving_official_hosts(local_server):
    with pytest.raises(UnsafeSourceUrl):
        OfficialBrowserTransport(test_origins=local_server.origins).fetch(
            local_server.escape_url
        )
    assert local_server.state["outside_hits"] == 0


@pytest.mark.parametrize(
    ("url", "response"),
    [
        (
            "https://resmigazete.gov.tr/document.html",
            BrowserResponse(200, "https://resmigazete.gov.tr/document.html", "text/html", b""),
        ),
        (
            "https://resmigazete.gov.tr/document.html",
            BrowserResponse(
                503,
                "https://resmigazete.gov.tr/document.html",
                "text/html",
                b"temporarily unavailable",
            ),
        ),
        (
            "https://resmigazete.gov.tr/document.pdf",
            BrowserResponse(
                200,
                "https://resmigazete.gov.tr/document.pdf",
                "text/html; charset=utf-8",
                b"<!doctype html><title>Error</title>",
            ),
        ),
        (
            "https://resmigazete.gov.tr/document.pdf",
            BrowserResponse(
                200,
                "https://resmigazete.gov.tr/document.pdf",
                "application/pdf",
                b"not a pdf",
            ),
        ),
        (
            "https://resmigazete.gov.tr/attachment.png",
            BrowserResponse(
                200,
                "https://resmigazete.gov.tr/attachment.png",
                "image/png",
                b"not a png",
            ),
        ),
    ],
)
def test_rejects_invalid_source_responses(url, response):
    with pytest.raises(InvalidSourceResponse):
        validate_browser_response(url, response)


def test_rejects_oversized_response():
    response = BrowserResponse(
        200,
        "https://resmigazete.gov.tr/document.html",
        "text/html",
        b"123456789",
    )

    with pytest.raises(InvalidSourceResponse):
        validate_browser_response(
            "https://resmigazete.gov.tr/document.html",
            response,
            max_response_bytes=8,
        )


@pytest.mark.parametrize(
    "body",
    [
        b"<!doctype html><title>Login</title><form><input type=password></form>",
        b"<!doctype html><title>Just a moment...</title><p>Checking your browser</p>",
        b"<!doctype html><title>Internal Server Error</title><p>request failed</p>",
    ],
)
def test_rejects_http_200_html_login_or_challenge(body):
    url = "https://resmigazete.gov.tr/document.html"
    response = BrowserResponse(200, url, "text/html; charset=utf-8", body)

    with pytest.raises(InvalidSourceResponse):
        validate_browser_response(url, response)


@pytest.mark.parametrize(
    "body",
    [
        (
            b"<!doctype html><title>Resmi Gazete</title>"
            b"<main><h1>Access denied</h1><p>Your request was blocked.</p></main>"
        ),
        (
            b"<!doctype html><title>Resmi Gazete</title>"
            b"<form action='/session'><input name='username'>"
            b"<input type='password' name='secret'></form>"
        ),
    ],
)
def test_rejects_http_200_html_structural_error_or_login_page(body):
    url = "https://resmigazete.gov.tr/document.html"
    response = BrowserResponse(200, url, "text/html; charset=utf-8", body)

    with pytest.raises(InvalidSourceResponse):
        validate_browser_response(url, response)


@pytest.mark.parametrize(
    "body",
    [
        (
            "<!doctype html><title>Resmî Gazete</title>"
            "<main><h1>Sayfa bulunamadı</h1></main>"
        ).encode("utf-8"),
        b"<!doctype html><title>404 Not Found</title><main></main>",
    ],
)
def test_rejects_http_200_explicit_not_found_title_or_heading(body):
    url = "https://resmigazete.gov.tr/document.html"
    response = BrowserResponse(200, url, "text/html; charset=utf-8", body)

    with pytest.raises(InvalidSourceResponse):
        validate_browser_response(url, response)


def test_accepts_gazette_prose_that_mentions_unauthorized_access():
    url = "https://resmigazete.gov.tr/document.html"
    body = (
        "<!doctype html>"
        "<title>Bilgi Güvenliği Tedbirleri Hakkında Tebliğ</title>"
        "<article>"
        "<h1>Bilgi Güvenliği Tedbirleri Hakkında Tebliğ</h1>"
        "<p>Bu Tebliğin amacı bilgi sistemlerinin güvenli işletilmesine ilişkin "
        "usul ve esasları düzenlemektir.</p>"
        "<p>Kurumlar, verileri yetkisiz erişimlere karşı korumak ve işlem kayıtlarını "
        "saklamak için gerekli idari ve teknik tedbirleri uygular.</p>"
        "<p>Yetkilendirme kayıtları düzenli olarak incelenir ve sonuçlar raporlanır.</p>"
        "</article>"
    ).encode("utf-8")
    response = BrowserResponse(200, url, "text/html; charset=utf-8", body)

    validate_browser_response(url, response)


@pytest.mark.parametrize(
    "title",
    [
        "Bakım Usul ve Esasları Hakkında Tebliğ",
        "Hata Payı Hesaplama Usulü Hakkında Tebliğ",
        "Giriş ve Bildirim İşlemleri Hakkında Tebliğ",
    ],
)
def test_accepts_legitimate_html_with_error_like_word_in_title(title):
    url = "https://resmigazete.gov.tr/document.html"
    body = f"<!doctype html><title>{title}</title><article><h1>{title}</h1></article>".encode(
        "utf-8"
    )
    response = BrowserResponse(200, url, "text/html; charset=utf-8", body)

    validate_browser_response(url, response)


def test_rejects_non_html_payload_masquerading_as_html_document():
    url = "https://resmigazete.gov.tr/document.html"
    response = BrowserResponse(200, url, "application/octet-stream", b"not html")

    with pytest.raises(InvalidSourceResponse):
        validate_browser_response(url, response)


@pytest.mark.parametrize(
    "playwright_error",
    [
        browser_transport_module.PlaywrightTimeoutError("timed out"),
        browser_transport_module.PlaywrightError("navigation failed"),
    ],
)
def test_playwright_failures_are_retryable(monkeypatch, playwright_error):
    def fail_to_start():
        raise playwright_error

    monkeypatch.setattr(browser_transport_module, "sync_playwright", fail_to_start)

    with pytest.raises(RetryableTransportError):
        OfficialBrowserTransport().fetch("https://resmigazete.gov.tr/document.html")


@pytest.mark.skipif(
    os.getenv("RUN_PLAYWRIGHT_INTEGRATION") != "1",
    reason="set RUN_PLAYWRIGHT_INTEGRATION=1 to run Chromium loopback coverage",
)
def test_fetches_html_pdf_and_image_bytes_from_loopback(local_server):
    transport = OfficialBrowserTransport(test_origins=local_server.origins)

    html = transport.fetch(local_server.html_url)
    pdf = transport.fetch(local_server.pdf_url)
    image = transport.fetch(local_server.image_url)

    assert html.status == 200
    assert html.content_type == "text/html; charset=utf-8"
    assert html.body == b"<!doctype html><title>fixture</title>"
    assert pdf.status == 200
    assert pdf.content_type == "application/pdf"
    assert pdf.body == PDF_BYTES
    assert image.status == 200
    assert image.content_type == "image/png"
    assert image.body == PNG_BYTES


@pytest.mark.skipif(
    os.getenv("RUN_PLAYWRIGHT_INTEGRATION") != "1",
    reason="set RUN_PLAYWRIGHT_INTEGRATION=1 to run Chromium loopback coverage",
)
def test_html_disables_javascript_and_cannot_open_websocket(
    monkeypatch, local_server
):
    with browser_transport_module.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(local_server.websocket_url)
        page.wait_for_timeout(300)
        browser.close()

    assert local_server.state["websocket_hits"] > 0
    local_server.state["websocket_hits"] = 0

    context_options = []
    real_new_context = Browser.new_context

    def record_context_options(browser, *args, **kwargs):
        context_options.append(kwargs)
        return real_new_context(browser, *args, **kwargs)

    monkeypatch.setattr(Browser, "new_context", record_context_options)

    transport = OfficialBrowserTransport(test_origins=local_server.origins)

    transport.fetch(local_server.websocket_url)

    assert context_options[-1]["java_script_enabled"] is False
    assert local_server.state["websocket_hits"] == 0


@pytest.mark.skipif(
    os.getenv("RUN_PLAYWRIGHT_INTEGRATION") != "1",
    reason="set RUN_PLAYWRIGHT_INTEGRATION=1 to run Chromium loopback coverage",
)
@pytest.mark.parametrize(
    ("url_attribute", "final_url_attribute", "content_type", "expected_body"),
    [
        (
            "extensionless_pdf_url",
            "extensionless_pdf_url",
            "application/pdf",
            PDF_BYTES,
        ),
        ("redirected_pdf_url", "pdf_url", "application/pdf", PDF_BYTES),
        (
            "extensionless_image_url",
            "extensionless_image_url",
            "image/png",
            PNG_BYTES,
        ),
        ("redirected_image_url", "image_url", "image/png", PNG_BYTES),
    ],
)
def test_binary_response_kind_is_resolved_before_html_navigation(
    local_server,
    url_attribute,
    final_url_attribute,
    content_type,
    expected_body,
):
    class RejectHtmlNavigationTransport(OfficialBrowserTransport):
        def _fetch_html(self, _context, _url):
            raise AssertionError("binary response entered HTML page navigation")

    transport = RejectHtmlNavigationTransport(test_origins=local_server.origins)

    response = transport.fetch(getattr(local_server, url_attribute))

    assert response.final_url == getattr(local_server, final_url_attribute)
    assert response.content_type == content_type
    assert response.body == expected_body


@pytest.mark.skipif(
    os.getenv("RUN_PLAYWRIGHT_INTEGRATION") != "1",
    reason="set RUN_PLAYWRIGHT_INTEGRATION=1 to run Chromium loopback coverage",
)
def test_fetch_returns_requested_main_document_not_iframe(monkeypatch, local_server):
    real_goto = Page.goto

    def wait_for_child_frame(page, *args, **kwargs):
        response = real_goto(page, *args, **kwargs)
        page.wait_for_timeout(300)
        return response

    monkeypatch.setattr(Page, "goto", wait_for_child_frame)
    transport = OfficialBrowserTransport(test_origins=local_server.origins)

    response = transport.fetch(local_server.iframe_url)

    assert response.final_url == local_server.iframe_url
    assert response.content_type == "text/html; charset=utf-8"
    assert response.body == IFRAME_HTML_BYTES


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.fetched_urls = []

    def fetch(self, url):
        self.fetched_urls.append(url)
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result


def test_fetcher_routes_fihrist_through_browser_transport(monkeypatch, tmp_path):
    url = "https://resmigazete.gov.tr/14.08.2026"
    response = BrowserResponse(
        status=200,
        final_url=url,
        content_type="text/html; charset=windows-1254",
        body="<!doctype html><title>Resmî Gazete</title>".encode("windows-1254"),
    )
    transport = FakeTransport({url: response})
    session_constructed = False

    def unexpected_requests_session():
        nonlocal session_constructed
        session_constructed = True
        raise AssertionError("fetcher must not construct a Requests download session")

    monkeypatch.setattr(
        fetcher_module,
        "requests",
        SimpleNamespace(Session=unexpected_requests_session),
        raising=False,
    )

    fetcher = MevzuatFetcher(
        "2026-08-14",
        output_base_dir=tmp_path,
        transport=transport,
    )

    assert fetcher.fetch_fihrist_page() == response
    assert transport.fetched_urls == [url]
    assert session_constructed is False


def test_fetcher_falls_back_to_alias_on_primary_server_error(tmp_path):
    primary_url = "https://resmigazete.gov.tr/14.08.2026"
    alias_url = "https://www.resmigazete.gov.tr/14.08.2026"
    primary_response = BrowserResponse(
        503,
        primary_url,
        "text/html; charset=utf-8",
        b"temporarily unavailable",
    )
    alias_response = BrowserResponse(
        200,
        alias_url,
        "text/html; charset=utf-8",
        b"<!doctype html><title>fixture</title>",
    )
    transport = FakeTransport(
        {
            primary_url: InvalidSourceResponse(
                "Source returned HTTP 503",
                response=primary_response,
            ),
            alias_url: alias_response,
        }
    )
    fetcher = MevzuatFetcher(
        "2026-08-14",
        output_base_dir=tmp_path,
        transport=transport,
    )

    assert fetcher.fetch_fihrist_page() == alias_response
    assert transport.fetched_urls == [primary_url, alias_url]


def test_run_rejects_fihrist_without_required_structure(tmp_path):
    url = "https://resmigazete.gov.tr/14.08.2026"
    fihrist_body = b"<!doctype html><title>Resmi Gazete</title><p>empty shell</p>"
    transport = FakeTransport(
        {url: BrowserResponse(200, url, "text/html; charset=utf-8", fihrist_body)}
    )
    fetcher = MevzuatFetcher(
        "2026-08-14",
        output_base_dir=tmp_path,
        transport=transport,
    )

    with pytest.raises(InvalidSourceResponse):
        fetcher.run()


def test_run_rejects_issue_page_without_index_structure(tmp_path):
    url = "https://resmigazete.gov.tr/14.08.2026"
    fihrist_body = (
        "<!doctype html><title>33299 Sayılı Resmî Gazete</title>"
        "<span id='spanGazeteTarih'>33299 Sayılı Resmî Gazete</span>"
        "<main><p>Bu sayfa bir fihrist değildir.</p></main>"
    ).encode("utf-8")
    transport = FakeTransport(
        {url: BrowserResponse(200, url, "text/html; charset=utf-8", fihrist_body)}
    )
    fetcher = MevzuatFetcher(
        "2026-08-14",
        output_base_dir=tmp_path,
        transport=transport,
    )

    with pytest.raises(InvalidSourceResponse):
        fetcher.run()


def test_run_accepts_valid_fihrist_without_teblig_section(tmp_path):
    url = "https://resmigazete.gov.tr/14.08.2026"
    fihrist_body = (
        "<!doctype html>"
        "<title>14 Ağustos 2026 Tarihli ve 33299 Sayılı Resmî Gazete</title>"
        "<span id='spanGazeteTarih'>33299 Sayılı Resmî Gazete</span>"
        "<h2 class='html-subtitle'>YÜRÜTME VE İDARE BÖLÜMÜ</h2>"
        "<div class='fihrist-item mb-1'>"
        "<a href='/eskiler/2026/08/20260814-1.pdf'>Cumhurbaşkanı Kararı</a>"
        "</div>"
    ).encode("utf-8")
    transport = FakeTransport(
        {url: BrowserResponse(200, url, "text/html; charset=utf-8", fihrist_body)}
    )
    fetcher = MevzuatFetcher(
        "2026-08-14",
        output_base_dir=tmp_path,
        transport=transport,
    )

    manifest, rg_dir = fetcher.run()

    assert manifest.resmi_gazete_sayisi == "33299"
    assert manifest.documents == []
    assert (rg_dir / "index.html").read_bytes() == fihrist_body
    assert (rg_dir / "source-manifest.json").is_file()


def test_download_file_preserves_transport_bytes_and_content_type(tmp_path):
    url = "https://resmigazete.gov.tr/document.pdf"
    response = BrowserResponse(
        status=200,
        final_url="https://www.resmigazete.gov.tr/document.pdf",
        content_type="application/pdf",
        body=PDF_BYTES + b"\x00\xff",
    )
    transport = FakeTransport({url: response})
    fetcher = MevzuatFetcher(
        "2026-08-14",
        output_base_dir=tmp_path,
        transport=transport,
    )
    target_path = tmp_path / "rg-33299" / "doc-01" / "source.pdf"

    manifest = fetcher.download_file(
        url,
        target_path,
        role="main_document",
        parent_doc_id="doc-01",
    )

    assert target_path.read_bytes() == response.body
    assert manifest.final_url == response.final_url
    assert manifest.http_status == response.status
    assert manifest.content_type == response.content_type
    assert manifest.size_bytes == len(response.body)
    assert transport.fetched_urls == [url]


@pytest.mark.parametrize(
    ("requested_url", "final_url"),
    [
        (
            "https://resmigazete.gov.tr/download?id=123",
            "https://resmigazete.gov.tr/download?id=123",
        ),
        (
            "https://resmigazete.gov.tr/document.html",
            "https://www.resmigazete.gov.tr/document.pdf",
        ),
    ],
)
def test_main_document_filename_follows_validated_pdf_response(
    tmp_path, requested_url, final_url
):
    response = BrowserResponse(200, final_url, "application/pdf", PDF_BYTES)
    fetcher = MevzuatFetcher(
        "2026-08-14",
        output_base_dir=tmp_path,
        transport=FakeTransport({requested_url: response}),
    )

    document = fetcher.process_teblig_document(
        {"title": "Fixture", "url": requested_url},
        doc_index=1,
        rg_dir=tmp_path / "rg-33299",
    )

    assert document.main_document.content_type == "application/pdf"
    assert document.main_document.local_relative_path.endswith("/source.pdf")
    assert (tmp_path / document.main_document.local_relative_path).read_bytes() == PDF_BYTES
    assert not (tmp_path / "rg-33299" / "doc-01" / "source.html").exists()


def test_document_fetches_only_https_official_attachments(tmp_path):
    document_url = "https://resmigazete.gov.tr/document.html"
    allowed_image_url = "https://resmigazete.gov.tr/allowed.png"
    allowed_pdf_url = "https://www.resmigazete.gov.tr/allowed.pdf"
    document_body = f"""
        <!doctype html>
        <a href="{allowed_pdf_url}">official PDF</a>
        <img src="/allowed.png">
        <a href="http://resmigazete.gov.tr/insecure.pdf">insecure</a>
        <a href="https://example.com/outside.pdf">outside</a>
        <a href="https://resmigazete.gov.tr.evil.test/deceptive.pdf">deceptive</a>
    """.encode()
    transport = FakeTransport(
        {
            document_url: BrowserResponse(
                200, document_url, "text/html; charset=utf-8", document_body
            ),
            allowed_image_url: BrowserResponse(
                200, allowed_image_url, "image/png", PNG_BYTES
            ),
            allowed_pdf_url: BrowserResponse(
                200, allowed_pdf_url, "application/pdf", PDF_BYTES
            ),
        }
    )
    fetcher = MevzuatFetcher(
        "2026-08-14",
        output_base_dir=tmp_path,
        transport=transport,
    )

    document = fetcher.process_teblig_document(
        {"title": "Fixture", "url": document_url},
        doc_index=1,
        rg_dir=tmp_path / "rg-33299",
    )

    assert transport.fetched_urls == [
        document_url,
        allowed_image_url,
        allowed_pdf_url,
    ]
    assert [attachment.source_url for attachment in document.attachments] == [
        allowed_image_url,
        allowed_pdf_url,
    ]


def test_attachment_validation_failure_aborts_document_archive(tmp_path):
    document_url = "https://resmigazete.gov.tr/document.html"
    attachment_url = "https://resmigazete.gov.tr/required.pdf"
    document_body = f'<a href="{attachment_url}">required attachment</a>'.encode()
    transport = FakeTransport(
        {
            document_url: BrowserResponse(
                200, document_url, "text/html; charset=utf-8", document_body
            ),
            attachment_url: InvalidSourceResponse("invalid PDF signature"),
        }
    )
    fetcher = MevzuatFetcher(
        "2026-08-14",
        output_base_dir=tmp_path,
        transport=transport,
    )

    with pytest.raises(InvalidSourceResponse):
        fetcher.process_teblig_document(
            {"title": "Fixture", "url": document_url},
            doc_index=1,
            rg_dir=tmp_path / "rg-33299",
        )

    assert not (tmp_path / "rg-33299" / "doc-01" / "manifest.json").exists()


def test_attachment_names_are_collision_safe_and_reserve_document_files(tmp_path):
    document_url = "https://resmigazete.gov.tr/document.html"
    first_duplicate_url = "https://resmigazete.gov.tr/a/shared.pdf"
    second_duplicate_url = "https://www.resmigazete.gov.tr/b/shared.pdf"
    reserved_url = "https://resmigazete.gov.tr/assets/source.html"
    response_bodies = {
        first_duplicate_url: PDF_BYTES + b"first",
        second_duplicate_url: PDF_BYTES + b"second",
        reserved_url: b"<!doctype html><title>attachment</title>",
    }
    document_body = (
        "<!doctype html><title>main document</title>"
        + "".join(f'<a href="{url}">attachment</a>' for url in response_bodies)
    ).encode()
    responses = {
        document_url: BrowserResponse(
            200, document_url, "text/html; charset=utf-8", document_body
        )
    }
    for url, body in response_bodies.items():
        content_type = "application/pdf" if url.endswith(".pdf") else "text/html"
        responses[url] = BrowserResponse(200, url, content_type, body)

    fetcher = MevzuatFetcher(
        "2026-08-14",
        output_base_dir=tmp_path,
        transport=FakeTransport(responses),
    )
    document = fetcher.process_teblig_document(
        {"title": "Fixture", "url": document_url},
        doc_index=1,
        rg_dir=tmp_path / "rg-33299",
    )

    relative_paths = [attachment.local_relative_path for attachment in document.attachments]
    assert len(relative_paths) == len(set(relative_paths)) == 3
    assert {
        Path(relative_path).name.casefold() for relative_path in relative_paths
    }.isdisjoint({"source.html", "source.pdf", "manifest.json"})
    assert (tmp_path / "rg-33299" / "doc-01" / "source.html").read_bytes() == document_body
    for attachment in document.attachments:
        archived_path = tmp_path / attachment.local_relative_path
        assert archived_path.read_bytes() == response_bodies[attachment.source_url]
