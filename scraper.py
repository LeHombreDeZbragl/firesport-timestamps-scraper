#!/usr/bin/env python3
"""Firesport timestamps scraper.

Scrapes downloaded competition result HTML pages from firesport.eu and writes
a CSV with columns:
    attack_date, league, place, placement, attack_type, category, team, lp, pp

The attack_type (2B or 3B) is resolved via a three-tier lookup:
  1. Per-league override in config.json ("categories" inside the league entry).
     Use "ignore" to blacklist a category, or "auto" to parse the type directly 
     from the HTML heading.
  2. Global default in config.json top-level "categories".
  3. Interactive prompt (debug mode) or warning+skip (automated mode).

Usage:
    python3 scraper.py <league_key> <input_dir> -o <output_file>

Example:
    python3 scraper.py zl zl/ -o zl_results.csv
    python3 scraper.py plpu plpu/ -o plpu_results.csv
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

# Result values that should produce an empty lp/pp field
_INVALID_TIME_STRS = frozenset({'NP', 'DSQ', 'MS', '-'})

# Configuration file path
_CONFIG_FILE = 'config.json'


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


def extract_team(td_team: Tag, td_district: Tag, district_map: dict | None = None) -> str:
    """Build 'TeamName [Suffix]/District' from the team and district cells.

    The team cell contains an optional &nbsp; + <a>Name</a> + optional suffix
    text (e.g. 'B', 'PN', 'Cirkus').  The district cell contains the short
    district code inside an <a> tag.
    
    If district_map is provided, attempts to map the full district name to a
    short SPZ code (e.g. 'Žďár nad Sázavou' → 'ZR'). If the mapping is not
    found, warns and uses the original full name.
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

    # If district_map provided, map full district name to SPZ code
    if district and district_map:
        if district in district_map:
            district = district_map[district]
        else:
            print(
                f"Warning: District '{district}' not found in district_abbreviations map — "
                f"using original name",
                file=sys.stderr,
            )

    parts = [name]
    if suffix:
        parts.append(suffix)
    team = ' '.join(parts)
    if district:
        team = f'{team}/{district}'
    return team


def load_config(config_path: Path) -> dict:
    """Load full config.json."""
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            "Please create config.json (see config structure in project docs)."
        )
    with open(config_path, encoding='utf-8') as f:
        return json.load(f)


def save_config(config: dict, config_path: Path) -> None:
    """Persist config back to disk."""
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write('\n')


def get_league_categories(config: dict, league_key: str) -> dict:
    """Return per-league category overrides (may include 'auto' values)."""
    return (
        config.get('leagues', {})
        .get(league_key, {})
        .get('categories', {})
    )


def get_category(h3_text: str, compound_prefixes: set[str] | None = None) -> str:
    """Return the category name extracted from the h3 heading.

    For single-word categories (e.g. 'Muži', 'Ženy') returns the first word.
    If the first word is listed in *compound_prefixes* and a second word exists,
    returns the first two words (e.g. 'Smíšený dorost').
    """
    words = h3_text.strip().split()
    if not words:
        return ''
    if compound_prefixes and words[0] in compound_prefixes and len(words) >= 2:
        return f'{words[0]} {words[1]}'
    return words[0]


def parse_attack_type_from_h3(h3_text: str) -> str | None:
    """Extract attack type from an h3 heading text.

    Looks for patterns like '3xB', '2xB' (NxB format) or '2B', '3B' (NB format)
    (case-insensitive) and converts them to '3B' / '2B'.  Returns None if no
    such pattern is found.

    Examples:
        '2xB' → '2B'
        '3B' → '3B'
    """
    m = re.search(r'(\d)(?:x)?B', h3_text, re.IGNORECASE)
    return f'{m.group(1)}B' if m else None


