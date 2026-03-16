#!/usr/bin/env python3
"""
Launch a real browser, wait for manual institutional login until PDF access works,
then continue batch downloads using the same live browser session.
"""

import json
import logging
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from bulk_download_by_venue import download_records
from ieee_download_via_page import fetch_pdf_bytes_via_document_page


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "downloads" / "ieee_context_auto.json"
if not STATE_FILE.exists():
    STATE_FILE = BASE_DIR / "downloads" / "ieee_context.json"
METADATA_FILE = BASE_DIR / "downloads" / "venue_harvest_2018_2025" / "metadata.json"
TEST_ARTICLE = "9181104"
TEST_DOC_URL = f"https://ieeexplore.ieee.org/document/{TEST_ARTICLE}"
TEST_PDF_URL = (
    f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={TEST_ARTICLE}&ref="
)


class LiveBrowserClient:
    def __init__(self, context, page):
        self.context = context
        self.page = page
        self.api = context.request

    def download_pdf(self, article_number: str, attempts: int = 3):
        try:
            body = fetch_pdf_bytes_via_document_page(self.context, article_number, page=self.page)
            if body and body.startswith(b"%PDF"):
                time.sleep(0.25)
                return body
        except Exception as e:
            logger.warning("Page-driven PDF download failed: %s", e)

        pdf_url = (
            f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={article_number}&ref="
        )
        for attempt in range(1, attempts + 1):
            response = self.api.get(pdf_url, timeout=60000)
            body = response.body()
            if body.startswith(b"%PDF"):
                time.sleep(0.25)
                return body
            snippet = body[:160].decode("utf-8", errors="ignore").replace("\n", " ")
            logger.warning(
                "Invalid PDF response: article=%s attempt=%s/%s status=%s type=%s body=%s",
                article_number,
                attempt,
                attempts,
                response.status,
                response.headers.get("content-type", ""),
                snippet,
            )
            time.sleep(attempt)
        return None


def load_records():
    records_list = json.loads(METADATA_FILE.read_text())
    return {item["article_number"]: item for item in records_list}


def main():
    records = load_records()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            storage_state=str(STATE_FILE),
            viewport={"width": 1600, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.goto(TEST_DOC_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        logger.info("Browser opened on test paper: %s", TEST_DOC_URL)
        logger.info(
            "Please complete institutional login in this browser and open the PDF once."
        )

        client = LiveBrowserClient(context, page)

        while True:
            try:
                body = fetch_pdf_bytes_via_document_page(context, TEST_ARTICLE, page=page)
                if body and body.startswith(b"%PDF"):
                    logger.info("PDF access verified in the live browser session.")
                    break
                logger.info("Waiting for login/PDF access through document page flow...")
            except Exception as e:
                logger.warning("PDF verification failed: %s", e)
            time.sleep(10)

        context.storage_state(path=str(STATE_FILE))
        logger.info("Live session saved to: %s", STATE_FILE)

        download_records(client, records)
        logger.info("Batch download completed.")


if __name__ == "__main__":
    main()
