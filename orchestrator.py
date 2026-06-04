#!/usr/bin/env python3
"""Firesport timestamps orchestrator.

Coordinates downloading, scraping, and uploading competition results to
Supabase for all active leagues defined in config.json.

Modes
-----
Daily (default)
    Downloads the global competition list for the current year, checks which
    competitions have not yet been scraped (via link-based deduplication), and
    processes any new competitions found.

    For each unscraped competition whose date is today or in the past:
      1. Download the individual competition page.
      2. Parse results using scraper.scrape_individual_page().
      3. Upload any rows not already in the DB.

Backfill (--backfill)
    Downloads global competition lists for a range of years and processes all
    competitions, uploading any rows not already in the DB.

    Year range:
      - Default: from the earliest start_year across all configured leagues
        to the current year.
      - With --year YEAR: only that single year.

    Filtering:
      - With --league KEY: only competitions for that league.
        FSEU (unlabeled) competitions are excluded in per-league backfills.
      - Without --league: all leagues including FSEU.

    Includes per-league attack_type conflict detection across years.
    On conflict: the league's DB rows are deleted and the league is skipped.

Usage
-----
    python orchestrator.py                              # daily mode
    python orchestrator.py --backfill                   # full history, all leagues
    python orchestrator.py --backfill --league zl       # one league only
    python orchestrator.py --backfill --year 2024       # one year only
    python orchestrator.py --backfill --league zl --year 2024
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import db
import downloader
import scraper

_CONFIG_FILE = Path('config.json')



# ── Config helpers ────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not _CONFIG_FILE.exists():
        print(f'Error: {_CONFIG_FILE} not found.', file=sys.stderr)
        sys.exit(1)
    with open(_CONFIG_FILE, encoding='utf-8') as f:
        return json.load(f)


def save_config(config: dict) -> None:
    with open(_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write('\n')


def _today() -> date:
    return date.today()


# ── League lookup helpers ─────────────────────────────────────────────────────

def _build_league_lookup(leagues_config: dict) -> dict:
    """Return {display_name: {league_key, full_league_name, league_categories}}.

    Used to match league name strings from the global competition list against
    configured league entries.
    """
    lookup = {}
    for key, cfg in leagues_config.items():
        display_name = cfg.get('display_name', key)
        lookup[display_name] = {
            'league_key': key,
            'full_league_name': cfg.get('full_league_name', ''),
            'league_categories': cfg.get('categories', {}),
        }
    return lookup


def _resolve_competition_meta(comp: dict, league_lookup: dict) -> dict | None:
    """Resolve a global list competition entry to a full meta dict.

    Returns an enriched dict with keys:
        date, link, district
        place             formatted as "PlaceName /DistrictName"
        league            resolved display name, or None for FSEU
        full_league_name  full name, or None for FSEU
        league_categories per-league category overrides (empty dict for FSEU)

    Returns None for unknown leagues (warning is printed).
    """
    league = (comp.get('league') or '').strip()
    district = comp.get('district', '').strip()
    place_full = f"{comp['place']} /{district}" if district else comp['place']

    if league == 'FSEU':
        return {
            **comp,
            'place': place_full,
            'league': None,        # stored as NULL in DB
            'full_league_name': None,
            'league_categories': {},
        }

    if league in league_lookup:
        entry = league_lookup[league]
        return {
            **comp,
            'place': place_full,
            'league': league,
            'full_league_name': entry['full_league_name'],
            'league_categories': entry['league_categories'],
        }

    print(
        f"Warning: Skipping '{comp['place']}' ({comp['date']}): "
        f"unknown league '{league}'",
        file=sys.stderr,
    )
    return None


# ── Category / conflict helpers ───────────────────────────────────────────────

def _exempt_categories(
    global_categories: dict,
    league_categories: dict,
) -> set[str]:
    """Return the set of category names whose effective mode is 'auto'.

    These are exempt from backfill conflict detection because intentional
    type variation has already been confirmed by the user.
    A per-league value of 'auto' takes precedence over a global 'testing'.
    """
    exempt: set[str] = set()
    all_cats = set(global_categories) | set(league_categories)
    for cat in all_cats:
        effective = league_categories.get(cat) or global_categories.get(cat)
        if effective == 'auto':
            exempt.add(cat)
    return exempt


def _check_attack_type_conflicts(
    new_rows: list[dict],
    year: int,
    seen: dict[str, set[str]],
    display: str,
    exempt_categories: set[str],
) -> list[str]:
    """Return a list of human-readable conflict messages, empty if none.

    Compares the attack_types in new_rows against the seen map which holds
    all types already accepted (from the DB or earlier years in this run).
    Categories in exempt_categories (mode 'auto') are skipped — intentional
    type variation has already been confirmed for those.
    'Ostatní' (others) is also skipped as an attack_type.
    """
    year_types: dict[str, set[str]] = {}
    for row in new_rows:
        cat = row['category']
        attack_type = row['attack_type']
        if cat in exempt_categories or attack_type == 'Ostatní':
            continue
        year_types.setdefault(cat, set()).add(attack_type)

    conflicts = []
    for cat, types_this_year in year_types.items():
        prior_types = seen.get(cat, set()) - {'Ostatní'}
        combined = types_this_year | prior_types
        if len(combined) > 1:
            conflicts.append(
                f"  Category '{cat}': found {sorted(types_this_year)} in "
                f"{year}, but prior data has {sorted(prior_types) if prior_types else 'none'}"
            )
    return conflicts


# ── Competition processing ────────────────────────────────────────────────────

def _process_competition(
    client,
    comp_meta: dict,
    global_categories: dict,
    district_map: dict,
    config: dict,
) -> list[dict]:
    """Download, scrape, and upload a single competition page.

    Returns the list of uploaded rows (empty if nothing was uploaded or an
    error occurred).  Used by daily mode where no conflict detection is needed.
    """
    link = comp_meta.get('link', '')
    league_label = comp_meta.get('league') or 'FSEU'

    try:
        html = downloader.download_competition_page(link)
    except Exception as exc:
        print(
            f'  [{league_label} {comp_meta.get("date")}] '
            f'Download failed for {link}: {exc}',
            file=sys.stderr,
        )
        return []

    rows = scraper.scrape_individual_page(
        html,
        comp_meta,
        global_categories,
        comp_meta.get('league_categories', {}),
        district_map,
        source_name=link,
        interactive=False,
        full_config=config,
        full_league_name=comp_meta.get('full_league_name') or '',
        category_aliases=config.get('category_aliases', {}),
        compound_prefixes=set(config.get('compound_category_prefixes', [])),
    )

    if not rows:
        return []

    uploaded = db.upload_records(client, rows)
    if uploaded:
        print(
            f'  [{league_label} {comp_meta["date"]}] '
            f'+ {comp_meta["place"]} ({uploaded} rows)'
        )
    return rows if uploaded else []


# ── Daily mode ────────────────────────────────────────────────────────────────

def _run_daily(client, config: dict, only_league: str | None = None) -> None:
    today = _today()
    year = today.year
    global_categories = config.get('categories', {})
    district_map = config.get('district_abbreviations', {})
    leagues = config.get('leagues', {})

    print(f'Fetching global competition list for {year} …')
    try:
        html = downloader.download_global_list(year)
    except Exception as exc:
        print(f'Error: Could not download global list for {year}: {exc}', file=sys.stderr)
        sys.exit(1)

    competitions = scraper.parse_global_list(html, source_name=f'vysledky-souteze-{year}')
    print(f'Found {len(competitions)} competitions in {year}.')

    scraped_links = db.get_scraped_links(client, year)
    print(f'{len(scraped_links)} competition(s) already scraped.')

    league_lookup = _build_league_lookup(leagues)

    # Filter: not yet scraped and date <= today (results not published for future competitions)
    new_comps = [
        c for c in competitions
        if c['link'] not in scraped_links
        and c['date'] <= today.isoformat()
    ]
    print(f'{len(new_comps)} new competition(s) to process.')

    # Resolve league filter
    target_display: str | None = None
    if only_league:
        if only_league not in leagues:
            print(f'Error: League key {only_league!r} not found in config.json.', file=sys.stderr)
            sys.exit(1)
        target_display = leagues[only_league]['display_name']

    total_uploaded = 0
    for comp in new_comps:
        meta = _resolve_competition_meta(comp, league_lookup)
        if meta is None:
            continue  # unknown league — warning already printed

        # Apply --league filter (FSEU excluded when filter is active)
        if target_display is not None and meta.get('league') != target_display:
            continue

        rows = _process_competition(client, meta, global_categories, district_map, config)
        total_uploaded += len(rows)

    print(f'\nDaily run complete. {total_uploaded} row(s) uploaded.')


# ── Backfill mode ─────────────────────────────────────────────────────────────

def _run_backfill(
    client,
    config: dict,
    only_league: str | None = None,
    only_year: int | None = None,
) -> None:
    today = _today()
    global_categories = config.get('categories', {})
    district_map = config.get('district_abbreviations', {})
    leagues = config.get('leagues', {})

    league_lookup = _build_league_lookup(leagues)

    # Determine year range
    if only_year is not None:
        year_range = range(only_year, only_year + 1)
    else:
        min_start_year = min(
            (cfg.get('start_year', today.year) for cfg in leagues.values()),
            default=today.year,
        )
        year_range = range(min_start_year, today.year + 1)

    # Resolve target league display name for filtering
    target_display: str | None = None
    if only_league:
        if only_league not in leagues:
            print(f'Error: League key {only_league!r} not found in config.json.', file=sys.stderr)
            sys.exit(1)
        target_display = leagues[only_league]['display_name']

    # Per-league seen maps for conflict detection {league_display | None → {cat → set(types)}}
    # Initialise from whatever is already in the DB.
    seen_by_league: dict = {}
    if target_display:
        seen_by_league[target_display] = db.get_category_attack_types(client, target_display)
    else:
        for cfg in leagues.values():
            d = cfg['display_name']
            seen_by_league[d] = db.get_category_attack_types(client, d)
        seen_by_league[None] = {}  # FSEU bucket (no conflict detection needed)

    failed_leagues: set = set()  # display names of leagues that had conflicts
    total_uploaded = 0

    for year in year_range:
        print(f'\n── Year {year} ──────────────────────────────────────────────')
        try:
            html = downloader.download_global_list(year)
        except Exception as exc:
            print(f'  Download failed for global list {year}: {exc}', file=sys.stderr)
            continue

        competitions = scraper.parse_global_list(html, source_name=f'vysledky-souteze-{year}')
        if not competitions:
            print(f'  No competitions found for {year}.')
            continue

        scraped_links = db.get_scraped_links(client, year)

        for comp in competitions:
            meta = _resolve_competition_meta(comp, league_lookup)
            if meta is None:
                continue  # unknown league

            league_display = meta.get('league')  # None for FSEU

            # League filter: skip other leagues (FSEU has league=None, excluded when filter active)
            if target_display is not None and league_display != target_display:
                continue

            # Respect per-league start_year (skip years before the league existed)
            if league_display is not None and only_year is None:
                league_key = league_lookup.get(league_display, {}).get('league_key')
                if league_key:
                    start_year = leagues[league_key].get('start_year', 1)
                    if year < start_year:
                        continue

            # Skip leagues that failed conflict detection earlier
            if league_display in failed_leagues:
                continue

            # Link-based deduplication
            if meta['link'] in scraped_links:
                continue

            league_label = league_display or 'FSEU'
            league_cats = meta.get('league_categories', {})
            exempt = _exempt_categories(global_categories, league_cats)
            link = meta['link']

            # Download individual competition page
            try:
                comp_html = downloader.download_competition_page(link)
            except Exception as exc:
                print(
                    f'  [{league_label} {meta["date"]}] Download failed for {link}: {exc}',
                    file=sys.stderr,
                )
                continue

            # Scrape
            rows = scraper.scrape_individual_page(
                comp_html,
                meta,
                global_categories,
                league_cats,
                district_map,
                source_name=link,
                interactive=False,
                full_config=config,
                full_league_name=meta.get('full_league_name') or '',
                category_aliases=config.get('category_aliases', {}),
                compound_prefixes=set(config.get('compound_category_prefixes', [])),
            )

            if not rows:
                continue

            # Conflict detection (skip for FSEU — no league-level tracking)
            if league_display is not None:
                if league_display not in seen_by_league:
                    seen_by_league[league_display] = {}
                conflicts = _check_attack_type_conflicts(
                    rows, year, seen_by_league[league_display], league_label, exempt
                )
                if conflicts:
                    print(
                        f'\n[{league_label}] CONFLICT detected in {year} — '
                        f'aborting entire league backfill.'
                    )
                    for msg in conflicts:
                        print(msg)
                    deleted = db.delete_league_records(client, league_display)
                    league_key = league_lookup.get(league_display, {}).get('league_key', league_display)
                    print(
                        f'  Deleted {deleted} row(s) for {league_label} from the DB.\n'
                        f'  Fix config.json category overrides then re-run:\n'
                        f'    python orchestrator.py --backfill --league {league_key}'
                    )
                    failed_leagues.add(league_display)
                    continue

            # Upload
            uploaded = db.upload_records(client, rows)
            if uploaded:
                total_uploaded += uploaded
                print(
                    f'  [{league_label} {year}] + {meta["place"]} ({uploaded} rows)'
                )

            # Update seen map for conflict detection in subsequent years
            if league_display is not None:
                for row in rows:
                    seen_by_league[league_display].setdefault(
                        row['category'], set()
                    ).add(row['attack_type'])

    print(f'\nBackfill complete. Total rows uploaded: {total_uploaded}')
    if failed_leagues:
        labels = sorted(str(l) for l in failed_leagues)
        print(f'Leagues with conflicts (skipped): {labels}')


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Orchestrate firesport scraping and Supabase uploads.'
    )
    parser.add_argument(
        '--backfill',
        action='store_true',
        help='Scrape full history (start_year → current year) for all leagues.',
    )
    parser.add_argument(
        '--league',
        metavar='KEY',
        help='Limit to a single league key (e.g. zl, excr).',
    )
    parser.add_argument(
        '--year',
        type=int,
        metavar='YEAR',
        help='Limit backfill to a single year. Only valid with --backfill.',
    )
    args = parser.parse_args()

    if args.year and not args.backfill:
        parser.error('--year is only valid with --backfill')

    config = load_config()
    client = db.init_client()

    only_league = args.league.lower() if args.league else None

    if args.backfill:
        _run_backfill(client, config, only_league, only_year=args.year)
    else:
        _run_daily(client, config, only_league)


if __name__ == '__main__':
    main()

