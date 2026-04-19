#!/usr/bin/env python3
"""Firesport timestamps orchestrator.

Coordinates downloading, scraping, and uploading competition results to
Supabase for all active leagues defined in config.json.

Modes
-----
Daily (default)
    For each active league, checks the current year and previous year.
    Uses pending_competitions to decide whether a download + scrape is
    even needed:

      - If any pending competition date is <= today and not yet in the DB,
        download the year page, scrape it, and upload newly found results.
        A pending entry is only removed once its results are in the DB.
        Results may not be published the same day as the competition, so
        entries stay in pending until data is actually found.

      - If no pending competitions exist for the year, re-download the year
        page and refresh the schedule — but only if last_schedule_check is
        older than SCHEDULE_CHECK_DAYS days (or has never been done).

      - If all pending competition dates are in the future, skip the league
        (no scraping needed yet).

Backfill (--backfill)
    Downloads and scrapes every year from start_year to the current year for
    every active league, uploading any rows not already in the DB.  Years
    that yield no tab divs (gap years or future years with no data) are
    skipped silently.

Usage
-----
    python orchestrator.py                 # daily mode
    python orchestrator.py --backfill      # full history
    python orchestrator.py --backfill --league zl   # one league only
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import db
import downloader
import scraper

_CONFIG_FILE = Path('config.json')
# Re-download year pages to refresh the schedule after this many days of
# inactivity when no pending competitions are known.
_SCHEDULE_CHECK_DAYS = 14


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


# ── Core per-league-per-year logic ────────────────────────────────────────────

def _process_year(
    client,
    league_key: str,
    league_cfg: dict,
    global_categories: dict,
    year: int,
    category_aliases: dict | None = None,
) -> int:
    """Download, scrape, diff, and upload for one league + year.

    Returns the number of rows uploaded (0 if nothing new).
    """
    display = league_cfg['display_name']
    full_league_name = league_cfg.get('full_league_name', '')

    try:
        html = downloader.download_html(league_key, year)
    except Exception as exc:
        print(f'  [{display} {year}] Download failed: {exc}', file=sys.stderr)
        return 0

    league_categories = league_cfg.get('categories', {})
    aliases = category_aliases or {}
    rows = scraper.scrape_html(
        html,
        f'{league_key.upper()}.{year}.html',
        display,
        global_categories,
        league_categories,
        interactive=False,
        full_league_name=full_league_name,
        category_aliases=aliases,
    )

    if not rows:
        return 0

    existing = db.get_existing_competitions(client, display, year)
    new_rows = [r for r in rows if (r['attack_date'], r['place']) not in existing]

    if not new_rows:
        return 0

    uploaded = db.upload_records(client, new_rows)
    # Group uploaded rows by competition for the summary log
    comps: dict[tuple[str, str], int] = {}
    for r in new_rows:
        key = (r['attack_date'], r['place'])
        comps[key] = comps.get(key, 0) + 1
    for (d, p), cnt in sorted(comps.items()):
        print(f'  [{display} {year}] + {d} {p} ({cnt} rows)')

    return uploaded


def _refresh_schedule(
    client,
    league_key: str,
    league_cfg: dict,
    year: int,
    today: date,
) -> bool:
    """Re-download the year page and update pending_competitions.

    Adds newly discovered future competitions that are not yet in the DB.
    Updates last_schedule_check to today.
    Returns True if the config was modified.
    """
    display = league_cfg['display_name']
    try:
        html = downloader.download_html(league_key, year)
    except Exception as exc:
        print(
            f'  [{display} {year}] Schedule refresh download failed: {exc}',
            file=sys.stderr,
        )
        return False

    schedule = scraper.scrape_schedule(html)
    if not schedule:
        league_cfg['last_schedule_check'] = today.isoformat()
        return True

    # Competitions already in DB for this year
    existing_in_db = db.get_existing_competitions(client, display, year)
    # Competitions already known as pending
    pending_dates_places = {
        (e['date'], e['place']) for e in league_cfg.get('pending_competitions', [])
    }

    added = 0
    for entry in schedule:
        key = (entry['date'], entry['place'])
        if key in existing_in_db:
            continue  # already scraped and uploaded
        if key in pending_dates_places:
            continue  # already tracked as pending
        league_cfg.setdefault('pending_competitions', []).append(
            {'date': entry['date'], 'place': entry['place']}
        )
        added += 1

    league_cfg['last_schedule_check'] = today.isoformat()

    if added:
        print(
            f'  [{display} {year}] Schedule refresh: {added} new '
            f'competition(s) added to pending'
        )
    else:
        print(f'  [{display} {year}] Schedule refresh: no new competitions found')

    return True


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
    """
    year_types: dict[str, set[str]] = {}
    for row in new_rows:
        cat = row['category']
        if cat in exempt_categories:
            continue
        year_types.setdefault(cat, set()).add(row['attack_type'])

    conflicts = []
    for cat, types_this_year in year_types.items():
        combined = types_this_year | seen.get(cat, set())
        if len(combined) > 1:
            prior = seen.get(cat, set())
            conflicts.append(
                f"  Category '{cat}': found {sorted(types_this_year)} in "
                f"{year}, but prior data has {sorted(prior) if prior else 'none'}"
            )
    return conflicts


# ── Daily mode ────────────────────────────────────────────────────────────────

