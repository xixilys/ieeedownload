#!/usr/bin/env python3
"""
Download full IEEE Journal of Solid-State Circuits papers year by year,
organizing outputs by issue and resuming safely across interrupted runs.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections import defaultdict
from json import JSONDecodeError
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

from ieee_auto_login import (
    AUTO_STATE_FILE,
    DEFAULT_STATE_FILE,
    auto_login_ieee_institution,
    has_ieee_institutional_access,
    load_ieee_credentials,
)
from ieee_download_via_page import fetch_pdf_bytes_via_document_page, page_has_paused_access


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
SEARCH_URL = "https://ieeexplore.ieee.org/rest/search"
JSSC_QUERY_TEMPLATE = '"IEEE Journal of Solid-State Circuits" {year}'
JSSC_TITLE_PATTERN = re.compile(r"^IEEE Journal of Solid-State Circuits$", re.I)
REQUEST_SLEEP_SECONDS = 1.5
ROWS_PER_PAGE = 100
DOWNLOAD_SLEEP_SECONDS = 8
LONG_BREAK_EVERY = 10
LONG_BREAK_SECONDS = 120
IEEE_BASE_URL = "https://ieeexplore.ieee.org"
DEFAULT_OUTPUT_ROOT = BASE_DIR / "downloads" / "jssc_full_harvest"


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:180] or "untitled"


def wait_until_writable(
    path: Path, timeout_seconds: int = 600, interval_seconds: int = 5
) -> None:
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".openclaw_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return
        except Exception as e:
            last_error = e
            logger.warning(
                "Output path not writable yet (%s): %s; retrying in %ss",
                path,
                e,
                interval_seconds,
            )
            time.sleep(interval_seconds)
    raise RuntimeError(
        f"Output path remained unavailable: {path}; last_error={last_error}"
    )


def parse_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def build_issue_key(record: Dict) -> str:
    volume = parse_int(record.get("volume"))
    issue = parse_int(record.get("issue"))
    is_number = str(record.get("isNumber") or record.get("is_number") or "").strip()
    if is_number:
        return f"vol{volume:02d}_issue{issue:02d}_is{is_number}"
    return f"vol{volume:02d}_issue{issue:02d}"


def build_issue_label(record: Dict) -> str:
    volume = str(record.get("volume") or "").strip() or "?"
    issue = str(record.get("issue") or "").strip() or "?"
    return f"Vol. {volume}, No. {issue}"


def build_issue_url(record: Dict) -> Optional[str]:
    publication_number = str(
        record.get("publicationNumber") or record.get("publication_number") or ""
    ).strip()
    is_number = str(record.get("isNumber") or record.get("is_number") or "").strip()
    publication_link = str(
        record.get("publicationLink") or record.get("publication_link") or ""
    ).strip()

    if publication_number and is_number:
        return (
            f"{IEEE_BASE_URL}/xpl/RecentIssue.jsp?"
            f"punumber={publication_number}&isnumber={is_number}"
        )
    if publication_link:
        return urljoin(IEEE_BASE_URL, publication_link)
    return None


class JSSCHarvester:
    def __init__(
        self,
        output_root: Path,
        state_file: Optional[Path] = None,
        headless: bool = False,
    ) -> None:
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
        try:
            self.context.storage_state(path=str(self.state_file))
        except Exception:
            pass
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
        self.playwright.stop()

    def _ensure_ieee_access(self) -> None:
        for attempt in range(1, 4):
            try:
                self.page.wait_for_timeout(500)
                self.page.goto(
                    "https://ieeexplore.ieee.org/Xplore/home.jsp",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                break
            except Exception as e:
                logger.warning(
                    "IEEE home navigation failed (attempt %s/3): %s", attempt, e
                )
                if attempt >= 3:
                    raise
                self.page.wait_for_timeout(2000)
        self.page.wait_for_timeout(5000)

        if has_ieee_institutional_access(self.page, self.context):
            logger.info("Existing IEEE institutional access detected.")
            return

        logger.info("No active IEEE institutional session detected; starting auto-login...")
        credentials = load_ieee_credentials()

        for attempt in range(3):
            success = auto_login_ieee_institution(
                self.page, self.context, credentials, self.state_file
            )
            if success:
                logger.info("IEEE institutional auto-login completed.")
                return
            logger.warning(
                "Auto-login returned False; clearing session and retrying (attempt %s/3)...",
                attempt + 1,
            )
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
        self.context = self.browser.new_context(
            viewport={"width": 1600, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
        )
        self.page = self.context.new_page()
        self.api = self.context.request
        self.page.set_default_timeout(60000)

        self._ensure_ieee_access()

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
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
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
        raise RuntimeError(
            f"Search failed after retries for year query {query} page {page_number}"
        )

    def _is_jssc_record(self, record: Dict, year: int) -> bool:
        publication_title = str(record.get("publicationTitle", "") or "")
        publication_year = str(record.get("publicationYear", "") or "")
        article_number = str(record.get("articleNumber", "") or "").strip()
        access_type = record.get("accessType") or {}
        if not article_number:
            return False
        if publication_year != str(year):
            return False
        if not JSSC_TITLE_PATTERN.search(publication_title):
            return False
        if isinstance(access_type, dict) and access_type.get("type") == "ephemera":
            return False
        return True

    def _normalize_record(self, record: Dict) -> Dict:
        authors = []
        for author in record.get("authors", []):
            if isinstance(author, dict):
                authors.append(author.get("preferredName") or author.get("name") or "")
            else:
                authors.append(str(author))

        article_number = str(record.get("articleNumber", "")).strip()
        issue_key = build_issue_key(record)
        issue_url = build_issue_url(record)
        return {
            "title": record.get("articleTitle", ""),
            "authors": authors,
            "abstract": record.get("abstract", ""),
            "publication_date": record.get("publicationDate", ""),
            "publication_year": str(record.get("publicationYear", "") or ""),
            "publication_title": record.get("publicationTitle", ""),
            "publication_number": str(record.get("publicationNumber", "") or ""),
            "publication_link": record.get("publicationLink", ""),
            "issue_url": issue_url,
            "volume": str(record.get("volume", "") or ""),
            "issue": str(record.get("issue", "") or ""),
            "is_number": str(record.get("isNumber", "") or ""),
            "issue_key": issue_key,
            "issue_label": build_issue_label(record),
            "start_page": str(record.get("startPage", "") or ""),
            "end_page": str(record.get("endPage", "") or ""),
            "doi": record.get("doi", ""),
            "article_number": article_number,
            "ieee_url": f"{IEEE_BASE_URL}/document/{article_number}",
            "document_link": urljoin(
                IEEE_BASE_URL, str(record.get("documentLink", "") or "")
            )
            if record.get("documentLink")
            else f"{IEEE_BASE_URL}/document/{article_number}",
            "content_type": record.get("contentType", ""),
            "access_type": record.get("accessType", {}),
            "pdf_path": None,
            "pdf_downloaded": False,
            "download_error": None,
        }

    def search_year(self, year: int) -> List[Dict]:
        query = JSSC_QUERY_TEMPLATE.format(year=year)
        records_by_article: Dict[str, Dict] = {}

        logger.info("Searching JSSC papers for year=%s query=%s", year, query)
        first_page = self._search_page(query, 1)
        total_pages = int(first_page.get("totalPages") or 0)
        logger.info(
            "Year=%s total records=%s total pages=%s",
            year,
            first_page.get("totalRecords"),
            total_pages,
        )

        def handle_page(page_result: Dict) -> None:
            for record in page_result.get("records", []):
                if not self._is_jssc_record(record, year):
                    continue
                article_number = str(record.get("articleNumber") or "").strip()
                if article_number and article_number not in records_by_article:
                    records_by_article[article_number] = self._normalize_record(record)

        handle_page(first_page)
        for page_number in range(2, total_pages + 1):
            page_result = self._search_page(query, page_number)
            handle_page(page_result)

        ordered = sorted(
            records_by_article.values(),
            key=lambda item: (
                parse_int(item.get("volume")),
                parse_int(item.get("issue")),
                parse_int(item.get("start_page")),
                item.get("title", ""),
                item.get("article_number", ""),
            ),
        )
        issue_count = len({item.get("issue_key", "") for item in ordered})
        logger.info(
            "Year=%s normalized JSSC records=%s across issues=%s",
            year,
            len(ordered),
            issue_count,
        )
        return ordered

    def _group_issue_summaries(self, records: Iterable[Dict]) -> List[Dict]:
        grouped: Dict[str, List[Dict]] = defaultdict(list)
        for record in records:
            grouped[record.get("issue_key", "unknown")].append(record)

        summaries: List[Dict] = []
        for issue_key, issue_records in grouped.items():
            first = issue_records[0]
            ordered_articles = sorted(
                issue_records,
                key=lambda item: (
                    parse_int(item.get("start_page")),
                    item.get("title", ""),
                    item.get("article_number", ""),
                ),
            )
            summaries.append(
                {
                    "issue_key": issue_key,
                    "issue_label": first.get("issue_label"),
                    "volume": first.get("volume"),
                    "issue": first.get("issue"),
                    "is_number": first.get("is_number"),
                    "publication_year": first.get("publication_year"),
                    "publication_title": first.get("publication_title"),
                    "publication_number": first.get("publication_number"),
                    "issue_url": first.get("issue_url"),
                    "article_count": len(ordered_articles),
                    "downloaded_count": sum(
                        1 for item in ordered_articles if item.get("pdf_downloaded")
                    ),
                    "articles": [
                        {
                            "article_number": item.get("article_number"),
                            "title": item.get("title"),
                            "start_page": item.get("start_page"),
                            "end_page": item.get("end_page"),
                            "pdf_downloaded": bool(item.get("pdf_downloaded")),
                            "pdf_path": item.get("pdf_path"),
                        }
                        for item in ordered_articles
                    ],
                }
            )

        return sorted(
            summaries,
            key=lambda item: (
                parse_int(item.get("volume")),
                parse_int(item.get("issue")),
                item.get("issue_key", ""),
            ),
        )

    def save_year_metadata(self, year: int, records: Iterable[Dict]) -> Path:
        year_dir = self.output_root / str(year)
        wait_until_writable(year_dir)

        ordered = sorted(
            records,
            key=lambda item: (
                parse_int(item.get("volume")),
                parse_int(item.get("issue")),
                parse_int(item.get("start_page")),
                item.get("title", ""),
                item.get("article_number", ""),
            ),
        )

        metadata_path = year_dir / "metadata.json"
        metadata_path.write_text(
            json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        issues_path = year_dir / "issues.json"
        issues_path.write_text(
            json.dumps(self._group_issue_summaries(ordered), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return metadata_path

    def load_existing_year_metadata(self, year: int) -> Dict[str, Dict]:
        path = self.output_root / str(year) / "metadata.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return {
            str(item.get("article_number", "")): item
            for item in data
            if item.get("article_number")
        }

    def download_year(
        self, year: int, records: List[Dict], max_downloads: Optional[int] = None
    ) -> None:
        year_dir = self.output_root / str(year)
        issue_root = year_dir / "issues"
        wait_until_writable(issue_root)

        total = len(records)
        successful_downloads_this_year = 0
        last_cooldown_at = 0

        for index, record in enumerate(records, start=1):
            if max_downloads is not None and successful_downloads_this_year >= max_downloads:
                break

            article_number = record["article_number"]
            title = record["title"]
            issue_key = record.get("issue_key", "unknown_issue")
            issue_dir = issue_root / issue_key / "pdfs"
            wait_until_writable(issue_dir)

            filename = f"{sanitize_filename(title)}_{article_number}.pdf"
            pdf_path = issue_dir / filename
            rel_path = Path(str(year)) / "issues" / issue_key / "pdfs" / filename

            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                record["pdf_path"] = str(rel_path)
                record["pdf_downloaded"] = True
                record["download_error"] = None
                logger.info(
                    "[%s/%s][%s][%s] Skip existing: %s",
                    index,
                    total,
                    year,
                    issue_key,
                    filename,
                )
                continue

            if page_has_paused_access(self.page):
                self.reconnect()

            logger.info(
                "[%s/%s][%s][%s] Downloading: %s",
                index,
                total,
                year,
                issue_key,
                title,
            )

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
                    file_size = (
                        pdf_path.stat().st_size if pdf_path.exists() else len(pdf_body)
                    )
                    record["pdf_path"] = str(rel_path)
                    record["pdf_downloaded"] = True
                    record["download_error"] = None
                    successful_downloads_this_year += 1
                    logger.info(
                        "[%s/%s][%s][%s] Download success: %s | article=%s | size=%s bytes | saved=%s",
                        index,
                        total,
                        year,
                        issue_key,
                        title,
                        article_number,
                        file_size,
                        rel_path,
                    )
                    break

                record["pdf_downloaded"] = False
                record["download_error"] = (
                    record.get("download_error") or "pdf_unavailable"
                )
                logger.warning(
                    "[%s/%s][%s][%s] Download incomplete (attempt %s/%s): %s | article=%s | reason=%s",
                    index,
                    total,
                    year,
                    issue_key,
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
                logger.info(
                    "Cooldown break after %s successful downloads; sleeping %ss",
                    successful_downloads_this_year,
                    LONG_BREAK_SECONDS,
                )
                last_cooldown_at = successful_downloads_this_year
                time.sleep(LONG_BREAK_SECONDS)

    def run(
        self, start_year: int, end_year: int, max_downloads_per_year: Optional[int] = None
    ) -> None:
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
    parser.add_argument("--start-year", type=int, default=2020)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--state-file", type=Path, default=None)
    parser.add_argument("--max-downloads-per-year", type=int, default=None)
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Playwright Chromium in headless mode",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    harvester = JSSCHarvester(args.output_root, args.state_file, headless=args.headless)
    try:
        harvester.run(args.start_year, args.end_year, args.max_downloads_per_year)
    finally:
        harvester.close()


if __name__ == "__main__":
    main()
