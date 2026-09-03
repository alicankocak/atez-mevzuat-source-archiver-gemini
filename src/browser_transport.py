from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import PurePosixPath
from typing import Iterable
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from src.config import DEFAULT_USER_AGENT


OFFICIAL_HOSTS = frozenset({"resmigazete.gov.tr", "www.resmigazete.gov.tr"})
_BINARY_SUFFIXES = frozenset({".pdf", ".jpg", ".jpeg", ".png", ".gif"})
_MAX_REDIRECTS = 10
MAX_RESPONSE_BYTES = 50 * 1024 * 1024


class BrowserTransportError(RuntimeError):
    """Base error for official-source transport failures."""


class UnsafeSourceUrl(BrowserTransportError):
    """Raised before a URL outside the approved origins can be requested."""


class RetryableTransportError(BrowserTransportError):
    """Raised for transient browser or network failures."""


class InvalidSourceResponse(BrowserTransportError):
    """Raised when downloaded bytes do not satisfy the source contract."""

    def __init__(self, message: str, *, response=None) -> None:
        super().__init__(message)
        self.response = response


@dataclass(frozen=True)
class BrowserResponse:
    status: int
    final_url: str
    content_type: str
    body: bytes


def _parsed_port(url: str) -> int | None:
    try:
        return urlparse(url).port
    except ValueError as exc:
        raise UnsafeSourceUrl(f"Invalid source URL: {url}") from exc


def _origin(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    port = _parsed_port(url)
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    port_part = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme.lower()}://{host}{port_part}"


def validate_official_url(url: str) -> None:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeSourceUrl(f"Invalid source URL: {url}") from exc

    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() not in OFFICIAL_HOSTS
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise UnsafeSourceUrl(f"Source URL is outside the official allowlist: {url}")


def _media_type(content_type: str) -> str:
    return content_type.partition(";")[0].strip().lower()


def _url_suffix(url: str) -> str:
    return PurePosixPath(urlparse(url).path).suffix.lower()


def _expected_binary_kind(requested_url: str, response: BrowserResponse) -> str | None:
    suffixes = {_url_suffix(requested_url), _url_suffix(response.final_url)}
    media_type = _media_type(response.content_type)

    if ".pdf" in suffixes or media_type == "application/pdf":
        return "pdf"
    if ".png" in suffixes or media_type == "image/png":
        return "png"
    if suffixes.intersection({".jpg", ".jpeg"}) or media_type == "image/jpeg":
        return "jpeg"
    if ".gif" in suffixes or media_type == "image/gif":
        return "gif"
    return None


def validate_browser_response(
    requested_url: str,
    response: BrowserResponse,
    *,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
) -> None:
    if not 200 <= response.status < 300:
        raise InvalidSourceResponse(
            f"Source returned HTTP {response.status}: {response.final_url}",
            response=response,
        )
    if not response.body:
        raise InvalidSourceResponse(
            f"Source returned an empty body: {response.final_url}",
            response=response,
        )
    if len(response.body) > max_response_bytes:
        raise InvalidSourceResponse(
            f"Source exceeded the {max_response_bytes}-byte limit: {response.final_url}",
            response=response,
        )

    media_type = _media_type(response.content_type)
    binary_kind = _expected_binary_kind(requested_url, response)
    allowed_media_types = {
        "pdf": {"application/pdf", "application/octet-stream", "binary/octet-stream"},
        "png": {"image/png", "application/octet-stream", "binary/octet-stream"},
        "jpeg": {"image/jpeg", "application/octet-stream", "binary/octet-stream"},
        "gif": {"image/gif", "application/octet-stream", "binary/octet-stream"},
    }
    signatures = {
        "pdf": (b"%PDF-",),
        "png": (b"\x89PNG\r\n\x1a\n",),
        "jpeg": (b"\xff\xd8\xff",),
        "gif": (b"GIF87a", b"GIF89a"),
    }

    if binary_kind is None:
        return
    if media_type not in allowed_media_types[binary_kind]:
        raise InvalidSourceResponse(
            f"Unexpected {response.content_type!r} content for {requested_url}",
            response=response,
        )
    if not response.body.startswith(signatures[binary_kind]):
        raise InvalidSourceResponse(
            f"Invalid {binary_kind.upper()} signature: {response.final_url}",
            response=response,
        )


def _normalize_test_origins(test_origins: Iterable[str] | None) -> frozenset[str]:
    normalized: set[str] = set()
    for origin in test_origins or ():
        parsed = urlparse(origin)
        host = parsed.hostname or ""
        try:
            is_loopback = host.lower() == "localhost" or ip_address(host).is_loopback
            port = parsed.port
        except ValueError as exc:
            raise UnsafeSourceUrl(f"Invalid test origin: {origin}") from exc

        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not is_loopback
            or port is None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise UnsafeSourceUrl(f"Test origins must be explicit loopback origins: {origin}")
        normalized.add(_origin(origin))
    return frozenset(normalized)