def resolve_attack_type(
    category: str,
    h3_text: str,
    global_categories: dict,
    league_categories: dict,
    source_name: str,
    interactive: bool = True,
    full_config: dict | None = None,
) -> str | None:
    """Resolve the attack type for a category using three-tier lookup.

    Priority order:
      1. Per-league override (league_categories).
      2. Global default (global_categories).
      3. Unknown: in interactive mode, prompt the user; in automated mode,
         warn and return None (rows skipped by the caller).

    Mode values accepted at tiers 1 and 2:
      'ignore'   Blacklist — skip all rows for this category silently.
      'testing'  Parse NxB from the h3 heading; WARN and fall back to
                 'Ostatní' when the pattern is not found.  Use this as the
                 discovery default for a new league.  Also subject to backfill
                 conflict detection.
      'auto'     Parse NxB from the h3 heading silently; fall back to
                 'Ostatní' when the pattern is absent (no warning).  Exempt
                 from backfill conflict detection.  Use once intentional type
                 variation is confirmed.
      Fixed value (e.g. '2B', '3B', 'Ostatní')
                 Any other string is returned as-is, no parsing. Use 'Ostatní'
                 (Czech: "Other") for categories that don't fit standard types.
    """
    def _parse_or_warn(mode: str, tier_label: str) -> str | None:
        attack_type = parse_attack_type_from_h3(h3_text)
        if attack_type:
            return attack_type
        if mode == 'testing':
            print(
                f"Warning: {tier_label} 'testing' for '{category}' — "
                f"no NxB pattern found in heading: {h3_text!r}  ({source_name})",
                file=sys.stderr,
            )
        return None  # 'auto' is silent

    # 1. Per-league override
    if category in league_categories:
        league_val = league_categories[category]
        if league_val == 'ignore':
            return None  # blacklisted — skip rows silently
        if league_val in ('auto', 'testing'):
            result = _parse_or_warn(league_val, 'per-league')
            if result:
                return result
            return 'Ostatní'  # no NxB pattern found
        else:
            return league_val

    # 2. Global default
    if category in global_categories:
        global_val = global_categories[category]
        if global_val == 'ignore':
            return None  # blacklisted — skip rows silently
        if global_val in ('auto', 'testing'):
            result = _parse_or_warn(global_val, 'global')
            if result:
                return result
            return 'Ostatní'  # no NxB pattern found
        else:
            return global_val

    # 3. Not found anywhere
    if not interactive:
        print(
            f"Warning: Category '{category}' not in config — skipping rows "
            f"from {source_name}",
            file=sys.stderr,
        )
        return None

    # Interactive: prompt user and persist to global categories
    print(f"\nUnknown category '{category}' in {source_name}", file=sys.stderr)
    while True:
        user_input = input(
            f"Enter attack type for '{category}' (2B or 3B), or 'skip' to stop: "
        ).strip().upper()
        if user_input == 'SKIP':
            print('Stopping due to unknown category.', file=sys.stderr)
            sys.exit(1)
        if user_input in ('2B', '3B'):
            global_categories[category] = user_input
            if full_config is not None:
                save_config(full_config, Path(_CONFIG_FILE))
                print(
                    f"✓ Added '{category}': '{user_input}' to {_CONFIG_FILE}",
                    file=sys.stderr,
                )
            return user_input
        print("Please enter '2B' or '3B'.", file=sys.stderr)


