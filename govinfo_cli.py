#!/usr/bin/env python3
"""\
Script Name: govinfo_cli.py
Author: ChatGPT (assistant for cbwinslow)
Date: 2025-11-19

Summary:
    Command-line tool for ingesting bulk data from the GovInfo API.

    This script focuses on the GovInfo "published" and "packages" services:
      * Uses the Published service to enumerate packageIds across
        one or more collections over a date range.
      * Uses the Packages service to retrieve package summaries and
        download available content formats (PDF, XML, TXT, MODS, etc.).

    The script can be used to ingest the last N years of data for
    one or more collections (e.g., BILLS, BILLSTATUS, CREC, FR).

Inputs (CLI arguments):
    --api-key / GOVINFO_API_KEY
        GovInfo / api.data.gov API key (required, unless ENV var present).

    Subcommand: download
        --collections
            Comma-separated list of collection codes (e.g. BILLS,BILLSTATUS,CREC).
        --start-date / --end-date
            Explicit date range (YYYY-MM-DD). If omitted, you can use --last-years.
        --last-years
            Convenience flag to fetch from today back N calendar years.
        --page-size
            Number of records per API call (default: 100, max per docs: 1000).
        --output-dir
            Target directory for downloaded files (default: ./govinfo_data).
        --formats
            Comma-separated list of formats to download per package
            (e.g. pdf,xml,txt,mods,premis,zip). Default: pdf,xml.
        --max-packages
            Optional safety limit on the number of packages to download.
        --dry-run
            Enumerate packages and show planned downloads, but do not
            actually download any content.
        --verbose
            Enable debug-level logging.

Outputs:
    * Directory tree rooted at --output-dir containing subdirectories
      by collection and year, with one file per package and format.

      Example layout:
          ./govinfo_data/
              BILLS/
                  2019/
                      BILLS-116hr34enr.pdf
                      BILLS-116hr34enr.xml
              CREC/
                  2024/
                      CREC-2024-01-03.pdf

Key Design Notes:
    * Highly commented and modular for re-use.
    * Uses logging for progress and error reporting.
    * Tries to be resilient to minor schema variations in the API.
    * Safe defaults plus a --max-packages guard for huge ingests.

Modification Log:
    2025-11-19: Initial version generated for bulk GovInfo ingestion.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import time

try:
    import requests
except ImportError as exc:  # pragma: no cover - runtime-only guard
    print("[FATAL] The 'requests' package is required. Install with: pip install requests", file=sys.stderr)
    raise


# ---------------------------------------------------------------------------
# Constants & Simple Types
# ---------------------------------------------------------------------------

API_BASE = "https://api.govinfo.gov"

# Default page size; API docs say up to 1000 is allowed for collections/published.
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000

# Mapping between human-friendly format names and summary.download keys.
DOWNLOAD_KEY_MAP: Dict[str, str] = {
    "txt": "txtLink",
    "htm": "htmLink",  # may not always be present
    "html": "htmLink",
    "xml": "xmlLink",
    "pdf": "pdfLink",
    "mods": "modsLink",
    "premis": "premisLink",
    "zip": "zipLink",
}


@dataclass
class PackageRecord:
    """Simple representation of a package returned by Published or Collections.

    Attributes
    ----------
    package_id: str
        GovInfo packageId (e.g. "BILLS-115hr1625enr").
    date_issued: Optional[str]
        Date the item was issued (YYYY-MM-DD or ISO string) if available.
    raw: dict
        Raw JSON item returned by the API (for debugging/future use).
    """

    package_id: str
    date_issued: Optional[str]
    raw: Dict


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def setup_logging(verbose: bool = False) -> None:
    """Configure root logger with a sensible default format.

    Parameters
    ----------
    verbose : bool
        If True, sets level to DEBUG; otherwise INFO.
    """

    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def resolve_api_key(cli_key: Optional[str]) -> str:
    """Resolve the GovInfo API key from CLI or environment.

    Order of precedence:
        1. --api-key argument
        2. GOVINFO_API_KEY environment variable

    Raises
    ------
    SystemExit
        If no key can be resolved.
    """

    key = cli_key or os.getenv("GOVINFO_API_KEY")
    if not key:
        logging.error("GovInfo API key is required. Use --api-key or set GOVINFO_API_KEY.")
        raise SystemExit(1)
    return key


def parse_date_arg(date_str: str) -> str:
    """Validate a YYYY-MM-DD date string.

    Returns the same string if valid; raises ValueError otherwise.
    """

    try:
        _dt.datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Invalid date format '{date_str}'. Expected YYYY-MM-DD.") from exc
    return date_str


def compute_date_range_from_years(last_years: int) -> Tuple[str, str]:
    """Compute a start/end date string pair for the last N years.

    Parameters
    ----------
    last_years : int
        Number of whole years to look back from today.

    Returns
    -------
    (start_date, end_date) : tuple[str, str]
        Each in YYYY-MM-DD format.
    """

    if last_years <= 0:
        raise ValueError("--last-years must be a positive integer")

    today = _dt.date.today()
    # Rough approximation: N years ago on the same month/day.
    try:
        start = today.replace(year=today.year - last_years)
    except ValueError:
        # Handle Feb 29th or other edge cases by subtracting 365 * N days.
        start = today - _dt.timedelta(days=365 * last_years)

    return start.isoformat(), today.isoformat()


def ensure_output_dir(base_dir: Path) -> None:
    """Create the base output directory if it doesn't exist."""

    base_dir.mkdir(parents=True, exist_ok=True)


