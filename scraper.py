"""
CodeAlpha Web Scraping Project
Target  : https://books.toscrape.com
Goal    : Scrape all book listings across all pages and save a clean CSV dataset.
Author  : CodeAlpha Internship Task
"""

import argparse
import csv
import logging
import os
import time
from dataclasses import dataclass, field, fields, astuple
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration  (all constants in one place — override via CLI or .env)
# ---------------------------------------------------------------------------
@dataclass
class Config:
    base_url: str       = "https://books.toscrape.com/catalogue/"
    start_url: str      = "https://books.toscrape.com/catalogue/page-1.html"
    output_file: str    = "books_dataset.csv"
    request_delay: float = 1.0    # seconds between requests (polite crawling)
    request_timeout: int = 15     # seconds before a request is considered failed
    max_retries: int    = 3       # retry count for transient HTTP errors
    flush_every: int    = 20      # append to CSV after every N books
    limit_pages: Optional[int] = None   # None = scrape all pages
    resume: bool        = False   # skip UPCs already present in output_file
    dry_run: bool       = False   # fetch/parse one page, no save

    # Read optional overrides from environment variables at init time
    def __post_init__(self) -> None:
        if v := os.getenv("SCRAPER_BASE_URL"):
            self.base_url = v
        if v := os.getenv("SCRAPER_OUTPUT"):
            self.output_file = v
        if v := os.getenv("SCRAPER_DELAY"):
            self.request_delay = float(v)
        if v := os.getenv("SCRAPER_TIMEOUT"):
            self.request_timeout = int(v)
        if v := os.getenv("SCRAPER_MAX_RETRIES"):
            self.max_retries = int(v)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Book:
    title: str
    price_gbp: Optional[float]
    rating: Optional[int]        # 1–5 (parsed from word like "Three")
    availability: str
    category: str
    upc: str
    num_reviews: Optional[int]
    detail_url: str


# Word-to-integer mapping used on the site for star ratings
RATING_MAP: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
}

# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
def fetch_page(
    url: str,
    session: requests.Session,
    cfg: Config,
) -> Optional[BeautifulSoup]:
    """
    Fetch a single URL and return a BeautifulSoup object.
    Retries up to cfg.max_retries times on network/HTTP errors with
    linear back-off.  Returns None if all retries fail so the caller
    can skip gracefully.
    """
    for attempt in range(1, cfg.max_retries + 1):
        try:
            response = session.get(url, timeout=cfg.request_timeout)
            response.raise_for_status()           # raise on 4xx / 5xx
            return BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as exc:
            log.warning(
                "Attempt %d/%d failed for %s — %s",
                attempt, cfg.max_retries, url, exc,
            )
            if attempt < cfg.max_retries:
                time.sleep(cfg.request_delay * attempt)   # back-off before retry
    log.error("Giving up on URL: %s", url)
    return None


# ---------------------------------------------------------------------------
# Detail-page parser  (UPC, category, num_reviews)
# ---------------------------------------------------------------------------
def parse_detail(soup: BeautifulSoup, detail_url: str) -> dict[str, object]:
    """
    Extract supplementary fields that only appear on the individual book page:
    UPC, category breadcrumb, and number of reviews.
    Returns a dict with keys: upc, category, num_reviews.
    """
    result: dict[str, object] = {
        "upc": "N/A",
        "category": "N/A",
        "num_reviews": None,
    }

    # ---- UPC and review count from the product information table ----
    try:
        table = soup.find("table", class_="table-striped")
        if table:
            rows = {
                tr.find("th").text.strip(): tr.find("td").text.strip()
                for tr in table.find_all("tr")
            }
            result["upc"] = rows.get("UPC", "N/A")
            num_str = rows.get("Number of reviews", "0").strip()
            result["num_reviews"] = int(num_str) if num_str.isdigit() else None
    except (AttributeError, ValueError) as exc:
        log.debug("Could not parse product table at %s: %s", detail_url, exc)

    # ---- Category from breadcrumb navigation ----
    try:
        breadcrumb = soup.find("ul", class_="breadcrumb")
        if breadcrumb:
            crumbs = breadcrumb.find_all("li")
            # Breadcrumb structure: Home > Category > Book Title
            if len(crumbs) >= 3:
                result["category"] = crumbs[-2].text.strip()
    except AttributeError as exc:
        log.debug("Could not parse breadcrumb at %s: %s", detail_url, exc)

    return result


