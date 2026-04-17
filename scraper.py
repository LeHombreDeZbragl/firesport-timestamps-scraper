#!/usr/bin/env python3
"""Firesport timestamps scraper.

Scrapes downloaded competition result HTML pages from firesport.eu and writes
a CSV with columns:
    attack_date, league, place, placement, attack_type, category, team, lp, pp

Usage:
    python3 scraper.py <league> <input_dir> -o <output_file>

Example:
    python3 scraper.py ZL zl/ -o zl_results.csv
    python3 scraper.py EXCR excr/ -o excr_results.csv
"""

import argparse
import csv
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

# Result values that should produce an empty lp/pp field
_INVALID_TIME_STRS = frozenset({'NP', 'DSQ', 'MS', '-'})


def parse_time(value: str) -> str:
    """Return a normalised time string (comma→dot), or '' for invalid values.

    Values treated as invalid: NP, DSQ, MS, -, 99.99, empty, non-numeric.
    """
    v = value.strip()
    if not v or v.upper() in _INVALID_TIME_STRS:
        return ''
    normalised = v.replace(',', '.')
    try:
        if float(normalised) == 99.99:
            return ''
    except ValueError:
        return ''
    return normalised


def parse_date(date_str: str) -> str:
    """Convert 'DD. MM. YYYY  HH:MM' (or similar) to 'YYYY-MM-DD'."""
    m = re.search(r'(\d{1,2})\.\s+(\d{1,2})\.\s+(\d{4})', date_str)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f'{year}-{month:02d}-{day:02d}'
    return ''


def extract_team(td_team: Tag, td_district: Tag) -> str:
    """Build 'TeamName [Suffix]/District' from the team and district cells.

    The team cell contains an optional &nbsp; + <a>Name</a> + optional suffix
    text (e.g. 'B', 'PN', 'Cirkus').  The district cell contains the short
    district code inside an <a> tag.
    """
    a_tag = td_team.find('a')
    if a_tag:
        name = a_tag.get_text(strip=True)
        # Collect any NavigableString text nodes that follow the <a> tag
        suffix_parts = []
        for sibling in a_tag.next_siblings:
            if isinstance(sibling, NavigableString):
                text = str(sibling).strip().strip('\xa0').strip()
                if text:
                    suffix_parts.append(text)
        suffix = ' '.join(suffix_parts)
    else:
        # Fallback for malformed HTML (e.g. <td>Otín</a></td>, <td>Name</a> Suffix</td>)
        # Use separator=' ' so text nodes around orphaned tags are space-joined, then
        # collapse any runs of whitespace/non-breaking-space into a single space.
        raw = td_team.get_text(separator=' ', strip=True)
        name = re.sub(r'[\xa0\s]+', ' ', raw).strip()
        suffix = ''

    a_dist = td_district.find('a')
    district = (
        a_dist.get_text(strip=True) if a_dist
        else td_district.get_text(strip=True).strip()
    )

    parts = [name]
    if suffix:
        parts.append(suffix)
    team = ' '.join(parts)
    if district:
        team = f'{team}/{district}'
    return team


def get_attack_type(h3_text: str) -> str:
    """Extract '3B' or '2B' from h3 text like '... 3xB úzké'."""
    m = re.search(r'(\d)xB', h3_text)
    return f'{m.group(1)}B' if m else ''


def get_category(h3_text: str) -> str:
    """Return the first word of the category h3 (e.g. 'Muži', 'Ženy')."""
    words = h3_text.strip().split()
    return words[0] if words else ''


