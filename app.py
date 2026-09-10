import asyncio
import json
import os
import re
import shutil
import time
import urllib.parse
import warnings
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from playwright.async_api import async_playwright, Browser, Page

VERSION = "1.2.4"

warnings.filterwarnings("ignore", message="Unverified HTTPS request")
warnings.filterwarnings("ignore", category=Warning, module="httpx")

ROOT = Path(__file__).parent
INDEX = ROOT / "index.html"
CONFIG_FILE = ROOT / "update.json"
HEADLESS = os.environ.get("HEADLESS", "1") != "0"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

UPDATE_FILES = (
    "app.py", "index.html", "requirements.txt", "start.bat",
    "privacy.html", "terms.html", "help.html",
)

state: dict = {"pw": None, "browser": None}


def load_config() -> dict:
    cfg = {
        "repo": os.environ.get("GITHUB_REPO", ""),
        "branch": os.environ.get("GITHUB_BRANCH", "main"),
    }
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg["repo"] = data.get("repo", cfg["repo"])
            cfg["branch"] = data.get("branch", cfg["branch"])
        except Exception:
            pass
    return cfg


def save_config(cfg: dict):
    CONFIG_FILE.write_text(
        json.dumps({"repo": cfg.get("repo", ""), "branch": cfg.get("branch", "main")}, indent=2),
        encoding="utf-8",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["pw"] = await async_playwright().start()
    state["browser"] = await state["pw"].chromium.launch(headless=HEADLESS)
    try:
        yield
    finally:
        try:
            await state["browser"].close()
        except Exception:
            pass
        try:
            await state["pw"].stop()
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX.read_text(encoding="utf-8")


@app.get("/logo.png")
async def logo_png():
    return FileResponse(ROOT / "logo.png", media_type="image/png")


@app.get("/logo-w.png")
async def logo_white_png():
    return FileResponse(ROOT / "logo-w.png", media_type="image/png")


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(ROOT / "logo.png", media_type="image/png")


@app.get("/privacy", response_class=HTMLResponse)
async def privacy():
    return (ROOT / "privacy.html").read_text(encoding="utf-8")


@app.get("/terms", response_class=HTMLResponse)
async def terms():
    return (ROOT / "terms.html").read_text(encoding="utf-8")


@app.get("/help", response_class=HTMLResponse)
async def help_page():
    return (ROOT / "help.html").read_text(encoding="utf-8")


@app.get("/health")
async def health():
    cfg = load_config()
    return {
        "ok": True,
        "version": VERSION,
        "browser_ready": state.get("browser") is not None,
        "github_repo": cfg["repo"],
        "github_branch": cfg["branch"],
    }


def sse(d: dict) -> str:
    return f"data: {json.dumps(d, ensure_ascii=False)}\n\n"


async def safe_text(page: Page, selector: str, timeout: int = 1000) -> str:
    try:
        return (await page.locator(selector).first.inner_text(timeout=timeout)).strip()
    except Exception:
        return ""


async def safe_attr(page: Page, selector: str, attr: str, timeout: int = 1000) -> str:
    try:
        return (await page.locator(selector).first.get_attribute(attr, timeout=timeout)) or ""
    except Exception:
        return ""


CONSENT_LABELS = ("Accept all", "I agree", "Reject all", "Alle akzeptieren", "Alles akzeptieren")


async def accept_consent(page: Page):
    for label in CONSENT_LABELS:
        try:
            await page.locator(f'button:has-text("{label}")').first.click(timeout=1200)
            await page.wait_for_timeout(800)
            return
        except Exception:
            continue
    for frame in page.frames:
        if "consent" not in (frame.url or "").lower():
            continue
        for label in CONSENT_LABELS:
            try:
                await frame.locator(f'button:has-text("{label}")').first.click(timeout=1200)
                await page.wait_for_timeout(800)
                return
            except Exception:
                continue


# ---------------------------------------------------------------- Email finder

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MAILTO_RE = re.compile(r'mailto:([^"\'?>\s]+)', re.IGNORECASE)

EMAIL_BAD_SUBSTR = (
    "@sentry.io", "@wixpress.com", "@example.com", "@example.org",
    "@yourdomain", "@domain.com", "@email.com", "@company.com",
    "@test.com", "@2x.png", "@3x.png", ".png@", ".jpg@", ".jpeg@",
    ".gif@", ".webp@", ".svg@", "u003e", "u003c",
)
EMAIL_BAD_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico")
CONTACT_PATHS = ("", "/contact", "/contact-us", "/contacts", "/about", "/about-us", "/kontakt")


def find_emails_in_text(text: str) -> set[str]:
    if not text:
        return set()
    deob = re.sub(r"\s*[\(\[]\s*at\s*[\)\]]\s*", "@", text, flags=re.I)
    deob = re.sub(r"\s*[\(\[]\s*dot\s*[\)\]]\s*", ".", deob, flags=re.I)
    found: set[str] = set()
    candidates = set(EMAIL_RE.findall(deob))
    # mailto: values are often percent-encoded (e.g. "mailto:%20info@x.com");
    # decode them so "%20info@x.com" doesn't survive as a bogus address.
    mailtos = " ".join(urllib.parse.unquote(m) for m in MAILTO_RE.findall(deob))
    candidates |= set(EMAIL_RE.findall(mailtos))
    for raw in candidates:
        e = raw.lower().strip(".,;:")
        if "%" in e:  # leftover percent-encoding is never a real local part
            continue
        if any(b in e for b in EMAIL_BAD_SUBSTR):
            continue
        if e.endswith(EMAIL_BAD_SUFFIX):
            continue
        if len(e) > 80 or len(e) < 6:
            continue
        found.add(e)
    return found


async def fetch_text(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url)
        if r.status_code == 200:
            ct = r.headers.get("content-type", "").lower()
            if "html" in ct or "text" in ct or ct == "":
                return r.text
    except Exception:
        return ""
    return ""


async def extract_emails(website_url: str, timeout: float = 7.0) -> list[str]:
    if not website_url:
        return []
    parsed = urlparse(website_url)
    if not parsed.scheme:
        website_url = "https://" + website_url
        parsed = urlparse(website_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    urls: list[str] = []
    for p in CONTACT_PATHS:
        u = website_url if p == "" else urljoin(base + "/", p.lstrip("/"))
        if u not in urls:
            urls.append(u)

    found: set[str] = set()
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            verify=False,
            headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.8"},
        ) as client:
            results = await asyncio.gather(
                *[fetch_text(client, u) for u in urls], return_exceptions=True
            )
        for text in results:
            if isinstance(text, str) and text:
                found |= find_emails_in_text(text)
    except Exception:
        pass

    own_domain = parsed.netloc.replace("www.", "").lower()
    ranked = sorted(
        found,
        key=lambda e: (own_domain not in e, "info" not in e and "contact" not in e, e),
    )
    return ranked[:5]


# ---------------------------------------------------------------- Scraper

FIELDS = [
    "name", "category", "rating", "reviews", "price", "address", "phone",
    "emails", "website", "hours", "status", "plus_code", "latitude", "longitude",
    "place_id", "place_url", "description", "services", "menu_url", "image_url",
]


def status_from_hours(hours: str) -> str:
    """Derive open/closed status from the hours summary line.

    Order matters: "Opens 9 AM"/"Opens soon" means currently CLOSED and must be
    checked before the "open" prefix (which it also matches); and "closes soon"
    means currently OPEN.
    """
    low = (hours or "").lower()
    if "permanently closed" in low:
        return "Permanently closed"
    if "temporarily closed" in low:
        return "Temporarily closed"
    if "open 24" in low:
        return "Open 24 hours"
    if low.startswith("closed"):
        return "Closed"
    if low.startswith("opens "):
        return "Closed"
    if low.startswith("open") or "closes soon" in low or low.startswith("closes "):
        return "Open"
    return ""


async def extract_detail(page: Page, fallback_name: str = "") -> dict:
    item = {f: "" for f in FIELDS}
    item["place_url"] = page.url

    # Coordinates: Google embeds them in `data=!3d{lat}!4d{lng}`; the older
    # `@lat,lng,zoom` form is only in the zoomed-out search URL.
    m = re.search(r"!3d(-?[\d.]+)!4d(-?[\d.]+)", page.url)
    if not m:
        m = re.search(r"@(-?[\d.]+),(-?[\d.]+)", page.url)
    if m:
        item["latitude"] = m.group(1)
        item["longitude"] = m.group(2)

    m = re.search(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)", page.url)
    if m:
        item["place_id"] = m.group(1)

    name = await safe_text(page, "h1", 4000)
    item["name"] = name or fallback_name

    rating_block = await safe_text(page, "div.F7nice")
    m = re.search(r"([\d.]+)", rating_block)
    if m:
        item["rating"] = m.group(1)
    m = re.search(r"\(([\d,]+)\)", rating_block)
    if m:
        item["reviews"] = m.group(1).replace(",", "")

    item["category"] = (
        await safe_text(page, "button.DkEaL")
        or await safe_text(page, 'button[jsaction*="category"]')
    )

    item["address"] = (
        await safe_text(page, 'button[data-item-id="address"] div.Io6YTe')
        or await safe_text(page, 'button[data-item-id="address"]')
    )

    item["phone"] = (
        await safe_text(page, 'button[data-item-id^="phone"] div.Io6YTe')
        or await safe_text(page, 'button[data-item-id^="phone:tel"]')
    )

    href = await safe_attr(page, 'a[data-item-id="authority"]', "href")
    if not href:
        href = await safe_attr(page, 'a[aria-label*="Website"]', "href")
    item["website"] = href

    item["plus_code"] = await safe_text(page, 'button[data-item-id="oloc"] div.Io6YTe')

    # Google's current markup exposes the "Open · Closes 11 PM" summary on an
    # element whose jsaction references the open-hours dropdown; the older
    # button[data-item-id="oh"] no longer exists. Keep both as fallbacks.
    hours_short = (
        await safe_text(page, '[jsaction*="openhours"]')
        or await safe_text(page, 'button[data-item-id="oh"]')
    )
    hours_long = await safe_text(page, 'div[aria-label*="Hours"]')
    hours_text = hours_short or hours_long or ""
    # Google wraps the hours text with Material Icons glyphs (Private Use Area
    # U+E000..U+F8FF) and a trailing "See more hours" disclosure link. Strip
    # the icons, collapse whitespace (including U+202F narrow no-break space),
    # then drop the suffix.
    hours_text = re.sub(r"[-]", "", hours_text)
    hours_text = re.sub(r"\s+", " ", hours_text)
    hours_text = re.sub(
        r"\s*(see more hours|suggest new hours|updated by this business|hours might differ).*$",
        "", hours_text, flags=re.I | re.DOTALL,
    )
    item["hours"] = hours_text.strip(" ·•-")

    item["status"] = status_from_hours(item["hours"])

    item["price"] = await safe_text(page, 'span[aria-label*="Price"]')
    item["description"] = await safe_text(page, "div.PYvSYb")

    try:
        chips = await page.locator(
            '[aria-label^="Has "], [aria-label^="Serves "], [aria-label^="No "], [aria-label^="Offers "]'
        ).all()
        seen: list[str] = []
        for c in chips[:25]:
            try:
                lab = await c.get_attribute("aria-label")
                if lab and lab not in seen:
                    seen.append(lab)
            except Exception:
                continue
        item["services"] = "; ".join(seen)
    except Exception:
        pass

    item["menu_url"] = await safe_attr(page, 'a[data-item-id="menu"]', "href")

    item["image_url"] = (
        await safe_attr(page, 'button[jsaction*="heroHeaderImage"] img', "src")
        or await safe_attr(page, "div.RZ66Rb img", "src")
        or await safe_attr(page, 'img[decoding="async"]', "src")
    )

    for k, v in list(item.items()):
        if isinstance(v, str) and "\n" in v:
            item[k] = " ".join(v.split())

    return item


async def scrape_query(query: str, max_results: int, do_emails: bool):
    browser: Browser = state["browser"]
    if not browser:
        yield sse({"type": "error", "message": "Browser not initialized — restart start.bat."})
        return

    ctx = await browser.new_context(
        user_agent=UA, locale="en-US", viewport={"width": 1366, "height": 900}
    )
    page = await ctx.new_page()
    try:
        url = f"https://www.google.com/maps/search/{urllib.parse.quote(query)}/?hl=en"
        yield sse({"type": "log", "message": f"Opening {url}"})
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await accept_consent(page)

        try:
            await page.wait_for_selector('div[role="feed"]', timeout=15000)
        except Exception:
            yield sse({
                "type": "error",
                "message": "Results feed not found — Google may be showing a CAPTCHA. "
                           "Stop the server, set HEADLESS=0, restart, and solve it manually.",
            })
            return

        feed = page.locator('div[role="feed"]')
        prev = -1
        stable = 0
        while True:
            count = await page.locator("a.hfpxzc").count()
            yield sse({"type": "log", "message": f"Loaded {count} listings in sidebar..."})
            if count >= max_results:
                break
            try:
                if await page.locator("p.fontBodyMedium >> text=/reached the end/i").count() > 0:
                    yield sse({"type": "log", "message": "Reached end of Google Maps results."})
                    break
            except Exception:
                pass
            try:
                await feed.evaluate("(el) => el.scrollBy(0, el.scrollHeight)")
            except Exception:
                pass
            await page.wait_for_timeout(1500)
            if count == prev:
                stable += 1
                if stable >= 4:
                    yield sse({"type": "log", "message": "No more new results loading."})
                    break
            else:
                stable = 0
            prev = count

        all_links = await page.locator("a.hfpxzc").all()
        fid_re = re.compile(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)")
        seen_fids: set[str] = set()
        targets: list[tuple[str, str]] = []
        dup_count = 0
        for a in all_links:
            if len(targets) >= max_results:
                break
            try:
                href = await a.get_attribute("href")
                name = await a.get_attribute("aria-label")
                if not href:
                    continue
                m = fid_re.search(href)
                fid = m.group(1) if m else href
                if fid in seen_fids:
                    dup_count += 1
                    continue
                seen_fids.add(fid)
                targets.append((name or "", href))
            except Exception:
                continue

        if dup_count:
            yield sse({"type": "log", "message": f"Skipped {dup_count} duplicate listings."})

        total = len(targets)
        yield sse({"type": "total", "total": total})

        for i, (name, href) in enumerate(targets):
            try:
                yield sse({"type": "log", "message": f"[{i+1}/{total}] {name}"})
                await page.goto(href, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(700)
                try:
                    await page.wait_for_selector("h1", timeout=8000)
                except Exception:
                    pass
                item = await extract_detail(page, fallback_name=name)

                if do_emails and item.get("website"):
                    try:
                        emails = await extract_emails(item["website"])
                        if emails:
                            item["emails"] = "; ".join(emails)
                            yield sse({"type": "log", "message": f"  found {len(emails)} email(s)"})
                    except Exception as e:
                        yield sse({"type": "log", "message": f"  email lookup failed: {e}"})

                yield sse({"type": "item", "index": i, "item": item})
            except Exception as e:
                yield sse({"type": "log", "message": f"Error on {name}: {e}"})

        yield sse({"type": "done"})
    finally:
        try:
            await ctx.close()
        except Exception:
            pass


@app.get("/scrape")
async def scrape_endpoint(query: str, max_results: int = 100, emails: int = 1):
    async def gen():
        async for chunk in scrape_query(query, max_results, do_emails=bool(emails)):
            yield chunk

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/fields")
async def fields():
    return {"fields": FIELDS}


# ---------------------------------------------------------------- Auto-update

def _ver_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v or ""))
    except Exception:
        return ()


