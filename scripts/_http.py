"""
scripts/_http.py
----------------
Shared HTTP machinery for the alias-mapper data pipeline scripts.

What's here:
  - HTTP error classes: PermanentHTTPError, TransientHTTPError
  - http_get_with_retry: exponential backoff on retryable errors
  - Re-exports of SSL_CONTEXT, SSL_BACKEND, HTTP_HEADERS from the
    installed package for backward compat with existing imports

The SSL setup (truststore -> certifi -> stdlib fallback) lives in the
installed package at src/alias_mapper/_ssl.py, single source of truth.
This script reaches into it via a sys.path hack identical to the one
in scripts/build_alias_db.py.

Note on SSL overrides: the caller can mutate `alias_mapper._ssl.SSL_CONTEXT`
at runtime to disable verification (the --insecure flag does this).
http_get_with_retry reads the context fresh on each call, so the mutation
is picked up automatically.
"""

import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Make the installed package importable when running from a source
# checkout without `pip install`. After `pip install`, this insert is
# harmless. Same hack as scripts/build_alias_db.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from alias_mapper import _ssl as _ssl_module
from alias_mapper._ssl import SSL_BACKEND


HTTP_HEADERS = {
    "User-Agent": "alias-mapper/2.0 (https://github.com/Max25R/alias-mapper)",
}


def _get_ssl_context():
    """
    Read the SSL context fresh from the _ssl module on every call.

    This is what makes the --insecure flag work: callers can mutate
    `alias_mapper._ssl.SSL_CONTEXT` to swap in an unverified context,
    and every subsequent http_get_with_retry picks up the new value
    without any further coordination.
    """
    return _ssl_module.SSL_CONTEXT


# Back-compat re-export. Existing callers do `from _http import SSL_CONTEXT`
# and reference it as a module attribute. Keep that working, but note that
# mutating *this* module's SSL_CONTEXT won't propagate — mutate
# alias_mapper._ssl.SSL_CONTEXT instead.
SSL_CONTEXT = _ssl_module.SSL_CONTEXT


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

    Reads the SSL context fresh on each attempt so runtime mutations
    (e.g. --insecure) take effect immediately.
    """
    last_detail = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers=HTTP_HEADERS)
            with urllib.request.urlopen(
                req, timeout=timeout, context=_get_ssl_context()
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
