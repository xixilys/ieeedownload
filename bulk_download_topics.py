#!/usr/bin/env python3
"""
Batch download IEEE papers for JSSC / VLSI / ISCAS in 2018-2025
covering AI accelerators, processors, co-processors, compute-in-memory,
near-memory computing, and related directions.
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from playwright.sync_api import sync_playwright

from ieee_download_via_page import fetch_pdf_bytes_via_document_page


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


START_YEAR = 2018
END_YEAR = 2025
ROWS_PER_PAGE = 100
REQUEST_SLEEP_SECONDS = 0.25

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads" / "topic_harvest_2018_2025"
PDF_DIR = DOWNLOAD_DIR / "pdfs"
STATE_FILE = BASE_DIR / "downloads" / "ieee_context_auto.json"
if not STATE_FILE.exists():
    STATE_FILE = BASE_DIR / "downloads" / "ieee_context.json"
METADATA_FILE = DOWNLOAD_DIR / "metadata.json"

SEARCH_URL = "https://ieeexplore.ieee.org/rest/search"
PDF_URL_TEMPLATE = (
    "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={article_number}&ref="
)

VENUES = [
    {
        "name": "JSSC",
        "query_phrase": '"IEEE Journal of Solid-State Circuits"',
        "title_pattern": re.compile(r"^IEEE Journal of Solid-State Circuits$", re.I),
    },
    {
        "name": "ISCAS",
        "query_phrase": '"International Symposium on Circuits and Systems"',
        "title_pattern": re.compile(
            r"International Symposium on Circuits and Systems|\(ISCAS\)", re.I
        ),
    },
    {
        "name": "VLSI",
        "query_phrase": '"Symposium on VLSI"',
        "title_pattern": re.compile(
            r"Symposium on VLSI|VLSI Technology and Circuits|VLSI Circuits", re.I
        ),
    },
]

TOPIC_PHRASES = [
    "AI accelerator",
    "artificial intelligence accelerator",
    "machine learning accelerator",
    "deep learning accelerator",
    "neural network accelerator",
    "DNN accelerator",
    "CNN accelerator",
    "transformer accelerator",
    "AI processor",
    "artificial intelligence processor",
    "neural processor",
    "neural processing unit",
    "coprocessor",
    "co-processor",
    "compute in memory",
    "compute-in-memory",
    "computing in memory",
    "in-memory computing",
    "processing in memory",
    "processing-in-memory",
    "near-memory computing",
    "near memory computing",
]

BROAD_PHRASE_CONTEXT = {
    "coprocessor": [
        "ai",
        "artificial intelligence",
        "neural",
        "machine learning",
        "deep learning",
        "cnn",
        "dnn",
        "transformer",
        "memory",
        "compute in memory",
        "in memory computing",
        "processing in memory",
        "near memory computing",
    ],
    "co-processor": [
        "ai",
        "artificial intelligence",
        "neural",
        "machine learning",
        "deep learning",
        "cnn",
        "dnn",
        "transformer",
        "memory",
        "compute in memory",
        "in memory computing",
        "processing in memory",
        "near memory computing",
    ],
}


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:150] or "untitled"


def normalize_text(value: str) -> str:
    lowered = (value or "").lower()
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def year_in_range(value: str) -> bool:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return False
    return START_YEAR <= year <= END_YEAR


class IEEEBatchHarvester:
    def __init__(self) -> None:
        if not STATE_FILE.exists():
            raise FileNotFoundError(f"Missing login state: {STATE_FILE}")

        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        PDF_DIR.mkdir(parents=True, exist_ok=True)

        self.playwright = sync_playwright().start()
        self.api = self.playwright.request.new_context(
            storage_state=str(STATE_FILE),
            extra_http_headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        self.browser = self.playwright.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.context = self.browser.new_context(
            storage_state=str(STATE_FILE),
            viewport={"width": 1600, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
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
        logger.info("HTTP request context ready")

    def close(self) -> None:
        self.context.storage_state(path=str(STATE_FILE))
        self.page.close()
        self.context.close()
        self.browser.close()
        self.api.dispose()
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


def build_query(venue_query_phrase: str, topic_phrase: str) -> str:
    return f"{venue_query_phrase} \"{topic_phrase}\""


def normalize_record(
    record: Dict, venue_name: str, matched_query: str, topic_phrase: str
) -> Dict:
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
        "doi": record.get("doi", ""),
        "article_number": str(record.get("articleNumber", "")),
        "ieee_url": f"https://ieeexplore.ieee.org/document/{record.get('articleNumber', '')}",
        "content_type": record.get("contentType", ""),
        "venue_group": venue_name,
        "matched_topic_phrases": [topic_phrase],
        "matched_queries": [matched_query],
        "pdf_path": None,
        "pdf_downloaded": False,
    }


def record_matches(record: Dict, venue: Dict, topic_phrase: str) -> bool:
    publication_title = record.get("publicationTitle", "")
    publication_year = record.get("publicationYear", "")
    article_number = record.get("articleNumber")
    if not article_number:
        return False
    if not venue["title_pattern"].search(publication_title):
        return False
    if not year_in_range(publication_year):
        return False
    haystack = " ".join(
        [
            record.get("articleTitle", ""),
            record.get("abstract", ""),
            publication_title,
        ]
    )
    normalized_haystack = normalize_text(haystack)
    if normalize_text(topic_phrase) not in normalized_haystack:
        return False
    extra_context_terms = BROAD_PHRASE_CONTEXT.get(topic_phrase, [])
    if extra_context_terms and not any(
        normalize_text(term) in normalized_haystack for term in extra_context_terms
    ):
        return False
    return True


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


def discover_records(harvester: IEEEBatchHarvester) -> Dict[str, Dict]:
    discovered: Dict[str, Dict] = {}

    for venue in VENUES:
        for topic_phrase in TOPIC_PHRASES:
            query = build_query(venue["query_phrase"], topic_phrase)
            logger.info("Searching query: %s", query)

            first_page = harvester.search_page(query, 1)
            total_pages = int(first_page.get("totalPages") or 0)
            logger.info(
                "Query total records=%s total pages=%s",
                first_page.get("totalRecords"),
                total_pages,
            )

            def handle_page(page_result: Dict) -> None:
                for record in page_result.get("records", []):
                    if not record_matches(record, venue, topic_phrase):
                        continue
                    article_number = str(record["articleNumber"])
                    if article_number not in discovered:
                        discovered[article_number] = normalize_record(
                            record, venue["name"], query, topic_phrase
                        )
                    elif query not in discovered[article_number]["matched_queries"]:
                        discovered[article_number]["matched_queries"].append(query)
                    if (
                        topic_phrase
                        not in discovered[article_number]["matched_topic_phrases"]
                    ):
                        discovered[article_number]["matched_topic_phrases"].append(
                            topic_phrase
                        )

            handle_page(first_page)

            for page_number in range(2, total_pages + 1):
                page_result = harvester.search_page(query, page_number)
                handle_page(page_result)

            save_metadata(discovered.values())
            logger.info("Discovered unique papers so far: %s", len(discovered))

    return discovered


def download_records(harvester: IEEEBatchHarvester, records: Dict[str, Dict]) -> None:
    ordered_records = sorted(
        records.values(),
        key=lambda item: (
            item.get("venue_group", ""),
            item.get("publication_year", ""),
            item.get("title", ""),
        ),
    )
    total = len(ordered_records)
    for index, record in enumerate(ordered_records, start=1):
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
        pdf_body = harvester.download_pdf(article_number)
        if pdf_body:
            absolute_pdf_path.write_bytes(pdf_body)
            record["pdf_path"] = str(relative_pdf_path)
            record["pdf_downloaded"] = True
        else:
            record["pdf_downloaded"] = False

        save_metadata(records.values())


def main() -> None:
    harvester = IEEEBatchHarvester()
    try:
        records = discover_records(harvester)
        logger.info("Discovery complete. Unique papers found: %s", len(records))
        download_records(harvester, records)
        logger.info("Batch download completed. Metadata: %s", METADATA_FILE)
    finally:
        harvester.close()


if __name__ == "__main__":
    main()