def parse_rows(
    table: Tag,
    attack_date: str,
    league: str,
    place: str,
    attack_type: str,
    category: str,
    full_league_name: str = '',
    district_map: dict | None = None,
    old_layout: bool = True,
    link: str = '',
) -> list:
    """Extract result rows from a data table element.
    
    Args:
        table:             Data table element to parse.
        attack_date:       Competition date (YYYY-MM-DD).
        league:            League display name or key.
        place:             Competition place name.
        attack_type:       Attack type (2B, 3B, or Ostatní).
        category:          Category name (Muži, Ženy, etc.).
        full_league_name:  Full human-readable league name.
        district_map:      Optional mapping of full district names to SPZ codes.
        old_layout:        If True, uses old column layout (td[2]=time, td[3]=district).
                          If False, uses new column layout (td[2]=district, td[3]=time).
        link:              Relative URL of the competition page (e.g., 'vysledek-*.html').
    """
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

            # Column layout varies between old and new pages
            if old_layout:
                # Old layout: td[0]=placement, td[1]=team, td[2]=final_time, td[3]=district, td[4]=lp, td[5]=pp
                time_idx = 2
                district_idx = 3
            else:
                # New layout: td[0]=placement, td[1]=team, td[2]=district, td[3]=final_time, td[4]=lp, td[5]=pp
                time_idx = 3
                district_idx = 2

            # td[1]: team name + optional suffix; district cell is at time_idx or district_idx
            team = extract_team(tds[1], tds[district_idx], district_map=district_map)

            # Final/combined time inside <b> (for only_final_time detection)
            b_final = tds[time_idx].find('b')
            final_time = parse_time(b_final.get_text(strip=True)) if b_final else ''

            # td[4]: first attempt (lp), td[5]: second attempt (pp)
            lp = parse_time(tds[4].get_text(strip=True))
            pp = parse_time(tds[5].get_text(strip=True))

            # When individual times are absent but a final time exists,
            # duplicate the final time into both lp and pp and flag the row.
            if lp == '' and pp == '' and final_time:
                lp = final_time
                pp = final_time
                only_final_time = True
            else:
                only_final_time = False

            # Skip rows where lp or pp is less than 12
            if (lp and float(lp) < 12) or (pp and float(pp) < 12):
                print(
                    f"Warning: Skipping row for team '{team}' — "
                    f"lp or pp is less than 12 (lp={lp}, pp={pp})",
                    file=sys.stderr,
                )
                continue

            rows.append({
                'attack_date': attack_date,
                'league': league,
                'full_league_name': full_league_name,
                'place': place,
                'placement': placement_raw,
                'attack_type': attack_type,
                'category': category,
                'team': team,
                'lp': lp,
                'pp': pp,
                'only_final_time': only_final_time,
                'link': link,
            })
    return rows


def scrape_html(
    html_content: str,
    source_name: str,
    league_display: str,
    global_categories: dict,
    league_categories: dict,
    interactive: bool = False,
    full_config: dict | None = None,
    full_league_name: str = '',
    category_aliases: dict | None = None,
    compound_prefixes: set[str] | None = None,
) -> list:
    """Parse HTML content string and return all extracted result rows.

    This is the core parsing function used by the orchestrator (in-memory
    pipeline).  scrape_file() is a file-based wrapper around this function.

    Args:
        html_content:      Raw HTML string to parse.
        source_name:       Label for warnings (e.g. filename or URL).
        league_display:    League alias written to the 'league' column.
        global_categories: Top-level category→attack_type mapping.
        league_categories: Per-league overrides (may contain 'auto').
        interactive:       Prompt for unknown categories when True, else skip.
        full_config:       Full config dict for persisting new categories.
        full_league_name:  Full human-readable league name for the DB column.
        category_aliases:  Mapping of raw category names to display names
                           (e.g. {"Finále": "Ostatní"}).
        compound_prefixes: Set of first words that form two-word category names
                           (e.g. {"Smíšený"} → "Smíšený dorost").
    """
    soup = BeautifulSoup(html_content, 'html.parser')

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
                f'Warning: {source_name} tab#{tab_div.get("id")}: '
                f'{len(cat_h3s)} category headings but {len(data_tables)} '
                f'data tables — pairing by index, extra entries will be skipped.',
                file=sys.stderr,
            )

        for h3, table in zip(cat_h3s, data_tables):
            h3_text = h3.get_text(strip=True)
            category = get_category(h3_text, compound_prefixes)
            if category_aliases:
                category = category_aliases.get(category, category)
            attack_type = resolve_attack_type(
                category,
                h3_text,
                global_categories,
                league_categories,
                source_name=source_name,
                interactive=interactive,
                full_config=full_config,
            )
            if attack_type is None:
                continue  # unknown category in automated mode — skip rows
            rows = parse_rows(table, attack_date, league_display, place, attack_type, category, full_league_name, old_layout=True)
            all_rows.extend(rows)

    return all_rows