def package_collection_code(package_id: str) -> str:
    """Extract the collection code from a packageId.

    Example: "BILLS-115hr1625enr" -> "BILLS".
    """

    return package_id.split("-", 1)[0] if "-" in package_id else "UNKNOWN"


def package_year_from_date(date_issued: Optional[str]) -> str:
    """Extract a year string from a dateIssued field.

    Falls back to "unknown_year" if no parseable value is present.
    """

    if not date_issued:
        return "unknown_year"

    # Try a few common formats.
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = _dt.datetime.strptime(date_issued[:19], fmt)  # trim if needed
            return str(dt.year)
        except ValueError:
            continue

    # Fallback: try first 4 chars as year
    year = date_issued[:4]
    return year if year.isdigit() else "unknown_year"


def next_offset_from_nextpage(next_page: Optional[str]) -> Optional[str]:
    """Extract the next offsetMark from the API's nextPage value.

    The GovInfo API's discovery endpoints return a `nextPage` field
    that may be either:
      * a URL containing an `offsetMark` query parameter, OR
      * a string offset mark.
    """

    if not next_page:
        return None

    if "offsetMark=" in next_page and next_page.startswith("http"):
        parsed = urlparse(next_page)
        params = parse_qs(parsed.query)
        values = params.get("offsetMark")
        if values:
            return values[0]

    # Otherwise assume this *is* the offset mark.
    return next_page


# ---------------------------------------------------------------------------
# API interaction helpers
# ---------------------------------------------------------------------------


