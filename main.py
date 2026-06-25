import os, re, csv, json, logging, requests, time, random, threading, queue, sqlite3
from contextlib import contextmanager
from dotenv import load_dotenv
load_dotenv()
from io import StringIO
from datetime import datetime, timezone
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

TIER_LABELS = {
    1: "🔥 Hot Leads — all requirements met",
    2: "⚡ Partial Match — WA signal present, geo missing",
    3: "🔍 Weak Signal — in follower range but no WA",
    4: "⬇ Out of Range — wrong follower count / verified / big brand",
}

# ── Business niche presets ─────────────────────────────────────────────────────
NICHE_PRESETS = {
    "sweets_pickles": {
        "label": "🍬 Sweets & Pickles",
        "niche": "homemade sweets, pickles, achaar, mithai, traditional food",
        "hashtags": "hyderabadsweets,telugusweets,andhrasweets,homemadesweets,mithai,traditionalsweets,andhrapickles,teluguachaar,homemadepickles,avakaya,gongura,pachadi,handmadesweets,desisweets",
        "geo": "hyderabad,andhra,telangana",
    },
    "aquaculture": {
        "label": "🐟 Aquaculture & Sea Fish Export",
        "niche": "fish export, seafood, aquaculture, fresh fish delivery, prawns",
        "hashtags": "seafoodexport,freshfish,aquaculture,fishfarm,prawnexport,hyderabadfishmarket,andhraseafood,vizagfish,seafooddelivery,freshseafood,fishsupplier",
        "geo": "hyderabad,vizag,kakinada,andhra,telangana",
    },
    "travel_agents": {
        "label": "✈️ Travel Agents",
        "niche": "travel agent, tour packages, holiday packages, visa, flight booking",
        "hashtags": "travelagent,tourpackages,holidaypackage,hyderabadtravel,indiatravel,touroperator,visaconsultant,travelbusiness,tourplanner,travelagency",
        "geo": "hyderabad,telangana,andhra",
    },
    "beauty_products": {
        "label": "💄 Beauty, Hair & Body Care",
        "niche": "beauty products, hair care, body care, skincare, herbal beauty",
        "hashtags": "hyderabadskincare,naturalskincare,organicbeauty,handmadesoap,haircare,bodycare,beautyproducts,herbalskincare,naturalbeauty,skincareroutine,hairgrowth,organicskincare",
        "geo": "hyderabad,telangana,andhra",
    },
    "cakes_bakers": {
        "label": "🎂 Cakes & Dessert Bakers",
        "niche": "custom cakes, home baker, desserts, cupcakes, birthday cakes",
        "hashtags": "hyderabadbaker,customcakes,homebaker,cakedesign,birthdaycake,weddingcake,designercakes,fondantcakes,bakery,desserts,cupcakes,hyderabadcakes,chocolatecakes",
        "geo": "hyderabad,telangana,andhra",
    },
    "gift_shops": {
        "label": "🎁 Personalised Gift Shops",
        "niche": "personalised gifts, custom gifts, gifting, engraved gifts",
        "hashtags": "personalisedgifts,customgifts,giftshop,uniquegifts,gifting,customizedgifts,corporategifts,handmadegifts,specialgifts,birthdaygifts,weddingfavors",
        "geo": "hyderabad,telangana,andhra",
    },
    "event_planners": {
        "label": "🎪 Event Planners & Decorators",
        "niche": "event planner, wedding decorator, party decoration, event management",
        "hashtags": "hyderabadevents,eventplanner,weddingdecor,partydecoration,eventmanagement,weddingevents,babyshower,housewarming,birthdayparty,eventdecor,weddingplanner",
        "geo": "hyderabad,telangana,andhra",
    },
    "home_interior": {
        "label": "🏠 Home Interior & Furniture",
        "niche": "home interior, furniture design, home decor, interior designer",
        "hashtags": "hyderabadinterior,homeinterior,furnituredesign,homedecor,interiordesign,homefurnishing,customfurniture,interiordecor,moderninterior,homedesign,officefurniture",
        "geo": "hyderabad,telangana,andhra",
    },
    "dairy_products": {
        "label": "🥛 Homemade Dairy Products",
        "niche": "homemade dairy, ghee, paneer, curd, butter, milk products",
        "hashtags": "homemadeghee,desi ghee,pureghee,homemadepaneer,dairyproducts,freshpaneer,homemadebutter,organicghee,hyderabadfarm,farmproducts,puremilk",
        "geo": "hyderabad,telangana,andhra",
    },
    "homemade_cosmetics": {
        "label": "🧴 Homemade Cosmetics & Soaps",
        "niche": "handmade soaps, homemade cosmetics, natural shampoo, herbal products",
        "hashtags": "handmadesoap,naturalsoap,organicsoap,homemadecosmetics,herbalshampo,naturalshampoo,herbalcosmetics,diybeauty,chemicalfree,naturalproducts,handcraftedsoap",
        "geo": "hyderabad,telangana,andhra",
    },
    "therapists": {
        "label": "🩺 Therapists & Online Doctors",
        "niche": "online doctor, therapist, dietician, health consultant, wellness",
        "hashtags": "onlinedoctor,therapist,mentalhealth,dietician,nutritionist,healthcoach,wellnesscoach,onlineconsultation,psychologist,lifecoach,healthconsultant",
        "geo": "hyderabad,telangana,andhra,india",
    },
    "fitness_trainers": {
        "label": "💪 Gym Trainers, MUA & Dieticians",
        "niche": "personal trainer, makeup artist, dietician, fitness coach, gym",
        "hashtags": "personaltrainer,makeupartist,gymtrainer,fitnesscoach,dietician,nutritionist,mua,makeupindia,hyderabadmakeup,fitnessmotivation,gymmotivation,makeupbride",
        "geo": "hyderabad,telangana,andhra",
    },
}

