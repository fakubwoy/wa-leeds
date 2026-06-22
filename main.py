import os, re, csv, json, logging, requests, time, random, threading, queue
from dotenv import load_dotenv
load_dotenv()
from io import StringIO
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates", static_folder="static")

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

MIN_FOLLOWERS = 10_000
MAX_FOLLOWERS = 500_000

WA_LINK_RE = re.compile(r"wa\.me/(\d+)|whatsapp\.com/send\?phone=(\d+)", re.I)
PHONE_RE   = re.compile(r"(?<!\d)(\+?91[\s\-]?[6-9]\d{9}|[6-9]\d{9})(?!\d)")
WA_KW      = [
    "whatsapp", "wa.me", "wa:", "wa -", "wa no", "wa num",
    "order on wa", "dm to order", "msg to order", "order via whatsapp",
    "ping on wa", "chat to order", "whatsapp number", "wa only", "wa orders",
    "contact on wa", "reach us on wa", "text on wa", "watsapp",
]

LINK_PAGES    = ["linktree", "linktr.ee", "beacons.ai", "bio.link", "taplink", "solo.to", "allmylinks"]
HEADERS       = {"User-Agent": "Mozilla/5.0 (compatible; LeadBot/1.0)"}
BRAND_SIGNALS = ["official account", "official page", "corporate", "pvt ltd", "private limited",
                 "ltd.", "llp", "group of companies", "franchise", "pan india"]

CITY_PATTERNS = [
    r'\b(hyderabad|secunderabad|warangal|vijayawada|visakhapatnam|vizag|guntur|tirupati|'
    r'nellore|karimnagar|nizamabad|khammam|rajahmundry|kakinada|anantapur|kurnool|'
    r'bangalore|bengaluru|chennai|mumbai|delhi|kolkata|pune|ahmedabad|surat|'
    r'jaipur|lucknow|nagpur|indore|bhopal|coimbatore|kochi|thiruvananthapuram|'
    r'mysore|mangalore|hubli|dharwad|belagavi|shimoga)\b',
    r'📍\s*([A-Za-z\s,]+?)(?:\s*[\|\n|,]|$)',
    r'based in\s+([A-Za-z\s]+?)(?:\s*[\|\n|,]|$)',
    r'located in\s+([A-Za-z\s]+?)(?:\s*[\|\n|,]|$)',
    r'(?:ship|deliver|delivery)\s+(?:from|across)\s+([A-Za-z\s]+?)(?:\s*[\|\n|,]|$)',
]

# ── Tier definitions ──────────────────────────────────────────────────────────
#
#  Tier 1 — Hot leads  : follower range ✓  |  not verified  |  not big brand  |  WA signal ✓  |  geo match ✓
#  Tier 2 — Partial    : follower range ✓  |  WA signal ✓   |  fails geo  (or geo not set)
#  Tier 3 — Weak       : follower range ✓  |  no WA signal  |  not verified  |  not big brand
#  Tier 4 — Out of range: fails follower range  OR  verified  OR  big brand

TIER_LABELS = {
    1: "🔥 Hot Leads — all requirements met",
    2: "⚡ Partial Match — WA signal present, geo missing",
    3: "🔍 Weak Signal — in follower range but no WA",
    4: "⬇ Out of Range — wrong follower count / verified / big brand",
}

# ══════════════════════════════════════════════════════════════════════════════
# Playwright worker thread (single thread owns the browser)
# ══════════════════════════════════════════════════════════════════════════════

_browser_queue: queue.Queue = queue.Queue()
_browser_ready  = threading.Event()
_browser_thread: threading.Thread | None = None


def _browser_worker():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("playwright not installed — run: pip install playwright && playwright install chromium")
        _browser_ready.set()
        return

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-setuid-sandbox",
                  "--disable-dev-shm-usage","--disable-blink-features=AutomationControlled"],
        )
        log.info("Playwright Chromium browser launched.")
        _browser_ready.set()
        while True:
            job = _browser_queue.get()
            if job is None:
                break
            fn, result_box = job
            try:
                result_box["result"] = fn(browser)
            except Exception as exc:
                result_box["error"] = exc
            finally:
                result_box["done"].set()


def _start_browser_thread():
    global _browser_thread
    if _browser_thread is not None:
        return
    _browser_thread = threading.Thread(target=_browser_worker, daemon=True, name="playwright-worker")
    _browser_thread.start()
    _browser_ready.wait(timeout=30)


