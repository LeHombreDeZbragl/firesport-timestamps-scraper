# firesport-timestamps-scraper

Scrapes competition results from [firesport.eu](https://firesport.eu) for Czech firesport leagues and stores them in a Supabase database. A GitHub Actions workflow runs the pipeline daily and commits any schedule changes back to the repo automatically.

---

## What it does

Each league has a season page at:

```
https://{league}.firesport.eu/web_souteze.php?akce=sel&rok={year}
```

The page lists all competitions of the season. Each competition is a tab with an HTML table of results — one row per competitor, with columns for placement, team, lp (first individual attempt), pp (second individual attempt), and final time.

The scraper collects all results and uploads them to a `public.timestamps` table in Supabase. On every daily run it only downloads pages whose competition dates are due or overdue (smart scheduling via `pending_competitions` in `config.json`), keeping network traffic low.

---

## Supported leagues

| Key    | Display name | Since |
|--------|--------------|-------|
| `zl`   | ŽL           | 2012  |
| `excr` | EXČR         | 1996  |
| `vcbl` | VCB          | 2002  |
| `plpu` | PLPU         | 2013  |

---

## Repository structure

```
orchestrator.py          # main pipeline — daily and backfill modes
downloader.py            # HTTP fetcher; also a standalone debug CLI
scraper.py               # HTML parser; also a standalone debug CLI
db.py                    # Supabase client wrapper
config.json              # leagues, categories, and pending schedule (committed)
requirements.txt         # Python dependencies
.env                     # credentials — NOT committed (see .env.example)
.env.example             # template for .env
.github/workflows/
    scrape.yml           # GitHub Actions daily cron
debug_output/            # local HTML cache used by --debug (gitignored)
```

---

## The four scripts

### `downloader.py`

Downloads HTML from firesport.eu.

**Used by the orchestrator** via:

```python
html: str = downloader.download_html(league_key, year)
```

Returns the raw HTML string in memory — nothing written to disk.

**Standalone CLI** for manual inspection:

```bash
# Save to debug_output/zl/ZL.2025.html
python downloader.py --debug zl 2025 2025

# Save a range of years
python downloader.py --debug vcbl 2010 2015
```

---

### `scraper.py`

Parses raw HTML into a list of result dicts.

**Used by the orchestrator** via two functions:

```python
# Parse all result rows from a downloaded page
rows: list[dict] = scraper.scrape_html(
    html, source_name, league_display,
    global_categories, league_categories, interactive=False
)

# Parse only the schedule (competition tabs) — no result tables needed
schedule: list[dict] = scraper.scrape_schedule(html)
# → [{"date": "2026-05-30", "place": "Zbraslav", "has_results": False}, ...]
```

**Standalone CLI** for debugging against a locally saved HTML file:

```bash
python scraper.py zl debug_output/zl -o out.csv
```

#### Attack type resolution

Each result row needs an `attack_type` value (`2B` or `3B`). The scraper resolves this in three tiers:

1. **Per-league override** in `config.json` — can be a fixed value (`"2B"` / `"3B"`) or `"auto"`, which parses the `NxB` pattern from the competition's h3 heading. Useful for leagues where attack type varies by competition (e.g. PLPU Muži).
2. **Global default** from the top-level `categories` map in `config.json`. All entries default to `"auto"` — the NxB pattern is parsed from the heading. Set a fixed value here to pin a category globally (e.g. if a league always runs `"Ženy"` as `2B` and the heading never contains `NxB`).
3. **Interactive prompt** (when running with `interactive=True`, i.e. the debug CLI) or a warning + row skip (automated mode) when no type could be determined.

#### `only_final_time` flag

Some older results list only the final time without individual lp/pp splits. When this is detected, `lp` and `pp` are set to the final time and `only_final_time` is set to `true` so downstream consumers can distinguish them.

---

### `db.py`

Thin wrapper around the Supabase Python client.

| Function | What it does |
|---|---|
| `init_client()` | Loads `.env`, validates credentials, returns a `Client` |
| `get_existing_competitions(client, league, year)` | Returns `{(date, place), ...}` already in the DB — used for dedup |
| `upload_records(client, records)` | Batch-inserts rows (500 at a time); converts `lp`/`pp` empty strings to `None` because the DB columns are `real` |

---

### `orchestrator.py`

The top-level coordinator. Calls downloader → scraper → db in sequence.

#### Daily mode (default)

For every league in `config.json`, checks all years from `last_scraped_year + 1` up to the current year (falls back to `start_year` when `last_scraped_year` is null). Per year:

- **Has overdue pending competitions** (date ≤ today, not yet in DB)? → Download and scrape the year page. Upload any new rows found. Remove resolved competitions from `pending_competitions`. If results are not published yet, leave the entry in pending and retry on the next run.
- **All pending competitions are in the future?** → Skip that year.
- **No pending competitions at all?** → Refresh the schedule from the website if `next_schedule_check` is today or in the past. New competitions found are added to `pending_competitions`.

The schedule-refresh gate is checked **once per league** (not per year), so both the current and previous year are refreshed on the same run when due. After a refresh, `next_schedule_check` is set to `today + randint(12, 16)` days, spreading checks across different days over time.

After the year loop, `last_scraped_year` is advanced for every sequential past year whose pending list is fully empty, and stale pending entries are pruned automatically.

After each run, `config.json` is saved if anything changed. The GitHub Actions workflow commits this file back to the repo.

#### Backfill mode

```bash
python orchestrator.py --backfill            # all leagues, all years
python orchestrator.py --backfill --league zl  # one league
```

Backfill reads the league definitions from `config.json` and processes the specified league(s) from that file.
Downloads every year from `start_year` to the current year, diffs against the DB, and uploads only new rows. Safe to re-run — it never creates duplicates.

After each successfully uploaded year, `last_scraped_year` is updated in `config.json` immediately so that a mid-run conflict leaves it pointing to the last clean year. After a clean run, `next_schedule_check` is also set.

**Conflict detection** runs before each upload. The scraper tracks which `attack_type` values it has seen for every category — both from the DB (existing data) and from all years already processed in the current run. If a category ever resolves to two different attack types (e.g. `Muži` is `3B` in 2022 but `2B` in 2023, or both within the same year), the backfill:

1. Prints a detailed conflict report naming the category, the conflicting types, and the year where the new conflict was detected vs the prior data
2. Deletes **all existing rows for that league** from the DB so the state is clean
3. Stops the league's backfill entirely

To resolve: inspect the HTML for the flagged year, decide whether the difference is intentional (add a per-league `categories` override in `config.json`) or a data error, then re-run the backfill for that league.

---

## Database schema

Table: `public.timestamps`

| Column           | Type      | Notes |
|------------------|-----------|-------|
| `attack_date`    | `text`    | YYYY-MM-DD |
| `league`         | `text`    | Display name (ŽL, EXČR, …) |
| `full_league_name` | `text`  | Full name (Žďárská liga, Extraliga ČR, …) |
| `place`          | `text`    | Competition venue |
| `placement`      | `text`    | Final ranking position |
| `attack_type`    | `text`    | `2B` or `3B` |
| `category`       | `text`    | e.g. Muži, Ženy, Junioři |
| `team`           | `text`    | Team name |
| `lp`             | `real`    | Nullable — first individual attempt (seconds) |
| `pp`             | `real`    | Nullable — second individual attempt (seconds) |
| `only_final_time`| `boolean` | `true` when lp/pp are duplicated final time, individual splits unavailable |

Deduplication key used by the scraper: `(league, attack_date, place)`.

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/LeHombreDeZbragl/firesport-timestamps-scraper.git
cd firesport-timestamps-scraper
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Credentials

```bash
cp .env.example .env
# Fill in SUPABASE_URL and SUPABASE_KEY
```

### 3. Create the Supabase table

```sql
CREATE TABLE public.timestamps (
    attack_date       text,
    league            text,
    full_league_name  text,
    place             text,
    placement         text,
    attack_type       text,
    category          text,
    team              text,
    lp                real,
    pp                real,
    only_final_time   boolean DEFAULT false
);

-- Grant access for Data API (required after May 30, 2026 for new Supabase projects)
grant select, insert, update, delete on public.timestamps to anon;
grant select, insert, update, delete on public.timestamps to authenticated;
grant select, insert, update, delete on public.timestamps to service_role;

-- Enable Row Level Security
alter table public.timestamps enable row level security;
```

### 4. Backfill historical data

```bash
python orchestrator.py --backfill
```

### 5. Daily automation

Push to GitHub. Add two repository secrets in **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Your Supabase service role key |

The workflow (`.github/workflows/scrape.yml`) runs at **06:00 UTC every day**. It can also be triggered manually from the Actions tab with a choice of `daily` (default) or `backfill` mode and an optional league filter.

After each run the workflow auto-commits `config.json` back to `main` if the pending schedule changed. Commits are tagged `[skip ci]` to prevent re-triggering.

---

## `config.json` reference

```jsonc
{
  // Global attack type defaults keyed by category name
  "categories": {
    "Muži": "3B",
    "Ženy": "2B"
    // ...
  },
  "leagues": {
    "zl": {
      "display_name": "ŽL",        // used as the league value in the DB
      "full_league_name": "Žďárská liga",  // written to full_league_name column
      "start_year": 2012,           // earliest year to scrape in backfill
      // Highest year that is fully scraped. Daily mode checks years after this.
      // Null = fall back to start_year (triggers a mini-backfill on next daily run).
      "last_scraped_year": 2025,
      // Competitions known to be scheduled but not yet in the DB.
      // Entries are added during schedule refreshes and removed once
      // results are successfully uploaded.
      "pending_competitions": [
        { "date": "2026-05-30", "place": "Zbraslav" }
      ],
      // Absolute date of the next schedule refresh. Null forces a refresh on
      // next run. Set to today + randint(12, 16) days after each refresh so
      // checks are spread out rather than clustering on the same day.
      "next_schedule_check": "2026-05-19"
    },
    "plpu": {
      // Per-league category override — "auto" parses NxB/NB from the h3 heading.
      // A fixed value ("2B"/"3B") overrides the global default for that category.
      "categories": { "Muži": "auto" }
    }
  }
}
```

---

## Local debug workflow

To inspect a specific year without touching the DB:

```bash
# 1. Download the HTML
python downloader.py --debug zl 2025 2025
# → debug_output/zl/ZL.2025.html

# 2. Parse and write CSV
python scraper.py zl debug_output/zl -o debug_output/zl_2025.csv
```

The `--debug` flag is available on both scripts. The `debug_output/` folder is gitignored.