def is_newer(latest: str, current: str) -> bool:
    a, b = _ver_tuple(latest), _ver_tuple(current)
    if a and b:
        return a > b
    return bool(latest) and latest != current


@app.get("/update/check")
async def update_check():
    cfg = load_config()
    repo = cfg["repo"]
    branch = cfg["branch"]
    if not repo:
        return {"enabled": False, "current": VERSION, "message": "Not configured. Click ⚙ Settings to add a GitHub repo."}

    api_release = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(api_release, headers={"Accept": "application/vnd.github+json"})
            if r.status_code == 200:
                d = r.json()
                latest = (d.get("tag_name") or "").lstrip("v")
                return {
                    "enabled": True,
                    "current": VERSION,
                    "latest": latest,
                    "has_update": is_newer(latest, VERSION),
                    "release_url": d.get("html_url", ""),
                    "release_name": d.get("name", "") or latest,
                    "release_notes": (d.get("body", "") or "")[:600],
                    "via": "release",
                }
            # No releases: compare HEAD commit on branch
            r2 = await client.get(f"https://api.github.com/repos/{repo}/commits/{branch}")
            if r2.status_code == 200:
                sha = (r2.json().get("sha") or "")[:7]
                stamp_file = ROOT / ".last_update_sha"
                last = stamp_file.read_text().strip() if stamp_file.exists() else ""
                return {
                    "enabled": True,
                    "current": VERSION + (f" @ {last}" if last else ""),
                    "latest": sha,
                    "has_update": bool(sha) and sha != last,
                    "release_url": f"https://github.com/{repo}/commits/{branch}",
                    "release_name": f"latest commit on {branch}",
                    "via": "branch",
                }
            return {"enabled": True, "current": VERSION, "error": f"GitHub API returned {r.status_code}. Check repo path."}
    except Exception as e:
        return {"enabled": True, "current": VERSION, "error": f"Could not reach GitHub: {e}"}


