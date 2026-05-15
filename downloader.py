#!/usr/bin/env python3
"""Firesport competition page downloader.

Provides download_html() for in-memory use by the orchestrator, and a CLI
for manual/debug downloading to disk.

Usage (CLI):
    python downloader.py <league> <start_year> <end_year>
    python downloader.py --debug <league> <start_year> <end_year>

Without --debug: saves to <league>/<LEAGUE>.<year>.html  (legacy location)
With    --debug: saves to debug_output/<league>/<LEAGUE>.<year>.html
"""

import argparse
import sys
from pathlib import Path

import requests

_BASE_URL = 'https://{league}.firesport.eu/web_souteze.php?akce=sel&rok={year}'
_BASE_URL_STANDALONE = 'https://{league}.cz/web_souteze.php?akce=sel&rok={year}'
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
        description='Download firesport.eu HTML pages for a league.'
    )
    parser.add_argument('league', help='League URL slug (e.g. zl, excr, vcbl)')
    parser.add_argument('start_year', type=int, help='First year to download')
    parser.add_argument('end_year', type=int, help='Last year to download (inclusive)')
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Save to debug_output/<league>/ instead of <league>/',
    )
    args = parser.parse_args()

    if args.debug:
        output_dir = Path('debug_output') / args.league.lower()
    else:
        output_dir = Path(args.league.lower())

    download_league_data(args.league, args.start_year, args.end_year, output_dir)


if __name__ == '__main__':
    main()