def parse_rows(
    table: Tag,
    attack_date: str,
    league: str,
    place: str,
    attack_type: str,
    category: str,
) -> list:
    """Extract result rows from a data table element."""
    rows = []
    for tbody in table.find_all('tbody'):
        for tr in tbody.find_all('tr', recursive=False):
            tds = tr.find_all('td', recursive=False)
            if len(tds) < 6:
                continue

            # td[0]: placement number inside <b>N.</b>
            b_tag = tds[0].find('b')
            if not b_tag:
                continue
            placement_raw = b_tag.get_text(strip=True).rstrip('.')
            if not placement_raw.isdigit():
                continue  # skip header-like / malformed rows

            # td[1]: team name + optional suffix; td[3]: district code
            team = extract_team(tds[1], tds[3])

            # td[4]: first attempt (lp), td[5]: second attempt (pp)
            lp = parse_time(tds[4].get_text(strip=True))
            pp = parse_time(tds[5].get_text(strip=True))

            rows.append({
                'attack_date': attack_date,
                'league': league,
                'place': place,
                'placement': placement_raw,
                'attack_type': attack_type,
                'category': category,
                'team': team,
                'lp': lp,
                'pp': pp,
            })
    return rows


def scrape_file(html_path: Path, league: str) -> list:
    """Parse one HTML file and return all extracted result rows."""
    with open(html_path, encoding='utf-8', errors='replace') as f:
        html = f.read()
    soup = BeautifulSoup(html, 'html.parser')

    # Each competition event lives in <div id='tabs-NNNNN'>
    tab_divs = soup.find_all('div', id=re.compile(r'^tabs-\d+$'))

    all_rows = []
    for tab_div in tab_divs:
        # ── Place: first <h3> in the tab (the place-name heading) ───────────
        place_h3 = tab_div.find('h3')
        place = place_h3.get_text(strip=True) if place_h3 else ''

        # ── Date: <b>Kdy: </b> → next sibling <td> ──────────────────────────
        attack_date = ''
        for b_tag in tab_div.find_all('b'):
            if 'Kdy' in b_tag.get_text():
                parent_td = b_tag.find_parent('td')
                if parent_td:
                    next_td = parent_td.find_next_sibling('td')
                    if next_td:
                        attack_date = parse_date(next_td.get_text(strip=True))
                break

        # ── Category sections ────────────────────────────────────────────────
        # <h3>Category kolo soutěže číslo N, ..., NxB úzké</h3>
        # <script>...</script>
        # <table data-role="table">...</table>
        cat_h3s = [
            h for h in tab_div.find_all('h3')
            if 'kolo soutěže' in h.get_text()
        ]
        # Pair each category heading with the corresponding data table by
        # position (both lists are in document order within the tab div).
        data_tables = tab_div.find_all('table', attrs={'data-role': 'table'})

        if len(cat_h3s) != len(data_tables):
            print(
                f'Warning: {html_path.name} tab#{tab_div.get("id")}: '
                f'{len(cat_h3s)} category headings but {len(data_tables)} '
                f'data tables — pairing by index, extra entries will be skipped.',
                file=sys.stderr,
            )

        for h3, table in zip(cat_h3s, data_tables):
            h3_text = h3.get_text(strip=True)
            category = get_category(h3_text)
            attack_type = get_attack_type(h3_text)
            rows = parse_rows(table, attack_date, league, place, attack_type, category)
            all_rows.extend(rows)

    return all_rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Scrape firesport.eu HTML result pages to CSV.'
    )
    parser.add_argument('league', help='League identifier (e.g. ZL, EXCR, VCBL)')
    parser.add_argument('input_dir', help='Directory containing HTML files to scrape')
    parser.add_argument('-o', '--output', required=True, help='Output CSV file path')
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    if not input_dir.is_dir():
        print(f'Error: {args.input_dir!r} is not a directory', file=sys.stderr)
        sys.exit(1)

    html_files = sorted(input_dir.glob('*.html'))
    if not html_files:
        print(f'No HTML files found in {args.input_dir!r}', file=sys.stderr)
        sys.exit(1)

    fieldnames = [
        'attack_date', 'league', 'place', 'placement',
        'attack_type', 'category', 'team', 'lp', 'pp',
    ]

    total_rows = 0
    with open(args.output, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for html_path in html_files:
            rows = scrape_file(html_path, args.league)
            writer.writerows(rows)
            total_rows += len(rows)
            print(f'  {html_path.name}: {len(rows)} rows')

    print(f'\nTotal: {total_rows} rows → {args.output}')


if __name__ == '__main__':
    main()
