"""
test_scraper.py
Unit tests for the CodeAlpha web scraper.

All tests use unittest.mock to intercept HTTP calls — the live site is
never contacted during the test suite.
"""

import csv
import io
import textwrap
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch, call

import pytest
import requests
from bs4 import BeautifulSoup

import scraper
from scraper import (
    Book,
    Config,
    RATING_MAP,
    append_books,
    fetch_page,
    get_next_page_url,
    init_csv,
    load_scraped_upcs,
    parse_detail,
    save_data,
    validate_and_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
FIXTURES = Path(__file__).parent / "fixtures"


def _soup(filename: str) -> BeautifulSoup:
    """Load a fixture HTML file and return a BeautifulSoup object."""
    return BeautifulSoup(
        (FIXTURES / filename).read_text(encoding="utf-8"),
        "html.parser",
    )


def _session() -> requests.Session:
    """Return a real Session (HTTP calls must be mocked by the caller)."""
    return requests.Session()


def _cfg(**kwargs) -> Config:
    """Return a Config with safe defaults for tests (no delays, no retries)."""
    cfg = Config(
        request_delay=0.0,
        max_retries=1,
        request_timeout=5,
    )
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


# ---------------------------------------------------------------------------
# 1. RATING_MAP
# ---------------------------------------------------------------------------
class TestRatingMap:
    def test_all_words_map_correctly(self) -> None:
        assert RATING_MAP["one"]   == 1
        assert RATING_MAP["two"]   == 2
        assert RATING_MAP["three"] == 3
        assert RATING_MAP["four"]  == 4
        assert RATING_MAP["five"]  == 5

    def test_missing_word_returns_none(self) -> None:
        assert RATING_MAP.get("six") is None
        assert RATING_MAP.get("")    is None
        assert RATING_MAP.get("Zero") is None

    def test_case_insensitive_lookup_via_lower(self) -> None:
        # The scraper calls cls.lower() before RATING_MAP lookup
        assert RATING_MAP.get("Three".lower()) == 3
        assert RATING_MAP.get("FIVE".lower())  == 5

    def test_partial_match_not_found(self) -> None:
        # "three" is in the map but "threee" must not be
        assert RATING_MAP.get("threee") is None


# ---------------------------------------------------------------------------
# 2. parse_detail  (against fixture HTML)
# ---------------------------------------------------------------------------
class TestParseDetail:
    def setup_method(self) -> None:
        self.soup = _soup("detail_page.html")
        self.url = "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"

    def test_upc_extracted(self) -> None:
        result = parse_detail(self.soup, self.url)
        assert result["upc"] == "a897fe39b1053632"

    def test_category_extracted(self) -> None:
        result = parse_detail(self.soup, self.url)
        assert result["category"] == "Poetry"

    def test_num_reviews_extracted(self) -> None:
        result = parse_detail(self.soup, self.url)
        assert result["num_reviews"] == 0

    def test_all_fields_present(self) -> None:
        result = parse_detail(self.soup, self.url)
        assert set(result.keys()) == {"upc", "category", "num_reviews"}

    def test_missing_table_returns_defaults(self) -> None:
        """If the product table is absent the function returns safe defaults."""
        empty_soup = BeautifulSoup("<html><body></body></html>", "html.parser")
        result = parse_detail(empty_soup, self.url)
        assert result["upc"]         == "N/A"
        assert result["category"]    == "N/A"
        assert result["num_reviews"] is None

    def test_missing_breadcrumb_category_defaults(self) -> None:
        """Table present but no breadcrumb — category should fall back to N/A."""
        html = textwrap.dedent("""
            <html><body>
            <table class="table-striped">
              <tr><th>UPC</th><td>abc123</td></tr>
              <tr><th>Number of reviews</th><td>5</td></tr>
            </table>
            </body></html>
        """)
        soup = BeautifulSoup(html, "html.parser")
        result = parse_detail(soup, self.url)
        assert result["upc"]         == "abc123"
        assert result["num_reviews"] == 5
        assert result["category"]    == "N/A"

    def test_non_digit_review_count_returns_none(self) -> None:
        html = textwrap.dedent("""
            <html><body>
            <table class="table-striped">
              <tr><th>UPC</th><td>xyz</td></tr>
              <tr><th>Number of reviews</th><td>N/A</td></tr>
            </table>
            </body></html>
        """)
        soup = BeautifulSoup(html, "html.parser")
        result = parse_detail(soup, "http://example.com")
        assert result["num_reviews"] is None


# ---------------------------------------------------------------------------
# 3. get_next_page_url
# ---------------------------------------------------------------------------
class TestGetNextPageUrl:
    def test_returns_next_url_when_button_present(self) -> None:
        soup = _soup("listing_page.html")
        cfg = _cfg()
        url = get_next_page_url(soup, cfg)
        assert url == cfg.base_url + "page-2.html"

    def test_returns_none_on_last_page(self) -> None:
        soup = _soup("last_page.html")
        cfg = _cfg()
        assert get_next_page_url(soup, cfg) is None


# ---------------------------------------------------------------------------
# 4. fetch_page — retry behaviour
# ---------------------------------------------------------------------------
class TestFetchPage:
    """
    fetch_page must retry on transient errors and return None after
    exhausting cfg.max_retries.
    """

    def _make_response(self, status: int = 200, text: str = "<html></html>") -> MagicMock:
        r = MagicMock()
        r.status_code = status
        r.text = text
        if status >= 400:
            r.raise_for_status.side_effect = requests.HTTPError(
                response=r, request=MagicMock()
            )
        else:
            r.raise_for_status.return_value = None
        return r

    def test_returns_soup_on_success(self) -> None:
        session = _session()
        cfg = _cfg()
        with patch.object(session, "get", return_value=self._make_response(200, "<html><p>ok</p></html>")):
            result = fetch_page("http://example.com", session, cfg)
        assert result is not None
        assert result.find("p").text == "ok"

    def test_retries_exactly_max_retries_times_on_500(self) -> None:
        session = _session()
        cfg = _cfg(max_retries=3, request_delay=0.0)
        with patch.object(session, "get", return_value=self._make_response(500)) as mock_get:
            result = fetch_page("http://example.com", session, cfg)
        assert result is None
        assert mock_get.call_count == 3

    def test_returns_none_after_all_retries_exhausted(self) -> None:
        session = _session()
        cfg = _cfg(max_retries=2, request_delay=0.0)
        with patch.object(
            session, "get",
            side_effect=requests.ConnectionError("timeout"),
        ) as mock_get:
            result = fetch_page("http://example.com", session, cfg)
        assert result is None
        assert mock_get.call_count == 2

    def test_succeeds_on_second_attempt_after_one_failure(self) -> None:
        session = _session()
        cfg = _cfg(max_retries=3, request_delay=0.0)
        good_response = self._make_response(200, "<html><body>ok</body></html>")
        bad_response  = self._make_response(503)
        with patch.object(
            session, "get",
            side_effect=[bad_response, good_response],
        ):
            result = fetch_page("http://example.com", session, cfg)
        assert result is not None
        assert result.find("body").text.strip() == "ok"

    def test_no_sleep_on_last_retry(self) -> None:
        """Back-off sleep must NOT fire on the final failed attempt."""
        session = _session()
        cfg = _cfg(max_retries=2, request_delay=0.5)
        with patch.object(session, "get", side_effect=requests.ConnectionError()):
            with patch("scraper.time.sleep") as mock_sleep:
                fetch_page("http://example.com", session, cfg)
        # With max_retries=2: sleep after attempt 1, NOT after attempt 2
        assert mock_sleep.call_count == 1


# ---------------------------------------------------------------------------
# 5. save_data / init_csv / append_books
# ---------------------------------------------------------------------------
class TestCsvHelpers:
    def test_save_data_writes_correct_headers(self, tmp_path: Path) -> None:
        fp = str(tmp_path / "out.csv")
        books = [
            Book("Title A", 9.99, 4, "In stock", "Fiction", "upc001", 0,
                 "http://example.com/a"),
        ]
        save_data(books, fp)
        with open(fp, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        assert rows[0]["title"]     == "Title A"
        assert rows[0]["price_gbp"] == "9.99"
        assert rows[0]["rating"]    == "4"
        assert rows[0]["upc"]       == "upc001"

    def test_save_data_empty_list_logs_warning(self, tmp_path: Path, caplog) -> None:
        import logging
        fp = str(tmp_path / "empty.csv")
        with caplog.at_level(logging.WARNING, logger="scraper"):
            save_data([], fp)
        assert "No data to save" in caplog.text

    def test_init_csv_creates_header_only_file(self, tmp_path: Path) -> None:
        fp = str(tmp_path / "hdr.csv")
        init_csv(fp)
        with open(fp, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        assert len(rows) == 1
        assert "title" in rows[0]
        assert "upc"   in rows[0]

    def test_append_books_adds_rows_without_header(self, tmp_path: Path) -> None:
        fp = str(tmp_path / "app.csv")
        init_csv(fp)
        books = [
            Book("Book X", 5.00, 2, "In stock", "Crime", "upc999", 1,
                 "http://example.com/x"),
        ]
        append_books(books, fp)
        with open(fp, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        # row 0 = header, row 1 = data
        assert len(rows) == 2
        assert rows[1][0] == "Book X"

    def test_incremental_append_produces_no_duplicate_header(self, tmp_path: Path) -> None:
        fp = str(tmp_path / "inc.csv")
        init_csv(fp)
        b1 = Book("B1", 1.0, 1, "In stock", "Cat", "u1", 0, "http://x/1")
        b2 = Book("B2", 2.0, 2, "In stock", "Cat", "u2", 0, "http://x/2")
        append_books([b1], fp)
        append_books([b2], fp)
        with open(fp, newline="", encoding="utf-8") as fh:
            all_rows = list(csv.reader(fh))
        header_rows = [r for r in all_rows if r[0] == "title"]
        assert len(header_rows) == 1   # exactly one header row


# ---------------------------------------------------------------------------
# 6. load_scraped_upcs
# ---------------------------------------------------------------------------
class TestLoadScrapedUpcs:
    def test_returns_empty_set_for_nonexistent_file(self, tmp_path: Path) -> None:
        upcs = load_scraped_upcs(str(tmp_path / "missing.csv"))
        assert upcs == set()

    def test_loads_upcs_correctly(self, tmp_path: Path) -> None:
        fp = tmp_path / "existing.csv"
        fp.write_text(
            "title,price_gbp,rating,availability,category,upc,num_reviews,detail_url\n"
            "Book A,9.99,3,In stock,Fiction,abc123,0,http://x\n"
            "Book B,5.00,2,In stock,Crime,def456,0,http://y\n",
            encoding="utf-8",
        )
        upcs = load_scraped_upcs(str(fp))
        assert upcs == {"abc123", "def456"}

    def test_skips_na_upcs(self, tmp_path: Path) -> None:
        fp = tmp_path / "na.csv"
        fp.write_text(
            "title,price_gbp,rating,availability,category,upc,num_reviews,detail_url\n"
            "Bad Book,0.00,,Out of stock,N/A,N/A,0,http://z\n",
            encoding="utf-8",
        )
        upcs = load_scraped_upcs(str(fp))
        assert upcs == set()


# ---------------------------------------------------------------------------
# 7. validate_and_report
# ---------------------------------------------------------------------------
class TestValidateAndReport:
    def _book(self, **kwargs) -> Book:
        defaults = dict(
            title="T", price_gbp=5.0, rating=3, availability="In stock",
            category="Cat", upc="u1", num_reviews=0,
            detail_url="http://x",
        )
        defaults.update(kwargs)
        return Book(**defaults)

    def test_clean_dataset_logs_zero_issues(self, caplog) -> None:
        import logging
        books = [self._book(upc=f"u{i}") for i in range(5)]
        with caplog.at_level(logging.INFO, logger="scraper"):
            validate_and_report(books)
        assert "0 issues" in caplog.text

    def test_duplicate_upc_is_flagged(self, caplog) -> None:
        import logging
        books = [self._book(upc="dupe"), self._book(upc="dupe"), self._book(upc="unique")]
        with caplog.at_level(logging.WARNING, logger="scraper"):
            validate_and_report(books)
        assert "duplicate upc" in caplog.text.lower()

    def test_missing_title_is_flagged(self, caplog) -> None:
        import logging
        books = [self._book(title=""), self._book(upc="u2")]
        with caplog.at_level(logging.WARNING, logger="scraper"):
            validate_and_report(books)
        assert "missing title" in caplog.text.lower()

    def test_none_price_is_flagged(self, caplog) -> None:
        import logging
        books = [self._book(price_gbp=None, upc="u9")]
        with caplog.at_level(logging.WARNING, logger="scraper"):
            validate_and_report(books)
        assert "missing price" in caplog.text.lower()

    def test_zero_price_is_flagged(self, caplog) -> None:
        import logging
        books = [self._book(price_gbp=0.0, upc="u8")]
        with caplog.at_level(logging.WARNING, logger="scraper"):
            validate_and_report(books)
        assert "non-positive price" in caplog.text.lower()

    def test_negative_price_is_flagged(self, caplog) -> None:
        import logging
        books = [self._book(price_gbp=-1.0, upc="u7")]
        with caplog.at_level(logging.WARNING, logger="scraper"):
            validate_and_report(books)
        assert "non-positive price" in caplog.text.lower()


# ---------------------------------------------------------------------------
# 8. Config — env-var overrides
# ---------------------------------------------------------------------------
class TestConfig:
    def test_defaults_are_set(self) -> None:
        cfg = Config()
        assert cfg.request_delay  == 1.0
        assert cfg.max_retries    == 3
        assert cfg.request_timeout == 15
        assert cfg.output_file    == "books_dataset.csv"
        assert cfg.resume         is False
        assert cfg.dry_run        is False

    def test_env_override_delay(self, monkeypatch) -> None:
        monkeypatch.setenv("SCRAPER_DELAY", "2.5")
        cfg = Config()
        assert cfg.request_delay == 2.5

    def test_env_override_output(self, monkeypatch) -> None:
        monkeypatch.setenv("SCRAPER_OUTPUT", "custom_out.csv")
        cfg = Config()
        assert cfg.output_file == "custom_out.csv"

    def test_env_override_max_retries(self, monkeypatch) -> None:
        monkeypatch.setenv("SCRAPER_MAX_RETRIES", "5")
        cfg = Config()
        assert cfg.max_retries == 5


# ---------------------------------------------------------------------------
# 9. CLI argument parser
# ---------------------------------------------------------------------------
class TestBuildArgParser:
    def test_defaults(self) -> None:
        parser = scraper.build_arg_parser()
        args = parser.parse_args([])
        assert args.output   is None
        assert args.limit    is None
        assert args.delay    is None
        assert args.resume   is False
        assert args.dry_run  is False

    def test_output_flag(self) -> None:
        args = scraper.build_arg_parser().parse_args(["--output", "foo.csv"])
        assert args.output == "foo.csv"

    def test_short_output_flag(self) -> None:
        args = scraper.build_arg_parser().parse_args(["-o", "bar.csv"])
        assert args.output == "bar.csv"

    def test_limit_flag(self) -> None:
        args = scraper.build_arg_parser().parse_args(["--limit", "3"])
        assert args.limit == 3

    def test_delay_flag(self) -> None:
        args = scraper.build_arg_parser().parse_args(["--delay", "0.5"])
        assert args.delay == 0.5

    def test_resume_flag(self) -> None:
        args = scraper.build_arg_parser().parse_args(["--resume"])
        assert args.resume is True

    def test_dry_run_flag(self) -> None:
        args = scraper.build_arg_parser().parse_args(["--dry-run"])
        assert args.dry_run is True