# ══════════════════════════════════════════════════════════════════════════════
# SQLite CRM Database
# ══════════════════════════════════════════════════════════════════════════════

DB_PATH = os.environ.get("DB_PATH", "crm.db")

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS leads (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT UNIQUE NOT NULL,
            business_name   TEXT,
            category        TEXT,
            business_type   TEXT,
            city            TEXT,
            state           TEXT,
            whatsapp_number TEXT,
            followers       INTEGER,
            bio             TEXT,
            ig_url          TEXT,
            website         TEXT,
            tier            INTEGER,
            confidence      TEXT,
            niche_preset    TEXT,
            sells_on_whatsapp INTEGER DEFAULT 0,
            ordering_method TEXT,
            products_services TEXT,
            languages       TEXT,
            gemini_reason   TEXT,
            added_at        TEXT DEFAULT (datetime('now')),
            outreach_status TEXT DEFAULT 'not_contacted',
            outreach_sent_at TEXT,
            outreach_notes  TEXT,
            responded       INTEGER DEFAULT 0,
            responded_at    TEXT,
            response_notes  TEXT,
            deal_status     TEXT DEFAULT 'none'
        );

        CREATE TABLE IF NOT EXISTS outreach_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id     INTEGER REFERENCES leads(id),
            username    TEXT,
            action      TEXT,
            notes       TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        -- Add new columns if upgrading from older schema
        CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(outreach_status);
        CREATE INDEX IF NOT EXISTS idx_leads_preset ON leads(niche_preset);
    """)
    # Migrate: add missing columns to existing DB without breaking it
    existing = {row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    new_cols = {
        "business_type":    "TEXT",
        "state":            "TEXT",
        "sells_on_whatsapp":"INTEGER DEFAULT 0",
        "ordering_method":  "TEXT",
        "products_services":"TEXT",
        "languages":        "TEXT",
        "gemini_reason":    "TEXT",
    }
    for col, typedef in new_cols.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {typedef}")
            log.info(f"DB migration: added column leads.{col}")
    conn.commit()
    conn.close()
    log.info("CRM DB initialised at %s", DB_PATH)

init_db()

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
    bio       = profile.get("bio", "")
    followers = profile.get("followers", 0)
    url       = profile.get("external_url", "")

    met, missing = [], []

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

    wa = has_wa_signal(bio, url, extra_text)
    if wa:
        met.append("WhatsApp signal detected")
    else:
        missing.append("No WhatsApp signal")

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
# Gemini — full profile parse + qualification (runs on every profile)
# ══════════════════════════════════════════════════════════════════════════════

_GEMINI_EMPTY = {
    "valid": False,
    "confidence": "low",
    "city": "",
    "state": "",
    "country": "India",
    "whatsapp_number": "",
    "whatsapp_signal": False,
    "category": "",
    "business_type": "",
    "sells_on_whatsapp": False,
    "is_small_business": True,
    "is_large_brand": False,
    "is_influencer": False,
    "ordering_method": "",
    "products_or_services": "",
    "languages": "",
    "reason": "Gemini not configured",
    "gemini_ran": False,
}

def gemini_parse_profile(profile: dict, niche: str, extra_text: str = "") -> dict:
    """
    Single Gemini call per profile that does everything:
      - Data extraction: city, state, phone/WA number, products, ordering method, languages
      - Classification: business type, category, is_influencer, is_large_brand
      - Qualification: sells on WA, niche match, confidence, validity
    Returns a rich dict. Falls back gracefully if no API key or on error.
    """
    if not GEMINI_KEY:
        return {**_GEMINI_EMPTY, "reason": "No Gemini API key configured", "gemini_ran": False}

    username  = profile.get("username", "")
    full_name = profile.get("full_name", "")
    followers = profile.get("followers", 0)
    following = profile.get("following", 0)
    bio       = profile.get("bio", "")
    url       = profile.get("external_url", "")
    ig_cat    = profile.get("ig_category", "")
    is_biz    = profile.get("is_business", False)
    post_cnt  = profile.get("post_count", 0)

    prompt = f"""You are a data extraction and lead qualification AI for a WhatsApp business outreach tool targeting small Indian businesses.