def scrape_schedule(html_content: str) -> list:
    """Extract competition schedule metadata from an HTML year page.

    Works on pages where competitions are listed but results are not yet
    published — does not require result tables to be present.

    Returns a list of dicts with keys:
        date        (str)   'YYYY-MM-DD' of the competition
        place       (str)   place name from the first <h3> in the tab
        has_results (bool)  True if the tab contains at least one result table

    Tabs without a parseable date are silently skipped.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    tab_divs = soup.find_all('div', id=re.compile(r'^tabs-\d+$'))

    schedule = []
    for tab_div in tab_divs:
        place_h3 = tab_div.find('h3')
        place = place_h3.get_text(strip=True) if place_h3 else ''

        attack_date = ''
        for b_tag in tab_div.find_all('b'):
            if 'Kdy' in b_tag.get_text():
                parent_td = b_tag.find_parent('td')
                if parent_td:
                    next_td = parent_td.find_next_sibling('td')
                    if next_td:
                        attack_date = parse_date(next_td.get_text(strip=True))
                break

        if not attack_date:
            continue  # skip tabs without a parseable date

        has_results = bool(tab_div.find('table', attrs={'data-role': 'table'}))
        schedule.append({'date': attack_date, 'place': place, 'has_results': has_results})

    return schedule


def parse_global_list(html_content: str, source_name: str = 'global_list') -> list:
    """Parse global competition list from vysledky-souteze-YYYY.htm.
    
    Extracts metadata from <table id="tabulkakal"> rows:
      - Date as YYYY-MM-DD (from <b>YYYY/MM/DD</b>)
      - Place name (text before first comma, e.g., 'Dolní Hradiště' from 
        'Dolní Hradiště, ICE CUP 2025')
      - Relative link (href attribute, e.g., 'vysledek-dolni-hradiste-14484.html')
      - District (full name, e.g., 'Plzeň-sever')
      - League display name from the fourth column <a> tag
    
    Returns a list of dicts with keys:
        date          (str)   'YYYY-MM-DD'
        place         (str)   place name (first part of competition title)
        link          (str)   relative URL (e.g., 'vysledek-*.html')
        district      (str)   full district name (e.g., 'Žďár nad Sázavou')
        league        (str)   league display name or 'FSEU' if unlabeled
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    table = soup.find('table', id='tabulkakal')
    if not table:
        print(
            f"Warning: {source_name}: No table with id='tabulkakal' found",
            file=sys.stderr,
        )
        return []
    
    competitions = []
    tbody = table.find('tbody')
    if not tbody:
        return []
    
    for tr in tbody.find_all('tr', recursive=False):
        tds = tr.find_all('td', recursive=False)
        if len(tds) < 4:
            continue
        
        # td[0]: date as <b>YYYY/MM/DD</b>
        b_date = tds[0].find('b')
        if b_date:
            date_str = b_date.get_text(strip=True)
            # Convert YYYY/MM/DD to YYYY-MM-DD
            parts = date_str.split('/')
            if len(parts) == 3:
                try:
                    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                    date = f'{year}-{month:02d}-{day:02d}'
                except (ValueError, IndexError):
                    continue
            else:
                continue
        else:
            continue
        
        # td[1]: place and link from <a href="...">PlaceName, EventName</a>
        a_place = tds[1].find('a')
        if not a_place:
            continue
        
        full_text = a_place.get_text(strip=True)
        # Extract place name (before first comma)
        place = full_text.split(',')[0].strip() if ',' in full_text else full_text
        
        link = a_place.get('href', '').strip()
        if not link:
            continue
        
        # td[2]: district (full name, not linked)
        district = tds[2].get_text(strip=True)
        
        # td[3]: league from <a>LeagueName</a> or plain text
        a_league = tds[3].find('a')
        league = (
            a_league.get_text(strip=True) if a_league
            else tds[3].get_text(strip=True)
        )
        # If empty or whitespace-only, mark as FSEU (unlabeled)
        if not league:
            league = 'FSEU'
        
        competitions.append({
            'date': date,
            'place': place,
            'link': link,
            'district': district,
            'league': league,
        })
    
    return competitions