def _run_daily(client, config: dict, only_league: str | None = None) -> None:
    today = _today()
    global_categories = config.get('categories', {})
    leagues = config.get('leagues', {})
    config_changed = False

    for league_key, league_cfg in leagues.items():
        if not league_cfg.get('active', False):
            continue
        if only_league and league_key != only_league:
            continue

        display = league_cfg['display_name']
        years_to_check = {today.year, today.year - 1}

        for year in sorted(years_to_check, reverse=True):
            pending = league_cfg.get('pending_competitions', [])
            year_pending = [e for e in pending if e['date'].startswith(str(year))]

            # ── Case 1: pending competitions exist for this year ─────────────
            if year_pending:
                overdue = [
                    e for e in year_pending
                    if date.fromisoformat(e['date']) <= today
                ]
                all_future = len(overdue) == 0

                if all_future:
                    # Earliest pending is still in the future — nothing to do
                    earliest = min(e['date'] for e in year_pending)
                    print(
                        f'  [{display} {year}] Skipping — next competition '
                        f'on {earliest}'
                    )
                    continue

                # There are overdue pending competitions — download and scrape
                uploaded = _process_year(
                    client, league_key, league_cfg, global_categories, year,
                    category_aliases=config.get('category_aliases', {}),
                )

                if uploaded > 0:
                    # Find which overdue competitions now have results in DB
                    existing = db.get_existing_competitions(client, display, year)
                    still_pending = [
                        e for e in pending
                        if not (
                            date.fromisoformat(e['date']) <= today
                            and (e['date'], e['place']) in existing
                        )
                    ]
                    removed = len(pending) - len(still_pending)
                    if removed:
                        league_cfg['pending_competitions'] = still_pending
                        config_changed = True
                        print(
                            f'  [{display} {year}] Removed {removed} resolved '
                            f'competition(s) from pending'
                        )
                else:
                    # Downloaded OK but no new rows — results not published yet
                    print(
                        f'  [{display} {year}] {len(overdue)} overdue pending '
                        f'competition(s) found, but no results published yet'
                    )
                config_changed = True  # last_schedule_check update below

            # ── Case 2: no pending competitions for this year ────────────────
            else:
                last_check_str = league_cfg.get('last_schedule_check')
                if last_check_str:
                    last_check = date.fromisoformat(last_check_str)
                    days_since = (today - last_check).days
                else:
                    days_since = _SCHEDULE_CHECK_DAYS + 1  # force refresh

                if days_since >= _SCHEDULE_CHECK_DAYS:
                    changed = _refresh_schedule(
                        client, league_key, league_cfg, year, today
                    )
                    if changed:
                        config_changed = True
                else:
                    print(
                        f'  [{display} {year}] Skipping schedule refresh — '
                        f'checked {days_since}d ago'
                    )

    if config_changed:
        save_config(config)
        print('\nconfig.json updated.')


# ── Backfill mode ─────────────────────────────────────────────────────────────

def _run_backfill(client, config: dict, only_league: str | None = None) -> None:
    today = _today()
    global_categories = config.get('categories', {})
    leagues = config.get('leagues', {})
    total_uploaded = 0

    for league_key, league_cfg in leagues.items():
        if only_league and league_key != only_league:
            continue

        display = league_cfg['display_name']
        start_year = league_cfg.get('start_year', today.year)
        print(f'\n[{display}] Backfilling {start_year}–{today.year} …')

        # Seed seen map from whatever is already in the DB for this league
        seen: dict[str, set[str]] = db.get_category_attack_types(client, display)
        league_categories = league_cfg.get('categories', {})
        exempt = _exempt_categories(global_categories, league_categories)
        conflict_found = False

        for year in range(start_year, today.year + 1):
            if conflict_found:
                break

            # Scrape the year without uploading yet so we can validate first
            try:
                html = downloader.download_html(league_key, year)
            except Exception as exc:
                print(f'  [{display} {year}] Download failed: {exc}', file=sys.stderr)
                continue

            full_league_name = league_cfg.get('full_league_name', '')
            rows = scraper.scrape_html(
                html,
                f'{league_key.upper()}.{year}.html',
                display,
                global_categories,
                league_categories,
                interactive=False,
                full_league_name=full_league_name,
            )

            if not rows:
                print(f'  [{display} {year}] No new rows')
                continue

            existing = db.get_existing_competitions(client, display, year)
            new_rows = [r for r in rows if (r['attack_date'], r['place']) not in existing]

            if not new_rows:
                print(f'  [{display} {year}] No new rows')
                continue

            conflicts = _check_attack_type_conflicts(new_rows, year, seen, display, exempt)
            if conflicts:
                print(
                    f'\n[{display}] CONFLICT detected in {year} — aborting entire league backfill.'
                )
                print('  Conflicting categories:')
                for msg in conflicts:
                    print(msg)
                deleted = db.delete_league_records(client, display)
                print(
                    f'  Deleted {deleted} existing row(s) for {display} from the DB.\n'
                    f'  Fix the config.json category overrides for [{league_key}]\n'
                    f'  then re-run:  python orchestrator.py --backfill --league {league_key}'
                )
                conflict_found = True
                break

            uploaded = db.upload_records(client, new_rows)
            total_uploaded += uploaded

            # Update seen with this year's newly confirmed types
            for row in new_rows:
                seen.setdefault(row['category'], set()).add(row['attack_type'])

            comps: dict[tuple[str, str], int] = {}
            for r in new_rows:
                key = (r['attack_date'], r['place'])
                comps[key] = comps.get(key, 0) + 1
            for (d, p), cnt in sorted(comps.items()):
                print(f'  [{display} {year}] + {d} {p} ({cnt} rows)')

    print(f'\nBackfill complete. Total rows uploaded: {total_uploaded}')


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
    args = parser.parse_args()

    config = load_config()
    client = db.init_client()

    only_league = args.league.lower() if args.league else None

    if args.backfill:
        _run_backfill(client, config, only_league)
    else:
        _run_daily(client, config, only_league)


if __name__ == '__main__':
    main()