Your job is to FULLY parse this Instagram profile and return a structured JSON object with every field filled as accurately as possible.

=== PROFILE DATA ===
Username:       @{username}
Display name:   {full_name}
Followers:      {followers:,}
Following:      {following:,}
Posts:          {post_cnt}
IG Category:    {ig_cat or 'not set'}
Is Business:    {is_biz}
Bio:
{bio}

External URL:   {url or 'none'}
Bio-link page content (if bio links to linktree/beacons/etc):
{extra_text[:800] if extra_text else 'none'}

=== TARGET NICHE ===
{niche}

=== INSTRUCTIONS ===
Return ONLY a valid JSON object with exactly these fields (no markdown, no extra text):

{{
  "valid": <true if this is a genuine lead for the niche, false otherwise>,
  "confidence": <"high" | "medium" | "low">,

  "city": "<city extracted from bio/name/url — just the city name, e.g. Hyderabad>",
  "state": "<Indian state, e.g. Telangana, Andhra Pradesh — infer from city if not explicit>",
  "country": "<country, default India>",

  "whatsapp_number": "<full phone number with country code if found, e.g. +919876543210 — check bio, wa.me links, bio-link page. Empty string if not found>",
  "whatsapp_signal": <true if any WhatsApp contact signal found — number, wa.me link, 'order on WA', 'DM to order', etc.>,

  "category": "<specific business category, e.g. Homemade Sweets, Fish Export, Travel Agent, Beauty Products, Custom Cakes, Gift Shop, Event Decorator, Interior Designer, Dairy Products, Handmade Soaps, Online Therapist, Personal Trainer>",
  "business_type": "<one of: product_seller | service_provider | both | influencer | brand | unknown>",

  "sells_on_whatsapp": <true if they primarily take orders or inquiries via WhatsApp>,
  "is_small_business": <true if this is a small/micro business run by an individual or small team>,
  "is_large_brand": <true if this appears to be a large brand, chain, franchise, or corporate>,
  "is_influencer": <true if this is primarily a content creator / influencer with no clear product/service>,

  "ordering_method": "<how customers order — e.g. WhatsApp, DM, website, phone, in-store, unknown>",
  "products_or_services": "<brief comma-separated list of what they sell, e.g. 'avakaya pickle, gongura pickle, homemade chutneys'>",
  "languages": "<detected language(s) in bio, e.g. English, Telugu, Hindi>",

  "reason": "<one clear sentence explaining why valid is true or false>"
}}

