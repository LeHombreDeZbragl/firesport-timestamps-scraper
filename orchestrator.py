#!/usr/bin/env python3
"""Firesport timestamps orchestrator.

Coordinates downloading, scraping, and uploading competition results to
Supabase for all active leagues defined in config.json.

Modes
-----
Daily (default)
    For every league in config.json, checks all years from
    (last_scraped_year + 1) up to the current year.  When
    last_scraped_year is null the range starts at start_year.

    Per year:
      - If any pending competition date is <= today and not yet in the DB,
        download the year page, scrape it, and upload newly found results.
        A pending entry is only removed once its results are in the DB.
        Results may not be published the same day as the competition, so
        entries stay in pending until data is actually found.

      - If no pending competitions exist for the year, re-download the year
        page and refresh the schedule — but only when next_schedule_check is
        today or in the past (checked once at league level, not per year).

      - If all pending competition dates are in the future, skip that year.

    After the year loop:
      - last_scraped_year is advanced for every past year whose pending list
        is fully empty, and stale pending entries are pruned.
      - next_schedule_check is set to today + randint(12, 16) days whenever
        a schedule refresh was performed (spreads checks over time).

Backfill (--backfill)
    Downloads and scrapes every year from start_year to the current year for
    every league, uploading any rows not already in the DB.  Years
    that yield no rows (gap years or future years with no data) are
    skipped silently.  Sets last_scraped_year and next_schedule_check in
    config.json after each league completes.

Usage
-----
    python orchestrator.py                 # daily mode
    python orchestrator.py --backfill      # full history
    python orchestrator.py --backfill --league zl   # one league only
"""

import argparse
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import db
import downloader
import scraper

