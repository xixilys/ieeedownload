#!/usr/bin/env python3
"""
Enumerate venue-year article lists first, then filter target topics locally.
This is more complete and less noisy than global keyword-first search.
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from playwright.sync_api import sync_playwright

from _bootstrap import bootstrap_project_root

PROJECT_ROOT = bootstrap_project_root()

from ieee_harvest.pdf import fetch_pdf_bytes_via_document_page


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


START_YEAR = 2018
END_YEAR = 2025
ROWS_PER_PAGE = 100
REQUEST_SLEEP_SECONDS = 0.25

STATE_FILE = PROJECT_ROOT / "downloads" / "ieee_context_auto.json"
if not STATE_FILE.exists():
    STATE_FILE = PROJECT_ROOT / "downloads" / "ieee_context.json"
OUTPUT_DIR = PROJECT_ROOT / "downloads" / "venue_harvest_2018_2025"
PDF_DIR = OUTPUT_DIR / "pdfs"
METADATA_FILE = OUTPUT_DIR / "metadata.json"

SEARCH_URL = "https://ieeexplore.ieee.org/rest/search"
PDF_URL_TEMPLATE = (
    "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={article_number}&ref="
)

VENUE_SPECS = [
    {
        "name": "JSSC",
        "query_for_year": lambda year: f'"IEEE Journal of Solid-State Circuits" {year}',
        "title_pattern": re.compile(r"^IEEE Journal of Solid-State Circuits$", re.I),
    },
    {
        "name": "ISCAS",
        "query_for_year": (
            lambda year: f'{year} "IEEE International Symposium on Circuits and Systems"'
        ),
        "title_pattern": re.compile(
            r"^" + r"\d{4} IEEE International Symposium on Circuits and Systems \(ISCAS\)$",
            re.I,
        ),
    },
    {
        "name": "VLSI",
        "query_for_year": lambda year: f'{year} "Symposium on VLSI Technology and Circuits"',
        "title_pattern": re.compile(
            r"^\d{4} (IEEE )?Symposium on VLSI Technology and Circuits \(VLSI Technology and Circuits\)$",
            re.I,
        ),
    },
]

PRIMARY_PHRASES = [
    "compute in memory",
    "compute-in-memory",
    "computing in memory",
    "in-memory computing",
    "in-memory computation",
    "processing in memory",
    "processing-in-memory",
    "near-memory computing",
    "near memory computing",
    "compute near memory",
    "processing near memory",
    "computational memory",
    "ai accelerator",
    "artificial intelligence accelerator",
    "machine learning accelerator",
    "deep learning accelerator",
    "neural network accelerator",
    "cnn accelerator",
    "dnn accelerator",
    "transformer accelerator",
    "ai processor",
    "artificial intelligence processor",
    "neural processor",
    "neural processing unit",
]

AI_CONTEXT_TERMS = [
    "ai",
    "artificial intelligence",
    "machine learning",
    "deep learning",
    "neural",
    "cnn",
    "dnn",
    "transformer",
    "llm",
]

MEMORY_COMPUTE_CONTEXT_TERMS = [
    "compute in memory",
    "compute-in-memory",
    "computing in memory",
    "in-memory computing",
    "in-memory computation",
    "near-memory computing",
    "near memory computing",
    "processing in memory",
    "processing-in-memory",
    "computational memory",
]


def normalize_text(value: str) -> str:
    lowered = (value or "").lower()
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def contains_phrase(haystack: str, phrase: str) -> bool:
    normalized_haystack = f" {normalize_text(haystack)} "
    normalized_phrase = normalize_text(phrase)
    if not normalized_phrase:
        return False
    return f" {normalized_phrase} " in normalized_haystack


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:150] or "untitled"


class IEEERequestClient:
    def __init__(self) -> None:
        if not STATE_FILE.exists():
            raise FileNotFoundError(f"Missing login state: {STATE_FILE}")

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        PDF_DIR.mkdir(parents=True, exist_ok=True)

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.context = self.browser.new_context(
            storage_state=str(STATE_FILE),
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        self.api = self.context.request
        self.page = self.context.new_page()
        try:
            self.page.goto(
                "https://ieeexplore.ieee.org/",
                timeout=30000,
                wait_until="domcontentloaded",
            )
            self.page.wait_for_timeout(3000)
        except Exception as e:
            logger.warning("Initial homepage load failed: %s", e)
        logger.info("Headed browser context ready")

    def close(self) -> None:
        self.context.storage_state(path=str(STATE_FILE))
        self.page.close()
        self.browser.close()
        self.playwright.stop()

    def search_page(self, query: str, page_number: int, attempts: int = 3) -> Dict:
        payload = {
            "queryText": query,
            "pageNumber": page_number,
            "rowsPerPage": ROWS_PER_PAGE,
            "returnFacets": ["ALL"],
            "returnType": "SEARCH",
        }
        for attempt in range(1, attempts + 1):
            response = self.api.post(
                SEARCH_URL,
                data=json.dumps(payload),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=60000,
            )
            if response.ok:
                time.sleep(REQUEST_SLEEP_SECONDS)
                return response.json()
            logger.warning(
                "Search failed: query=%s page=%s attempt=%s/%s status=%s",
                query,
                page_number,
                attempt,
                attempts,
                response.status,
            )
            time.sleep(max(REQUEST_SLEEP_SECONDS, attempt))
        raise RuntimeError(f"Search failed after retries for {query} page {page_number}")

    def download_pdf(self, article_number: str, attempts: int = 3) -> Optional[bytes]:
        page = getattr(self, 'page', None)
        if page is not None:
            try:
                body = fetch_pdf_bytes_via_document_page(self.context, article_number, page=page)
                if body and body.startswith(b"%PDF"):
                    time.sleep(REQUEST_SLEEP_SECONDS)
                    return body
            except Exception as e:
                logger.warning("Page-driven PDF download failed for %s: %s", article_number, e)

        pdf_url = PDF_URL_TEMPLATE.format(article_number=article_number)
        for attempt in range(1, attempts + 1):
            response = self.api.get(pdf_url, timeout=60000)
            body = response.body()
            if body.startswith(b"%PDF"):
                time.sleep(REQUEST_SLEEP_SECONDS)
                return body
            snippet = body[:200].decode("utf-8", errors="ignore").replace("\n", " ")
            logger.warning(
                "Invalid PDF response: article=%s attempt=%s/%s status=%s type=%s body=%s",
                article_number,
                attempt,
                attempts,
                response.status,
                response.headers.get("content-type", ""),
                snippet,
            )
            time.sleep(max(REQUEST_SLEEP_SECONDS, attempt))
        return None


def year_range() -> Iterable[int]:
    return range(START_YEAR, END_YEAR + 1)


def record_matches_venue(record: Dict, venue_spec: Dict, year: int) -> bool:
    publication_title = record.get("publicationTitle", "")
    publication_year = str(record.get("publicationYear", ""))
    article_number = record.get("articleNumber")
    if not article_number:
        return False
    if publication_year != str(year):
        return False
    if not venue_spec["title_pattern"].search(publication_title):
        return False
    return True


def record_matches_topic(record: Dict) -> bool:
    haystack = " ".join(
        [
            record.get("articleTitle", ""),
            record.get("abstract", ""),
            record.get("publicationTitle", ""),
        ]
    )
    for phrase in PRIMARY_PHRASES:
        if contains_phrase(haystack, phrase):
            return True

    has_ai_context = any(contains_phrase(haystack, term) for term in AI_CONTEXT_TERMS)
    has_memory_compute_context = any(
        contains_phrase(haystack, term) for term in MEMORY_COMPUTE_CONTEXT_TERMS
    )

    if contains_phrase(haystack, "accelerator") and (
        has_ai_context or has_memory_compute_context
    ):
        return True
    if contains_phrase(haystack, "processor") and has_ai_context:
        return True
    if (
        contains_phrase(haystack, "coprocessor")
        or contains_phrase(haystack, "co-processor")
        or contains_phrase(haystack, "co processor")
    ) and (has_ai_context or has_memory_compute_context):
        return True
    if contains_phrase(haystack, "engine") and (
        has_ai_context or has_memory_compute_context
    ):
        return True
    return False


def normalize_record(record: Dict, venue_name: str) -> Dict:
    authors = []
    for author in record.get("authors", []):
        if isinstance(author, dict):
            authors.append(author.get("preferredName") or author.get("name") or "")
        else:
            authors.append(str(author))
    return {
        "title": record.get("articleTitle", ""),
        "authors": authors,
        "abstract": record.get("abstract", ""),
        "publication_date": record.get("publicationDate", ""),
        "publication_year": record.get("publicationYear", ""),
        "publication_title": record.get("publicationTitle", ""),
        "publication_number": record.get("publicationNumber", ""),
        "doi": record.get("doi", ""),
        "article_number": str(record.get("articleNumber", "")),
        "ieee_url": f"https://ieeexplore.ieee.org/document/{record.get('articleNumber', '')}",
        "content_type": record.get("contentType", ""),
        "venue_group": venue_name,
        "pdf_path": None,
        "pdf_downloaded": False,
    }


def save_metadata(records: Iterable[Dict]) -> None:
    ordered = sorted(
        records,
        key=lambda item: (
            item.get("venue_group", ""),
            item.get("publication_year", ""),
            item.get("title", ""),
        ),
    )
    METADATA_FILE.write_text(json.dumps(ordered, ensure_ascii=False, indent=2))


def discover_records(client: IEEERequestClient) -> Dict[str, Dict]:
    discovered: Dict[str, Dict] = {}
    for venue_spec in VENUE_SPECS:
        for year in year_range():
            query = venue_spec["query_for_year"](year)
            logger.info("Enumerating venue=%s year=%s", venue_spec["name"], year)
            first_page = client.search_page(query, 1)
            total_pages = int(first_page.get("totalPages") or 0)
            logger.info(
                "Venue query total records=%s total pages=%s",
                first_page.get("totalRecords"),
                total_pages,
            )

            def handle_page(page_result: Dict) -> None:
                for record in page_result.get("records", []):
                    if not record_matches_venue(record, venue_spec, year):
                        continue
                    if not record_matches_topic(record):
                        continue
                    article_number = str(record["articleNumber"])
                    if article_number not in discovered:
                        discovered[article_number] = normalize_record(
                            record, venue_spec["name"]
                        )

            handle_page(first_page)
            for page_number in range(2, total_pages + 1):
                page_result = client.search_page(query, page_number)
                handle_page(page_result)

            save_metadata(discovered.values())
            logger.info("Relevant papers so far: %s", len(discovered))
    return discovered


def download_records(client: IEEERequestClient, records: Dict[str, Dict]) -> None:
    ordered = sorted(
        records.values(),
        key=lambda item: (
            item.get("venue_group", ""),
            item.get("publication_year", ""),
            item.get("title", ""),
        ),
    )
    total = len(ordered)
    for index, record in enumerate(ordered, start=1):
        article_number = record["article_number"]
        title = record["title"]
        filename = f"{sanitize_filename(title)}_{article_number}.pdf"
        relative_pdf_path = Path("pdfs") / filename
        absolute_pdf_path = PDF_DIR / filename

        if absolute_pdf_path.exists() and absolute_pdf_path.stat().st_size > 0:
            record["pdf_path"] = str(relative_pdf_path)
            record["pdf_downloaded"] = True
            logger.info("[%s/%s] Skip existing PDF: %s", index, total, filename)
            continue

        logger.info("[%s/%s] Downloading: %s", index, total, title)
        pdf_body = client.download_pdf(article_number)
        if pdf_body:
            absolute_pdf_path.write_bytes(pdf_body)
            record["pdf_path"] = str(relative_pdf_path)
            record["pdf_downloaded"] = True
        else:
            record["pdf_downloaded"] = False
        save_metadata(records.values())


def main() -> None:
    client = IEEERequestClient()
    try:
        records = discover_records(client)
        logger.info("Discovery complete. Relevant papers found: %s", len(records))
        download_records(client, records)
        logger.info("Batch download completed. Metadata: %s", METADATA_FILE)
    finally:
        client.close()


if __name__ == "__main__":
    main()