def fetch_published_packages(
    api_key: str,
    collections: Iterable[str],
    start_date: str,
    end_date: str,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_packages: Optional[int] = None,
) -> List[PackageRecord]:
    """Fetch package records via the Published service.

    This uses the `published` endpoint described in the GovInfo API docs:

        https://api.govinfo.gov/published/{startDate}/{endDate}?offsetMark=*&pageSize=100&collection=BILLS&api_key=...  # noqa: E501

    See: GovInfo API Quickstart (Published service).\
    (GitHub usgpo/api README, "Published Service" section.)

    Parameters
    ----------
    api_key : str
        GovInfo API key.
    collections : Iterable[str]
        One or more collection codes, e.g. ["BILLS", "BILLSTATUS"].
    start_date, end_date : str
        Date range in YYYY-MM-DD format.
    page_size : int
        Records per API request (max 1000 per docs).
    max_packages : Optional[int]
        Optional safety-limit on number of packages; None for no explicit limit.

    Returns
    -------
    List[PackageRecord]
        All packages discovered within the date and collection filters (up to max_packages).
    """

    if page_size <= 0 or page_size > MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")

    coll_param = ",".join(sorted({c.strip() for c in collections if c.strip()}))
    if not coll_param:
        raise ValueError("At least one collection code must be specified")

    packages: List[PackageRecord] = []
    offset_mark = "*"
    seen_offsets = set()

    while True:
        params = {
            "offsetMark": offset_mark,
            "pageSize": str(page_size),
            "collection": coll_param,
            "api_key": api_key,
        }

        url = f"{API_BASE}/published/{start_date}/{end_date}"
        logging.debug("Requesting published page: %s params=%s", url, params)

        try:
            resp = requests.get(url, params=params, timeout=60)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logging.error("Error fetching published data: %s", exc)
            break

        try:
            data = resp.json()
        except json.JSONDecodeError:
            logging.error("Failed to decode JSON response from published endpoint")
            break

        # The exact key name for the result set can vary; try some common ones.
        items = []
        for key in ("packages", "results", "records"):
            if isinstance(data.get(key), list):
                items = data[key]
                break

        if not items:
            logging.info("No more results (empty page).")
            break

        for item in items:
            pkg_id = item.get("packageId") or item.get("package_id")
            if not pkg_id:
                logging.debug("Skipping item without packageId: %s", item)
                continue

            date_issued = item.get("dateIssued") or item.get("date_issued")
            packages.append(PackageRecord(package_id=pkg_id, date_issued=date_issued, raw=item))

            if max_packages is not None and len(packages) >= max_packages:
                logging.warning(
                    "Reached max-packages limit (%d); stopping discovery.", max_packages
                )
                return packages

        # Handle pagination
        next_page_val = data.get("nextPage")
        next_offset = next_offset_from_nextpage(next_page_val)

        if not next_offset:
            logging.info("No nextPage value; finished traversal.")
            break

        if next_offset in seen_offsets:
            logging.warning("Detected repeated offsetMark '%s'; breaking to avoid loop.", next_offset)
            break

        seen_offsets.add(next_offset)
        offset_mark = next_offset

    return packages


