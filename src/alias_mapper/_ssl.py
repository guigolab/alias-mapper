"""
_ssl.py
-------
Shared SSL context setup for the installed alias-mapper package.

Mirrors scripts/_http.py's setup, but lives inside the package so
bootstrap.py and any future HTTP-using module (e.g. HttpAliasSource)
can import it without depending on scripts/.

Order of preference: truststore > certifi > stdlib defaults.

  - truststore: uses the system keychain (necessary on networks with
    TLS inspection like CRG's, which inject a non-Mozilla root cert)
  - certifi: Mozilla's CA bundle, covers most environments including
    GitHub Actions runners
  - stdlib: last fallback, used if neither extra is installed

Both truststore and certifi are optional installs. The package will
work without them on any network where the system already trusts the
NCBI/GitHub cert chains.
"""

import ssl

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
