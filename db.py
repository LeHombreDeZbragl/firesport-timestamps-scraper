#!/usr/bin/env python3
"""Supabase client wrapper for firesport timestamps.

Expected schema for public.timestamps:
    attack_date      text     YYYY-MM-DD
    league           text     display name (ŽL, EXČR, VCB, ...)
    full_league_name text     full human-readable name (Žďárská liga, ...)
    place            text
    placement        text
    attack_type      text     '2B' or '3B'
    category         text
    team             text
    lp               real     nullable — first individual attempt time
    pp               real     nullable — second individual attempt time
    only_final_time  boolean  default false — lp/pp are the same final time
                              because individual times were not recorded
"""

import os
import sys

from dotenv import load_dotenv
from postgrest.exceptions import APIError
from supabase import Client, create_client

_TABLE = 'timestamps'
# Supabase/PostgREST returns at most this many rows per request; also used as
# the insert batch size to stay well within API payload limits.
_PAGE_SIZE = 500


def init_client() -> Client:
    """Create and return an authenticated Supabase client.

    Reads SUPABASE_URL and SUPABASE_KEY from the environment.  Calls
    load_dotenv() so a .env file in the working directory is honoured.

    Raises SystemExit if either variable is missing or empty.
    """
    load_dotenv()
    url = os.environ.get('SUPABASE_URL', '').strip()
    key = os.environ.get('SUPABASE_KEY', '').strip()
    if not url or not key:
        print(
            'Error: SUPABASE_URL and SUPABASE_KEY must be set '
            'in the environment or in a .env file.',
            file=sys.stderr,
        )
        sys.exit(1)
    return create_client(url, key)


def get_existing_competitions(
    client: Client,
    league_display: str,
    year: int,
) -> set[tuple[str, str, str]]:
    """Return (attack_date, place, category) triples already present in the DB.

    Only rows matching the given league and calendar year are considered.
    Paginates automatically so years with many rows are fully covered.
    """
    existing: set[tuple[str, str, str]] = set()
    offset = 0
    while True:
        result = (
            client.table(_TABLE)
            .select('attack_date, place, category')
            .eq('league', league_display)
            .gte('attack_date', f'{year}-01-01')
            .lte('attack_date', f'{year}-12-31')
            .range(offset, offset + _PAGE_SIZE - 1)
            .execute()
        )
        for row in result.data:
            existing.add((row['attack_date'], row['place'], row['category']))
        if len(result.data) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return existing


def get_category_attack_types(
    client: Client,
    league_display: str,
) -> dict[str, set[str]]:
    """Return all (category → set of attack_types) already in the DB for a league.

    Used by backfill conflict detection to compare new scraped data against
    whatever has already been uploaded across all years.
    """
    result: dict[str, set[str]] = {}
    offset = 0
    while True:
        resp = (
            client.table(_TABLE)
            .select('category, attack_type')
            .eq('league', league_display)
            .range(offset, offset + _PAGE_SIZE - 1)
            .execute()
        )
        for row in resp.data:
            cat = row['category']
            at = row['attack_type']
            result.setdefault(cat, set()).add(at)
        if len(resp.data) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return result


def delete_league_records(client: Client, league_display: str) -> int:
    """Delete ALL rows for a league from the DB.

    Returns the number of rows deleted.
    Used when a backfill conflict is detected — wipes the league so it can be
    re-loaded cleanly once the config is corrected.
    """
    result = (
        client.table(_TABLE)
        .delete()
        .eq('league', league_display)
        .execute()
    )
    return len(result.data)


_NULLABLE_FLOAT_COLS = ('lp', 'pp')


def _prepare_records(records: list[dict]) -> list[dict]:
    """Coerce record values to types the DB schema expects.

    - lp / pp: empty string → None  (DB columns are real / float, not text)
    """
    out = []
    for rec in records:
        row = dict(rec)
        for col in _NULLABLE_FLOAT_COLS:
            if col in row and row[col] == '':
                row[col] = None
        out.append(row)
    return out


def upload_records(client: Client, records: list[dict]) -> int:
    """Insert records into public.timestamps in batches.

    Returns the total number of rows inserted.
    Raises SchemaError with clear instructions if a required column is missing.
    Raises APIError (from the Supabase SDK) on other failures.
    """
    if not records:
        return 0
    prepared = _prepare_records(records)
    total = 0
    for i in range(0, len(prepared), _PAGE_SIZE):
        batch = prepared[i : i + _PAGE_SIZE]
        try:
            result = client.table(_TABLE).insert(batch).execute()
        except APIError as exc:
            # PGRST204 = column not found in schema cache
            if exc.code == 'PGRST204' and 'only_final_time' in str(exc.message):
                print(
                    '\nError: The only_final_time column is missing from '
                    f'public.{_TABLE}.\n'
                    'Run the following SQL in the Supabase SQL Editor:\n\n'
                    '    ALTER TABLE public.timestamps\n'
                    '        ADD COLUMN IF NOT EXISTS only_final_time '
                    'boolean NOT NULL DEFAULT false;\n\n'
                    'Or run:  psql ... -f schema_migration.sql\n',
                    file=sys.stderr,
                )
                sys.exit(1)
            raise
        total += len(result.data)
    return total