def fetch_package_summary(api_key: str, package_id: str) -> Optional[Dict]:
    """Retrieve a package summary JSON document.

    Uses the GovInfo Packages service `/packages/{packageId}/summary`.

    See usgpo/api README (Packages Service) for details. In particular,
    the `download` object in the summary typically looks like:

        "download": {
            "txtLink": "https://api.govinfo.gov/packages/BILLS-115hr1625enr/htm",
            "xmlLink": "https://api.govinfo.gov/packages/BILLS-115hr1625enr/xml",
            "pdfLink": "https://api.govinfo.gov/packages/BILLS-115hr1625enr/pdf",
            ...
        }

    Parameters
    ----------
    api_key : str
        GovInfo API key.
    package_id : str
        Package identifier.

    Returns
    -------
    dict or None
        The parsed JSON summary if successful; otherwise None.
    """

    url = f"{API_BASE}/packages/{package_id}/summary"
    params = {"api_key": api_key}

    try:
        resp = requests.get(url, params=params, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logging.error("Error fetching summary for %s: %s", package_id, exc)
        return None

    try:
        return resp.json()
    except json.JSONDecodeError:
        logging.error("Failed to decode summary JSON for %s", package_id)
        return None


def download_with_retries(url: str, dest: Path, max_retries: int = 3) -> bool:
    """Download a file with basic retry logic.

    Specifically handles GovInfo's ZIP generation behavior where a
    HTTP 503 with a Retry-After header may be returned while a ZIP
    is being generated.

    Parameters
    ----------
    url : str
        Direct download URL (e.g. pdfLink).
    dest : Path
        Destination path to write the content to.
    max_retries : int
        Maximum retries for transient errors.

    Returns
    -------
    bool
        True if the file was successfully downloaded; False otherwise.
    """

    attempt = 0
    while attempt <= max_retries:
        attempt += 1
        try:
            with requests.get(url, stream=True, timeout=120) as resp:
                if resp.status_code == 503:
                    # Check for Retry-After header; default to 30 seconds.
                    retry_after = int(resp.headers.get("Retry-After", "30"))
                    logging.warning(
                        "503 from %s (ZIP generation). Retry %d/%d after %d seconds.",
                        url,
                        attempt,
                        max_retries,
                        retry_after,
                    )
                    time.sleep(retry_after)
                    continue

                resp.raise_for_status()

                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:  # filter out keep-alive chunks
                            fh.write(chunk)

                logging.info("Downloaded %s -> %s", url, dest)
                return True

        except requests.RequestException as exc:
            logging.error("Error downloading %s (attempt %d/%d): %s", url, attempt, max_retries, exc)
            if attempt > max_retries:
                break
            # brief backoff
            time.sleep(5 * attempt)

    logging.error("Failed to download %s after %d attempts", url, max_retries)
    return False


def download_package_formats(
    api_key: str,
    package: PackageRecord,
    formats: Iterable[str],
    base_output_dir: Path,
    dry_run: bool = False,
) -> None:
    """Download selected formats for a given package.

    Parameters
    ----------
    api_key : str
        GovInfo API key.
    package : PackageRecord
        Package record with id and optional date.
    formats : Iterable[str]
        Format names such as ["pdf", "xml", "zip"].
    base_output_dir : Path
        Root directory for all downloaded content.
    dry_run : bool
        If True, log what would be downloaded but skip actual HTTP calls.
    """

    fmt_list = [f.strip().lower() for f in formats if f.strip()]
    if not fmt_list:
        logging.debug("No formats requested for %s; skipping.", package.package_id)
        return

    summary = fetch_package_summary(api_key, package.package_id)
    if not summary:
        return

    download_info = summary.get("download") or {}
    if not download_info:
        logging.debug("No 'download' section in summary for %s", package.package_id)
        return

    collection_code = package_collection_code(package.package_id)
    year = package_year_from_date(package.date_issued)

    for fmt in fmt_list:
        link_key = DOWNLOAD_KEY_MAP.get(fmt)
        if not link_key:
            logging.warning("Unknown format '%s' requested; skipping.", fmt)
            continue

        url = download_info.get(link_key)
        if not url:
            logging.debug(
                "Format '%s' (%s) not available for %s", fmt, link_key, package.package_id
            )
            continue

        # Extension: use fmt as file extension.
        dest = base_output_dir / collection_code / year / f"{package.package_id}.{fmt}"

        if dry_run:
            logging.info("[DRY-RUN] Would download %s -> %s", url, dest)
            continue

        download_with_retries(url, dest)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the top-level argument parser."""

    parser = argparse.ArgumentParser(
        description=(
            "GovInfo bulk ingestion CLI. Use the 'download' subcommand to "
            "enumerate packages via the Published service and download "
            "selected formats via the Packages service."
        )
    )

    parser.add_argument(
        "--api-key",
        dest="api_key",
        help="GovInfo / api.data.gov API key (or set GOVINFO_API_KEY)",
    )

    parser.add_argument(
        "--verbose",
        dest="verbose",
        action="store_true",
        help="Enable debug logging.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # download subcommand
    dl = subparsers.add_parser(
        "download",
        help="Enumerate packages and download content for a date range.",
    )

    dl.add_argument(
        "--collections",
        required=True,
        help=(
            "Comma-separated list of collection codes (e.g. BILLS,BILLSTATUS,CREC). "
            "See https://api.govinfo.gov/collections for full list."
        ),
    )

    dl.add_argument(
        "--start-date",
        help="Start date (YYYY-MM-DD). Required unless --last-years is used.",
    )

    dl.add_argument(
        "--end-date",
        help="End date (YYYY-MM-DD). Defaults to today if omitted.",
    )

    dl.add_argument(
        "--last-years",
        type=int,
        help=(
            "Convenience: ignore --start-date/--end-date and instead ingest the last N years "
            "of data, based on publication date."
        ),
    )

    dl.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=f"Number of records per API call (1-{MAX_PAGE_SIZE}, default {DEFAULT_PAGE_SIZE}).",
    )

    dl.add_argument(
        "--output-dir",
        type=str,
        default="govinfo_data",
        help="Base directory for downloaded content (default: ./govinfo_data)",
    )

    dl.add_argument(
        "--formats",
        type=str,
        default="pdf,xml",
        help=(
            "Comma-separated list of formats to download per package (e.g. pdf,xml,txt,mods,zip). "
            "Default: pdf,xml."
        ),
    )

    dl.add_argument(
        "--max-packages",
        type=int,
        help=(
            "Optional safety cap on the number of packages to download. If omitted, the script "
            "will process all discovered packages in the date range."
        ),
    )

    dl.add_argument(
        "--dry-run",
        action="store_true",
        help="Enumerate what would be downloaded without performing any downloads.",
    )

    return parser


def handle_download_command(args: argparse.Namespace) -> None:
    """Execute the 'download' subcommand logic."""

    api_key = resolve_api_key(args.api_key)

    # Determine date range
    if args.last_years is not None:
        start_date, end_date = compute_date_range_from_years(args.last_years)
        logging.info(
            "Using last %d years: %s to %s", args.last_years, start_date, end_date
        )
    else:
        if not args.start_date:
            raise SystemExit("--start-date is required when --last-years is not used")
        start_date = parse_date_arg(args.start_date)
        if args.end_date:
            end_date = parse_date_arg(args.end_date)
        else:
            end_date = _dt.date.today().isoformat()
        logging.info("Using explicit date range: %s to %s", start_date, end_date)

    collections = [c.strip().upper() for c in args.collections.split(",") if c.strip()]
    if not collections:
        raise SystemExit("At least one collection code must be provided in --collections")

    output_dir = Path(args.output_dir).expanduser().resolve()
    ensure_output_dir(output_dir)

    fmt_list = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    if not fmt_list:
        raise SystemExit("At least one format must be specified via --formats")

    logging.info(
        "Discovering packages for collections=%s, range=%s to %s",
        ",".join(collections),
        start_date,
        end_date,
    )

    packages = fetch_published_packages(
        api_key=api_key,
        collections=collections,
        start_date=start_date,
        end_date=end_date,
        page_size=args.page_size,
        max_packages=args.max_packages,
    )

    logging.info("Discovered %d packages.", len(packages))

    if not packages:
        logging.warning("No packages found; exiting.")
        return

    for idx, pkg in enumerate(packages, start=1):
        logging.info("Processing package %d/%d: %s", idx, len(packages), pkg.package_id)
        download_package_formats(
            api_key=api_key,
            package=pkg,
            formats=fmt_list,
            base_output_dir=output_dir,
            dry_run=args.dry_run,
        )

    logging.info("Done. Processed %d packages.", len(packages))


def main(argv: Optional[List[str]] = None) -> None:
    """Entry point for the CLI."""

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    setup_logging(verbose=args.verbose)

    if args.command == "download":
        handle_download_command(args)
    else:  # pragma: no cover - future-proofing
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":  # pragma: no cover
    try:
        main()
    except KeyboardInterrupt:
        logging.warning("Interrupted by user.")
        sys.exit(130)
