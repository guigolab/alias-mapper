"""
bootstrap.py
------------
First-run setup: download the latest alias TSV from GitHub Releases
and build a local SQLite database.

The CLI checks for a cached local DB on startup. If it's missing,
this module runs to bring it into being. The same code path is
exposed as `alias-mapper update` for manual refresh.

Design notes:
  - The TSV is the source of truth; the local DB is a derived,
    disposable cache. `update` always does a full rebuild rather
    than trying to merge new rows into an existing DB.
  - Latest-release discovery uses the GitHub API to find the most
    recent `data-*` tagged release. We don't trust filesystem dates
    or any client-side heuristic; the server is authoritative.
  - All failure paths print a clear manual fallback (download the
    TSV yourself, run build_alias_db.py yourself) so a user with a
    flaky network or rate-limited GitHub can always work around it.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import platformdirs

from .build_alias_db import build_db
from .alias_source import StaleSchemaError, verify_schema_version
from ._ssl import SSL_CONTEXT


# GitHub repo coordinates. Constants up here so they're easy to change
# if the project moves or someone forks it.
GITHUB_OWNER = "Max25R"
GITHUB_REPO = "alias-mapper"
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases"

# Name of the TSV asset on each data release.
TSV_ASSET_NAME = "aliases.tsv.gz"

# Prefix that identifies data releases (vs. code releases like v1.0.0).
DATA_RELEASE_PREFIX = "data-"

# Filename for the local cached database.
LOCAL_DB_NAME = "aliases.db"

# The downloaded TSV is kept alongside the DB after a successful build
# (rather than deleted) so a schema-only rebuild — triggered when the
# user upgrades the CLI and their cached DB no longer matches the
# expected schema — can run from the local copy without a network round
# trip. `alias-mapper update` still re-downloads, since its whole purpose
# is to fetch fresher data.
LOCAL_TSV_NAME = TSV_ASSET_NAME

# User-Agent string for HTTP requests. GitHub appreciates a real
# identifier; some endpoints reject requests without one.
USER_AGENT = f"{GITHUB_REPO}/bootstrap (https://github.com/{GITHUB_OWNER}/{GITHUB_REPO})"


class BootstrapError(Exception):
    """Raised when first-run setup fails. The CLI translates this into a user-facing error."""


def default_cache_path() -> Path:
    """
    Return the platform-appropriate location for the cached DB.

    macOS:   ~/Library/Caches/alias-mapper/aliases.db
    Linux:   ~/.cache/alias-mapper/aliases.db
    Windows: %LOCALAPPDATA%\\alias-mapper\\Cache\\aliases.db
    """
    cache_dir = Path(platformdirs.user_cache_dir(GITHUB_REPO))
    return cache_dir / LOCAL_DB_NAME


def _http_get_json(url: str):
    """GET a URL, parse the JSON response, return the parsed object."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        if e.code == 403 and "rate limit" in body.lower():
            raise BootstrapError(
                "GitHub API rate limit exceeded. Retry later, or download "
                f"{TSV_ASSET_NAME} manually from "
                f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
            )
        raise BootstrapError(f"GitHub API returned HTTP {e.code}: {e}")
    except urllib.error.URLError as e:
        raise BootstrapError(f"could not reach GitHub API: {e.reason}")


def find_latest_data_release_url() -> str:
    """
    Query the GitHub API for the most recent data-* release.

    Returns the browser_download_url of its aliases.tsv.gz asset.
    Raises BootstrapError if no data release is found or the API
    call fails.
    """
    print(f"  Looking up latest data release on GitHub...", file=sys.stderr)
    releases = _http_get_json(RELEASES_API_URL)

    # Filter to data-* tags. The API returns releases sorted newest-first
    # by created_at, so the first match is the most recent.
    data_releases = [
        r for r in releases
        if r.get("tag_name", "").startswith(DATA_RELEASE_PREFIX)
    ]

    if not data_releases:
        raise BootstrapError(
            f"no data release found in the {GITHUB_OWNER}/{GITHUB_REPO} repo. "
            f"This shouldn't happen unless the weekly workflow has never run "
            f"successfully. Check "
            f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
        )

    latest = data_releases[0]
    tag = latest.get("tag_name", "<unknown>")
    print(f"  Found: {tag}", file=sys.stderr)

    # Find the aliases.tsv.gz asset.
    for asset in latest.get("assets", []):
        if asset.get("name") == TSV_ASSET_NAME:
            return asset["browser_download_url"]

    raise BootstrapError(
        f"data release {tag} exists but does not contain {TSV_ASSET_NAME}. "
        f"Check the assets at "
        f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/tag/{tag}"
    )