def browser_run(fn):
    _start_browser_thread()
    box = {"done": threading.Event()}
    _browser_queue.put((fn, box))
    box["done"].wait()
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _make_context(browser):
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        timezone_id="Asia/Kolkata",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "window.chrome={runtime:{}};"
    )
    return ctx


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def extract_city(bio: str, full_name: str) -> str:
    text = f"{bio} {full_name}".lower()
    m = re.search(CITY_PATTERNS[0], text, re.I)
    if m:
        return m.group(1).title()
    for pat in CITY_PATTERNS[1:]:
        m = re.search(pat, f"{bio} {full_name}", re.I)
        if m:
            city = m.group(1).strip().title()
            if 2 < len(city) < 40:
                return city
    return ""


def extract_wa_number(bio: str, extra_text: str = "") -> str:
    combined = f"{bio} {extra_text}"
    for m in WA_LINK_RE.finditer(combined):
        num = (m.group(1) or m.group(2) or "").strip()
        if len(num) >= 10:
            return "+" + num if not num.startswith("+") else num
    phones = PHONE_RE.findall(combined)
    if phones:
        num = re.sub(r"[\s\-]", "", phones[0])
        if not num.startswith("+"):
            num = "+91" + num.lstrip("+").lstrip("91")
            if len(num) > 13:
                num = "+91" + num[-10:]
        return num
    return ""


def has_wa_signal(bio: str, url: str, extra_text: str = "") -> bool:
    combined = f"{bio} {url} {extra_text}".lower()
    if WA_LINK_RE.search(combined): return True
    if PHONE_RE.search(f"{bio} {extra_text}"): return True
    return any(kw in combined for kw in WA_KW)


def is_large_brand(bio: str, followers: int, max_followers: int = MAX_FOLLOWERS) -> bool:
    if followers > max_followers: return True
    return sum(1 for b in BRAND_SIGNALS if b in bio.lower()) >= 2


def fetch_bio_link(url: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=6, allow_redirects=True)
        return r.text[:6000]
    except Exception:
        return ""


def score_profile(profile: dict, geo_filter: str, min_followers: int, max_followers: int,
                  extra_text: str = "") -> tuple[int, list[str], list[str]]:
    """
    Return (tier, met_signals, missing_signals).

    Tier 1 — meets everything
    Tier 2 — follower range + WA signal, but geo fails (or no geo set)
    Tier 3 — follower range only, no WA signal
    Tier 4 — out of range / verified / big brand
    """
    bio       = profile.get("bio", "")
    followers = profile.get("followers", 0)
    url       = profile.get("external_url", "")

    met, missing = [], []

    # ── Follower range ────────────────────────────────────────────────────────
    in_range  = min_followers <= followers <= max_followers
    verified  = profile.get("is_verified", False)
    big_brand = is_large_brand(bio, followers, max_followers)

    if in_range:
        met.append(f"Followers {followers:,} in range")
    else:
        missing.append(f"Followers {followers:,} out of range ({min_followers:,}–{max_followers:,})")

    if verified:
        missing.append("Verified account")
    if big_brand:
        missing.append("Large brand signals")

    if not in_range or verified or big_brand:
        return 4, met, missing

    # ── WA signal ─────────────────────────────────────────────────────────────
    wa = has_wa_signal(bio, url, extra_text)
    if wa:
        met.append("WhatsApp signal detected")
    else:
        missing.append("No WhatsApp signal")

    # ── Geo filter ────────────────────────────────────────────────────────────
    geo_ok = True
    if geo_filter:
        geo_text = f"{bio} {profile.get('full_name','')} {profile.get('username','')}".lower()
        geo_ok   = geo_filter.lower() in geo_text or any(
            w.strip() in geo_text for w in geo_filter.lower().split(",") if w.strip()
        )
        if geo_ok:
            met.append(f"Geo match ({geo_filter})")
        else:
            missing.append(f"No geo match for '{geo_filter}'")

    if wa and geo_ok:
        return 1, met, missing
    if wa and not geo_ok:
        return 2, met, missing
    return 3, met, missing


# ══════════════════════════════════════════════════════════════════════════════
# Gemini validation
# ══════════════════════════════════════════════════════════════════════════════