=== EXTRACTION RULES ===
- whatsapp_number: scan the ENTIRE bio and bio-link content for wa.me/XXXXXXXXXX links or Indian mobile numbers (10 digits starting with 6-9, or with +91 prefix). Format as +91XXXXXXXXXX.
- city: look for 📍 pin emoji, "based in", "located in", city names, area names. Indian cities only.
- state: infer from city (e.g. Hyderabad → Telangana, Chennai → Tamil Nadu, Mumbai → Maharashtra).
- is_influencer: true ONLY if they have no product/service to sell — pure content, reels, memes, fashion blogging with no own product.
- is_large_brand: true if followers > 200000 OR bio contains corporate signals (pvt ltd, llp, franchise, pan india, official page).
- valid: true ONLY if (a) niche matches AND (b) whatsapp_signal is true AND (c) is_small_business is true AND (d) NOT is_influencer.
- confidence: high = WA number found + clear niche match; medium = WA signal (no number) + probable match; low = weak signals."""

    try:
        resp = requests.post(
            GEMINI_URL,
            params={"key": GEMINI_KEY},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 400,
                    "responseMimeType": "application/json",
                },
            },
            timeout=20,
        )
        resp.raise_for_status()
        raw  = resp.json()
        text = raw["candidates"][0]["content"]["parts"][0]["text"].strip()
        text = re.sub(r"^```json\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        parsed = json.loads(text)
        parsed["gemini_ran"] = True
        # Normalise types in case Gemini returns strings for bools
        for bfield in ("valid","whatsapp_signal","sells_on_whatsapp","is_small_business",
                       "is_large_brand","is_influencer"):
            if bfield in parsed and isinstance(parsed[bfield], str):
                parsed[bfield] = parsed[bfield].lower() == "true"
        log.info(f"Gemini @{username}: valid={parsed.get('valid')} conf={parsed.get('confidence')} wa={parsed.get('whatsapp_number') or 'none'} city={parsed.get('city') or 'none'}")
        return parsed
    except Exception as e:
        log.warning(f"Gemini failed for @{username}: {e}")
        return {**_GEMINI_EMPTY, "reason": f"Gemini error: {e}", "gemini_ran": False}


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


def _normalise_user(user: dict, username: str) -> dict | None:
    """Convert any raw Instagram user blob into our standard profile dict."""
    if not user:
        return None
    followers  = (user.get("edge_followed_by", {}).get("count")
                  or user.get("follower_count")
                  or user.get("followers_count", 0))
    following  = (user.get("edge_follow", {}).get("count")
                  or user.get("following_count", 0))
    post_count = (user.get("edge_owner_to_timeline_media", {}).get("count")
                  or user.get("media_count", 0))
    bio        = user.get("biography") or user.get("bio", "") or ""
    return {
        "username":     user.get("username", username),
        "full_name":    user.get("full_name", ""),
        "followers":    int(followers or 0),
        "following":    int(following or 0),
        "post_count":   int(post_count or 0),
        "bio":          bio,
        "external_url": user.get("external_url", "") or "",
        "is_verified":  bool(user.get("is_verified", False)),
        "is_business":  bool(user.get("is_business_account", False)
                             or user.get("is_professional_account", False)),
        "ig_category":  user.get("category_name", "") or user.get("category", "") or "",
    }


def _extract_user_from_html(content: str, username: str) -> dict | None:
    """
    Multi-pattern HTML extraction — tries every known Instagram embedding format.
    Instagram embeds profile data in several ways; we try them all.
    """
    # Strategy 1: window.__additionalDataLoaded or window._sharedData
    for pat in [
        r'window\.__additionalDataLoaded\s*\(\s*[\'"][^\'"]*[\'"]\s*,\s*(\{.*?\})\s*\)',
        r'window\._sharedData\s*=\s*(\{.*?\})\s*;',
        r'<script type="application/json" data-sj>(\{.*?\})</script>',
        r'<script type="application/json" data-content-type="media-symbol[^"]*">(\{.*?\})</script>',
    ]:
        for m in re.finditer(pat, content, re.S):
            try:
                blob = json.loads(m.group(1))
                # Navigate to user inside sharedData
                user = (blob.get("entry_data", {}).get("ProfilePage", [{}])[0]
                            .get("graphql", {}).get("user")
                        or blob.get("graphql", {}).get("user")
                        or blob.get("data", {}).get("user")
                        or blob.get("user"))
                if user and user.get("biography") is not None:
                    return _normalise_user(user, username)
            except Exception:
                pass

    # Strategy 2: bare JSON blobs containing biography (what the old code did, but broader regex)
    for m in re.finditer(r'(\{"[^"]*biography[^"]*".*?\})', content, re.S):
        try:
            d = json.loads(m.group(1))
            if d.get("username") and d.get("biography") is not None:
                return _normalise_user(d, username)
        except Exception:
            pass

    # Strategy 3: JSON-LD (some profile pages emit schema.org Person/ProfilePage)
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', content, re.S):
        try:
            ld = json.loads(m.group(1))
            if isinstance(ld, list):
                ld = ld[0]
            if ld.get("@type") in ("Person", "ProfilePage"):
                # Map schema.org → our format
                return {
                    "username":     username,
                    "full_name":    ld.get("name", ""),
                    "followers":    0,
                    "following":    0,
                    "post_count":   0,
                    "bio":          ld.get("description", ""),
                    "external_url": ld.get("url", ""),
                    "is_verified":  False,
                    "is_business":  False,
                    "ig_category":  "",
                }
        except Exception:
            pass

    # Strategy 4: scrape meta tags as last resort (gives name + description only)
    name_m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', content)
    desc_m = re.search(r'<meta\s+(?:name="description"|property="og:description")\s+content="([^"]+)"', content)
    if name_m and desc_m:
        desc = desc_m.group(1)
        # Instagram description format: "Xk Followers, Y Following, Z Posts - Bio"
        foll_m = re.search(r'([\d,.]+[kKmM]?)\s+Followers', desc, re.I)
        foll = 0
        if foll_m:
            raw = foll_m.group(1).replace(",", "")
            try:
                if raw[-1].lower() == 'k': foll = int(float(raw[:-1]) * 1000)
                elif raw[-1].lower() == 'm': foll = int(float(raw[:-1]) * 1_000_000)
                else: foll = int(raw)
            except Exception: pass
        bio_part = re.sub(r'^.*?Posts\s*[-–]\s*', '', desc, flags=re.S).strip()
        log.info(f"@{username}: meta-tag fallback — {foll:,} followers")
        return {
            "username":     username,
            "full_name":    name_m.group(1).split(" (@")[0].strip(),
            "followers":    foll,
            "following":    0,
            "post_count":   0,
            "bio":          bio_part[:500],
            "external_url": "",
            "is_verified":  False,
            "is_business":  False,
            "ig_category":  "",
        }

    return None


def fetch_profile(username: str) -> dict | None:
    """
    Multi-strategy Instagram profile fetcher.

    Strategy order (fastest/most reliable first):
      1. XHR intercept: web_profile_info  (IG private API — best data, often blocked)
      2. XHR intercept: graphql/query     (older IG API endpoint)
      3. HTML extraction: window.__additionalDataLoaded / _sharedData / JSON blobs
      4. Meta-tag fallback: og:title + og:description (always present, follower count only)
    """
    def _job(browser):
        ctx  = _make_context(browser)
        page = ctx.new_page()
        try:
            captured_xhr: list[dict] = []

            def on_response(response):
                try:
                    url = response.url
                    if response.status != 200:
                        return
                    if ("web_profile_info" in url
                            or "graphql/query" in url
                            or "/api/v1/users/" in url):
                        data = response.json()
                        captured_xhr.append(data)
                except Exception:
                    pass

            page.on("response", on_response)

            page.goto(
                f"https://www.instagram.com/{username}/",
                wait_until="domcontentloaded",
                timeout=20_000,
            )
            # Give XHR a bit of time; also lets lazy JS embeds run
            page.wait_for_timeout(3500)

            # ── Strategy 1 & 2: XHR data ──────────────────────────────────
            for body in captured_xhr:
                user = (body.get("data", {}).get("user")
                        or body.get("graphql", {}).get("user")
                        or body.get("user"))
                if not user:
                    # /api/v1/users/{id}/info/ format
                    user = body.get("user") or body
                result = _normalise_user(user, username)
                if result and result.get("followers", 0) > 0:
                    return result

            # ── Strategy 3 & 4: HTML extraction ───────────────────────────
            content = page.content()
            result  = _extract_user_from_html(content, username)
            return result

        finally:
            page.close()
            ctx.close()

    try:
        p = browser_run(_job)
        if p and p.get("followers", 0) > 0:
            log.info(f"@{username}: {p['followers']:,} followers | bio: {len(p.get('bio',''))} chars")
        elif p:
            log.info(f"@{username}: fetched (0 followers — meta fallback)")
        else:
            log.warning(f"@{username}: no data from any strategy")
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

def score_profile_with_gemini(profile: dict, gem: dict, geo_filter: str,
                              min_followers: int, max_followers: int) -> tuple[int, list[str], list[str]]:
    """
    Tier a profile using Gemini-enriched data instead of regex signals.
    Tier 1 — hot : follower range ✓ | small biz ✓ | WA signal ✓ | niche valid ✓ | geo ✓ (or no geo set)
    Tier 2 — partial : follower range ✓ | WA signal ✓ | niche valid ✓ | geo fails
    Tier 3 — weak : follower range ✓ | no WA signal OR low confidence | not disqualified
    Tier 4 — out : follower range ✗ | large brand | influencer | is_verified
    """
    followers  = profile.get("followers", 0)
    verified   = profile.get("is_verified", False)
    # 0 = meta-tag fallback (follower count unknown) — don't disqualify on range
    in_range   = (followers == 0) or (min_followers <= followers <= max_followers)
    large      = gem.get("is_large_brand", False) or (followers > max_followers and followers > 0)
    influencer = gem.get("is_influencer", False)

    met, missing = [], []

    if followers == 0:
        met.append("Followers: unknown (meta-fallback)")
    elif in_range:
        met.append(f"Followers {followers:,} in range")
    else:
        missing.append(f"Followers {followers:,} out of range ({min_followers:,}–{max_followers:,})")
    if verified:
        missing.append("Verified account")
    if large:
        missing.append("Large brand / chain")
    if influencer:
        missing.append("Influencer — no own product")

    if not in_range or verified or large or influencer:
        return 4, met, missing

    # WA signal from Gemini
    wa = gem.get("whatsapp_signal", False) or bool(gem.get("whatsapp_number", ""))
    valid = gem.get("valid", False)
    conf  = gem.get("confidence", "low")

    if wa:
        wa_num = gem.get("whatsapp_number", "")
        met.append(f"WA signal{' — ' + wa_num if wa_num else ''}")
    else:
        missing.append("No WhatsApp signal found by AI")

    if valid:
        met.append(f"Niche match ({conf} confidence)")
    else:
        missing.append(f"Niche mismatch: {gem.get('reason','')[:60]}")

    # Geo check — use AI-extracted city/state first, fall back to regex
    geo_ok = True
    if geo_filter:
        ai_loc = f"{gem.get('city','')} {gem.get('state','')}".lower()
        bio_text = f"{profile.get('bio','')} {profile.get('full_name','')} {profile.get('username','')}".lower()
        combined_loc = f"{ai_loc} {bio_text}"
        geo_ok = any(w.strip() in combined_loc for w in geo_filter.lower().split(",") if w.strip())
        if geo_ok:
            loc_label = gem.get("city") or gem.get("state") or geo_filter
            met.append(f"Geo match — {loc_label}")
        else:
            missing.append(f"No geo match for '{geo_filter}'")

    if wa and valid and geo_ok:
        return 1, met, missing
    if wa and valid and not geo_ok:
        return 2, met, missing
    return 3, met, missing


def run_pipeline(hashtags, niche, geo_filter, limit, debug_mode=False,
                 search_keywords=None, min_followers=MIN_FOLLOWERS, max_followers=MAX_FOLLOWERS,
                 niche_preset=None):

    try:
        _start_browser_thread()
    except Exception as e:
        yield {"type": "error", "message": str(e)}; return

    if not hashtags and not search_keywords and not (niche and geo_filter):
        yield {"type": "error", "message": "No hashtags or search keywords provided."}; return

    # ── Phase 1a: hashtag scraping ─────────────────────────────────────────────
    all_usernames: set[str] = set()
    done_tags = []

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

    # ── Phase 1b: keyword search ───────────────────────────────────────────────
    queries = build_search_queries(niche, geo_filter, search_keywords or [])
    if queries:
        yield {"type": "progress", "stage": "hashtag_scan",
               "detail": f"Running {len(queries)} keyword search quer{'y' if len(queries)==1 else 'ies'}…"}
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

    # ── Phase 2: fetch full profiles ───────────────────────────────────────────
    yield {"type": "progress", "stage": "profiles",
           "detail": f"Fetching full profiles for {len(all_usernames)} accounts…"}

    username_list = list(all_usernames)
    all_profiles: dict[str, dict] = {}
    fetched = 0

    for uname in username_list:
        profile = fetch_profile(uname)
        fetched += 1
        if profile:
            foll = profile.get("followers", 0)
            # Keep if: followers in range, OR we got 0 (meta-fallback — let Gemini decide)
            # Drop only if we have a definitive over/under count
            if foll > 0 and foll < min_followers * 0.5:
                log.info(f"@{uname}: {foll:,} followers — pre-filtered (too few)")
            elif foll > max_followers * 2:
                log.info(f"@{uname}: {foll:,} followers — pre-filtered (too many)")
            else:
                all_profiles[uname] = profile
        if fetched % 5 == 0 or fetched == len(username_list):
            yield {"type": "progress", "stage": "profiles",
                   "detail": f"Profiles fetched: {fetched} / {len(username_list)} ({len(all_profiles)} passed filter)",
                   "fetched": fetched, "total": len(username_list)}
        time.sleep(random.uniform(1.5, 3.0))

    if not all_profiles:
        yield {"type": "error", "message": "Profile fetch returned no data. Instagram may be throttling — try again in a few minutes."}
        return

    # ── Phase 3: Gemini parses EVERY profile in parallel ──────────────────────
    # This is the new early-AI step. Gemini extracts city, WA number, category,
    # products, business type, and qualifies the lead — all in one call per profile.
    total_to_parse = len(all_profiles)
    yield {"type": "progress", "stage": "gemini",
           "detail": f"AI parsing {total_to_parse} profiles — extracting WA numbers, cities, categories…",
           "total_candidates": total_to_parse, "validated": 0}

    gem_results: dict[str, dict] = {}   # username → gemini output
    parsed_count = 0

    def _parse_one(uname_profile):
        uname, prof = uname_profile
        url = prof.get("external_url", "")
        extra = ""
        if url and any(lp in url.lower() for lp in LINK_PAGES):
            extra = fetch_bio_link(url)
        if debug_mode:
            # In debug, skip Gemini but still fetch bio-link
            return uname, prof, extra, {
                **_GEMINI_EMPTY,
                "valid": True, "confidence": "n/a",
                "whatsapp_signal": has_wa_signal(prof.get("bio",""), url, extra),
                "whatsapp_number": extract_wa_number(prof.get("bio",""), extra),
                "city": extract_city(prof.get("bio",""), prof.get("full_name","")),
                "category": prof.get("ig_category",""),
                "reason": "DEBUG mode — Gemini skipped",
                "gemini_ran": False,
                "is_small_business": True,
                "is_large_brand": is_large_brand(prof.get("bio",""), prof.get("followers",0)),
                "is_influencer": False,
            }
        gem = gemini_parse_profile(prof, niche, extra)
        return uname, prof, extra, gem

    # Use 8 workers — Gemini 2.5 Flash handles concurrent requests well
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_parse_one, item): item for item in all_profiles.items()}
        for fut in as_completed(futs):
            uname, prof, extra, gem = fut.result()
            gem_results[uname] = (prof, extra, gem)
            parsed_count += 1
            yield {"type": "progress", "stage": "gemini",
                   "detail": f"AI parsed {parsed_count} / {total_to_parse} profiles…",
                   "validated": parsed_count, "total_candidates": total_to_parse}

    # ── Phase 4: Score & tier using Gemini data ────────────────────────────────
    yield {"type": "progress", "stage": "filtering",
           "detail": f"Tiering {total_to_parse} AI-enriched profiles…"}

    tiered: dict[int, list] = {1: [], 2: [], 3: [], 4: []}

    for uname, (prof, extra, gem) in gem_results.items():
        tier, met, missing = score_profile_with_gemini(prof, gem, geo_filter, min_followers, max_followers)
        tiered[tier].append((prof, extra, gem, met, missing))

    yield {"type": "tier_summary",
           "counts": {str(t): len(v) for t, v in tiered.items()},
           "total": total_to_parse}

    # ── Phase 5: emit all profiles grouped by tier ─────────────────────────────
    row_n    = 0
    exported = 0

    for tier in [1, 2, 3, 4]:
        if not tiered[tier]:
            continue

        yield {"type": "tier_header", "tier": tier, "label": TIER_LABELS[tier],
               "count": len(tiered[tier])}

        for prof, extra, gem, met, missing in tiered[tier]:
            row_n   += 1
            username = prof["username"]
            bio      = prof.get("bio", "")
            url      = prof.get("external_url", "")

            # Prefer Gemini-extracted fields, fall back to regex
            wa_number = gem.get("whatsapp_number") or extract_wa_number(bio, extra)
            city      = gem.get("city") or extract_city(bio, prof.get("full_name", ""))
            state     = gem.get("state", "")
            category  = gem.get("category") or prof.get("ig_category") or "—"
            biz_type  = gem.get("business_type", "")
            products  = gem.get("products_or_services", "")
            languages = gem.get("languages", "")
            ordering  = gem.get("ordering_method", "")
            confidence= gem.get("confidence", "—")
            gemini_valid = gem.get("valid", False)
            gemini_ran   = gem.get("gemini_ran", False)

            profile_data = {
                "business_name":     prof.get("full_name") or username,
                "page_name":         f"@{username}",
                "business_category": category,
                "business_type":     biz_type,
                "city":              city,
                "state":             state,
                "whatsapp_number":   wa_number,
                "confidence":        confidence,
                "gemini_valid":      gemini_valid,
                "gemini_ran":        gemini_ran,
                "sells_on_whatsapp": gem.get("sells_on_whatsapp", False),
                "ordering_method":   ordering,
                "products_services": products,
                "languages":         languages,
                "followers":         prof.get("followers", 0),
                "following":         prof.get("following", 0),
                "total_posts":       prof.get("post_count", 0),
                "is_business_acct":  "Yes" if prof.get("is_business") else "No",
                "bio":               bio[:250],
                "website":           url,
                "ig_url":            f"https://instagram.com/{username}",
                "gemini_reason":     gem.get("reason", ""),
                "met":               met,
                "missing":           missing,
                "scraped_at":        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "niche_preset":      niche_preset or "",
            }

            # Auto-save tier-1 validated leads to CRM
            if tier == 1 and gemini_valid:
                try:
                    conn = get_db()
                    conn.execute("""
                        INSERT OR IGNORE INTO leads
                          (username, business_name, category, business_type,
                           city, state, whatsapp_number,
                           followers, bio, ig_url, website,
                           tier, confidence, niche_preset,
                           sells_on_whatsapp, ordering_method, products_services,
                           languages, gemini_reason)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (username, profile_data["business_name"], category, biz_type,
                          city, state, wa_number,
                          prof.get("followers", 0), bio[:500],
                          f"https://instagram.com/{username}", url,
                          tier, confidence, niche_preset or "",
                          1 if gem.get("sells_on_whatsapp") else 0,
                          ordering, products, languages,
                          gem.get("reason", "")))
                    conn.commit()
                    conn.close()
                except Exception as e:
                    log.warning(f"CRM save failed for @{username}: {e}")

            yield {
                "type":    "profile",
                "tier":    tier,
                "row_n":   row_n,
                "profile": profile_data,
            }

            if tier == 1 and gemini_valid:
                exported += 1

    yield {"type": "done", "total": exported, "total_profiles": total_to_parse,
           "tier_counts": {str(t): len(v) for t, v in tiered.items()}}


