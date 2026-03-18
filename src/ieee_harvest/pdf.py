#!/usr/bin/env python3
"""
IEEE PDF download helpers via real document page navigation.

Rationale:
- Direct request to stampPDF/getPDF.jsp can hit 418 / Request Rejected.
- More reliable flow is document page -> click/open stamp/stamp.jsp ->
  extract embedded PDF iframe -> fetch/save PDF in the same logged-in context.
"""

from __future__ import annotations

import time
from typing import Optional

from playwright.sync_api import BrowserContext, Page

IEEE_DOC_URL_TEMPLATE = "https://ieeexplore.ieee.org/document/{article_number}"
STAMP_PAGE_URL_TEMPLATE = "https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber={article_number}"
PDF_IFRAME_PREFIX = "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber="
DIRECT_PDF_URL_TEMPLATE = "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={article_number}&ref="
PAUSE_MARKERS = [
    "You have reached the limit of download",
    "automatically paused access",
    "Please try again shortly",
]


def page_has_paused_access(page: Page) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=3000)
    except Exception:
        body = ""
    return any(marker in body for marker in PAUSE_MARKERS)


def _dismiss_ieee_overlays(page: Page) -> None:
    for label in ["全部接受", "Accept All", "接受"]:
        try:
            page.get_by_role("button", name=label).click(timeout=1200)
            page.wait_for_timeout(500)
            break
        except Exception:
            pass

    # Common blocking sign-up / survey / close buttons.
    for selector in [
        'button[aria-label="Close"]',
        'button.close',
        '.close-btn',
        '.modal-close',
        'button:has-text("Close")',
        'button:has-text("关闭")',
    ]:
        try:
            loc = page.locator(selector).first
            if loc.count():
                loc.click(timeout=800)
                page.wait_for_timeout(300)
        except Exception:
            pass


def _extract_pdf_url_from_stamp(page: Page, article_number: str) -> Optional[str]:
    deadline = time.time() + 20
    target_prefix = PDF_IFRAME_PREFIX + str(article_number)
    while time.time() < deadline:
        try:
            src = page.evaluate(
                """
                () => {
                    const frame = document.querySelector('iframe[src*="/stampPDF/getPDF.jsp"]');
                    return frame ? frame.getAttribute('src') : null;
                }
                """
            )
            if src:
                if src.startswith("http"):
                    return src
                return "https://ieeexplore.ieee.org" + src
        except Exception:
            pass
        for frame in page.frames:
            if frame.url.startswith(target_prefix):
                return frame.url
        page.wait_for_timeout(500)
    return None


def fetch_pdf_bytes_via_document_page(
    context: BrowserContext,
    article_number: str,
    *,
    page: Optional[Page] = None,
    timeout_ms: int = 120000,
) -> Optional[bytes]:
    close_page = False
    if page is None:
        page = context.new_page()
        close_page = True

    try:
        def fetch_pdf_bytes(pdf_url: str) -> Optional[bytes]:
            response = context.request.get(pdf_url, timeout=timeout_ms)
            body = response.body()
            if body.startswith(b"%PDF"):
                return body
            return None

        doc_url = IEEE_DOC_URL_TEMPLATE.format(article_number=article_number)
        page.goto(doc_url, wait_until="commit", timeout=timeout_ms)
        page.wait_for_timeout(5000)
        _dismiss_ieee_overlays(page)
        if page_has_paused_access(page):
            return None

        # Prefer the actual PDF/Download PDF link on the document page.
        clicked = False
        for selector in [
            f'a[href*="/stamp/stamp.jsp?tp=&arnumber={article_number}"]',
            'a.doc-actions-link.pdf',
            'a.xpl-btn-pdf',
        ]:
            try:
                loc = page.locator(selector).first
                if loc.count():
                    loc.click(timeout=5000)
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            page.goto(
                STAMP_PAGE_URL_TEMPLATE.format(article_number=article_number),
                wait_until="commit",
                timeout=timeout_ms,
            )
        else:
            page.wait_for_timeout(4000)

        if page_has_paused_access(page):
            return None

        if "/stamp/stamp.jsp" not in page.url:
            # Fallback to direct stamp page when SPA click does not navigate cleanly.
            page.goto(
                STAMP_PAGE_URL_TEMPLATE.format(article_number=article_number),
                wait_until="commit",
                timeout=timeout_ms,
            )
            page.wait_for_timeout(4000)

        if page_has_paused_access(page):
            return None

        pdf_url = _extract_pdf_url_from_stamp(page, article_number)
        if not pdf_url:
            direct_pdf_url = DIRECT_PDF_URL_TEMPLATE.format(article_number=article_number)
            return fetch_pdf_bytes(direct_pdf_url)

        return fetch_pdf_bytes(pdf_url)
    finally:
        if close_page:
            page.close()
