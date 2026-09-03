import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest

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


@pytest.fixture
def local_server():
    state = {"outside_hits": 0}

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

            if self.path == "/document.pdf":
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.end_headers()
                self.wfile.write(PDF_BYTES)
                return

            if self.path == "/attachment.png":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                self.wfile.write(PNG_BYTES)
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<!doctype html><title>fixture</title>")

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"

    try:
        yield SimpleNamespace(
            origins={origin},
            escape_url=f"{origin}/escape",
            html_url=f"{origin}/index.html",
            pdf_url=f"{origin}/document.pdf",
            image_url=f"{origin}/attachment.png",
            state=state,
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


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