def scrape_individual_page(
    html_content: str,
    meta: dict,
    global_categories: dict,
    league_categories: dict,
    district_map: dict,
    source_name: str,
    interactive: bool = False,
    full_config: dict | None = None,
    full_league_name: str = '',
    category_aliases: dict | None = None,
    compound_prefixes: set[str] | None = None,
) -> list:
    """Parse an individual competition result page (vysledek-*.html).
    
    Uses new column layout with <div id='tabs-1'> structure (not tabs-NNNNN).
    
    Args:
        html_content:      Raw HTML string.
        meta:              Competition metadata dict with keys:
                          'date', 'place', 'link', 'district', 'league'
        global_categories: Top-level category→attack_type mapping.
        league_categories: Per-league overrides (may contain 'auto').
        district_map:      Mapping of full district names to SPZ codes.
        source_name:       Label for warnings (filename or URL).
        interactive:       Prompt for unknown categories when True.
        full_config:       Full config dict for persisting new categories.
        full_league_name:  Full human-readable league name.
        category_aliases:  Mapping of raw category names to display names.
        compound_prefixes: Set of first words forming two-word categories.
    
    Returns list of extracted result row dicts (includes 'link' field).
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Individual pages have a single <div id='tabs-1'>
    tab_div = soup.find('div', id='tabs-1')
    if not tab_div:
        print(
            f"Warning: {source_name}: No <div id='tabs-1'> found",
            file=sys.stderr,
        )
        return []
    
    attack_date = meta.get('date', '')
    place = meta.get('place', '')
    link = meta.get('link', '')
    league_display = meta.get('league', '')
    
    all_rows = []
    
    # Category sections in the new layout
    cat_h3s = [
        h for h in tab_div.find_all('h3')
        if 'kolo soutěže' in h.get_text()
    ]
    data_tables = tab_div.find_all('table', attrs={'data-role': 'table'})
    
    if len(cat_h3s) != len(data_tables):
        print(
            f'Warning: {source_name}: {len(cat_h3s)} category headings but '
            f'{len(data_tables)} data tables — pairing by index.',
            file=sys.stderr,
        )
    
    for h3, table in zip(cat_h3s, data_tables):
        h3_text = h3.get_text(strip=True)
        category = get_category(h3_text, compound_prefixes)
        if category_aliases:
            category = category_aliases.get(category, category)
        
        attack_type = resolve_attack_type(
            category,
            h3_text,
            global_categories,
            league_categories,
            source_name=source_name,
            interactive=interactive,
            full_config=full_config,
        )
        if attack_type is None:
            continue  # unknown category in automated mode — skip rows
        
        rows = parse_rows(
            table,
            attack_date,
            league_display,
            place,
            attack_type,
            category,
            full_league_name,
            district_map=district_map,
            old_layout=False,  # new layout: district at td[2], time at td[3]
            link=link,
        )
        all_rows.extend(rows)
    
    return all_rows


def scrape_file(
    html_path: Path,
    league_display: str,
    global_categories: dict,
    league_categories: dict,
    interactive: bool = True,
    full_config: dict | None = None,
) -> list:
    """Parse one HTML file and return all extracted result rows.

    Convenience wrapper around scrape_html() for file-based (debug) workflows.
    The orchestrator uses scrape_html() directly with in-memory HTML strings.
    """
    with open(html_path, encoding='utf-8', errors='replace') as f:
        html_content = f.read()
    return scrape_html(
        html_content,
        html_path.name,
        league_display,
        global_categories,
        league_categories,
        interactive=interactive,
        full_config=full_config,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Scrape firesport.eu HTML result pages to CSV.'
    )
    parser.add_argument(
        'league_key',
        help='League key matching config.json (e.g. zl, excr, plpu)',
    )
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

    config_path = Path(_CONFIG_FILE)
    try:
        config = load_config(config_path)
    except FileNotFoundError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)

    league_key = args.league_key.lower()
    global_categories = config.get('categories', {})
    league_categories = get_league_categories(config, league_key)

    # Use display_name from config if available, else fall back to upper-cased key
    league_display = (
        config.get('leagues', {})
        .get(league_key, {})
        .get('display_name', args.league_key.upper())
    )

    fieldnames = [
        'attack_date', 'league', 'full_league_name', 'place', 'placement',
        'attack_type', 'category', 'team', 'lp', 'pp',
        'only_final_time', 'link',
    ]

    total_rows = 0
    with open(args.output, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for html_path in html_files:
            rows = scrape_file(
                html_path,
                league_display,
                global_categories,
                league_categories,
                interactive=True,
                full_config=config,
            )
            writer.writerows(rows)
            total_rows += len(rows)
            print(f'  {html_path.name}: {len(rows)} rows')

    print(f'\nTotal: {total_rows} rows → {args.output}')


if __name__ == '__main__':
    main()
