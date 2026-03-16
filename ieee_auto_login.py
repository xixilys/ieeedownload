#!/usr/bin/env python3
"""
IEEE Xplore institutional auto-login helpers.

Verified flow:
1. IEEE home -> Institutional Sign In
2. Access Through Your Institution / remembered institution entry
3. Jump to passport.escience.cn
4. Real visible login form lives inside oauth2 iframe
5. Type username/password like a human and submit
6. Return to IEEE with institutional access
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, Optional

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_FILE = BASE_DIR / "downloads" / "ieee_context.json"
AUTO_STATE_FILE = BASE_DIR / "downloads" / "ieee_context_auto.json"
DEFAULT_CREDENTIAL_FILE = Path("/Users/xixilys/clawd/.credentials/ieee.env")
IEEE_HOME = "https://ieeexplore.ieee.org/Xplore/home.jsp"
IEEE_INST_HELP = "https://ieeexplore.ieee.org/Xplorehelp/Help_Institutional_Sign_In.html"
PASSPORT_HOST = "passport.escience.cn"
EXPECTED_INSTITUTION_TEXT = "University of Chinese Academy of Sciences"
EXPECTED_ACCESS_TEXT = "Access provided by:"


def load_ieee_credentials(env_path: Optional[Path] = None) -> Dict[str, str]:
    env_path = env_path or DEFAULT_CREDENTIAL_FILE
    data: Dict[str, str] = {}

    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()

    for key in ["IEEE_INST_NAME", "IEEE_INST_USERNAME", "IEEE_INST_PASSWORD"]:
        if os.getenv(key):
            data[key] = os.getenv(key, "")

    required = ["IEEE_INST_NAME", "IEEE_INST_USERNAME", "IEEE_INST_PASSWORD"]
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise RuntimeError(
            f"Missing IEEE institutional credentials: {', '.join(missing)}; "
            f"expected in {env_path} or environment"
        )

    return data


def create_ieee_context(browser, storage_state: Optional[Path] = None) -> BrowserContext:
    kwargs = {
        "viewport": {"width": 1600, "height": 1000},
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "accept_downloads": True,
    }
    if storage_state and Path(storage_state).exists():
        kwargs["storage_state"] = str(storage_state)
    return browser.new_context(**kwargs)


def _safe_click_cookie_accept(page: Page) -> None:
    for label in ["全部接受", "Accept All", "接受"]:
        try:
            page.get_by_role("button", name=label).click(timeout=1500)
            page.wait_for_timeout(500)
            return
        except Exception:
            pass


def has_ieee_institutional_access(page: Page, context: BrowserContext) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=5000)
    except Exception:
        body = ""

    if EXPECTED_ACCESS_TEXT in body and EXPECTED_INSTITUTION_TEXT in body:
        return True

    try:
        cookies = context.cookies(["https://ieeexplore.ieee.org"])
    except Exception:
        return False

    cookie_map = {cookie.get("name", ""): cookie.get("value", "") for cookie in cookies}
    xpluserinfo = cookie_map.get("xpluserinfo", "")
    erights = cookie_map.get("ERIGHTS", "")
    return EXPECTED_INSTITUTION_TEXT.replace(" ", "") in xpluserinfo or bool(erights)


def _open_institutional_modal(page: Page) -> None:
    # First try the normal homepage control.
    try:
        page.locator("a.inst-sign-in").first.click(timeout=5000)
        page.wait_for_timeout(1000)
        return
    except Exception:
        pass

    # Some expired states land on a personal-account shell without the normal entry.
    # Fallback to the institutional sign-in help page and trigger the modal from there.
    page.goto(IEEE_INST_HELP, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)
    _safe_click_cookie_accept(page)

    candidates = [
        page.locator("button", has_text="Institutional Sign In").first,
        page.locator("a", has_text="Institutional Sign In").first,
        page.locator("button", has_text="Access Through Your Institution").first,
        page.locator("a", has_text="Access Through Your Institution").first,
    ]
    for candidate in candidates:
        try:
            candidate.click(timeout=4000)
            page.wait_for_timeout(1200)
            return
        except Exception:
            pass

    raise RuntimeError("Unable to open IEEE institutional sign-in modal")


def _click_institution_entry(page: Page, institution_name: str) -> bool:
    import logging
    logger = logging.getLogger(__name__)

    # Step 1: always try to open the SeamlessAccess institution chooser.
    for target in [page, *page.frames]:
        try:
            access_button = target.locator("button", has_text="Access Through Your Institution").first
            if access_button.count():
                access_button.click(timeout=5000)
                page.wait_for_timeout(2000)
                break
        except Exception:
            pass

    # Step 2: if IEEE remembers the institution directly in the modal/frame, use it.
    for target in [page, *page.frames]:
        for selector in [
            ("button", "Access Through"),
            ("a", "Access Through"),
            ("button", institution_name),
            ("a", institution_name),
        ]:
            try:
                loc = target.locator(selector[0], has_text=selector[1]).filter(has_text=institution_name).first
                if loc.count():
                    loc.click(timeout=3000)
                    page.wait_for_timeout(3000)
                    if PASSPORT_HOST in page.url:
                        return True
                
            except Exception:
                pass

    # Step 3: normal institution search flow; input may live in top doc or iframe.
    search_surfaces = [page, *page.frames]
    search_input = None
    search_surface = None
    for target in search_surfaces:
        try:
            loc = target.locator('input[aria-label="Search for your Institution"]').first
            if loc.count():
                loc.wait_for(timeout=3000)
                search_input = loc
                search_surface = target
                break
        except Exception:
            continue

    if search_input is None:
        logger.warning("Institution search input not found in page or frames")
        return False

    search_input.click(timeout=3000)
    search_input.fill("")
    page.wait_for_timeout(500)
    search_input.type(institution_name, delay=100)
    page.wait_for_timeout(4000)  # give it more time to fetch results

    # Try precise click on the typeahead dropdown using the exact institution_name
    candidate = search_surface.locator("a.stats-Global_Inst_signin_typeahead", has_text=institution_name).first
    try:
        if candidate.is_visible():
            # The click triggers a navigation chain: wayf.jsp → SAML → passport.escience.cn
            try:
                candidate.click()
            except Exception:
                pass

            deadline = time.time() + 30
            while time.time() < deadline:
                if PASSPORT_HOST in page.url:
                    return True
                page.wait_for_timeout(1000)

            if PASSPORT_HOST in page.url:
                return True
    except Exception:
        pass

    # Fallback: keyboard ArrowDown + Enter
    for _ in range(3):
        try:
            search_input.press("ArrowDown")
        except Exception:
            page.keyboard.press("ArrowDown")
        page.wait_for_timeout(500)
        try:
            search_input.press("Enter")
        except Exception:
            page.keyboard.press("Enter")
        page.wait_for_timeout(8000)
        if PASSPORT_HOST in page.url:
            return True

    return PASSPORT_HOST in page.url


def _find_passport_login_frame(page: Page):
    """Return the visible login surface (Page or Frame).

    In renewal flows the visible CSTNet form can appear either:
    - inside an oauth2 iframe on passport.escience.cn, or
    - directly in the top document.
    """
    deadline = time.time() + 45
    import logging
    logger = logging.getLogger(__name__)
    while time.time() < deadline:
        try:
            main_visible = page.evaluate(
                "() => { const el = document.getElementById('username') || document.getElementById('userName'); "
                "return el ? el.offsetWidth > 0 && el.offsetHeight > 0 : false; }"
            )
            if main_visible:
                logger.info("_find_passport: visible login form found in top document")
                return page
        except Exception:
            pass

        # Prefer the /oauth2/authorize frame with visible #userName
        for frame in page.frames:
            try:
                if "/oauth2/authorize" in frame.url:
                    visible = frame.evaluate(
                        "() => { const el = document.getElementById('userName') || document.getElementById('username'); "
                        "return el ? el.offsetWidth > 0 && el.offsetHeight > 0 : false; }"
                    )
                    logger.info(f"_find_passport: oauth2 frame visible={visible}")
                    if visible:
                        return frame
            except Exception as e:
                logger.info(f"_find_passport: oauth2 frame error: {e}")
                continue

        # Fallback: any frame with a visible #username or #userName
        for frame in page.frames:
            try:
                visible = frame.evaluate(
                    "() => { const el = document.getElementById('username') || document.getElementById('userName'); "
                    "return el ? el.offsetWidth > 0 && el.offsetHeight > 0 : false; }"
                )
                if visible:
                    logger.info(f"_find_passport: fallback frame matched: {frame.url[:60]}")
                    return frame
            except Exception:
                continue

        page.wait_for_timeout(1000)

    raise RuntimeError("Visible passport login iframe not found")

def _submit_passport_login(page: Page, username: str, password: str) -> bool:
    # Give the page a moment to load the iframe or finish redirecting
    page.wait_for_timeout(10000)
    
    # Debug: log current state
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"_submit_passport_login: URL={page.url}")
    logger.info(f"_submit_passport_login: {len(page.frames)} frames")
    for i, f in enumerate(page.frames):
        try:
            logger.info(f"  Frame {i}: {f.url[:80]}")
        except Exception:
            pass
    
    # If we already bounced back to IEEE and access is active, there's nothing to submit.
    if "ieeexplore.ieee.org" in page.url and PASSPORT_HOST not in page.url:
        logger.info("_submit_passport_login: already back on IEEE; skipping form submission")
        return False

    try:
        frame = _find_passport_login_frame(page)
    except Exception as e:
        logger.warning(f"_submit_passport_login: visible login surface not found: {e}")
        return False

    # Unhide form and login div if it's hidden
    try:
        if hasattr(frame, "evaluate"):
            frame.evaluate("() => { const f = document.getElementById('loginForm'); if (f) f.style.display = 'block'; }")
            frame.evaluate("() => { const d = document.getElementById('div-uname-pwd'); if (d) d.style.display = 'block'; }")
            frame.evaluate("() => { const q = document.getElementById('div-qrcode'); if (q) q.style.display = 'none'; }")
    except Exception:
        pass

    # Check which format the login form has
    if frame.locator("#userName").count() > 0:
        user = frame.locator("#userName")
        pwd = frame.locator("#password")
        submit = frame.locator("#loginBtn")
    else:
        user = frame.locator("#username")
        pwd = frame.locator("#password")
        submit = frame.locator("#submitBtn") if frame.locator("#submitBtn").count() > 0 else frame.locator("#loginBtn")

    user.wait_for(state="attached", timeout=20000)
    pwd.wait_for(state="attached", timeout=20000)
    submit.wait_for(state="attached", timeout=20000)

    # Some platforms intercept fill, so use focus+type
    user.click(force=True)
    user.fill("")
    user.type(username, delay=100)

    pwd.click(force=True)
    pwd.fill("")
    pwd.type(password, delay=100)

    page.wait_for_timeout(1000)
    submit.click(force=True)
    return True


def auto_login_ieee_institution(
    page: Page,
    context: BrowserContext,
    credentials: Dict[str, str],
    save_state_path: Optional[Path] = None,
) -> bool:
    import logging
    logger = logging.getLogger(__name__)
    save_state_path = Path(save_state_path or DEFAULT_STATE_FILE)

    page.goto(IEEE_HOME, wait_until="commit", timeout=60000)
    page.wait_for_timeout(5000)
    _safe_click_cookie_accept(page)

    if has_ieee_institutional_access(page, context):
        context.storage_state(path=str(save_state_path))
        if save_state_path != AUTO_STATE_FILE:
            context.storage_state(path=str(AUTO_STATE_FILE))
        return True

    _open_institutional_modal(page)
    
    # Make sure we are on the IEEE home page (not the help page fallback)
    if "ieeexplore.ieee.org/Xplorehelp" in page.url:
        page.goto(IEEE_HOME, wait_until="commit", timeout=60000)
        page.wait_for_timeout(5000)
        _safe_click_cookie_accept(page)
        # Try the inst-sign-in link directly on home
        try:
            page.locator("a.inst-sign-in").first.click(timeout=5000)
            page.wait_for_timeout(1000)
        except Exception:
            pass
    
    ok = _click_institution_entry(page, credentials["IEEE_INST_NAME"])
    if not ok and PASSPORT_HOST not in page.url:
        logger.error("Failed to trigger institutional redirect to passport.escience.cn")
        return False

    deadline = time.time() + 40
    while time.time() < deadline and PASSPORT_HOST not in page.url:
        page.wait_for_timeout(500)

    if PASSPORT_HOST not in page.url:
        logger.error(f"Expected passport redirect, got {page.url}")
        return False

    # Passport may auto-complete the SSO if credentials are still valid in session cookies.
    # Wait up to 20s: if passport redirects back to IEEE, check access and return.
    # If it stays on passport (needs fresh login), submit the login form.
    sso_deadline = time.time() + 20
    while time.time() < sso_deadline:
        if "ieeexplore.ieee.org" in page.url:
            # SSO auto-completed, check access
            page.wait_for_timeout(5000)
            if has_ieee_institutional_access(page, context):
                context.storage_state(path=str(save_state_path))
                if save_state_path != AUTO_STATE_FILE:
                    context.storage_state(path=str(AUTO_STATE_FILE))
                logger.info("SSO auto-completed, institutional access confirmed.")
                return True
            else:
                # Sometimes it needs a reload after redirect
                page.reload(wait_until="commit")
                page.wait_for_timeout(5000)
                if has_ieee_institutional_access(page, context):
                    context.storage_state(path=str(save_state_path))
                    if save_state_path != AUTO_STATE_FILE:
                        context.storage_state(path=str(AUTO_STATE_FILE))
                    logger.info("SSO auto-completed, institutional access confirmed after reload.")
                    return True
                logger.warning("SSO auto-completed but no institutional access detected; re-trying login flow")
                return False
        page.wait_for_timeout(1000)

    # If we're still on passport, submit the login form
    if PASSPORT_HOST in page.url:
        submitted = _submit_passport_login(
            page,
            credentials["IEEE_INST_USERNAME"],
            credentials["IEEE_INST_PASSWORD"],
        )

        if submitted:
            page.wait_for_load_state("domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)

        try:
            current_title = page.title() if PASSPORT_HOST in page.url else ""
        except Exception as e:
            logger.info(f"Skipping passport title check during navigation race: {e}")
            current_title = ""
        if PASSPORT_HOST in page.url and "Uncaught Exception" in current_title:
            logger.error("Passport login failed with server-side Uncaught Exception")
            return False

    # We expect to return to IEEE and see institutional access.
    if "ieeexplore.ieee.org" not in page.url:
        try:
            page.goto(IEEE_HOME, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
        except PlaywrightTimeoutError:
            pass

    if not has_ieee_institutional_access(page, context):
        logger.error("Institutional login did not produce IEEE access state")
        return False

    context.storage_state(path=str(save_state_path))
    if save_state_path != AUTO_STATE_FILE:
        context.storage_state(path=str(AUTO_STATE_FILE))
    return True
