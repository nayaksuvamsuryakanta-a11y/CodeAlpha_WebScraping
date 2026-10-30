# CodeAlpha_WebScraping

[Technical Documentation](docs/TECHNICAL_DOCUMENTATION.md)

A Python web-scraping project built as part of the **CodeAlpha Data Analytics Internship**.  
It scrapes the entirety of [books.toscrape.com](https://books.toscrape.com) — a publicly available, scraping-friendly sandbox — and produces a clean, analysis-ready CSV dataset of all 1 000 books.

---

## Table of Contents

- [Goal](#goal)
- [Site Scraped](#site-scraped)
- [Fields Extracted](#fields-extracted)
- [Project Structure](#project-structure)
- [Setup](#setup)
- [Usage & CLI Flags](#usage--cli-flags)
- [Output Sample](#output-sample)
- [Testing](#testing)
- [Design Decisions](#design-decisions)
- [Ethical Scraping Notes](#ethical-scraping-notes)

---

## Goal

Demonstrate a clean, production-quality web-scraping pipeline that:

1. Traverses paginated catalogue listings automatically.
2. Enriches each record by visiting individual book detail pages.
3. Handles errors, missing values, and malformed HTML without crashing.
4. Persists the dataset incrementally as a well-structured CSV, so a crash midway doesn't lose progress.
5. Validates the final dataset and reports data-quality issues.

---

## Site Scraped

| Property | Value |
|---|---|
| URL | <https://books.toscrape.com> |
| Type | Fictional online bookstore (scraping sandbox) |
| Pages | 50 catalogue pages × 20 books = **1 000 books** |
| robots.txt | Fully permissive — scraping explicitly allowed |
| Anti-bot measures | None |

---

## Fields Extracted

| Column | Type | Description |
|---|---|---|
| `title` | string | Full book title |
| `price_gbp` | float | Price in British Pounds (£) |
| `rating` | int (1–5) | Star rating parsed from CSS class word |
| `availability` | string | Stock status (`In stock` / `Out of stock`) |
| `category` | string | Genre / category from breadcrumb navigation |
| `upc` | string | Universal Product Code from product info table |
| `num_reviews` | int | Number of customer reviews |
| `detail_url` | string | Canonical URL of the book's detail page |

---

## Project Structure

```
CodeAlpha_WebScraping/
├── scraper.py            # Main scraping script
├── test_scraper.py       # Pytest unit tests (no live HTTP calls)
├── requirements.txt      # Python dependencies
├── README.md             # This file
├── books_dataset.csv     # Output dataset (generated on first run)
└── fixtures/
    ├── detail_page.html  # Sample book detail page (used by tests)
    ├── listing_page.html # Sample catalogue listing page (used by tests)
    └── last_page.html    # Sample last page with no "next" button (used by tests)
```

### Key functions in `scraper.py`

| Function | Responsibility |
|---|---|
| `fetch_page(url, session, cfg)` | HTTP GET with retry + linear back-off; returns `None` on failure |
| `parse_detail(soup, url)` | Extract UPC, category, review count from a book detail page |
| `parse_page(soup, session, cfg)` | Extract all books from one catalogue listing page |
| `get_next_page_url(soup, cfg)` | Resolve the "next" pagination link |
| `init_csv(filepath)` | Create output CSV with header row |
| `append_books(books, filepath)` | Append a batch of rows to an existing CSV |
| `save_data(books, filepath)` | Write a full CSV (header + all rows) — used in non-incremental mode |
| `load_scraped_upcs(filepath)` | Load existing UPCs for `--resume` deduplication |
| `validate_and_report(books)` | Post-scrape data-quality checks + summary log |
| `run_scraper(cfg)` | Orchestrator: pagination loop → incremental flush → validate |

---

## Setup

### Prerequisites

- Python 3.10 or newer
- pip

### 1 — Clone the repository

```bash
git clone https://github.com/<your-username>/CodeAlpha_WebScraping.git
cd CodeAlpha_WebScraping
```

### 2 — Create a virtual environment (recommended)

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
```

### 3 — Install dependencies

```bash
pip install -r requirements-dev.txt
python -m pytest
```

---

## Usage & CLI Flags

```
python scraper.py [OPTIONS]
```

| Flag | Short | Default | Description |
|---|---|---|---|
| `--output FILE` | `-o` | `books_dataset.csv` | Output CSV filename |
| `--limit N` | `-l` | *(all 50 pages)* | Stop after scraping N catalogue pages |
| `--delay SECS` | `-d` | `1.0` | Politeness delay between requests (seconds) |
| `--resume` | `-r` | off | Skip books whose UPC already exists in the output CSV |
| `--dry-run` | — | off | Fetch and parse one page only; do not save anything |

You can also override key constants using **environment variables**:

| Variable | Overrides |
|---|---|
| `SCRAPER_OUTPUT` | `--output` default |
| `SCRAPER_DELAY` | `--delay` default |
| `SCRAPER_TIMEOUT` | Request timeout in seconds |
| `SCRAPER_MAX_RETRIES` | Number of HTTP retry attempts |

### Examples

```bash
# Full scrape — all 50 pages → books_dataset.csv
python scraper.py

# Quick test — scrape 2 pages into a temp file
python scraper.py --limit 2 --output test_run.csv

# Fast sanity check — parse one page, print logs, don't save
python scraper.py --dry-run

# Resume an interrupted run — skip already-scraped UPCs
python scraper.py --resume --output books_dataset.csv

# Faster scrape (0.5 s delay) into a custom file
python scraper.py --delay 0.5 --output fast_run.csv
```

**Expected runtime:** ~20–30 minutes for the full 1 000-book run (1 s delay × 1 000 detail pages).

---

## Output Sample

```
title,price_gbp,rating,availability,category,upc,num_reviews,detail_url
A Light in the Attic,51.77,3,In stock,Poetry,a897fe39b1053632,0,https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html
Tipping the Velvet,53.74,1,In stock,Historical Fiction,90fa61229261140a,0,https://books.toscrape.com/catalogue/tipping-the-velvet_999/index.html
Soumission,50.10,1,In stock,Fiction,6957f44c3847a760,0,https://books.toscrape.com/catalogue/soumission_998/index.html
```

A sample 40-row file (`books_dataset.csv`) is committed to the repository.  
Delete it and re-run the scraper to generate the complete 1 000-row dataset.

---

## Testing

The test suite uses **pytest** with `unittest.mock` — no live HTTP calls are made.  
Fixtures in `fixtures/` provide representative HTML pages for parser tests.

### Run all tests

```bash
pytest test_scraper.py -v
```

### Run a specific test class

```bash
pytest test_scraper.py::TestParseDetail -v
pytest test_scraper.py::TestFetchPage -v
```

### What is tested

| Test class | Coverage |
|---|---|
| `TestRatingMap` | All 5 word→int mappings, missing/partial words, case folding |
| `TestParseDetail` | UPC, category, review count; missing table; bad review format |
| `TestGetNextPageUrl` | Returns URL when next button present; `None` on last page |
| `TestFetchPage` | Success path; retry count on 500; gives up after `max_retries`; succeeds on 2nd attempt; no sleep on last retry |
| `TestCsvHelpers` | `save_data`, `init_csv`, `append_books`; no duplicate headers after incremental appends |
| `TestLoadScrapedUpcs` | Missing file → empty set; normal load; skips `N/A` UPCs |
| `TestValidateAndReport` | Clean data → 0 issues; duplicate UPC; missing title; `None` price; zero/negative price |
| `TestConfig` | Default values; env-var overrides for delay, output, retries |
| `TestBuildArgParser` | Default args; `--output`, `-o`, `--limit`, `--delay`, `--resume`, `--dry-run` |

---

## Design Decisions

- **`Config` dataclass** — All constants live in one place; CLI args and env vars override it cleanly without touching function signatures.
- **`dataclass` for `Book`** — Typed field definitions; `astuple()` and `fields()` eliminate manual column mapping.
- **Incremental CSV flush** — Rows are appended every `flush_every` (default: 20) books so a crash midway loses at most one page of data.
- **`--resume` mode** — Reads existing UPCs from the output CSV and skips already-scraped books, enabling safe reruns after interruptions.
- **`--dry-run` flag** — Fetches and parses a single page for fast sanity checks without writing anything.
- **`requests.Session`** — Connection reuse reduces overhead across 1 000+ requests.
- **Retry with linear back-off** — `fetch_page` retries up to `max_retries` times with increasing delay so a single transient error doesn't abort the run.
- **Detail-page visits** — The listing pages lack category and UPC; these require a follow-up GET per book.
- **`html.parser`** — Stdlib parser; no binary dependencies. `lxml` can be added as a faster drop-in (`pip install lxml`) with no code changes.

---

## Ethical Scraping Notes

- `robots.txt` at <https://books.toscrape.com/robots.txt> is fully permissive.
- A 1-second `time.sleep` delay (configurable via `--delay`) is inserted between every request.
- A descriptive `User-Agent` string identifies this bot and links to a GitHub repo.
- The target site is a purpose-built scraping sandbox with no real commercial data.
