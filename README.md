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

1. **Per-league override** in `config.json` — can be a fixed value or `"auto"`, which parses the `NxB` pattern from the competition's h3 heading. Useful for leagues where attack type varies by competition (e.g. PLPU Muži).
2. **Global default** from the top-level `categories` map in `config.json`.
3. **Interactive prompt** (when running with `interactive=True`, i.e. the debug CLI).

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

For each active league, checks the current year and the previous year:

- **Has overdue pending competitions** (date ≤ today, not yet in DB)? → Download and scrape the year page. Upload any new rows found. Remove resolved competitions from `pending_competitions`. If the page has no results yet (data not published), leave the entry in pending and retry on the next run.
- **All pending competitions are in the future?** → Skip (nothing to scrape yet).
- **No pending competitions at all?** → Refresh the schedule from the website, but only if `last_schedule_check` is older than 14 days (avoids hammering the site every run). New competitions found are added to `pending_competitions`.

After each run, `config.json` is saved if anything changed (pending list or schedule check date). The GitHub Actions workflow commits this file back to the repo.

#### Backfill mode

```bash
python orchestrator.py --backfill            # all leagues, all years
python orchestrator.py --backfill --league zl  # one league
```

Backfill reads the league definitions from `config.json` and processes the specified league(s) from that file.
Downloads every year from `start_year` to the current year, diffs against the DB, and uploads only new rows. Safe to re-run — it never creates duplicates.

---

## Database schema

Table: `public.timestamps`

| Column           | Type      | Notes |
|------------------|-----------|-------|
| `attack_date`    | `text`    | YYYY-MM-DD |
| `league`         | `text`    | Display name (ŽL, EXČR, …) |
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
    place             text,
    placement         text,
    attack_type       text,
    category          text,
    team              text,
    lp                real,
    pp                real,
    only_final_time   boolean DEFAULT false
);
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
      "display_name": "ŽL",   // used as the league value in the DB
      "start_year": 2012,      // earliest year to scrape in backfill
      "active": true,          // false = skip entirely
      // Competitions known to be scheduled but not yet in the DB.
      // Entries are added during schedule refreshes and removed once
      // results are successfully uploaded.
      "pending_competitions": [
        { "date": "2026-05-30", "place": "Zbraslav" }
      ],
      // ISO date of last schedule refresh. Null forces a refresh on next run.
      "last_schedule_check": "2026-04-19"
    },
    "plpu": {
      // Per-league category override — "auto" parses NxB from the h3 heading
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
