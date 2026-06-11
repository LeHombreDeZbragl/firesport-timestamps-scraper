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
import sys
from pathlib import Path

import requests

from colors import cprint


_BASE_URL_GLOBAL_LIST = 'https://www.firesport.eu/vysledky-souteze-{year}'
_BASE_URL_COMPETITION = 'https://www.firesport.eu/{link}'
_TIMEOUT = 30  # seconds


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
    response = requests.get(url, timeout=_TIMEOUT)
    response.raise_for_status()
    return response.text


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
    response = requests.get(url, timeout=_TIMEOUT)
    response.raise_for_status()
    return response.text


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