_CONFIG_FILE = Path('config.json')
# next_schedule_check is set to today + randint(MIN, MAX) days after each
# schedule refresh, spreading checks across different days over time.
_SCHEDULE_CHECK_DAYS_MIN = 12
_SCHEDULE_CHECK_DAYS_MAX = 16


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
    compound_prefixes: set | None = None,
) -> int:
    """Download, scrape, diff, and upload for one league + year.

    Returns the number of rows uploaded (0 if nothing new).
    """
    display = league_cfg['display_name']
    full_league_name = league_cfg.get('full_league_name', '')
    standalone_domain = league_cfg.get('standalone_domain', False)

    try:
        html = downloader.download_html(league_key, year, standalone_domain=standalone_domain)
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
        compound_prefixes=compound_prefixes,
    )

    if not rows:
        return 0

    existing = db.get_existing_competitions(client, display, year)
    new_rows = [r for r in rows if (r['attack_date'], r['place'], r['category']) not in existing]

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
) -> bool:
    """Re-download the year page and update pending_competitions.

    Adds newly discovered competitions that are not yet in the DB or pending.
    Does NOT touch next_schedule_check — the caller sets it after all years
    are processed.
    Returns True if the config was modified (new entries added).
    """
    display = league_cfg['display_name']
    standalone_domain = league_cfg.get('standalone_domain', False)
    try:
        html = downloader.download_html(league_key, year, standalone_domain=standalone_domain)
    except Exception as exc:
        print(
            f'  [{display} {year}] Schedule refresh download failed: {exc}',
            file=sys.stderr,
        )
        return False

    schedule = scraper.scrape_schedule(html)
    if not schedule:
        return False

    # Competitions already in DB for this year
    existing_in_db = db.get_existing_competitions(client, display, year)
    existing_dates_places = {(d, p) for d, p, _c in existing_in_db}
    # Competitions already known as pending
    pending_dates_places = {
        (e['date'], e['place']) for e in league_cfg.get('pending_competitions', [])
    }

    added = 0
    for entry in schedule:
        key = (entry['date'], entry['place'])
        if key in existing_dates_places:
            continue  # already scraped and uploaded
        if key in pending_dates_places:
            continue  # already tracked as pending
        league_cfg.setdefault('pending_competitions', []).append(
            {'date': entry['date'], 'place': entry['place']}
        )
        added += 1

    if added:
        print(
            f'  [{display} {year}] Schedule refresh: {added} new '
            f'competition(s) added to pending'
        )
    else:
        print(f'  [{display} {year}] Schedule refresh: no new competitions found')

    return added > 0


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
    'Ostatní' (others) is also skipped as conflicts with it are not meaningful.
    """
    year_types: dict[str, set[str]] = {}
    for row in new_rows:
        cat = row['category']
        if cat in exempt_categories or cat == 'Ostatní':
            continue
        year_types.setdefault(cat, set()).add(row['attack_type'])

    conflicts = []
    for cat, types_this_year in year_types.items():
        # Filter 'Ostatní' from prior data too — it's not a real attack type
        prior_types = seen.get(cat, set()) - {'Ostatní'}
        combined = types_this_year | prior_types
        if len(combined) > 1:
            conflicts.append(
                f"  Category '{cat}': found {sorted(types_this_year)} in "
                f"{year}, but prior data has {sorted(prior_types) if prior_types else 'none'}"
            )
    return conflicts


# ── Daily mode ────────────────────────────────────────────────────────────────

def _run_daily(client, config: dict, only_league: str | None = None) -> None:
    today = _today()
    global_categories = config.get('categories', {})
    leagues = config.get('leagues', {})
    config_changed = False

    for league_key, league_cfg in leagues.items():
        if only_league and league_key != only_league:
            continue

        display = league_cfg['display_name']

        # Determine year range: years after last fully-scraped year up to now
        last_scraped = league_cfg.get('last_scraped_year')
        first_year = (last_scraped + 1) if last_scraped else league_cfg.get('start_year', today.year)
        years_to_check = range(first_year, today.year + 1)

        # Schedule-refresh gate evaluated once at league level (not per year)
        # so that refreshing year N never blocks year N-1 on the same run.
        next_check_str = league_cfg.get('next_schedule_check')
        do_schedule_refresh = (
            next_check_str is None
            or date.fromisoformat(next_check_str) <= today
        )

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
                    compound_prefixes=set(config.get('compound_category_prefixes', [])),
                )

                if uploaded > 0:
                    # Find which overdue competitions now have results in DB
                    existing = db.get_existing_competitions(client, display, year)
                    existing_dates_places = {(d, p) for d, p, _c in existing}
                    still_pending = [
                        e for e in pending
                        if not (
                            date.fromisoformat(e['date']) <= today
                            and (e['date'], e['place']) in existing_dates_places
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

            # ── Case 2: no pending competitions for this year ────────────────
            else:
                if do_schedule_refresh:
                    changed = _refresh_schedule(
                        client, league_key, league_cfg, year,
                    )
                    if changed:
                        config_changed = True
                else:
                    print(
                        f'  [{display} {year}] Skipping schedule refresh — '
                        f'next check on {next_check_str}'
                    )

        # ── Post-year-loop: advance last_scraped_year ────────────────────────
        # Walk forward from last_scraped through completed past years; stop at
        # the first year that still has pending entries.
        candidate = last_scraped if last_scraped is not None else (league_cfg.get('start_year', today.year) - 1)
        while candidate + 1 < today.year:
            next_year = candidate + 1
            year_still_pending = [
                e for e in league_cfg.get('pending_competitions', [])
                if e['date'].startswith(str(next_year))
            ]
            if year_still_pending:
                break
            candidate = next_year
        if candidate != last_scraped:
            league_cfg['last_scraped_year'] = candidate
            config_changed = True
            print(f'  [{display}] Advanced last_scraped_year to {candidate}')

        # Prune pending entries for years now covered by last_scraped_year
        new_last = league_cfg.get('last_scraped_year')
        if new_last:
            all_pending = league_cfg.get('pending_competitions', [])
            pruned = [e for e in all_pending if int(e['date'][:4]) > new_last]
            if len(pruned) < len(all_pending):
                league_cfg['pending_competitions'] = pruned
                config_changed = True

        # ── Post-year-loop: update next_schedule_check ───────────────────────
        if do_schedule_refresh:
            next_days = random.randint(_SCHEDULE_CHECK_DAYS_MIN, _SCHEDULE_CHECK_DAYS_MAX)
            league_cfg['next_schedule_check'] = (today + timedelta(days=next_days)).isoformat()
            config_changed = True

    if config_changed:
        save_config(config)
        print('\nconfig.json updated.')


# ── Backfill mode ─────────────────────────────────────────────────────────────

def _run_backfill(client, config: dict, only_league: str | None = None) -> None:
    today = _today()
    global_categories = config.get('categories', {})
    leagues = config.get('leagues', {})
    total_uploaded = 0
    config_changed = False
    any_conflict_found = False

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
                html = downloader.download_html(league_key, year, standalone_domain=league_cfg.get('standalone_domain', False))
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
                compound_prefixes=set(config.get('compound_category_prefixes', [])),
            )

            if not rows:
                print(f'  [{display} {year}] No new rows')
                continue

            existing = db.get_existing_competitions(client, display, year)
            new_rows = [r for r in rows if (r['attack_date'], r['place'], r['category']) not in existing]

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
                any_conflict_found = True
                break

            uploaded = db.upload_records(client, new_rows)
            total_uploaded += uploaded

            # Track last successfully scraped year so a mid-run conflict leaves
            # last_scraped_year pointing to the last clean year.
            league_cfg['last_scraped_year'] = year
            config_changed = True

            # Update seen with this year's newly confirmed types
            for row in new_rows:
                seen.setdefault(row['category'], set()).add(row['attack_type'])

            comps: dict[tuple[str, str], int] = {}
            for r in new_rows:
                key = (r['attack_date'], r['place'])
                comps[key] = comps.get(key, 0) + 1
            for (d, p), cnt in sorted(comps.items()):
                print(f'  [{display} {year}] + {d} {p} ({cnt} rows)')

        # Prune pending entries for years now covered by last_scraped_year
        new_last = league_cfg.get('last_scraped_year')
        if new_last:
            all_pending = league_cfg.get('pending_competitions', [])
            pruned = [e for e in all_pending if int(e['date'][:4]) > new_last]
            if len(pruned) < len(all_pending):
                league_cfg['pending_competitions'] = pruned
                config_changed = True

        # Set next_schedule_check on clean completion
        if not conflict_found:
            next_days = random.randint(_SCHEDULE_CHECK_DAYS_MIN, _SCHEDULE_CHECK_DAYS_MAX)
            league_cfg['next_schedule_check'] = (today + timedelta(days=next_days)).isoformat()
            config_changed = True

    if not any_conflict_found:
        print(f'\nBackfill complete. Total rows uploaded: {total_uploaded}')
        if config_changed:
            save_config(config)
            print('config.json updated.')


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
