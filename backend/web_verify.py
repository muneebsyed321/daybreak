"""Independent real-world verification with Playwright. Fresh context: zero cookies, no user session."""
import logging
from pathlib import Path

from playwright.async_api import async_playwright

import db

log = logging.getLogger("daybreak.web")
AUTH_MARKERS = ("/login", "/signin", "auth")


async def check_url(url: str, expected_keyword: str | None, screenshot_name: str) -> dict:
    """HTTP 200 is never treated as proof of public access. Returns every check with its evidence."""
    res = {"url": url, "final_url": None, "http_status": None, "redirect_chain": [], "nav_error": None,
           "password_input": None, "keyword": expected_keyword, "keyword_found": None, "title": None,
           "checks": [], "passed": False, "screenshot": None, "fresh_context": True, "cookies_sent": 0}
    is_file = url.startswith("file://")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await context.new_page()
            response = None
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass
            except Exception as e:
                res["nav_error"] = f"{type(e).__name__}: {str(e).splitlines()[0]}"[:300]

            res["final_url"] = page.url
            if response is not None:
                res["http_status"] = response.status
                chain, req = [], response.request
                while req is not None:
                    prev = req.redirected_from
                    hop = {"url": req.url, "status": None}
                    try:
                        r = await req.response()
                        hop["status"] = r.status if r else None
                    except Exception:
                        pass
                    chain.append(hop)
                    req = prev
                res["redirect_chain"] = list(reversed(chain))

            body_text = ""
            if not res["nav_error"]:
                try:
                    res["password_input"] = await page.locator('input[type="password"]').count() > 0
                    body_text = await page.inner_text("body", timeout=5000)
                    res["title"] = await page.title()
                except Exception as e:
                    res["nav_error"] = f"DOM read failed: {type(e).__name__}"

            try:
                path = db.ARTIFACTS_DIR / screenshot_name
                await page.screenshot(path=str(path), full_page=True)
                res["screenshot"] = f"/artifacts/{screenshot_name}"
            except Exception as e:
                log.warning("screenshot failed: %s", e)
        finally:
            await browser.close()

    status = res["http_status"]
    ok1 = res["nav_error"] is None and (is_file or (status is not None and status < 400))
    res["checks"].append({"name": "HTTP status < 400 and no navigation error", "passed": ok1,
                          "detail": res["nav_error"] or (f"HTTP {status}" if status is not None else "local file, no HTTP")})
    final = (res["final_url"] or "").lower()
    probe = final if not is_file else Path(final).name
    hit = next((m for m in AUTH_MARKERS if m in probe), None)
    res["checks"].append({"name": "Final URL is not a login/signin/auth page", "passed": hit is None,
                          "detail": f"final URL {res['final_url']}" + (f" contains '{hit}'" if hit else "")})
    pw = res["password_input"]
    res["checks"].append({"name": 'No input[type="password"] in rendered DOM', "passed": pw is False,
                          "detail": "password field present (login wall)" if pw else ("none found" if pw is False else "DOM not readable")})
    if expected_keyword:
        res["keyword_found"] = expected_keyword.lower() in body_text.lower()
        res["checks"].append({"name": f'Expected keyword "{expected_keyword}" in rendered body text',
                              "passed": res["keyword_found"],
                              "detail": "found" if res["keyword_found"] else f'keyword "{expected_keyword}" missing from rendered page'})
    res["hard_failure"] = not all(c["passed"] for c in res["checks"])
    # Without a keyword the content cannot be confirmed, so this can never be a full pass.
    res["passed"] = bool(expected_keyword) and not res["hard_failure"]
    return res
