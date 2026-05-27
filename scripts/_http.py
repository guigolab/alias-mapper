"""
scripts/_http.py
----------------
Shared HTTP machinery for the alias-mapper data pipeline scripts.

What's here:
  - SSL context setup (truststore -> certifi -> stdlib fallback)
  - HTTP error classes: PermanentHTTPError, TransientHTTPError
  - http_get_with_retry: exponential backoff on retryable errors

Note on SSL: this module is imported at top of the calling script.
If the caller wants to override SSL behavior at runtime (e.g. an
--insecure flag), it can mutate `SSL_CONTEXT` after import. See
collect_aliases.py's main() for the pattern.

Duplicates the SSL setup in src/alias_mapper/_ssl.py because scripts/
can't easily import from the installed package without a sys.path hack.
Keep both in sync if you change one.
"""

import random
import ssl
import time
import urllib.error
import urllib.request


# -- SSL setup -------------------------------------------------------------
# Order of preference: truststore > certifi > stdlib defaults. truststore
# uses the system keychain (necessary on CRG wifi where TLS inspection
# injects a non-Mozilla root). certifi covers CI on Ubuntu. stdlib is
# the last fallback.
try:
    import truststore
    truststore.inject_into_ssl()
    SSL_BACKEND = "truststore"
except ImportError:
    SSL_BACKEND = None

try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
    if SSL_BACKEND is None:
        SSL_BACKEND = "certifi"
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()
    if SSL_BACKEND is None:
        SSL_BACKEND = "stdlib"
# --------------------------------------------------------------------------


HTTP_HEADERS = {
    "User-Agent": "alias-mapper/2.0 (https://github.com/Max25R/alias-mapper)",
}

# HTTP error classification.
PERMANENT_HTTP_CODES = frozenset({404, 410})
RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})

# Retry policy: 3 attempts, exponential backoff with jitter.
MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 2.0


class PermanentHTTPError(Exception):
    """Definitive failure: 404/410, or non-retryable 4xx. Don't retry."""
    def __init__(self, code: int, message: str):
        self.code = code
        super().__init__(message)


class TransientHTTPError(Exception):
    """All retries exhausted on transient errors. Worth trying again next week."""


def http_get_with_retry(url: str, timeout: int = 60) -> str:
    """
    Fetch a URL with retry on transient errors.

    Returns response body text on success.
    Raises PermanentHTTPError on 404/410/non-retryable 4xx.
    Raises TransientHTTPError if all retries are exhausted.
    """
    last_detail = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers=HTTP_HEADERS)
            with urllib.request.urlopen(
                req, timeout=timeout, context=SSL_CONTEXT
            ) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in PERMANENT_HTTP_CODES:
                raise PermanentHTTPError(e.code, f"HTTP {e.code}: {e.reason}")
            if e.code in RETRYABLE_HTTP_CODES:
                last_detail = f"HTTP {e.code}: {e.reason}"
            else:
                raise PermanentHTTPError(e.code, f"HTTP {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            last_detail = f"URLError: {e.reason}"

        if attempt < MAX_ATTEMPTS:
            sleep_for = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            sleep_for += random.uniform(0, sleep_for / 2)
            time.sleep(sleep_for)

    raise TransientHTTPError(
        f"all {MAX_ATTEMPTS} attempts failed for {url}: {last_detail}"
    )
