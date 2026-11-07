"""Small injectable JSON transport. No retries, background refresh or persistence."""

import json
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from threading import Lock
from time import monotonic, sleep, time
from typing import TYPE_CHECKING, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from shelfmark.metadata_providers.editions.catalog_types import RateLimited, SourceError

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: object
    headers: Mapping[str, str]


class JsonTransport(Protocol):
    def get(self, path: str, parameters: Mapping[str, str], timeout: float) -> HttpResponse: ...


class JsonPostTransport(JsonTransport, Protocol):
    def post(self, path: str, data: Mapping[str, object], timeout: float) -> HttpResponse: ...


class _NoCredentialRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        del req, fp, newurl  # Required urllib override signature; redirects are always refused.
        raise SourceError("Authenticated metadata redirects are not supported")


class HttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        min_interval: float = 0.25,
        api_key: str = "",
        user_agent: str = "Shelfmark/editions",
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Expected a trusted base URL without credentials/query")
        if not 0 <= min_interval <= 30:
            raise ValueError("Invalid request interval")
        if api_key and parsed.scheme != "https":
            raise ValueError("Metadata API keys require HTTPS")
        if not user_agent.strip() or any(c in api_key + user_agent for c in "\r\n"):
            raise ValueError("Invalid metadata authentication headers")
        self._headers = {"Accept": "application/json", "User-Agent": user_agent}
        if api_key:
            self._headers["X-API-Key"] = api_key
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self._lock = Lock()
        self._next = 0.0

    def get(self, path: str, parameters: Mapping[str, str], timeout: float) -> HttpResponse:
        return self._request(path, parameters, timeout)

    def post(self, path: str, data: Mapping[str, object], timeout: float) -> HttpResponse:
        payload = json.dumps(dict(data), allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > 16 * 1024:
            raise SourceError("Metadata request exceeded size limit")
        return self._request(path, {}, timeout, payload)

    def _request(
        self, path: str, parameters: Mapping[str, str], timeout: float, payload: bytes | None = None
    ) -> HttpResponse:
        if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            raise ValueError("Invalid source path")
        started = monotonic()
        if not self._lock.acquire(timeout=max(0, timeout)):
            raise SourceError("Request pacing deadline exceeded")
        try:
            delay = max(0, self._next - monotonic())
            if delay >= timeout - (monotonic() - started):
                raise RateLimited(delay)
            if delay:
                sleep(delay)
            self._next = monotonic() + self.min_interval
        finally:
            self._lock.release()
        headers = dict(self._headers)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path + ("?" + urlencode(parameters) if parameters else ""),
            headers=headers,
            data=payload,
        )
        try:
            remaining = max(0.01, timeout - (monotonic() - started))
            response = (
                build_opener(_NoCredentialRedirect()).open(request, timeout=remaining)
                if "X-API-Key" in headers
                else urlopen(request, timeout=remaining)
            )
        except HTTPError as exc:
            response = exc
        except (URLError, OSError, TimeoutError) as exc:
            raise SourceError("Metadata service unavailable") from exc
        with response:
            headers = {key.lower(): value for key, value in response.headers.items()}
            if response.status in (429, 503):
                raw = headers.get("retry-after", "30")
                try:
                    delay = float(raw)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(raw).timestamp() - time()
                    except TypeError, ValueError, OverflowError:
                        delay = 30
                delay = max(1, min(delay, 86400))
                with self._lock:
                    self._next = max(self._next, monotonic() + delay)
                raise RateLimited(delay)
            raw_body = response.read(4 * 1024 * 1024 + 1)
            status = response.status
            if not isinstance(raw_body, bytes) or not isinstance(status, int):
                raise SourceError("Invalid metadata HTTP response")
            if len(raw_body) > 4 * 1024 * 1024:
                raise SourceError("Metadata response exceeded size limit")
            try:

                def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
                    result: dict[str, object] = {}
                    for key, value in pairs:
                        if key in result:
                            raise SourceError("Duplicate JSON field from metadata source")
                        result[key] = value
                    return result

                body: object = json.loads(raw_body, object_pairs_hook=unique_object)
            except (ValueError, UnicodeError) as exc:
                raise SourceError("Invalid metadata JSON") from exc
            return HttpResponse(status, body, headers)
