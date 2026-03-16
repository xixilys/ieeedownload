#!/usr/bin/env python3
"""
Download full top-conference VLSI Symposium papers from IEEE Xplore (2018-2026 by default),
covering Technology + Circuits (including combined Technology and Circuits proceedings),
organized by year under a target directory.

Output layout:
/Volumes/extend_2/research_data/vlsi_paper/
  ├── 2018/
  │   ├── metadata.json
  │   └── pdfs/*.pdf
  ├── 2019/
  │   ├── metadata.json
  │   └── pdfs/*.pdf
  └── ...
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from json import JSONDecodeError
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from playwright.sync_api import sync_playwright

from ieee_auto_login import (
    DEFAULT_STATE_FILE,
    AUTO_STATE_FILE,
    auto_login_ieee_institution,
    has_ieee_institutional_access,
    load_ieee_credentials,
)
from ieee_download_via_page import fetch_pdf_bytes_via_document_page, page_has_paused_access

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

SEARCH_URL = "https://ieeexplore.ieee.org/rest/search"
REQUEST_SLEEP_SECONDS = 1.5
ROWS_PER_PAGE = 100
DOWNLOAD_SLEEP_SECONDS = 8
LONG_BREAK_EVERY = 10
LONG_BREAK_SECONDS = 120
DEFAULT_OUTPUT_ROOT = Path("/Volumes/extend_2/research_data/vlsi_paper")
VLSI_QUERY_TEMPLATES = [
    '{year} "Symposium on VLSI Technology"',
    '{year} "Symposium on VLSI Circuits"',
    '{year} "Symposium on VLSI Technology and Circuits"',
]


VLSI_INCLUDE_PATTERNS = [
    # Matches titles like "2019 Symposium on VLSI Technology",
    # "2020 IEEE Symposium on VLSI Technology",
    # "2023 IEEE Symposium on VLSI Technology and Circuits (VLSI Technology and Circuits)"
    re.compile(r"Symposium on VLSI Technology(?:\s|$)", re.I),
    re.compile(r"Symposium on VLSI Circuits(?:\s|$)", re.I),
]
VLSI_EXCLUDE_PATTERNS = [
    re.compile(r"Systems and Application", re.I),
    re.compile(r"VLSI-TSA", re.I),
]


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:180] or "untitled"


def wait_until_writable(path: Path, timeout_seconds: int = 600, interval_seconds: int = 5) -> None:
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / '.openclaw_write_probe'
            probe.write_text('ok', encoding='utf-8')
            probe.unlink(missing_ok=True)
            return
        except Exception as e:
            last_error = e
            logger.warning('Output path not writable yet (%s): %s; retrying in %ss', path, e, interval_seconds)
            time.sleep(interval_seconds)
    raise RuntimeError(f'Output path remained unavailable: {path}; last_error={last_error}')


class VLSIHarvester:
    def __init__(self, output_root: Path, state_file: Optional[Path] = None, headless: bool = False) -> None:
        self.output_root = output_root
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.headless = headless

        if state_file is None:
            state_file = AUTO_STATE_FILE if AUTO_STATE_FILE.exists() else DEFAULT_STATE_FILE
        self.state_file = Path(state_file)

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context_kwargs = {
            "viewport": {"width": 1600, "height": 1000},
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "accept_downloads": True,
        }
        if self.state_file.exists():
            context_kwargs["storage_state"] = str(self.state_file)
        self.context = self.browser.new_context(**context_kwargs)
        self.page = self.context.new_page()
        self.api = self.context.request
        self.page.set_default_timeout(60000)

        self._ensure_ieee_access()

    def close(self) -> None:
        self.context.storage_state(path=str(self.state_file))
        self.page.close()
        self.context.close()
        self.browser.close()
        self.playwright.stop()

    def _ensure_ieee_access(self) -> None:
        # Playwright occasionally throws: navigation interrupted by another navigation to about:blank.
        # Retry a few times to stabilize the first navigation.
        for attempt in range(1, 4):
            try:
                self.page.wait_for_timeout(500)  # let initial about:blank settle
                self.page.goto(
                    "https://ieeexplore.ieee.org/Xplore/home.jsp",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                break
            except Exception as e:
                logger.warning("IEEE home navigation failed (attempt %s/3): %s", attempt, e)
                if attempt >= 3:
                    raise
                self.page.wait_for_timeout(2000)
        self.page.wait_for_timeout(5000)
        body = self.page.locator("body").inner_text(timeout=5000)
        if "Access provided by:" in body and "University of Chinese Academy of Sciences" in body:
            logger.info("Existing IEEE institutional access detected.")
            return

        logger.info("No active IEEE institutional session detected; starting auto-login...")
        credentials = load_ieee_credentials()
        
        for attempt in range(3):
            success = auto_login_ieee_institution(self.page, self.context, credentials, self.state_file)
            if success:
                logger.info("IEEE institutional auto-login completed.")
                return
            logger.warning("Auto-login returned False; clearing session and retrying (attempt %s/3)...", attempt + 1)
            self.context.clear_cookies()
            self.page.wait_for_timeout(2000)
        
        raise RuntimeError("Institutional login failed after multiple attempts")

    def reconnect(self) -> None:
        logger.info("Reconnecting browser session to recover IEEE access...")

        try:
            self.page.close()
        except Exception:
            pass
        try:
            self.context.close()
        except Exception:
            pass
        try:
            self.browser.close()
        except Exception:
            pass

        # Phase 1: preserve existing storage_state and try a soft reconnect first.
        # In headless mode, re-running the full institutional login flow is the most
        # fragile part. Reusing the saved IEEE session often works and avoids UI-only
        # institution chooser issues.
        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context_kwargs = {
            "viewport": {"width": 1600, "height": 1000},
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "accept_downloads": True,
        }
        if self.state_file.exists():
            context_kwargs["storage_state"] = str(self.state_file)
        self.context = self.browser.new_context(**context_kwargs)
        self.page = self.context.new_page()
        self.api = self.context.request
        self.page.set_default_timeout(60000)

        try:
            self._ensure_ieee_access()
            logger.info("Reconnect succeeded with preserved storage state.")
            return
        except Exception as e:
            logger.warning("Soft reconnect with preserved state failed: %s", e)

        # Phase 2: fall back to a hard reset and full login.
        try:
            self.page.close()
        except Exception:
            pass
        try:
            self.context.close()
        except Exception:
            pass
        try:
            self.browser.close()
        except Exception:
            pass
        if self.state_file.exists():
            self.state_file.unlink()

        logger.info("Falling back to hard reconnect with cleared storage state...")
        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context_kwargs = {
            "viewport": {"width": 1600, "height": 1000},
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "accept_downloads": True,
        }
        self.context = self.browser.new_context(**context_kwargs)
        self.page = self.context.new_page()
        self.api = self.context.request
        self.page.set_default_timeout(60000)

        self._ensure_ieee_access()

    def search_year(self, year: int) -> List[Dict]:
        records_by_article: Dict[str, Dict] = {}

        def handle_page(page_result: Dict) -> None:
            for record in page_result.get("records", []):
                if not self._is_vlsi_record(record, year):
                    continue
                article_number = str(record.get("articleNumber") or "").strip()
                if not article_number:
                    continue
                if article_number not in records_by_article:
                    records_by_article[article_number] = self._normalize_record(record)

        for query_template in VLSI_QUERY_TEMPLATES:
            query = query_template.format(year=year)
            logger.info("Searching VLSI papers for year=%s query=%s", year, query)
            first_page = self._search_page(query, 1)
            total_pages = int(first_page.get("totalPages") or 0)
            logger.info(
                "Year=%s total records=%s total pages=%s",
                year,
                first_page.get("totalRecords"),
                total_pages,
            )
            handle_page(first_page)
            for page_number in range(2, total_pages + 1):
                page_result = self._search_page(query, page_number)
                handle_page(page_result)

        ordered = sorted(
            records_by_article.values(), key=lambda item: (item.get("title", ""), item.get("article_number", ""))
        )
        logger.info("Year=%s normalized VLSI records=%s", year, len(ordered))
        return ordered

    def _search_page(self, query: str, page_number: int, attempts: int = 5) -> Dict:
        payload = {
            "queryText": query,
            "pageNumber": page_number,
            "rowsPerPage": ROWS_PER_PAGE,
            "returnFacets": ["ALL"],
            "returnType": "SEARCH",
        }
        for attempt in range(1, attempts + 1):
            try:
                response = self.api.post(
                    SEARCH_URL,
                    data=json.dumps(payload),
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    timeout=60000,
                )
            except Exception as e:
                logger.warning(
                    "Search request exception query=%s page=%s attempt=%s/%s: %s",
                    query,
                    page_number,
                    attempt,
                    attempts,
                    e,
                )
                self.reconnect()
                time.sleep(max(REQUEST_SLEEP_SECONDS, attempt * 2))
                continue

            if response.ok:
                try:
                    result = response.json()
                except (JSONDecodeError, ValueError) as e:
                    body_preview = response.text()[:200].replace("\n", " ")
                    logger.warning(
                        "Search returned non-JSON query=%s page=%s attempt=%s/%s: %s | preview=%r",
                        query,
                        page_number,
                        attempt,
                        attempts,
                        e,
                        body_preview,
                    )
                    self.reconnect()
                    time.sleep(max(REQUEST_SLEEP_SECONDS, attempt * 2))
                    continue
                time.sleep(REQUEST_SLEEP_SECONDS)
                return result

            logger.warning(
                "Search failed query=%s page=%s attempt=%s/%s status=%s",
                query,
                page_number,
                attempt,
                attempts,
                response.status,
            )
            if response.status in (401, 403, 429, 500, 502, 503, 504):
                self.reconnect()
            time.sleep(max(REQUEST_SLEEP_SECONDS, attempt * 2))
        raise RuntimeError(f"Search failed after retries for year query {query} page {page_number}")

    def _is_vlsi_record(self, record: Dict, year: int) -> bool:
        publication_title = str(record.get("publicationTitle", "") or "")
        publication_year = str(record.get("publicationYear", "") or "")
        if publication_year != str(year):
            return False
        if not any(pattern.search(publication_title) for pattern in VLSI_INCLUDE_PATTERNS):
            return False
        if any(pattern.search(publication_title) for pattern in VLSI_EXCLUDE_PATTERNS):
            return False
        return True

    def _normalize_record(self, record: Dict) -> Dict:
        authors = []
        for author in record.get("authors", []):
            if isinstance(author, dict):
                authors.append(author.get("preferredName") or author.get("name") or "")
            else:
                authors.append(str(author))
        article_number = str(record.get("articleNumber", ""))
        return {
            "title": record.get("articleTitle", ""),
            "authors": authors,
            "abstract": record.get("abstract", ""),
            "publication_date": record.get("publicationDate", ""),
            "publication_year": record.get("publicationYear", ""),
            "publication_title": record.get("publicationTitle", ""),
            "doi": record.get("doi", ""),
            "article_number": article_number,
            "ieee_url": f"https://ieeexplore.ieee.org/document/{article_number}",
            "content_type": record.get("contentType", ""),
            "pdf_path": None,
            "pdf_downloaded": False,
            "download_error": None,
        }

    def save_year_metadata(self, year: int, records: Iterable[Dict]) -> Path:
        year_dir = self.output_root / str(year)
        wait_until_writable(year_dir)
        path = year_dir / "metadata.json"
        ordered = sorted(records, key=lambda item: (item.get("title", ""), item.get("article_number", "")))
        path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load_existing_year_metadata(self, year: int) -> Dict[str, Dict]:
        path = self.output_root / str(year) / "metadata.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return {str(item.get("article_number", "")): item for item in data if item.get("article_number")}

    def download_year(self, year: int, records: List[Dict], max_downloads: Optional[int] = None) -> None:
        year_dir = self.output_root / str(year)
        pdf_dir = year_dir / "pdfs"
        wait_until_writable(pdf_dir)

        total = len(records)
        successful_downloads_this_year = 0
        last_cooldown_at = 0
        for index, record in enumerate(records, start=1):
            if max_downloads is not None and successful_downloads_this_year >= max_downloads:
                break
            article_number = record["article_number"]
            title = record["title"]
            filename = f"{sanitize_filename(title)}_{article_number}.pdf"
            pdf_path = pdf_dir / filename
            rel_path = Path(str(year)) / "pdfs" / filename

            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                record["pdf_path"] = str(rel_path)
                record["pdf_downloaded"] = True
                record["download_error"] = None
                logger.info("[%s/%s][%s] Skip existing: %s", index, total, year, filename)
                continue

            if page_has_paused_access(self.page):
                self.reconnect()

            logger.info("[%s/%s][%s] Downloading: %s", index, total, year, title)

            max_attempts = 6
            for attempt in range(1, max_attempts + 1):
                if page_has_paused_access(self.page):
                    logger.warning(
                        "IEEE paused access detected before fetch; clearing session (attempt %s/%s)...",
                        attempt,
                        max_attempts,
                    )
                    record["download_error"] = "paused_access"
                    self.reconnect()
                    time.sleep(max(DOWNLOAD_SLEEP_SECONDS, 30))

                if not has_ieee_institutional_access(self.page, self.context):
                    logger.warning(
                        "Institutional access missing before fetch for article=%s; reconnecting (attempt %s/%s)...",
                        article_number,
                        attempt,
                        max_attempts,
                    )
                    record["download_error"] = "institutional_access_lost"
                    self.reconnect()
                    time.sleep(5 * attempt)

                try:
                    pdf_body = fetch_pdf_bytes_via_document_page(
                        self.context,
                        article_number,
                        page=self.page,
                        timeout_ms=180000,
                    )
                except Exception as e:
                    logger.warning(
                        "Page-driven PDF download failed for article=%s attempt=%s/%s: %s",
                        article_number,
                        attempt,
                        max_attempts,
                        e,
                    )
                    pdf_body = None
                    record["download_error"] = str(e)
                    self.reconnect()
                    if attempt < max_attempts:
                        time.sleep(5 * attempt)
                        continue
                    break

                if page_has_paused_access(self.page):
                    logger.warning(
                        "IEEE paused access detected after document request; clearing session (attempt %s/%s)...",
                        attempt,
                        max_attempts,
                    )
                    record["download_error"] = "paused_access"
                    self.reconnect()
                    if attempt < max_attempts:
                        time.sleep(max(LONG_BREAK_SECONDS, 300))
                        continue
                    break

                if not has_ieee_institutional_access(self.page, self.context):
                    logger.warning(
                        "Institutional access missing after fetch for article=%s; reconnecting and retrying (attempt %s/%s)...",
                        article_number,
                        attempt,
                        max_attempts,
                    )
                    record["download_error"] = "institutional_access_lost"
                    self.reconnect()
                    if attempt < max_attempts:
                        time.sleep(5 * attempt)
                        continue
                    break

                if pdf_body and pdf_body.startswith(b"%PDF"):
                    pdf_path.write_bytes(pdf_body)
                    file_size = pdf_path.stat().st_size if pdf_path.exists() else len(pdf_body)
                    record["pdf_path"] = str(rel_path)
                    record["pdf_downloaded"] = True
                    record["download_error"] = None
                    successful_downloads_this_year += 1
                    logger.info(
                        "[%s/%s][%s] Download success: %s | article=%s | size=%s bytes | saved=%s",
                        index,
                        total,
                        year,
                        title,
                        article_number,
                        file_size,
                        rel_path,
                    )
                    break

                record["pdf_downloaded"] = False
                record["download_error"] = record.get("download_error") or "pdf_unavailable"
                logger.warning(
                    "[%s/%s][%s] Download incomplete (attempt %s/%s): %s | article=%s | reason=%s",
                    index,
                    total,
                    year,
                    attempt,
                    max_attempts,
                    title,
                    article_number,
                    record["download_error"],
                )

                if attempt < max_attempts:
                    time.sleep(5 * attempt)
                    continue
                break

            self.save_year_metadata(year, records)
            time.sleep(DOWNLOAD_SLEEP_SECONDS)
            if (
                successful_downloads_this_year
                and successful_downloads_this_year % LONG_BREAK_EVERY == 0
                and successful_downloads_this_year != last_cooldown_at
            ):
                logger.info("Cooldown break after %s successful downloads; sleeping %ss", successful_downloads_this_year, LONG_BREAK_SECONDS)
                last_cooldown_at = successful_downloads_this_year
                time.sleep(LONG_BREAK_SECONDS)

    def run(self, start_year: int, end_year: int, max_downloads_per_year: Optional[int] = None) -> None:
        for year in range(start_year, end_year + 1):
            records = self.search_year(year)
            existing = self.load_existing_year_metadata(year)
            if existing:
                for record in records:
                    prev = existing.get(record["article_number"])
                    if prev:
                        record["pdf_path"] = prev.get("pdf_path")
                        record["pdf_downloaded"] = bool(prev.get("pdf_downloaded"))
                        record["download_error"] = prev.get("download_error")
            self.save_year_metadata(year, records)
            self.download_year(year, records, max_downloads=max_downloads_per_year)
            self.save_year_metadata(year, records)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=2018)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--state-file", type=Path, default=None)
    parser.add_argument("--max-downloads-per-year", type=int, default=None)
    parser.add_argument("--headless", action="store_true", help="Run Playwright Chromium in headless mode")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    harvester = VLSIHarvester(args.output_root, args.state_file, headless=args.headless)
    try:
        harvester.run(args.start_year, args.end_year, args.max_downloads_per_year)
    finally:
        harvester.close()


if __name__ == "__main__":
    main()