# ══════════════════════════════════════════════════════════════════════════════
# Routes — Scan
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/niche_presets")
def niche_presets():
    return jsonify(NICHE_PRESETS)

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
    niche_preset    = data.get("niche_preset", "")

    def generate():
        for item in run_pipeline(hashtags, niche, geo_filter, limit, debug_mode,
                                 search_keywords, min_followers, max_followers, niche_preset):
            yield f"data: {json.dumps(item)}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

@app.route("/export", methods=["POST"])
def export_csv():
    leads = request.json or []
    if not leads:
        return jsonify({"error": "No leads to export."}), 400

    si     = StringIO()
    fields = ["business_name","page_name","business_category","business_type",
              "city","state","whatsapp_number","confidence","gemini_valid","gemini_ran",
              "sells_on_whatsapp","ordering_method","products_services","languages",
              "tier","followers","following","total_posts",
              "is_business_acct","bio","website","ig_url",
              "met","missing","gemini_reason","scraped_at"]
    writer = csv.DictWriter(si, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for lead in leads:
        row = dict(lead)
        row["met"]     = " | ".join(lead.get("met", []))
        row["missing"] = " | ".join(lead.get("missing", []))
        writer.writerow(row)

    fname = f"wa_leads_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(si.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


# ══════════════════════════════════════════════════════════════════════════════
# Routes — CRM
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/crm/leads")
def crm_leads():
    status   = request.args.get("status", "")
    preset   = request.args.get("preset", "")
    search   = request.args.get("q", "")
    page     = max(1, int(request.args.get("page", 1)))
    per_page = 50

    conn  = get_db()
    where = ["1=1"]
    params = []
    if status:
        where.append("outreach_status = ?"); params.append(status)
    if preset:
        where.append("niche_preset = ?"); params.append(preset)
    if search:
        where.append("(username LIKE ? OR business_name LIKE ? OR city LIKE ? OR whatsapp_number LIKE ?)")
        params += [f"%{search}%"]*4

    clause = " AND ".join(where)
    total  = conn.execute(f"SELECT COUNT(*) FROM leads WHERE {clause}", params).fetchone()[0]
    rows   = conn.execute(
        f"SELECT * FROM leads WHERE {clause} ORDER BY added_at DESC LIMIT ? OFFSET ?",
        params + [per_page, (page-1)*per_page]
    ).fetchall()

    # Status counts
    status_counts = {r["outreach_status"]: r["cnt"] for r in
                     conn.execute("SELECT outreach_status, COUNT(*) as cnt FROM leads GROUP BY outreach_status").fetchall()}
    conn.close()

    return jsonify({
        "leads": [dict(r) for r in rows],
        "total": total,
        "page":  page,
        "pages": (total + per_page - 1) // per_page,
        "status_counts": status_counts,
    })


@app.route("/crm/leads/<int:lead_id>", methods=["PATCH"])
def crm_update_lead(lead_id):
    data   = request.json or {}
    fields = {}
    allowed = ["outreach_status","outreach_notes","response_notes","deal_status","responded"]
    for k in allowed:
        if k in data:
            fields[k] = data[k]

    if "outreach_status" in fields and fields["outreach_status"] != "not_contacted":
        fields["outreach_sent_at"] = datetime.now(timezone.utc).isoformat()
    if "responded" in fields and fields["responded"]:
        fields["responded_at"] = datetime.now(timezone.utc).isoformat()

    if not fields:
        return jsonify({"error": "Nothing to update"}), 400

    set_clause = ", ".join(f"{k}=?" for k in fields)
    vals       = list(fields.values()) + [lead_id]
    conn = get_db()
    conn.execute(f"UPDATE leads SET {set_clause} WHERE id=?", vals)

    # Log the action
    action = data.get("outreach_status") or ("responded" if "responded" in data else "updated")
    notes  = data.get("outreach_notes") or data.get("response_notes") or ""
    username = conn.execute("SELECT username FROM leads WHERE id=?", [lead_id]).fetchone()
    uname = username["username"] if username else ""
    conn.execute("INSERT INTO outreach_log (lead_id, username, action, notes) VALUES (?,?,?,?)",
                 [lead_id, uname, action, notes])
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/crm/leads/bulk", methods=["POST"])
def crm_bulk_update():
    """Bulk mark leads as contacted"""
    data    = request.json or {}
    ids     = data.get("ids", [])
    status  = data.get("status", "contacted")
    notes   = data.get("notes", "")
    if not ids:
        return jsonify({"error": "No IDs"}), 400

    conn = get_db()
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE leads SET outreach_status=?, outreach_notes=?, outreach_sent_at=? WHERE id IN ({placeholders})",
        [status, notes, datetime.now(timezone.utc).isoformat()] + ids
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "updated": len(ids)})