def gemini_validate(profile: dict, niche: str, extra_text: str = "") -> dict:
    if not GEMINI_KEY:
        return {"valid": True, "confidence": "medium", "city": "", "reason": "No Gemini key", "category": ""}

    bio       = profile.get("bio", "")
    full_name = profile.get("full_name", "")
    username  = profile.get("username", "")
    followers = profile.get("followers", 0)
    url       = profile.get("external_url", "")

    prompt = f"""You are a lead qualification AI for a WhatsApp outreach tool.

Analyze this Instagram profile and determine if it's a genuine small business that:
1. Sells products/services primarily through WhatsApp (not a proper e-commerce website)
2. Is relevant to the niche: "{niche}"
3. Is a real small/micro business (not a large brand, reseller aggregator, or influencer)

Profile:
- Username: @{username}
- Name: {full_name}
- Followers: {followers:,}
- Bio: {bio}
- External URL: {url}
- Bio link page content (if any): {extra_text[:500] if extra_text else 'None'}

Respond ONLY in this exact JSON format (no markdown, no explanation):
{{
  "valid": true/false,
  "confidence": "high"/"medium"/"low",
  "city": "extracted city name or empty string",
  "category": "detected business category (e.g. Food & Sweets, Clothing, Jewellery, etc.)",
  "reason": "one sentence explaining your decision"
}}

Rules:
- valid=true only if it matches the niche AND shows WhatsApp ordering signals
- high confidence = clear WA number/link + niche match
- medium = some WA signals + probable niche match
- low = weak signals but possible
- city = extract from bio/name if mentioned (city name only, not full address)
- If not relevant to niche "{niche}", set valid=false"""

    try:
        resp = requests.post(
            GEMINI_URL, params={"key": GEMINI_KEY},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 200, "responseMimeType": "application/json"}
            },
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        text = re.sub(r"^```json\s*", "", text); text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except Exception as e:
        log.warning(f"Gemini failed for @{username}: {e}")
        return {"valid": True, "confidence": "low", "city": "", "reason": f"Gemini error: {e}", "category": ""}


# ══════════════════════════════════════════════════════════════════════════════
# Instagram scrapers
# ══════════════════════════════════════════════════════════════════════════════

def _parse_usernames_from_content(content: str) -> set[str]:
    found = set()
    for m in re.finditer(r'"username"\s*:\s*"([A-Za-z0-9_.]{1,30})"', content):
        found.add(m.group(1))
    return found


