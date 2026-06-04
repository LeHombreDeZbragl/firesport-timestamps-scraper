#!/usr/bin/env python3
"""Firesport competition page downloader.

Provides download functions for in-memory use by the orchestrator, and a CLI
for manual/debug downloading to disk.

Usage (CLI):
    # Legacy per-league-year pages:
    python downloader.py <league> <start_year> <end_year>
    python downloader.py --debug <league> <start_year> <end_year>

    # New global competition list pages:
    python downloader.py --global-list <year>
    python downloader.py --global-list <year> --debug

    # Individual competition pages:
    python downloader.py --competition <link>
    python downloader.py --competition <link> --debug

Examples:
    python downloader.py zl 2020 2025                           # Legacy
    python downloader.py --global-list 2025                     # Global list for 2025
    python downloader.py --competition vysledek-marsovice-14838.html

Without --debug: returns to stdout or keeps in memory
With    --debug: saves to debug_output/<type>/<filename>
"""

import argparse
import sys
from pathlib import Path

import requests

_BASE_URL = 'https://{league}.firesport.eu/web_souteze.php?akce=sel&rok={year}'
_BASE_URL_STANDALONE = 'https://{league}.cz/web_souteze.php?akce=sel&rok={year}'
_BASE_URL_GLOBAL_LIST = 'https://www.firesport.eu/vysledky-souteze-{year}'
_BASE_URL_COMPETITION = 'https://www.firesport.eu/{link}'
_TIMEOUT = 30  # seconds


def download_html(league: str, year: int, standalone_domain: bool = False) -> str:
    """Download the competition-list page for a league/year and return HTML.

    Args:
        league:           League URL slug (e.g. 'zl', 'excr', 'vcbl').
        year:             Season year.
        standalone_domain: When True, uses {league}.cz instead of
                          {league}.firesport.eu (for leagues with their own domain).

    Returns:
        Raw HTML as a string.

    Raises:
        requests.RequestException on network or HTTP errors.
    """
    template = _BASE_URL_STANDALONE if standalone_domain else _BASE_URL
    url = template.format(league=league.lower(), year=year)
    response = requests.get(url, timeout=_TIMEOUT)
    response.raise_for_status()
    return response.text


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


def download_league_data(league: str, start_year: int, end_year: int, output_dir: Path) -> None:
    """Download HTML files for a league across multiple years and save to disk.

    Args:
        league:     League URL slug.
        start_year: First year to download.
        end_year:   Last year to download (inclusive).
        output_dir: Directory to save files into (created if absent).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    for year in range(start_year, end_year + 1):
        try:
            html = download_html(league, year)
            output_file = output_dir / f'{league.upper()}.{year}.html'
            output_file.write_text(html, encoding='utf-8')
            print(f'Saved {output_file}')
        except requests.RequestException as exc:
            print(f'Error downloading {league} {year}: {exc}', file=sys.stderr)


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
    parser.add_argument(
        'league',
        nargs='?',
        help='League URL slug (e.g. zl, excr, vcbl) — required for legacy mode.',
    )
    parser.add_argument(
        'start_year',
        type=int,
        nargs='?',
        help='First year to download — required for legacy mode.',
    )
    parser.add_argument(
        'end_year',
        type=int,
        nargs='?',
        help='Last year to download (inclusive) — required for legacy mode.',
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
            print(f'Error downloading global list for {args.global_list}: {exc}', file=sys.stderr)
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
            print(f'Error downloading competition {args.competition}: {exc}', file=sys.stderr)
            sys.exit(1)
        return

    # Legacy per-league-year mode
    if not args.league or args.start_year is None or args.end_year is None:
        parser.print_help()
        sys.exit(1)

    if args.debug:
        output_dir = Path('debug_output') / args.league.lower()
    else:
        output_dir = Path(args.league.lower())

    download_league_data(args.league, args.start_year, args.end_year, output_dir)


if __name__ == '__main__':
    main()