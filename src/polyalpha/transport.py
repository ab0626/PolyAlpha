"""GET-only transport with bounded retries and request spacing."""

import json
import time
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class Transport(Protocol):
    def get(self, url: str, params: dict) -> tuple[Any, datetime]: ...


class PublicHTTP:
    def __init__(self, timeout: float = 20, attempts: int = 3):
        if timeout <= 0 or attempts < 1:
            raise ValueError("invalid HTTP settings")
        self.timeout, self.attempts = timeout, attempts

    def get(self, url: str, params: dict) -> tuple[Any, datetime]:
        request = Request(
            url + "?" + urlencode(params),
            headers={"User-Agent": "polyalpha-research/0.1"},
        )
        for attempt in range(self.attempts):
            time.sleep(0.1)
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    received = datetime.now(UTC)
                return json.loads(body), received
            except HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == self.attempts - 1:
                    raise
                retry_after = error.headers.get("Retry-After", "")
                delay = min(30, float(retry_after)) if retry_after.isdigit() else 2**attempt
            except (URLError, TimeoutError):
                if attempt == self.attempts - 1:
                    raise
                delay = 2**attempt
            time.sleep(delay)
        raise RuntimeError("unreachable")