# ---------------------------------------------------------------------------
# Listing-page parser
# ---------------------------------------------------------------------------
def parse_page(
    soup: BeautifulSoup,
    session: requests.Session,
    cfg: Config,
) -> list[Book]:
    """
    Parse one catalogue listing page.
    For each book article on the page, visit its detail page to collect
    the full set of fields, then return a list of Book dataclass instances.
    """
    books: list[Book] = []
    articles = soup.find_all("article", class_="product_pod")

    for article in articles:
        # ---- Title ----
        title_tag = article.find("h3")
        title: str = (
            title_tag.find("a")["title"].strip()
            if title_tag and title_tag.find("a")
            else "N/A"
        )

        # ---- Price ----
        price_tag = article.find("p", class_="price_color")
        price_text: str = price_tag.text.strip() if price_tag else ""
        price: Optional[float]
        try:
            # Strip the currency symbol (£ or Â£ due to encoding quirks)
            price = float("".join(c for c in price_text if c.isdigit() or c == "."))
        except ValueError:
            price = None

        # ---- Star rating (word form → integer) ----
        rating_tag = article.find("p", class_="star-rating")
        rating: Optional[int] = None
        if rating_tag:
            # The second CSS class encodes the rating word, e.g. "star-rating Three"
            for cls in rating_tag.get("class", []):
                rating = RATING_MAP.get(cls.lower())
                if rating is not None:
                    break

        # ---- Availability ----
        avail_tag = article.find("p", class_="instock")
        availability: str = avail_tag.text.strip() if avail_tag else "Out of stock"

        # ---- Build detail URL ----
        relative_href: str = (
            title_tag.find("a")["href"]
            if title_tag and title_tag.find("a")
            else ""
        )
        # href values on listing pages look like "../../../book-name/index.html"
        # Strip leading "../" segments to get the canonical catalogue path
        clean_href = relative_href.lstrip("./").lstrip("../").lstrip("./")
        if clean_href.startswith("catalogue/"):
            detail_url = "https://books.toscrape.com/" + clean_href
        else:
            detail_url = cfg.base_url + clean_href

        # ---- Fetch detail page for extra fields ----
        time.sleep(cfg.request_delay)
        detail_soup = fetch_page(detail_url, session, cfg)
        extra: dict[str, object]
        if detail_soup:
            extra = parse_detail(detail_soup, detail_url)
        else:
            extra = {"upc": "N/A", "category": "N/A", "num_reviews": None}

        books.append(Book(
            title=title,
            price_gbp=price,
            rating=rating,
            availability=availability,
            category=str(extra["category"]),
            upc=str(extra["upc"]),
            num_reviews=extra["num_reviews"],  # type: ignore[arg-type]
            detail_url=detail_url,
        ))

    return books


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------
def get_next_page_url(
    soup: BeautifulSoup,
    cfg: Config,
) -> Optional[str]:
    """
    Look for the 'next' button on a catalogue page and return its absolute URL.
    Returns None when there is no next page (i.e., last page reached).
    """
    next_btn = soup.find("li", class_="next")
    if not next_btn:
        return None
    next_href = next_btn.find("a")["href"]
    # next_href is a relative filename like "page-2.html"
    return cfg.base_url + next_href


# ---------------------------------------------------------------------------
# CSV writer — supports header-only init, incremental append, and full write
# ---------------------------------------------------------------------------
def _csv_headers() -> list[str]:
    return [f.name for f in fields(Book)]