def download_with_progress(url: str, dest: Path) -> None:
    """
    Stream a URL to a local file, printing a progress line as bytes arrive.

    Writes to a `.part` file first and renames on success, so an
    interrupted download doesn't leave a half-finished file looking
    like a finished one.
    """
    part = dest.with_suffix(dest.suffix + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as response:
            total_str = response.headers.get("Content-Length")
            total = int(total_str) if total_str else None
            downloaded = 0
            chunk_size = 64 * 1024  # 64 KB

            with open(part, "wb") as f:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    _print_progress(downloaded, total)

            # Newline after the carriage-return progress line so the next
            # stderr message doesn't overwrite it.
            print("", file=sys.stderr)

    except urllib.error.HTTPError as e:
        part.unlink(missing_ok=True)
        raise BootstrapError(f"download failed with HTTP {e.code}: {e}")
    except urllib.error.URLError as e:
        part.unlink(missing_ok=True)
        raise BootstrapError(f"download failed: {e.reason}")
    except KeyboardInterrupt:
        part.unlink(missing_ok=True)
        raise

    # Atomic rename: only after the full download succeeds.
    part.replace(dest)


def _print_progress(downloaded: int, total: int | None) -> None:
    """Print a one-line progress indicator that updates in place."""
    mb_down = downloaded / (1024 * 1024)
    if total:
        mb_total = total / (1024 * 1024)
        pct = (downloaded / total) * 100
        msg = f"  Downloading... {mb_down:6.2f} MB / {mb_total:6.2f} MB ({pct:5.1f}%)"
    else:
        msg = f"  Downloading... {mb_down:6.2f} MB"
    sys.stderr.write("\r" + msg)
    sys.stderr.flush()


def ensure_db(db_path: Path | None = None, force: bool = False) -> Path:
    """
    Ensure a local DB exists, downloading and building it if needed.

    Args:
        db_path: Where to place the DB. Defaults to the platform cache path.
        force:   If True, rebuild even if the DB already exists. Used by
                 `alias-mapper update`.

    Returns:
        The path to the DB (same as db_path, or the default if None).

    Raises:
        BootstrapError on any failure, with a message including a manual
        workaround.
    """
    if db_path is None:
        db_path = default_cache_path()

    # Tracks why we're (re)building, which decides whether a locally
    # cached TSV may be reused instead of re-downloading.
    stale_schema_rebuild = False

    if db_path.exists() and not force:
        # Check schema version; a stale cache forces a rebuild even
        # if the user didn't ask for one. This is the silent-upgrade
        # path when a user updates the CLI and their old DB no longer
        # matches the expected schema.
        try:
            verify_schema_version(db_path)
            return db_path
        except StaleSchemaError as e:
            print(
                f"Cached alias DB has stale schema ({e}); rebuilding...",
                file=sys.stderr,
            )
            force = True
            stale_schema_rebuild = True

    if force and db_path.exists():
        print(f"Refreshing alias database at {db_path}", file=sys.stderr)
    else:
        print(f"No local alias database found. Setting up...", file=sys.stderr)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    tsv_path = db_path.parent / LOCAL_TSV_NAME

    # A schema-only rebuild can reuse a cached TSV if one is present:
    # the data is current, only the DB shape is stale, so there's no
    # need to touch the network. An explicit `update` (force=True but
    # not stale_schema_rebuild) always re-downloads — fetching fresher
    # data is the point.
    if stale_schema_rebuild and tsv_path.exists():
        print(
            f"  Reusing cached TSV at {tsv_path} (offline rebuild).",
            file=sys.stderr,
        )
        try:
            build_db(tsv_path, db_path)
        except (FileNotFoundError, ValueError) as e:
            # The cached TSV is unusable (corrupt or itself stale-format).
            # Fall through to a fresh download rather than failing.
            print(
                f"  Cached TSV could not be used ({e}); downloading fresh...",
                file=sys.stderr,
            )
        else:
            print(f"  Cached at {db_path}", file=sys.stderr)
            return db_path

    # Either a first run, an explicit update, or a stale-schema rebuild
    # with no usable cached TSV: download and build.
    downloaded_this_call = False
    try:
        url = find_latest_data_release_url()
        download_with_progress(url, tsv_path)
        downloaded_this_call = True
        print(f"  Building local database from TSV...", file=sys.stderr)
        try:
            build_db(tsv_path, db_path)
        except (FileNotFoundError, ValueError) as e:
            raise BootstrapError(f"build failed: {e}")
    except BootstrapError:
        # If the download itself failed, don't leave a partial/freshly
        # downloaded TSV behind. (download_with_progress already cleans
        # up its own .part file; this guards a TSV that downloaded but
        # then failed to build.)
        if downloaded_this_call:
            tsv_path.unlink(missing_ok=True)
        raise

    # Keep the TSV on success — it's the offline-rebuild fallback for the
    # next schema bump. It's the same artifact a future `update` would
    # overwrite anyway, so the only cost is ~9 MB of cache.
    print(f"  Cached at {db_path}", file=sys.stderr)
    return db_path