def collect_usernames_from_hashtag(tag: str, limit: int) -> set[str]:
    def _job(browser):
        usernames: set[str] = set()
        ctx  = _make_context(browser)
        page = ctx.new_page()
        try:
            captured: list[dict] = []
            def on_response(response):
                try:
                    if "/api/v1/tags/" in response.url and response.status == 200:
                        captured.append(response.json())
                except Exception: pass
            page.on("response", on_response)
            page.goto(f"https://www.instagram.com/explore/tags/{tag}/",
                      wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_timeout(4000)
            for body in captured:
                sections = (body.get("data", {}).get("recent", {}).get("sections", [])
                            or body.get("sections", []))
                for section in sections:
                    for item in section.get("layout_content", {}).get("medias", []):
                        media = item.get("media", {})
                        uname = (media.get("user", {}).get("username", "")
                                 or media.get("owner", {}).get("username", ""))
                        if uname: usernames.add(uname)
            if not usernames:
                log.info(f"#{tag}: XHR intercept got nothing, trying DOM scrape…")
                usernames.update(_parse_usernames_from_content(page.content()))
        finally:
            page.close(); ctx.close()
        log.info(f"#{tag} → {len(usernames)} usernames")
        return set(list(usernames)[:limit])

    try:
        return browser_run(_job)
    except Exception as e:
        log.warning(f"Hashtag scrape failed for #{tag}: {e}")
        return set()


def collect_usernames_from_search(query: str, limit: int) -> set[str]:
    def _job(browser):
        usernames: set[str] = set()
        ctx  = _make_context(browser)
        page = ctx.new_page()
        try:
            captured: list[dict] = []
            def on_response(response):
                try:
                    if "fbsearch/topsearch" in response.url and response.status == 200:
                        captured.append(response.json())
                except Exception: pass
            page.on("response", on_response)
            encoded = requests.utils.quote(query)
            page.goto(f"https://www.instagram.com/explore/search/keyword/?q={encoded}",
                      wait_until="domcontentloaded", timeout=15_000)
            page.wait_for_timeout(3000)
            for body in captured:
                for item in body.get("users", []):
                    uname = item.get("user", {}).get("username", "")
                    if uname: usernames.add(uname)
            if not usernames:
                usernames.update(_parse_usernames_from_content(page.content()))
        finally:
            page.close(); ctx.close()
        log.info(f"search:'{query}' → {len(usernames)} usernames")
        return set(list(usernames)[:limit])

    try:
        return browser_run(_job)
    except Exception as e:
        log.warning(f"Search failed for '{query}': {e}")
        return set()


def fetch_profile(username: str) -> dict | None:
    def _job(browser):
        ctx  = _make_context(browser)
        page = ctx.new_page()
        try:
            captured: list[dict] = []
            def on_response(response):
                try:
                    if "web_profile_info" in response.url and response.status == 200:
                        captured.append(response.json())
                except Exception: pass
            page.on("response", on_response)
            page.goto(f"https://www.instagram.com/{username}/",
                      wait_until="domcontentloaded", timeout=18_000)
            page.wait_for_timeout(3000)
            user: dict | None = None
            for body in captured:
                user = (body.get("data", {}).get("user")
                        or body.get("graphql", {}).get("user")
                        or body.get("user"))
                if user: break
            if not user:
                content = page.content()
                if '"biography"' in content:
                    for m in re.finditer(r'\{[^{}]{200,}\}', content):
                        try:
                            d = json.loads(m.group(0))
                            if d.get("username") and d.get("biography") is not None:
                                user = d; break
                        except Exception: pass
            if not user: return None
            followers  = user.get("edge_followed_by", {}).get("count") or user.get("follower_count", 0)
            following  = user.get("edge_follow", {}).get("count") or user.get("following_count", 0)
            post_count = user.get("edge_owner_to_timeline_media", {}).get("count") or user.get("media_count", 0)
            return {
                "username":     user.get("username", username),
                "full_name":    user.get("full_name", ""),
                "followers":    int(followers or 0),
                "following":    int(following or 0),
                "post_count":   int(post_count or 0),
                "bio":          user.get("biography", ""),
                "external_url": user.get("external_url", "") or "",
                "is_verified":  bool(user.get("is_verified", False)),
                "is_business":  bool(user.get("is_business_account", False) or user.get("is_professional_account", False)),
                "ig_category":  user.get("category_name", "") or user.get("category", "") or "",
            }
        finally:
            page.close(); ctx.close()

    try:
        p = browser_run(_job)
        if p: log.info(f"@{username}: {p['followers']:,} followers")
        else: log.warning(f"@{username}: no data")
        return p
    except Exception as e:
        log.warning(f"fetch_profile @{username}: {e}")
        return None


def build_search_queries(niche: str, geo_filter: str, explicit: list[str]) -> list[str]:
    if explicit: return explicit[:10]
    queries, geo_words = [], ([w.strip() for w in geo_filter.split(",") if w.strip()] if geo_filter else [])
    if niche and geo_words:
        for w in geo_words[:5]: queries.append(f"{niche} {w}")
    elif niche:
        queries.append(niche)
    return queries[:10]


# ══════════════════════════════════════════════════════════════════════════════
# Pipeline
# ══════════════════════════════════════════════════════════════════════════════

def run_pipeline(hashtags, niche, geo_filter, limit, debug_mode=False,
                 search_keywords=None, min_followers=MIN_FOLLOWERS, max_followers=MAX_FOLLOWERS):

    try:
        _start_browser_thread()
    except Exception as e:
        yield {"type": "error", "message": str(e)}; return

    if not hashtags and not search_keywords and not (niche and geo_filter):
        yield {"type": "error", "message": "No hashtags or search keywords provided."}; return

    all_usernames: set[str] = set()
    done_tags = []

    # ── Phase 1a: hashtag scraping ────────────────────────────────────────────
    if hashtags:
        per_tag = max(30, (limit * 8) // max(len(hashtags), 1))
        yield {"type": "progress", "stage": "hashtag_scan",
               "detail": f"Scanning {len(hashtags)} hashtag(s) via browser…",
               "done_tags": [], "total_tags": len(hashtags)}
        for tag in hashtags:
            yield {"type": "progress", "stage": "hashtag_scan", "detail": f"Scanning #{tag}…",
                   "done_tags": list(done_tags), "total_tags": len(hashtags)}
            users = collect_usernames_from_hashtag(tag, per_tag)
            all_usernames.update(users)
            done_tags.append(tag)
            yield {"type": "progress", "stage": "hashtag_scan",
                   "detail": f"#{tag} → {len(users)} accounts found",
                   "done_tags": list(done_tags), "total_tags": len(hashtags),
                   "total_users": len(all_usernames)}
            time.sleep(random.uniform(2, 4))

    # ── Phase 1b: keyword search ──────────────────────────────────────────────
    queries = build_search_queries(niche, geo_filter, search_keywords or [])
    if queries:
        yield {"type": "progress", "stage": "hashtag_scan",
               "detail": f"Running {len(queries)} profile search quer{'y' if len(queries)==1 else 'ies'}…"}
        for q in queries:
            users  = collect_usernames_from_search(q, 50)
            before = len(all_usernames)
            all_usernames.update(users)
            yield {"type": "progress", "stage": "hashtag_scan",
                   "detail": f"search:\"{q}\" → {len(users)} profiles ({len(all_usernames)-before} new)",
                   "total_users": len(all_usernames)}
            time.sleep(random.uniform(2, 4))

    if not all_usernames:
        yield {"type": "error", "message": "No accounts found. Try different hashtags or keywords."}; return

    # ── Phase 2: fetch full profiles ──────────────────────────────────────────
    yield {"type": "progress", "stage": "profiles",
           "detail": f"Fetching full profiles for {len(all_usernames)} accounts…"}

    username_list = list(all_usernames)
    all_profiles: dict[str, dict] = {}
    fetched = 0

    for uname in username_list:
        profile = fetch_profile(uname)
        fetched += 1
        if profile:
            all_profiles[uname] = profile
        if fetched % 5 == 0 or fetched == len(username_list):
            yield {"type": "progress", "stage": "profiles",
                   "detail": f"Profiles fetched: {fetched} / {len(username_list)}",
                   "fetched": fetched, "total": len(username_list)}
        time.sleep(random.uniform(1.5, 3.0))

    if not all_profiles:
        yield {"type": "error", "message": "Profile fetch returned no data. Instagram may be throttling — try again in a few minutes."}
        return

    # ── Phase 3: score & tier every profile ───────────────────────────────────
    yield {"type": "progress", "stage": "filtering",
           "detail": f"Scoring and tiering {len(all_profiles)} profiles…"}

    tiered: dict[int, list] = {1: [], 2: [], 3: [], 4: []}

    for uname, profile in all_profiles.items():
        bio = profile.get("bio", "")
        url = profile.get("external_url", "")
        extra_text = ""
        if url and any(lp in url.lower() for lp in LINK_PAGES):
            extra_text = fetch_bio_link(url)

        tier, met, missing = score_profile(profile, geo_filter, min_followers, max_followers, extra_text)
        tiered[tier].append((profile, extra_text, met, missing))

    # Emit tier counts summary
    yield {"type": "tier_summary",
           "counts": {str(t): len(v) for t, v in tiered.items()},
           "total": len(all_profiles)}

    # ── Phase 4: Gemini validate tier-1 (and tier-2 in debug mode) ───────────
    hot_candidates = tiered[1] if not debug_mode else tiered[1] + tiered[2]

    yield {"type": "progress", "stage": "gemini",
           "detail": f"{len(hot_candidates)} hot candidates → Gemini validation…",
           "total_candidates": len(hot_candidates)}

    exported  = 0
    validated = 0
    gemini_results: dict[str, dict] = {}   # username → gem result

    def validate_one(args):
        profile, extra_text, met, missing = args
        if debug_mode:
            return profile, extra_text, met, missing, {
                "valid": True, "confidence": "n/a", "city": "", "category": "",
                "reason": "DEBUG — shown regardless of signals",
            }
        return profile, extra_text, met, missing, gemini_validate(profile, niche, extra_text)

    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(validate_one, c): c for c in hot_candidates[:limit]}
        for fut in as_completed(futs):
            profile, extra_text, met, missing, gem = fut.result()
            validated += 1
            username  = profile["username"]
            gemini_results[username] = gem

            yield {"type": "progress", "stage": "gemini",   # keep progress bar moving
                   "detail": f"Validated {validated}/{len(hot_candidates)}",
                   "validated": validated, "total_candidates": len(hot_candidates)}

    # ── Phase 5: emit all profiles grouped by tier ────────────────────────────
    row_n = 0
    for tier in [1, 2, 3, 4]:
        if not tiered[tier]:
            continue

        yield {"type": "tier_header", "tier": tier, "label": TIER_LABELS[tier],
               "count": len(tiered[tier])}

        for profile, extra_text, met, missing in tiered[tier]:
            row_n += 1
            username  = profile["username"]
            bio       = profile.get("bio", "")
            url       = profile.get("external_url", "")
            gem       = gemini_results.get(username, {})

            wa_number = extract_wa_number(bio, extra_text)
            city      = gem.get("city") or extract_city(bio, profile.get("full_name", ""))
            category  = gem.get("category") or profile.get("ig_category") or "—"
            confidence = gem.get("confidence", "—") if gem else "—"
            gemini_valid = gem.get("valid", None) if gem else None

            yield {
                "type":     "profile",
                "tier":     tier,
                "row_n":    row_n,
                "profile": {
                    "business_name":     profile.get("full_name") or username,
                    "page_name":         f"@{username}",
                    "business_category": category,
                    "city":              city,
                    "whatsapp_number":   wa_number,
                    "confidence":        confidence,
                    "gemini_valid":      gemini_valid,
                    "followers":         profile.get("followers", 0),
                    "following":         profile.get("following", 0),
                    "total_posts":       profile.get("post_count", 0),
                    "is_business_acct":  "Yes" if profile.get("is_business") else "No",
                    "bio":               bio[:250],
                    "website":           url,
                    "ig_url":            f"https://instagram.com/{username}",
                    "gemini_reason":     gem.get("reason", ""),
                    "met":               met,
                    "missing":           missing,
                    "scraped_at":        datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                }
            }

            if tier == 1 and gemini_valid:
                exported += 1

    yield {"type": "done", "total": exported, "total_profiles": len(all_profiles),
           "tier_counts": {str(t): len(v) for t, v in tiered.items()}}


# ══════════════════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/scan", methods=["POST"])
def scan():
    data            = request.json or {}
    raw_tags        = data.get("hashtags", "")
    niche           = data.get("niche", "").strip() or "small business selling on WhatsApp"
    geo_filter      = data.get("geo_filter", "").strip()
    limit           = max(10, min(int(data.get("limit", 50)), 200))
    debug_mode      = bool(data.get("debug_mode", False))
    raw_search      = data.get("search_keywords", "")
    search_keywords = [s.strip() for s in raw_search.split(",") if s.strip()]
    min_followers   = max(0, int(data.get("min_followers", MIN_FOLLOWERS)))
    max_followers   = max(min_followers, int(data.get("max_followers", MAX_FOLLOWERS)))
    hashtags        = [t.strip().lstrip("#") for t in raw_tags.split(",") if t.strip()]

    def generate():
        for item in run_pipeline(hashtags, niche, geo_filter, limit, debug_mode,
                                 search_keywords, min_followers, max_followers):
            yield f"data: {json.dumps(item)}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

@app.route("/export", methods=["POST"])
def export_csv():
    leads = request.json or []
    if not leads:
        return jsonify({"error": "No leads to export."}), 400

    si     = StringIO()
    fields = ["business_name","page_name","business_category","city","whatsapp_number",
              "confidence","gemini_valid","tier","followers","following","total_posts",
              "is_business_acct","bio","website","ig_url","met","missing","gemini_reason","scraped_at"]
    writer = csv.DictWriter(si, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for lead in leads:
        row = dict(lead)
        row["met"]     = " | ".join(lead.get("met", []))
        row["missing"] = " | ".join(lead.get("missing", []))
        writer.writerow(row)

    fname = f"wa_leads_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(si.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})

@app.route("/ig_challenge", methods=["POST"])
def ig_challenge():
    return jsonify({"status": "no challenge — running in headless browser mode"})

@app.route("/ig_status")
def ig_status():
    return jsonify({"logged_in": True, "challenge_code_pending": False,
                    "browser_ready": _browser_thread is not None and _browser_thread.is_alive(),
                    "mode": "playwright_headless_no_login"})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "mode": "playwright_headless_no_login",
                    "browser_ready": _browser_thread is not None and _browser_thread.is_alive(),
                    "gemini_configured": bool(GEMINI_KEY)})

@app.route("/logout", methods=["POST"])
def logout():
    return jsonify({"status": "no session — running in headless browser mode"})

if __name__ == "__main__":
    _start_browser_thread()
    app.run(host="0.0.0.0", port=5000, debug=False)