def init_csv(filepath: str) -> None:
    """Write (or overwrite) the CSV with just a header row."""
    with open(filepath, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerow(_csv_headers())


def append_books(books: list[Book], filepath: str) -> None:
    """Append a batch of Book rows to an existing CSV (no header)."""
    with open(filepath, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        for book in books:
            writer.writerow(astuple(book))


def save_data(books: list[Book], filepath: str) -> None:
    """
    Write the entire list of Book records to a CSV file (header + rows).
    Used when incremental saving is not in effect.
    """
    if not books:
        log.warning("No data to save.")
        return
    with open(filepath, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_csv_headers())
        for book in books:
            writer.writerow(astuple(book))
    log.info("Dataset saved → %s  (%d rows)", filepath, len(books))


# ---------------------------------------------------------------------------
# Resume helper — load already-scraped UPCs from an existing CSV
# ---------------------------------------------------------------------------
def load_scraped_upcs(filepath: str) -> set[str]:
    """
    Return the set of UPCs already present in *filepath*.
    Returns an empty set if the file does not exist or has no data rows.
    """
    path = Path(filepath)
    if not path.exists():
        return set()
    upcs: set[str] = set()
    with open(filepath, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            upc = row.get("upc", "").strip()
            if upc and upc != "N/A":
                upcs.add(upc)
    log.info("Resume mode: %d already-scraped UPCs loaded from %s", len(upcs), filepath)
    return upcs


# ---------------------------------------------------------------------------
# Post-scrape validation & summary report
# ---------------------------------------------------------------------------
def validate_and_report(books: list[Book]) -> None:
    """
    Inspect the collected dataset and log a structured summary report covering:
    - duplicate UPCs
    - missing/null critical fields (title, price_gbp)
    - non-positive prices
    Logs a one-line success/skip summary at the end.
    """
    total = len(books)
    issues = 0

    # --- Duplicate UPCs ---
    seen_upcs: dict[str, int] = {}
    for b in books:
        if b.upc and b.upc != "N/A":
            seen_upcs[b.upc] = seen_upcs.get(b.upc, 0) + 1
    dupe_upcs = {upc: cnt for upc, cnt in seen_upcs.items() if cnt > 1}
    if dupe_upcs:
        log.warning(
            "Validation — %d duplicate UPC(s) found: %s",
            len(dupe_upcs),
            ", ".join(f"{u}×{c}" for u, c in dupe_upcs.items()),
        )
        issues += sum(c - 1 for c in dupe_upcs.values())

    # --- Missing critical fields ---
    missing_title  = [b for b in books if not b.title or b.title == "N/A"]
    missing_price  = [b for b in books if b.price_gbp is None]
    if missing_title:
        log.warning("Validation — %d book(s) with missing title.", len(missing_title))
        issues += len(missing_title)
    if missing_price:
        log.warning("Validation — %d book(s) with missing price.", len(missing_price))
        issues += len(missing_price)

    # --- Non-positive prices ---
    bad_price = [b for b in books if b.price_gbp is not None and b.price_gbp <= 0]
    if bad_price:
        log.warning(
            "Validation — %d book(s) with non-positive price: %s",
            len(bad_price),
            [b.title for b in bad_price],
        )
        issues += len(bad_price)

    ok = total - issues
    if issues == 0:
        log.info("Validation report — %d/%d books scraped successfully, 0 issues.", total, total)
    else:
        log.warning(
            "Validation report — %d/%d books scraped successfully, %d flagged with issues.",
            ok, total, issues,
        )


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scraper",
        description="Scrape books.toscrape.com into a clean CSV dataset.",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        metavar="FILE",
        help="Output CSV filename (default: books_dataset.csv)",
    )
    parser.add_argument(
        "--limit", "-l",
        type=int,
        default=None,
        metavar="N",
        help="Stop after scraping N catalogue pages (omit for all 50).",
    )
    parser.add_argument(
        "--delay", "-d",
        type=float,
        default=None,
        metavar="SECS",
        help="Politeness delay between requests in seconds (default: 1.0).",
    )
    parser.add_argument(
        "--resume", "-r",
        action="store_true",
        help="Skip books whose UPC already exists in the output CSV.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse one catalogue page, log results, do not save.",
    )
    return parser


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def run_scraper(cfg: Optional[Config] = None) -> list[Book]:
    """
    Main entry point.  Iterates over catalogue pages, collects every
    book record, and saves the combined result.

    Accepts an optional Config so callers (tests, scripts) can inject
    settings without touching module-level constants.

    Returns the list of all collected Book objects.
    """
    if cfg is None:
        cfg = Config()

    # ---- Resume: load already-scraped UPCs ----
    scraped_upcs: set[str] = set()
    if cfg.resume:
        scraped_upcs = load_scraped_upcs(cfg.output_file)

    # ---- Incremental save: initialise CSV with header if starting fresh ----
    if not cfg.dry_run and not cfg.resume:
        init_csv(cfg.output_file)
    elif not cfg.dry_run and cfg.resume and not Path(cfg.output_file).exists():
        init_csv(cfg.output_file)

    all_books: list[Book] = []
    pending_flush: list[Book] = []   # buffer flushed every cfg.flush_every rows
    page_num = 1
    current_url: Optional[str] = cfg.start_url

    # Persistent session for connection reuse and consistent headers
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (compatible; CodeAlphaBot/1.0; "
            "+https://github.com/codealpha/webscraping)"
        )
    })

    log.info("Starting scrape — target: https://books.toscrape.com")
    if cfg.dry_run:
        log.info("DRY RUN — will fetch one page only, no output written.")
    if cfg.limit_pages:
        log.info("Page limit: %d", cfg.limit_pages)

    while current_url:
        log.info("Scraping page %d → %s", page_num, current_url)

        soup = fetch_page(current_url, session, cfg)
        if soup is None:
            log.error("Failed to fetch page %d. Stopping early.", page_num)
            break

        page_books = parse_page(soup, session, cfg)

        # ---- Resume: filter out already-scraped books ----
        if cfg.resume and scraped_upcs:
            before = len(page_books)
            page_books = [b for b in page_books if b.upc not in scraped_upcs]
            skipped = before - len(page_books)
            if skipped:
                log.info("  Skipped %d already-scraped book(s) on page %d.", skipped, page_num)

        all_books.extend(page_books)
        pending_flush.extend(page_books)

        log.info(
            "  Page %d: %d books collected  (total so far: %d)",
            page_num, len(page_books), len(all_books),
        )

        # ---- Incremental flush ----
        if not cfg.dry_run and len(pending_flush) >= cfg.flush_every:
            append_books(pending_flush, cfg.output_file)
            log.info("  Flushed %d rows to %s", len(pending_flush), cfg.output_file)
            pending_flush.clear()

        # ---- Dry-run: stop after first page ----
        if cfg.dry_run:
            log.info("Dry run complete — %d books parsed, nothing saved.", len(all_books))
            break

        current_url = get_next_page_url(soup, cfg)
        page_num += 1

        # ---- Page limit ----
        if cfg.limit_pages is not None and page_num > cfg.limit_pages:
            log.info("Page limit of %d reached. Stopping.", cfg.limit_pages)
            break

        # Polite delay between catalogue pages
        if current_url:
            time.sleep(cfg.request_delay)

    # ---- Flush any remaining buffered rows ----
    if not cfg.dry_run and pending_flush:
        append_books(pending_flush, cfg.output_file)
        log.info("Final flush: %d rows written to %s", len(pending_flush), cfg.output_file)

    log.info("Scraping complete. Total books collected: %d", len(all_books))

    # ---- Post-scrape validation ----
    if not cfg.dry_run and all_books:
        validate_and_report(all_books)

    return all_books


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    cfg = Config()
    if args.output:
        cfg.output_file = args.output
    if args.limit is not None:
        cfg.limit_pages = args.limit
    if args.delay is not None:
        cfg.request_delay = args.delay
    if args.resume:
        cfg.resume = True
    if args.dry_run:
        cfg.dry_run = True

    run_scraper(cfg)


if __name__ == "__main__":
    main()