class OfficialBrowserTransport:
    def __init__(
        self,
        *,
        test_origins: Iterable[str] | None = None,
        timeout_ms: int = 30_000,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self._test_origins = _normalize_test_origins(test_origins)
        self._timeout_ms = timeout_ms
        self._max_response_bytes = max_response_bytes

    def _validate_url(self, url: str) -> None:
        try:
            validate_official_url(url)
            return
        except UnsafeSourceUrl:
            parsed = urlparse(url)
            if (
                parsed.username is not None
                or parsed.password is not None
                or _origin(url) not in self._test_origins
            ):
                raise

    def fetch(self, url: str) -> BrowserResponse:
        self._validate_url(url)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    context = browser.new_context(
                        user_agent=DEFAULT_USER_AGENT,
                        ignore_https_errors=True,
                        service_workers="block",
                    )
                    try:
                        if PurePosixPath(urlparse(url).path).suffix.lower() in _BINARY_SUFFIXES:
                            response = self._fetch_bytes(context, url)
                        else:
                            response = self._fetch_html(context, url)
                        validate_browser_response(
                            url,
                            response,
                            max_response_bytes=self._max_response_bytes,
                        )
                        return response
                    finally:
                        context.close()
                finally:
                    browser.close()
        except UnsafeSourceUrl:
            raise
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise RetryableTransportError(f"Browser fetch failed for {url}: {exc}") from exc

    def _fetch_html(self, context, url: str) -> BrowserResponse:
        navigation_result: list[BrowserResponse] = []
        navigation_errors: list[BrowserTransportError] = []

        def constrain_request(route) -> None:
            request_url = route.request.url
            try:
                self._validate_url(request_url)
                routed_response = self._fetch_routed_request(route, request_url)
                if route.request.is_navigation_request():
                    navigation_result.append(routed_response)
                route.fulfill(
                    status=routed_response.status,
                    content_type=routed_response.content_type,
                    body=routed_response.body,
                )
            except BrowserTransportError as exc:
                if route.request.is_navigation_request():
                    navigation_errors.append(exc)
                route.abort()

        context.route("**/*", constrain_request)
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            if navigation_errors:
                raise navigation_errors[-1] from exc
            raise

        if navigation_errors:
            raise navigation_errors[-1]
        if not navigation_result:
            raise RetryableTransportError(f"Browser returned no response for {url}")
        return navigation_result[-1]

    def _fetch_routed_request(self, route, url: str) -> BrowserResponse:
        current_url = url
        for redirect_count in range(_MAX_REDIRECTS + 1):
            self._validate_url(current_url)
            options = {
                "max_redirects": 0,
                "timeout": self._timeout_ms,
            }
            if redirect_count:
                options["url"] = current_url
            response = route.fetch(**options)
            try:
                status = response.status
                headers = response.headers
                final_url = response.url
                self._validate_url(final_url)

                if status in {301, 302, 303, 307, 308} and "location" in headers:
                    next_url = urljoin(final_url, headers["location"])
                    self._validate_url(next_url)
                    current_url = next_url
                    continue

                self._reject_oversized_content_length(headers, final_url)
                body = response.body()
                return BrowserResponse(
                    status=status,
                    final_url=final_url,
                    content_type=headers.get("content-type", "application/octet-stream"),
                    body=body,
                )
            finally:
                response.dispose()

        raise RetryableTransportError(f"Too many redirects while fetching {url}")

    def _fetch_bytes(self, context, url: str) -> BrowserResponse:
        current_url = url
        for _ in range(_MAX_REDIRECTS + 1):
            self._validate_url(current_url)
            response = context.request.get(
                current_url,
                fail_on_status_code=False,
                max_redirects=0,
                timeout=self._timeout_ms,
            )
            try:
                status = response.status
                headers = response.headers
                final_url = response.url
                self._validate_url(final_url)

                if status in {301, 302, 303, 307, 308} and "location" in headers:
                    current_url = urljoin(final_url, headers["location"])
                    continue

                self._reject_oversized_content_length(headers, final_url)
                body = response.body()
                return BrowserResponse(
                    status=status,
                    final_url=final_url,
                    content_type=headers.get("content-type", "application/octet-stream"),
                    body=body,
                )
            finally:
                response.dispose()

        raise RetryableTransportError(f"Too many redirects while fetching {url}")

    def _reject_oversized_content_length(self, headers, url: str) -> None:
        content_length = headers.get("content-length")
        if content_length is None:
            return
        try:
            is_oversized = int(content_length) > self._max_response_bytes
        except ValueError:
            return
        if is_oversized:
            raise InvalidSourceResponse(
                f"Source exceeded the {self._max_response_bytes}-byte limit: {url}"
            )
