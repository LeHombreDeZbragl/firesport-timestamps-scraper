#!/usr/bin/env python3
"""Firesport timestamps scraper.

Pure HTML→dict parsing for firesport.eu competition pages.
No CLI, no I/O — call parse_global_list() or scrape_individual_page() directly.

The attack_type (2B or 3B) is resolved via a three-tier lookup:
  1. Per-league override in config.json ("categories" inside the league entry).
     Use "ignore" to blacklist a category, or "auto" to parse the type directly
     from the HTML heading.
  2. Global default in config.json top-level "categories".
  3. Unknown category: warn and skip rows.
"""

import re
import sys

from bs4 import BeautifulSoup, NavigableString, Tag

from colors import cprint

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
            cprint(
                f"Warning: District '{district}' not found in district_abbreviations map — "
                f"using 'NP'",
                'yellow', stream=sys.stderr,
            )
            district = 'NP'

    parts = [name]
    if suffix:
        parts.append(suffix)
    team = ' '.join(parts)
    if district:
        team = f'{team}/{district}'
    return team


def get_league_categories(config: dict, league_key: str) -> dict:
    """Return per-league category overrides (may include 'auto' values)."""
    return (
        config.get('leagues', {})
        .get(league_key, {})
        .get('categories', {})
    )


def get_category(h3_text: str, known_categories: set[str] | None = None) -> str:
    """Return the category name extracted from the h3 heading.

    Matches the longest prefix of the heading words against *known_categories*.
    Falls back to the first word when no match is found.
    """
    words = h3_text.strip().split()
    if not words:
        return ''
    if known_categories:
        max_words = max((len(c.split()) for c in known_categories), default=1)
        for n in range(min(max_words, len(words)), 0, -1):
            candidate = ' '.join(words[:n])
            if candidate in known_categories:
                return candidate
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
) -> str | None:
    """Resolve the attack type for a category using three-tier lookup.

    Priority order:
      1. Per-league override (league_categories).
      2. Global default (global_categories).
      3. Unknown: warn and return None (rows skipped by the caller).

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
            cprint(
                f"Warning: {tier_label} 'testing' for '{category}' — "
                f"no NxB pattern found in heading: {h3_text!r}  ({source_name})",
                'orange', stream=sys.stderr,
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

    # 3. Not found anywhere — warn and skip
    cprint(
        f"Warning: Category '{category}' not in config — skipping rows "
        f"from {source_name}",
        'yellow', stream=sys.stderr,
    )
    return None


def parse_rows(
    table: Tag,
    attack_date: str,
    league: str,
    place: str,
    attack_type: str,
    category: str,
    full_league_name: str = '',
    district_map: dict | None = None,
    link: str = '',
) -> list:
    """Extract result rows from a data table element.

    Expects the current column layout: td[0]=placement, td[1]=team,
    td[2]=district, td[3]=final_time, td[4]=lp, td[5]=pp.

    Args:
        table:             Data table element to parse.
        attack_date:       Competition date (YYYY-MM-DD).
        league:            League display name or key.
        place:             Competition place name.
        attack_type:       Attack type (2B, 3B, or Ostatní).
        category:          Category name (Muži, Ženy, etc.).
        full_league_name:  Full human-readable league name.
        district_map:      Optional mapping of full district names to SPZ codes.
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

            # td[0]=placement, td[1]=team, td[2]=district, td[3]=final_time, td[4]=lp, td[5]=pp
            time_idx = 3
            district_idx = 2

            # td[1]: team name + optional suffix; district cell is at district_idx
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
                cprint(
                    f"Warning: Skipping row for team '{team}' — "
                    f"lp or pp is less than 12 (lp={lp}, pp={pp})",
                    'yellow', stream=sys.stderr,
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
        cprint(
            f"Warning: {source_name}: No table with id='tabulkakal' found",
            'yellow', stream=sys.stderr,
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
    full_league_name: str = '',
    excluded_keywords: list[str] | None = None,
    other_categories: list[str] | None = None,
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
        full_league_name:  Full human-readable league name.
        excluded_keywords: Substrings (matched case-insensitively against the
                          full category heading) that cause the whole section
                          to be skipped — used to drop unwanted disciplines
                          (Plamen, štafeta, 60m, CTIF, …).
        other_categories:  Category names (matched case-insensitively) that are
                          collapsed to 'Ostatní' before attack-type resolution.

    Returns list of extracted result row dicts (includes 'link' field).
    """
    soup = BeautifulSoup(html_content, 'html.parser')

    # Individual pages have a single <div id='tabs-1'>
    tab_div = soup.find('div', id='tabs-1')
    if not tab_div:
        cprint(
            f"Warning: {source_name}: No <div id='tabs-1'> found",
            'yellow', stream=sys.stderr,
        )
        return []

    attack_date = meta.get('date', '')
    place = meta.get('place', '')
    place_district = meta.get('district', '')
    link = meta.get('link', '')
    league_display = meta.get('league', '')

    # Abbreviate and format place with district code
    if place_district and district_map:
        if place_district in district_map:
            place_district = district_map[place_district]
        else:
            cprint(
                f"Warning: District '{place_district}' not found in district_abbreviations map — "
                f"using 'NP'",
                'yellow', stream=sys.stderr,
            )
            place_district = 'NP'

    if place_district:
        place = f'{place}/{place_district}'

    all_rows = []
    known_categories = set(global_categories) | set(league_categories)
    excluded_lower = [k.lower() for k in (excluded_keywords or [])]
    other_lower = {c.lower() for c in (other_categories or [])}

    # Category sections in the new layout
    cat_h3s = [
        h for h in tab_div.find_all('h3')
        if 'kolo soutěže' in h.get_text()
    ]
    data_tables = tab_div.find_all('table', attrs={'data-role': 'table'})

    if len(cat_h3s) != len(data_tables):
        cprint(
            f'Warning: {source_name}: {len(cat_h3s)} category headings but '
            f'{len(data_tables)} data tables — pairing by index.',
            'yellow', stream=sys.stderr,
        )

    for h3, table in zip(cat_h3s, data_tables):
        h3_text = h3.get_text(strip=True)

        # Skip unwanted disciplines: any excluded keyword found in the heading
        lowered = h3_text.lower()
        hit = next((kw for kw in excluded_lower if kw in lowered), None)
        if hit:
            cprint(
                f"Warning: {source_name}: skipping section — heading "
                f"matched excluded keyword {hit!r}",
                'white', stream=sys.stderr,
            )
            continue

        category = get_category(h3_text, known_categories)

        # Collapse miscellaneous categories to 'Ostatní' before type resolution
        if category.lower() in other_lower:
            category = 'Ostatní'

        attack_type = resolve_attack_type(
            category,
            h3_text,
            global_categories,
            league_categories,
            source_name=source_name,
        )
        if attack_type is None:
            continue  # unknown category — skip rows

        rows = parse_rows(
            table,
            attack_date,
            league_display,
            place,
            attack_type,
            category,
            full_league_name,
            district_map=district_map,
            link=link,
        )
        all_rows.extend(rows)

    return all_rows
