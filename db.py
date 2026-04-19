#!/usr/bin/env python3
"""Supabase client wrapper for firesport timestamps.

Expected schema for public.timestamps:
    attack_date      text     YYYY-MM-DD
    league           text     display name (ŽL, EXČR, VCB, ...)
    place            text
    placement        text
    attack_type      text     '2B' or '3B'
    category         text
    team             text
    lp               text     nullable — first individual attempt time
    pp               text     nullable — second individual attempt time
    only_final_time  boolean  default false — lp/pp are the same final time
                              because individual times were not recorded

The only_final_time column must be added to the table before uploading records
that contain it:
    ALTER TABLE public.timestamps ADD COLUMN only_final_time boolean DEFAULT false;
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
) -> set[tuple[str, str]]:
    """Return (attack_date, place) pairs already present in the DB.

    Only rows matching the given league and calendar year are considered.
    Paginates automatically so years with many rows are fully covered.
    """
    existing: set[tuple[str, str]] = set()
    offset = 0
    while True:
        result = (
            client.table(_TABLE)
            .select('attack_date, place')
            .eq('league', league_display)
            .gte('attack_date', f'{year}-01-01')
            .lte('attack_date', f'{year}-12-31')
            .range(offset, offset + _PAGE_SIZE - 1)
            .execute()
        )
        for row in result.data:
            existing.add((row['attack_date'], row['place']))
        if len(result.data) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return existing


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
