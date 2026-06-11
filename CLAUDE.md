# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A scraper that pulls Czech firesport competition results from firesport.eu and writes them to a Supabase `public.timestamps` table. A daily GitHub Actions cron runs the pipeline. There is no build step, no linter, and no test suite.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill SUPABASE_URL and SUPABASE_KEY

# Run the pipeline (orchestrator is the ONLY entry point that touches the DB)
python orchestrator.py                            # daily: current year, new competitions only
python orchestrator.py --league zl                # daily, single league
python orchestrator.py --backfill                 # full history, all leagues
python orchestrator.py --backfill --league zl     # one league
python orchestrator.py --backfill --year 2024     # one year (only valid with --backfill)

# Debug downloads to debug_output/ (no DB, no parsing)
python downloader.py --global-list 2025 --debug
python downloader.py --competition vysledek-marsovice-14838.html --debug
```

To test parsing logic without the DB, download a page with `downloader.py --debug` and call `scraper.parse_global_list()` / `scraper.scrape_individual_page()` on the saved HTML from a Python REPL.

## Architecture

Four pipeline modules in a strict one-directional flow (plus a shared `colors.py` helper). **`orchestrator.py` is the only stateful/coordinating layer**; the other three are intended to stay independently callable:

- `downloader.py` — HTTP only. Two functions: `download_global_list(year)`, `download_competition_page(link)`. Has a debug CLI.
- `scraper.py` — pure HTML→dict parsing, no I/O except config and stderr warnings. No CLI / no `main()`.
- `db.py` — Supabase client wrapper. All reads paginate at `_PAGE_SIZE` (500); inserts batch at the same size.
- `orchestrator.py` — wires downloader → scraper → db, owns both run modes.
- `colors.py` — `cprint(text, color, stream=…)` ANSI helper used by all of the above. Color is emitted only when the target stream is a TTY (auto-detected), unless overridden by `FORCE_COLOR` / disabled by `NO_COLOR`, so redirected output stays plain. Convention: green = uploaded rows, red = errors, orange = no-NxB-pattern warning, yellow = other warnings, white = excluded-keyword section skip, magenta = backfill conflict/delete, blue = progress/info.

**Two-step scrape:** (1) download one *global list* page per year (`vysledky-souteze-{year}`) → `parse_global_list()` yields one dict per competition (date, place, link, district, league). (2) For each competition, download its individual page (`vysledek-*.html`) → `scrape_individual_page()` yields one dict per result row.

**Deduplication is link-based only.** `db.get_scraped_links(year)` returns links already in the DB; competitions whose `link` is present are skipped entirely. There is no row-level dedup in the live path. Re-running is therefore safe but only at competition granularity — a partially-uploaded competition is treated as fully done.

**FSEU** is the sentinel for unlabeled competitions: stored with `league = NULL`, excluded from `--league` runs, and exempt from conflict detection.

### Attack-type resolution — the core domain logic

Every result row needs an `attack_type`. This is the most intricate part of the codebase and is split between `config.json` and `scraper.resolve_attack_type()`. **Read that function's docstring before touching anything attack-type-related.** Three-tier lookup (per-league `categories` override → top-level `categories` default → skip (warn)), where each config value is one of four *modes*:

- `"2B"` / `"3B"` / `"Ostatní"` (any fixed string) — used as-is.
- `"auto"` — parse `NxB` from the competition's h3 heading; silently fall back to `"Ostatní"`. **Exempt from backfill conflict detection.**
- `"testing"` — same parsing as `auto`, but **warns** on fallback and **is subject to conflict detection**. This is the discovery default for a newly-added league.
- `"ignore"` — blacklist the category; rows skipped silently.

**League onboarding workflow:** start a new league's varying categories as `"testing"`, run a backfill, resolve any reported conflicts, then promote to `"auto"` or a fixed value once the variation is understood.

**Category filtering (applied per h3 heading, before resolution):** the top-level `excluded_keywords` list holds case-insensitive substrings; if any appears in a category heading, that whole section is skipped with a warning (drops unwanted disciplines — Plamen, štafeta, 60m, CTIF, …). Only the matched section is dropped, not the page. The top-level `other_categories` list holds category names (matched case-insensitively) that are renamed to `"Ostatní"` before resolution, collapsing miscellaneous categories under one value (and thus `attack_type = "Ostatní"`).

**Backfill conflict detection** (`_check_attack_type_conflicts`): if a single category resolves to two different attack types across the league's history (e.g. `Muži` is `3B` one year, `2B` another), the backfill prints a report, **deletes ALL of that league's rows from the DB** (`delete_league_records`), marks the league failed, and continues. Fix the config override, then re-run that league. `"auto"` categories and the `"Ostatní"` type are excluded from this check.

### config.json

Committed to the repo. Holds `leagues` (~50, each with display name, full name, `start_year`, optional `categories` override, and optional `aliases` list for alternative names that appear in competition data), the global `categories` map, `district_abbreviations` (full district name → SPZ code, e.g. "Žďár nad Sázavou" → "ZR"), and the two category-filtering lists `excluded_keywords` and `other_categories` (see attack-type section above). Multi-word category names (e.g. "Smíšený dorost") are matched automatically — the scraper tries the longest prefix of the h3 heading that matches a configured category name.

### Parsing quirks worth knowing (in `scraper.py`)

- Times: comma→dot normalized; `NP`/`DSQ`/`MS`/`-`/`99.99`/non-numeric all become empty.
- Rows whose `lp`, `pp`, or final time fall outside the plausible `[12, 120]` second range are dropped as implausible (`_MIN_TIME` / `_MAX_TIME` in `scraper.py`).
- `only_final_time`: when individual splits are absent but a final time exists, both `lp` and `pp` are set to the final time and this flag is set true.
- `team` is formatted `"Name Suffix/SPZ"`; `place` is `"Place/SPZ"` — both via the `district_abbreviations` map.
