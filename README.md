# firesport-timestamps-scraper

Scrapes competition results from [firesport.eu](https://firesport.eu) for Czech firesport leagues and stores them in a Supabase database. A GitHub Actions workflow runs the pipeline daily and commits any changes back to the repo automatically.

---

## What it does

The scraper uses a two-step pipeline:

1. **Download global competition list** from `https://www.firesport.eu/vysledky-souteze-{year}` — a summary table for the entire year listing all competitions, their dates, places, districts, and affiliated leagues.

2. **For each competition, download and parse the individual result page** from `https://www.firesport.eu/vysledek-*.html` — extract results table(s) organized by category and upload to Supabase.

Results are stored in a `public.timestamps` table. Link-based deduplication (`link` column) prevents re-scraping the same competition page, so the pipeline is safe to run multiple times.

---

## Supported leagues

~50 Czech firesport leagues are supported. A complete list is defined in `config.json` under the `"leagues"` key. Each league has:

- A **display name** (e.g., `ŽL` for Žďárská liga)
- A **full human-readable name** (e.g., Žďárská liga)
- A **start year** for backfill operations
- Optional **per-league category overrides** (to handle league-specific attack type variations)

---

## Repository structure

```
orchestrator.py          # main pipeline — daily and backfill modes
downloader.py            # HTTP fetcher for global lists and individual pages
scraper.py               # HTML parser for lists and individual pages
db.py                    # Supabase client wrapper
config.json              # leagues, categories, district abbreviations (committed)
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

Downloads HTML pages from firesport.eu.

**Used by orchestrator**:

```python
# Download global list
html = downloader.download_global_list(year=2025)

# Download individual competition page
html = downloader.download_competition_page(link='vysledek-marsovice-14838.html')
```

**Standalone CLI** for debugging:

```bash
# Download global list to debug_output/global_lists/
python downloader.py --global-list 2025 --debug

# Download individual competition to debug_output/competitions/
python downloader.py --competition vysledek-marsovice-14838.html --debug
```

---

### `scraper.py`

Parses raw HTML into structured result dicts.

**Used by orchestrator**:

```python
# Parse global competition list
competitions = scraper.parse_global_list(html)
# → [{"date": "2025-01-11", "place": "Dolní Hradiště", "link": "vysledek-...", 
#      "district": "Plzeň-sever", "league": "FSEU"}, ...]

# Parse individual competition page
rows = scraper.scrape_individual_page(
    html, meta, global_categories, league_categories, district_map,
    source_name='vysledek-marsovice-14838.html'
)
# → [{"attack_date": "2025-05-10", "place": "Maršovice /Žďár nad Sázavou", 
#     "team": "Trnava/TR", "lp": "16.45", "pp": "16.45", "link": "vysledek-...", ...}, ...]
```

#### Attack type resolution

Each result row needs an `attack_type` value (`2B` or `3B`). The scraper resolves this in three tiers:

1. **Per-league override** in `config.json` — can be a fixed value (`"2B"` / `"3B"`) or `"auto"`, which parses the `NxB` pattern from the competition's h3 heading.
2. **Global default** from the top-level `categories` map in `config.json`. All entries default to `"auto"`.
3. **Interactive prompt** (debug CLI) or a warning + row skip (automated mode).

#### `only_final_time` flag

Some results list only the final time without individual lp/pp splits. When detected, `lp` and `pp` are set to the final time and `only_final_time` is set to `true`.

#### District mapping

The global list provides full Czech district names (e.g., "Žďár nad Sázavou"). The `scraper.extract_team()` function maps these to short SPZ codes (e.g., "ZR") using the `district_abbreviations` map in `config.json`. Team records in the DB are formatted as `"TeamName/SPZ"` (e.g., `"Trnava/TR"`).

---

### `db.py`

Wrapper around the Supabase Python client.

| Function | What it does |
|---|---|
| `init_client()` | Loads `.env`, validates credentials, returns a `Client` |
| `get_scraped_links(client, year)` | Returns `{link, ...}` already in the DB for competition-level dedup |
| `get_category_attack_types(client, league)` | Returns `{category: {attack_type, ...}, ...}` for conflict detection in backfill |
| `upload_records(client, records)` | Batch-inserts rows (500 at a time); converts empty lp/pp to `None` |

---

### `orchestrator.py`

Top-level coordinator. Calls downloader → scraper → db in sequence.

#### Daily mode (default)

```bash
python orchestrator.py
python orchestrator.py --league zl
```

For the current year only:

1. Download the global competition list
2. Query the DB for already-scraped competitions (via `link` column)
3. Filter to competitions not yet scraped and with date ≤ today (future competition results are not published yet)
4. For each new competition:
   - Download the individual page
   - Parse results using `scrape_individual_page()`
   - Upload all rows (competition-level link dedup in step 2 ensures no double-processing)

If `--league KEY` is specified, only competitions for that league are processed (FSEU unlabeled competitions are excluded in per-league runs).

#### Backfill mode

```bash
python orchestrator.py --backfill               # all leagues, all years
python orchestrator.py --backfill --league zl   # one league
python orchestrator.py --backfill --year 2024   # one year
python orchestrator.py --backfill --league zl --year 2024
```

Processes the specified year range and league(s), uploading any rows not already in the DB.

**Year range**:
- `--year YEAR` specified: only that year
- Otherwise: from `min(league start_years)` to current year

**League filtering**:
- `--league KEY` specified: only that league (FSEU excluded)
- Otherwise: all leagues including FSEU

**Conflict detection**: Before uploading, the backfill checks if any category resolves to two different attack types within or across years (e.g., `Muži` is `3B` in 2022 but `2B` in 2023). On conflict:

1. Prints a detailed conflict report
2. Deletes **all existing rows for that league** from the DB
3. Marks the league as failed and continues with other leagues

To resolve: inspect the flagged HTML, decide whether the difference is intentional (add a per-league `categories` override), then re-run.

---

## Database schema

Table: `public.timestamps`

| Column           | Type      | Notes |
|------------------|-----------|-------|
| `attack_date`    | `text`    | YYYY-MM-DD |
| `league`         | `text`    | Display name (ŽL, EXČR, …) or NULL for FSEU |
| `full_league_name` | `text`  | Full name (Žďárská liga, …) or NULL for FSEU |
| `place`          | `text`    | "PlaceName /DistrictName" (e.g., "Maršovice /Žďár nad Sázavou") |
| `placement`      | `text`    | Final ranking position |
| `attack_type`    | `text`    | `2B` or `3B` |
| `category`       | `text`    | e.g. Muži, Ženy, Junioři |
| `team`           | `text`    | "TeamName/SPZ" (e.g., "Trnava/TR") — SPZ code from district_abbreviations |
| `lp`             | `real`    | Nullable — first individual attempt (seconds) |
| `pp`             | `real`    | Nullable — second individual attempt (seconds) |
| `only_final_time`| `boolean` | `true` when lp/pp are duplicated final time |
| `link`           | `text`    | Nullable — relative URL of competition page (e.g., "vysledek-marsovice-14838.html") |

**Deduplication key**:
- Competition-level: `link` for a given year — prevents re-scraping the same page

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
    only_final_time   boolean DEFAULT false,
    link              text
);

-- Grant access for Data API
grant select, insert, update, delete on public.timestamps to anon;
grant select, insert, update, delete on public.timestamps to authenticated;
grant select, insert, update, delete on public.timestamps to service_role;

-- Enable Row Level Security
alter table public.timestamps enable row level security;
```

### 4. (Migration) Clear old data

If migrating from the old per-league-year scraper, **clear the `timestamps` table first**:

```sql
DELETE FROM public.timestamps;
```

The new scraper uses a different `place` format (now includes district), so mixing old and new row formats will cause deduplication issues.

### 5. Backfill historical data

```bash
python orchestrator.py --backfill
```

This will take a while on the first run (processing all years back to each league's `start_year`). Progress is printed per competition.

### 6. Daily automation

Push to GitHub. Add two repository secrets in **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Your Supabase service role key |

The workflow (`.github/workflows/scrape.yml`) runs at **06:00 UTC every day**. It can also be triggered manually from the Actions tab.

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

  // District name → SPZ code mapping (e.g., "Žďár nad Sázavou" → "ZR")
  // Used by scraper.extract_team() to convert full district names to short codes
  "district_abbreviations": {
    "Benešov": "BE",
    "Blansko": "BK",
    // ... 59 total districts
  },

  // Category display aliases (e.g., some competitions label the category differently)
  "category_aliases": {
    // "Raw category text from HTML": "Standardized name"
  },

  // First words that form compound category names (e.g., "Smíšený dorost")
  "compound_category_prefixes": ["Smíšený", ...],

  "leagues": {
    "zl": {
      "display_name": "ŽL",                // used as the league value in the DB
      "full_league_name": "Žďárská liga",  // written to full_league_name column
      "start_year": 2012,                   // earliest year to scrape in backfill

      // Optional per-league category override for attack types
      // "auto" parses NxB from h3 heading; fixed "2B"/"3B" overrides global default
      "categories": {
        "Muži": "3B"
      },

      // Optional: use {league}.cz domain instead of {league}.firesport.eu
      // "standalone_domain": true
    },
    // ... ~50 more leagues
  }
}
```

Removed fields (from old scraper):
- `pending_competitions` — schedule tracking no longer needed
- `next_schedule_check` — schedule tracking no longer needed
- `last_scraped_year` — not used by the new global-list approach

---

## Local debug workflow

To inspect a specific competition without touching the DB:

```bash
# 1. Download global list for 2025
python downloader.py --global-list 2025 --debug
# → debug_output/global_lists/vysledky-souteze-2025.html

# 2. Parse to find competition link
python3 << 'EOF'
from scraper import parse_global_list
with open('debug_output/global_lists/vysledky-souteze-2025.html') as f:
    comps = parse_global_list(f.read())
for c in comps[:5]:
    print(f"{c['date']} | {c['place']} | {c['link']}")
EOF

# 3. Download the individual competition
python downloader.py --competition vysledek-marsovice-14838.html --debug
# → debug_output/competitions/vysledek-marsovice-14838.html

# 4. Inspect results (or parse with scraper.scrape_individual_page() for full processing)
```