@app.route("/crm/stats")
def crm_stats():
    conn = get_db()
    total        = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    by_status    = {r["outreach_status"]: r["cnt"] for r in
                    conn.execute("SELECT outreach_status, COUNT(*) as cnt FROM leads GROUP BY outreach_status").fetchall()}
    by_preset    = {r["niche_preset"]: r["cnt"] for r in
                    conn.execute("SELECT niche_preset, COUNT(*) as cnt FROM leads GROUP BY niche_preset").fetchall()}
    responded    = conn.execute("SELECT COUNT(*) FROM leads WHERE responded=1").fetchone()[0]
    deals        = conn.execute("SELECT COUNT(*) FROM leads WHERE deal_status='closed'").fetchone()[0]
    conn.close()
    return jsonify({"total": total, "by_status": by_status, "by_preset": by_preset,
                    "responded": responded, "deals": deals})


@app.route("/crm/export")
def crm_export():
    conn  = get_db()
    rows  = conn.execute("SELECT * FROM leads ORDER BY added_at DESC").fetchall()
    conn.close()

    si     = StringIO()
    fields = ["id","username","business_name","category","city","whatsapp_number","followers",
              "ig_url","tier","confidence","niche_preset","added_at",
              "outreach_status","outreach_sent_at","outreach_notes",
              "responded","responded_at","response_notes","deal_status"]
    writer = csv.DictWriter(si, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(dict(row))

    fname = f"crm_leads_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(si.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


# ══════════════════════════════════════════════════════════════════════════════
# Misc routes
# ══════════════════════════════════════════════════════════════════════════════

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