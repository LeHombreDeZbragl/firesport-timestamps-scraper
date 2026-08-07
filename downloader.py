#!/usr/bin/env python3
"""Firesport competition page downloader.

Provides download functions for in-memory use by the orchestrator, and a CLI
for manual/debug downloading to disk.

Usage (CLI):
    python downloader.py --global-list <year> [--debug]
    python downloader.py --competition <link> [--debug]

Examples:
    python downloader.py --global-list 2025 --debug
    python downloader.py --competition vysledek-marsovice-14838.html --debug

Without --debug: prints HTML to stdout
With    --debug: saves to debug_output/<type>/<filename>
"""

import argparse
import os
import random
import sys
import time
from pathlib import Path

import requests

from colors import cprint


_BASE_URL_GLOBAL_LIST = 'https://www.firesport.eu/vysledky-souteze-{year}'
_BASE_URL_COMPETITION = 'https://www.firesport.eu/{link}'
_TIMEOUT = 30  # seconds

# firesport.eu 403s the default `python-requests/<ver>` (and `curl/<ver>`)
# User-Agent. Any other value is accepted, so send a plain browser one.
_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'cs,en;q=0.9',
}

# Minimum gap between consecutive requests, in seconds.  A backfill walks
# thousands of competition pages; without this it hits the site as fast as the
# network allows.  Override with FIRESPORT_REQUEST_DELAY (0 disables).
_MIN_REQUEST_INTERVAL = float(os.environ.get('FIRESPORT_REQUEST_DELAY', '1.0'))

# Statuses worth retrying: 403 because that is how the site currently refuses
# traffic it dislikes, 429 for explicit rate limiting, 5xx for transient faults.
_RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})
_MAX_RETRIES = 4
_BACKOFF_BASE = 2.0  # seconds; doubled per attempt

_SESSION = requests.Session()
_last_request_at = 0.0


def _throttle() -> None:
    """Sleep as needed so requests are at least _MIN_REQUEST_INTERVAL apart."""
    global _last_request_at
    if _MIN_REQUEST_INTERVAL <= 0:
        return
    wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _parse_retry_after(response: requests.Response) -> float | None:
    """Return the Retry-After delay in seconds, if the server sent a usable one."""
    raw = response.headers.get('Retry-After')
    if not raw:
        return None
    try:
        return max(0.0, float(raw))  # delta-seconds form; HTTP-date form ignored
    except ValueError:
        return None


def _get(url: str) -> requests.Response:
    """GET a URL with throttling and backoff, returning the successful response.

    Retries on _RETRY_STATUSES and on connection/timeout errors, honouring
    Retry-After when present.  Other HTTP errors (404, …) raise immediately.

    Raises:
        requests.RequestException if every attempt fails.
    """
    for attempt in range(_MAX_RETRIES + 1):
        _throttle()
        retry_after = None
        try:
            response = _SESSION.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            response.raise_for_status()
            return response
        except requests.HTTPError as exc:
            if exc.response.status_code not in _RETRY_STATUSES:
                raise
            retry_after = _parse_retry_after(exc.response)
            last_exc = exc
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc

        if attempt == _MAX_RETRIES:
            raise last_exc

        delay = retry_after if retry_after is not None else _BACKOFF_BASE ** attempt
        delay += random.uniform(0, delay / 2)  # jitter, so retries don't sync up
        cprint(
            f'  Request failed ({last_exc}); '
            f'retry {attempt + 1}/{_MAX_RETRIES} in {delay:.1f}s',
            'yellow', stream=sys.stderr,
        )
        time.sleep(delay)


def download_global_list(year: int) -> str:
    """Download the global competition list page for a year and return HTML.

    Args:
        year: Season year.

    Returns:
        Raw HTML as a string.

    Raises:
        requests.RequestException on network or HTTP errors.
    """
    url = _BASE_URL_GLOBAL_LIST.format(year=year)
    return _get(url).text


def download_competition_page(link: str) -> str:
    """Download an individual competition result page and return HTML.

    Args:
        link: Relative URL of the competition page (e.g., 'vysledek-marsovice-14838.html').

    Returns:
        Raw HTML as a string.

    Raises:
        requests.RequestException on network or HTTP errors.
    """
    url = _BASE_URL_COMPETITION.format(link=link)
    return _get(url).text


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Download firesport.eu HTML pages.'
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Save to debug_output/ instead of stdout/in-memory.',
    )
    parser.add_argument(
        '--global-list',
        type=int,
        metavar='YEAR',
        help='Download global competition list for a year.',
    )
    parser.add_argument(
        '--competition',
        metavar='LINK',
        help='Download a specific competition page (relative link, e.g., vysledek-marsovice-14838.html).',
    )
    args = parser.parse_args()

    # Handle --global-list mode
    if args.global_list is not None:
        try:
            html = download_global_list(args.global_list)
            if args.debug:
                output_dir = Path('debug_output') / 'global_lists'
                output_dir.mkdir(parents=True, exist_ok=True)
                output_file = output_dir / f'vysledky-souteze-{args.global_list}.html'
                output_file.write_text(html, encoding='utf-8')
                print(f'Saved {output_file}')
            else:
                print(html)
        except requests.RequestException as exc:
            cprint(f'Error downloading global list for {args.global_list}: {exc}', 'red', stream=sys.stderr)
            sys.exit(1)
        return

    # Handle --competition mode
    if args.competition:
        try:
            html = download_competition_page(args.competition)
            if args.debug:
                output_dir = Path('debug_output') / 'competitions'
                output_dir.mkdir(parents=True, exist_ok=True)
                # Use the link as filename (e.g., vysledek-marsovice-14838.html)
                output_file = output_dir / args.competition
                output_file.write_text(html, encoding='utf-8')
                print(f'Saved {output_file}')
            else:
                print(html)
        except requests.RequestException as exc:
            cprint(f'Error downloading competition {args.competition}: {exc}', 'red', stream=sys.stderr)
            sys.exit(1)
        return

    parser.print_help()
    sys.exit(1)


if __name__ == '__main__':
    main()