@app.post("/update/apply")
async def update_apply():
    cfg = load_config()
    repo = cfg["repo"]
    branch = cfg["branch"]
    if not repo:
        return JSONResponse({"ok": False, "error": "GitHub repo not configured."}, status_code=400)

    backup_dir = ROOT / f".backup-{int(time.time())}"
    backup_dir.mkdir(exist_ok=True)

    updated: list[str] = []
    errors: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for fname in UPDATE_FILES:
                raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{fname}"
                try:
                    r = await client.get(raw_url)
                    if r.status_code == 200 and r.content:
                        local = ROOT / fname
                        if local.exists():
                            shutil.copy2(local, backup_dir / fname)
                        local.write_bytes(r.content)
                        updated.append(fname)
                    else:
                        errors.append(f"{fname}: HTTP {r.status_code}")
                except Exception as e:
                    errors.append(f"{fname}: {e}")

            # Stamp the latest sha if available so /update/check tracks it
            try:
                r3 = await client.get(f"https://api.github.com/repos/{repo}/commits/{branch}")
                if r3.status_code == 200:
                    sha = (r3.json().get("sha") or "")[:7]
                    (ROOT / ".last_update_sha").write_text(sha)
            except Exception:
                pass
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "backup": str(backup_dir)}, status_code=500)

    if not updated:
        return JSONResponse({"ok": False, "error": "; ".join(errors) or "Nothing updated."}, status_code=500)

    return {
        "ok": True,
        "updated": updated,
        "errors": errors,
        "backup": str(backup_dir),
        "restart_message": "Stop and re-run start.bat to load the new code.",
    }


@app.get("/update/config")
async def update_config_get():
    return load_config()


@app.post("/update/config")
async def update_config_set(payload: dict):
    cfg = {
        "repo": (payload.get("repo") or "").strip(),
        "branch": (payload.get("branch") or "main").strip() or "main",
    }
    if cfg["repo"] and not re.match(r"^[\w.-]+/[\w.-]+$", cfg["repo"]):
        return JSONResponse(
            {"ok": False, "error": "Repo must be 'owner/name' format."},
            status_code=400,
        )
    save_config(cfg)
    return {"ok": True, **cfg}
