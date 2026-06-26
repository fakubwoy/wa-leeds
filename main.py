import os, re, csv, json, logging, requests, time, random, threading, queue, sqlite3
from contextlib import contextmanager
from dotenv import load_dotenv
load_dotenv()
from io import StringIO
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
# Suppress noisy DEBUG from urllib3/requests — keep our own debug clean
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("playwright").setLevel(logging.WARNING)

app = Flask(__name__, template_folder="templates", static_folder="static")

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

MIN_FOLLOWERS = 1_000
MAX_FOLLOWERS = 200_000

WA_LINK_RE = re.compile(r"wa\.me/\+?(\d+)|whatsapp\.com/send\?phone=(\d+)", re.I)
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
# Hashtag strategy: use ORDER-INTENT tags (not food blogger tags).
# Tags like #whatsapporders, #homedelivery, #orderonwhatsapp pull actual sellers.
# Geo-specific Telugu tags find Hyderabad/Andhra home businesses.
NICHE_PRESETS = {
    "sweets_pickles": {
        "label": "🍬 Sweets & Pickles",
        "niche": "homemade sweets, pickles, achaar, mithai, traditional Telugu food, avakaya, gongura, home delivery sweets",
        "hashtags": "homemadesweets,homemadesweet,homemadepickles,pickleorders,acharorders,avakayapickle,gonguraorders,telugusweets,andhrasweets,sweetsdelivery,mithaiorders,traditionalsweets,handmadesweets,homemadefood,homemadefoodbusiness",
        "geo": "hyderabad,andhra,telangana,secunderabad",
        "search_keywords": "homemade sweets order hyderabad whatsapp,avakaya pickle order online,gongura pickle home delivery,andhra pickles order",
    },
    "aquaculture": {
        "label": "🐟 Aquaculture & Sea Fish Export",
        "niche": "fish export, seafood, aquaculture, fresh fish home delivery, prawns, bulk fish supply",
        "hashtags": "freshfishdelivery,fishdeliveryhome,seafoodhomedelivery,freshfishorders,prawndelivery,fishsupplier,seafoodsupplier,fishexporter,aquaculturebusiness,freshwaterfish,fishbusiness,homedeliveryfish",
        "geo": "hyderabad,vizag,kakinada,andhra,telangana",
        "search_keywords": "fresh fish home delivery hyderabad,seafood supplier bulk order,prawns home delivery andhra,fish export vizag whatsapp",
    },
    "travel_agents": {
        "label": "✈️ Travel Agents",
        "niche": "travel agent, tour packages, holiday booking, visa service, flight tickets, honeymoon packages",
        "hashtags": "travelagentindia,tourpackagesindia,travelagency,holidaypackages,visaservices,flightbooking,travelbusiness,tourismpackages,honeymoonpackage,pilgrimtours,touroperator,travelplanner",
        "geo": "hyderabad,telangana,andhra",
        "search_keywords": "travel agent hyderabad whatsapp,tour package booking hyderabad,visa consultant hyderabad contact,honeymoon package andhra",
    },
    "beauty_products": {
        "label": "💄 Beauty, Hair & Body Care",
        "niche": "homemade beauty products, herbal skincare, natural hair oil, organic face cream, handmade body care",
        "hashtags": "homemadebeauty,naturalbeautyproducts,herbalskincare,organicbeauty,handmadecosmetics,naturalfacepack,hairgrowthproducts,herbalbeauty,organicskincare,naturalhaircare,homemadeskincare,herbalcosmetics",
        "geo": "hyderabad,telangana,andhra",
        "search_keywords": "homemade beauty products order hyderabad,herbal hair oil order online andhra,organic skincare whatsapp order",
    },
    "cakes_bakers": {
        "label": "🎂 Cakes & Dessert Bakers",
        "niche": "custom cakes home baker, birthday cake delivery, fondant cake orders, cupcakes order, homemade desserts",
        "hashtags": "homebaker,homebakery,customcakeorders,birthdaycakedelivery,cakeorders,cakeorder,fondantcakeorders,homemadecakes,cakebusiness,customcakes,designercakeorders,cakedelivery,homebakerbusiness",
        "geo": "hyderabad,secunderabad,telangana",
        "search_keywords": "custom cake order hyderabad whatsapp,home baker birthday cake hyderabad,cake delivery hyderabad order,fondant cake hyderabad",
    },
    "gift_shops": {
        "label": "🎁 Personalised Gift Shops",
        "niche": "personalised gifts, custom engraved gifts, photo gifts, customized gifting, corporate gifts order",
        "hashtags": "personalisedgifts,customgifts,giftbusiness,customizedgifts,personalisedgiftshop,giftorders,handmadegifts,photogifts,uniquegifts,corporategifts,customgiftshop,specialgifts",
        "geo": "hyderabad,telangana",
        "search_keywords": "personalised gift order hyderabad whatsapp,custom photo gift hyderabad,engraved gifts order online andhra",
    },
    "event_planners": {
        "label": "🎪 Event Planners & Decorators",
        "niche": "birthday decoration, wedding decoration, event planning, balloon decoration, party setup home",
        "hashtags": "birthdaydecoration,eventdecorator,weddingdecor,partydecoration,balloondecoration,eventplanner,birthdaysetup,homepartysetup,weddingplanner,partyorganizer,eventmanagement,decorationbusiness",
        "geo": "hyderabad,secunderabad,telangana",
        "search_keywords": "birthday decoration hyderabad whatsapp,event decorator hyderabad order,wedding decoration hyderabad contact,balloon decoration hyderabad",
    },
    "home_interior": {
        "label": "🏠 Home Interior & Furniture",
        "niche": "home interior designer, custom furniture, home decor, interior decoration service, furniture maker",
        "hashtags": "homeinteriordesign,customfurniture,homedecorbusiness,interiordecorator,furnituremaker,interiordesignbusiness,homedecorseller,customhomedecor,handmadefurniture,homedecorations",
        "geo": "hyderabad,secunderabad,telangana",
        "search_keywords": "interior designer hyderabad whatsapp,custom furniture hyderabad order,home decor seller hyderabad contact",
    },
    "dairy_products": {
        "label": "🥛 Homemade Dairy Products",
        "niche": "homemade ghee, fresh paneer, desi butter, curd, milk products home delivery, farm fresh dairy",
        "hashtags": "homemadeghee,desighee,pureghee,cowghee,homemadepaneer,freshpaneer,farmfreshdairy,homemadebutterr,homemadecurd,dairyproducts,farmfreshproducts,puredairy,gheeorders",
        "geo": "hyderabad,telangana,andhra",
        "search_keywords": "homemade ghee order hyderabad whatsapp,fresh paneer home delivery hyderabad,farm fresh dairy andhra order,desi ghee order online",
    },
    "homemade_cosmetics": {
        "label": "🧴 Homemade Cosmetics & Soaps",
        "niche": "handmade soap, herbal shampoo, natural cosmetics, chemical free beauty, homemade body care products",
        "hashtags": "handmadesoap,naturalsoap,handmadesoaps,herbalshampoobusiness,naturalsoapmaker,organicsoapmaker,chemicalfreeproducts,handcraftedsoap,naturalbodycare,homemadecosmetics,soapbusiness,naturalshampoo",
        "geo": "hyderabad,telangana,andhra",
        "search_keywords": "handmade soap order hyderabad whatsapp,herbal shampoo order online andhra,natural cosmetics home delivery hyderabad",
    },
    "therapists": {
        "label": "🩺 Therapists & Online Doctors",
        "niche": "online consultation doctor, dietician online, mental health therapist, wellness coach, health advisor",
        "hashtags": "onlineconsultation,dieticianconsultation,nutritionistreels,onlinehealthcoach,mentalHealthcoach,therapistonline,wellnesscoachonline,healthadvisor,onlinedietician,lifecoachonline,nutritionistindia",
        "geo": "hyderabad,telangana,andhra,india",
        "search_keywords": "online dietician whatsapp consultation,therapist online booking india,wellness coach hyderabad contact,health coach andhra whatsapp",
    },
    "fitness_trainers": {
        "label": "💪 Gym Trainers, MUA & Dieticians",
        "niche": "personal trainer online, makeup artist booking, bridal makeup, fitness coach, gym trainer home",
        "hashtags": "personaltrainerindia,makeupartistbooking,bridalmakeup,makeupbooking,fitnesstrainer,gymtrainer,makeuporders,bridalmakeupartist,homegymtrainer,makeupbusiness,fitnessbusiness",
        "geo": "hyderabad,secunderabad,telangana",
        "search_keywords": "makeup artist hyderabad whatsapp booking,personal trainer hyderabad contact,bridal makeup hyderabad order,gym trainer home hyderabad",
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

# ── DOM bio extraction JS — module-level raw string to avoid escape issues ────
_DOM_BIO_JS = r"""() => {
    // Parse the profile header section as structured text.
    // Instagram's class names are randomly generated and change weekly.
    // We use the stable page STRUCTURE instead:
    //   header > section contains: username, name, stats, bio, links

    function parseFollowers(txt) {
        if (!txt) return 0;
        const t = txt.trim().replace(/,/g, '');
        const m = t.match(/^([\d.]+)([kKmM]?)$/);
        if (!m) return 0;
        const n = parseFloat(m[1]);
        if (m[2].toLowerCase() === 'k') return Math.round(n * 1000);
        if (m[2].toLowerCase() === 'm') return Math.round(n * 1000000);
        return Math.round(n);
    }

    // Resolve an Instagram redirect link (l.instagram.com/?u=...) to the real URL
    function resolveIgRedirect(href) {
        try {
            if (href.includes('l.instagram.com')) {
                const u = new URL(href).searchParams.get('u');
                if (u) return decodeURIComponent(u);
            }
        } catch(e) {}
        return href;
    }

    // Extract wa.me URLs from any block of visible text (handles "wa.me/917011437821 and 1 more")
    function extractWaFromText(text) {
        const found = [];
        // Match wa.me/+DIGITS or wa.me/DIGITS anywhere in the string
        const re = /wa\.me\/\+?(\d+)/gi;
        let m;
        while ((m = re.exec(text)) !== null) {
            const url = 'https://wa.me/' + m[1];
            if (!found.includes(url)) found.push(url);
        }
        return found;
    }

    // Detect the IG internal links-page URL (instagram.com/username/links/)
    // Instagram collapses multiple profile links into a pill showing "wa.me/... and N more"
    // that links internally rather than to l.instagram.com. We capture it so Python
    // can navigate to it with Playwright and scrape the real URLs.
    function isIgLinksPage(href) {
        return /instagram\.com\/[^/]+\/links\/?/i.test(href);
    }

    const header = document.querySelector('header') ||
                   document.querySelector('main') ||
                   document.querySelector('[role="main"]');

    let bio = '', followers = 0, fullName = '', extUrl = '';
    let allLinks = [];   // ALL external links found — wa.me, linktree, etc.
    let igLinksPageUrl = ''; // IG internal /links/ page if present

    if (header) {
        // Full name: first h1 or h2 in header
        const h = header.querySelector('h1, h2');
        if (h) fullName = h.innerText.trim();

        // Follower count: find a list item containing "followers"
        const allLi = header.querySelectorAll('li');
        for (const li of allLi) {
            const txt = li.innerText || '';
            if (/follower/i.test(txt)) {
                const numMatch = txt.match(/([\d.,]+[kKmM]?)/);
                if (numMatch) followers = parseFollowers(numMatch[1]);
                break;
            }
        }

        // ── Strategy A: scan all <a> tags in header ─────────────────────────
        // Instagram shows profile links as <a> elements below the bio.
        // Some go via l.instagram.com (single link), others go to an internal
        // /links/ page (multiple links collapsed into a pill widget).
        const linkEls = header.querySelectorAll('a[href]');
        for (const a of linkEls) {
            const href = a.href || '';
            if (!href || href.includes('#')) continue;

            // Capture the IG /links/ page URL — we'll navigate there in Python
            if (isIgLinksPage(href)) {
                igLinksPageUrl = href;
                // Don't skip — also scan the pill text below
            } else if (href.match(/instagram\.com\/([\w.]+)\/?$/) && !href.includes('l.instagram.com')) {
                // Pure internal nav link (profile, explore, etc.) — skip
                continue;
            } else {
                // External or l.instagram.com link — resolve and keep
                const resolved = resolveIgRedirect(href);
                if (resolved && !allLinks.includes(resolved)) {
                    allLinks.push(resolved);
                }
            }

            // Always scan the visible anchor text for wa.me patterns.
            // The collapsed pill shows "wa.me/917011437821 and 1 more" as plain text
            // even when the href is an internal /links/ page.
            const linkText = (a.innerText || a.textContent || '').trim();
            for (const waUrl of extractWaFromText(linkText)) {
                if (!allLinks.includes(waUrl)) allLinks.push(waUrl);
            }
        }

        // ── Strategy B: scan ALL visible text in the header for wa.me patterns ──
        // Catches cases where the link widget is rendered as a <div>/<span> with
        // no underlying <a> tag, or where the text is outside the anchor.
        const headerText = header.innerText || '';
        for (const waUrl of extractWaFromText(headerText)) {
            if (!allLinks.includes(waUrl)) allLinks.push(waUrl);
        }

        // extUrl = first non-IG link (for backward compat), prefer wa.me links
        const waLink = allLinks.find(l => l.includes('wa.me'));
        extUrl = waLink || allLinks[0] || '';

        // Bio: find the longest meaningful text block in the header,
        // excluding the username, display name, stat labels/numbers,
        // and the links widget (which is inside a <button> element).
        const username = location.pathname.replace(/\//g, '');
        const statWords = ['posts', 'followers', 'following'];

        const candidates = header.querySelectorAll('span, div, p');
        let bestBio = '';
        for (const el of candidates) {
            // Skip anything inside a <button> — that's the links widget, not bio text
            if (el.closest('button')) continue;
            const directText = el.innerText ? el.innerText.trim() : '';
            if (!directText || directText.length < 3) continue;
            if (directText === username || directText === '@' + username) continue;
            if (fullName && directText === fullName) continue;
            if (statWords.some(w => directText.toLowerCase() === w)) continue;
            if (/^[\d.,]+[kKmM]?$/.test(directText)) continue;
            if (directText.length < 5 && !/[a-zA-Z0-9]/.test(directText)) continue;
            if (['Edit profile','Follow','Message','More','Share'].includes(directText)) continue;
            // Skip if the text is just a wa.me URL — that's a link, not bio
            if (/^wa\.me\//.test(directText)) continue;
            if (directText.length > bestBio.length && directText.length > 10) {
                bestBio = directText;
            }
        }
        // Strip any trailing wa.me/... lines that leaked into the bio via a parent container
        bio = bestBio.replace(/\s*wa\.me\/\S+(\s+and\s+\d+\s+more)?/gi, '').trim();
    }

    // Fallback: meta description (only if it contains actual bio content)
    if (!bio) {
        const metaDesc = document.querySelector('meta[name="description"]');
        if (metaDesc) {
            const d = metaDesc.content || '';
            const stripped = d.replace(/^.*?Posts\s*[-\u2013]\s*/i, '').trim();
            if (stripped && !stripped.startsWith('See Instagram')) {
                bio = stripped;
            }
        }
    }

    return { bio, followers, fullName, extUrl, allLinks, igLinksPageUrl };
}"""


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
    """
    Extract a WhatsApp/phone number from bio + extra text.
    Returns a validated +91XXXXXXXXXX string (12 chars, last 10 digits start 6-9),
    or empty string if no valid number is found.
    """
    def _normalise_to_e164(raw: str) -> str:
        """Convert a raw number string to +91XXXXXXXXXX or return '' if invalid."""
        digits = re.sub(r"[\s\-()]", "", raw)
        # Strip leading + if present
        if digits.startswith("+"):
            digits = digits[1:]
        # Strip country code 91 if present, leaving exactly 10 digits
        if digits.startswith("91") and len(digits) == 12:
            digits = digits[2:]
        elif digits.startswith("091") and len(digits) == 13:
            digits = digits[3:]
        # Must be exactly 10 digits starting with 6-9
        if len(digits) == 10 and digits[0] in "6789":
            return "+91" + digits
        return ""

    combined = f"{bio} {extra_text}"

    # Priority 1: wa.me or whatsapp.com links — most reliable
    for m in WA_LINK_RE.finditer(combined):
        raw = (m.group(1) or m.group(2) or "").strip()
        result = _normalise_to_e164(raw)
        if result:
            return result

    # Priority 2: PHONE_RE — +91 prefixed or bare 10-digit numbers
    # Collapse spaces/dashes between digits first so "98765 43210" matches cleanly
    combined_collapsed = re.sub(r'(\d)[\s\-]+(\d)', r'\1\2', combined)
    combined_collapsed = re.sub(r'(\d)[\s\-]+(\d)', r'\1\2', combined_collapsed)  # second pass for "9 8765 43210"
    for raw in PHONE_RE.findall(combined_collapsed):
        result = _normalise_to_e164(raw)
        if result:
            return result

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

def _sanitize_for_prompt(text: str, max_len: int = 600) -> str:
    """
    Clean text before embedding in the Gemini prompt.
    Removes / replaces characters that corrupt JSON output.
    """
    if not text:
        return ""
    # Truncate first
    text = text[:max_len]
    # Replace actual newlines with space (they break JSON string values)
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    # Remove or replace characters that break JSON strings inside Gemini's output
    text = text.replace("\\", "/")   # backslash → forward slash
    text = text.replace('"', "'")    # double-quote → single-quote
    # Collapse multiple spaces
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def gemini_parse_profile(profile: dict, niche: str, extra_text: str = "") -> dict:
    """
    Single Gemini call per profile — extracts WA number, city, category,
    products, business type, and qualifies the lead all in one shot.
    """
    if not GEMINI_KEY:
        return {**_GEMINI_EMPTY, "reason": "No Gemini API key configured", "gemini_ran": False}

    username  = profile.get("username", "")
    full_name = _sanitize_for_prompt(profile.get("full_name", ""), 80)
    followers = profile.get("followers", 0)
    following = profile.get("following", 0)
    bio_raw   = profile.get("bio", "")
    url_raw   = profile.get("external_url", "") or ""
    bio       = _sanitize_for_prompt(bio_raw, 500)
    url       = _sanitize_for_prompt(url_raw, 120)
    ig_cat    = _sanitize_for_prompt(profile.get("ig_category", ""), 60)
    is_biz    = profile.get("is_business", False)
    post_cnt  = profile.get("post_count", 0)
    extra     = _sanitize_for_prompt(extra_text, 600)

    # ── Pre-extract WA number with regex BEFORE calling Gemini ───────────────
    # Gemini often hallucinates or corrupts phone numbers from sanitized bio text.
    # Our regex operates on the RAW (unsanitized) bio + extra_text, which is more
    # reliable. We inject the result as a "verified" fact so Gemini just confirms
    # it rather than trying to extract it independently.
    regex_wa_number = extract_wa_number(bio_raw, extra_text)
    verified_wa_hint = (
        f"VERIFIED WhatsApp number extracted by regex: {regex_wa_number} — "
        f"use this exact value for whatsapp_number; set whatsapp_signal=true."
        if regex_wa_number
        else "No WhatsApp number found by regex pre-scan — check bio text yourself."
    )

    # ── Use a two-message structure: system context + user data ───────────────
    # Separating data from the JSON schema prevents bio content from
    # corrupting Gemini's JSON template output.
    system_prompt = f"""You are a data extraction AI for a WhatsApp business outreach tool targeting small Indian businesses.

TARGET NICHE: {niche}

Analyze the Instagram profile provided and return ONLY a valid JSON object with these exact fields:

{{
  "valid": true/false,
  "confidence": "high" or "medium" or "low",
  "city": "city name or empty string",
  "state": "Indian state or empty string",
  "country": "country name, default India",
  "whatsapp_number": "full number like +919876543210 or empty string",
  "whatsapp_signal": true/false,
  "category": "specific category like Homemade Sweets or Custom Cakes",
  "business_type": "product_seller or service_provider or both or influencer or brand or unknown",
  "sells_on_whatsapp": true/false,
  "is_small_business": true/false,
  "is_large_brand": true/false,
  "is_influencer": true/false,
  "ordering_method": "WhatsApp or DM or website or phone or unknown",
  "products_or_services": "comma-separated list",
  "languages": "language names",
  "reason": "one sentence"
}}

RULES:
- valid=true only if niche matches AND whatsapp_signal=true AND is_small_business=true AND is_influencer=false
- whatsapp_number: {verified_wa_hint} If no verified number, look for wa.me links or 10-digit Indian numbers starting 6-9, format as +91XXXXXXXXXX
- IMPORTANT: only output a whatsapp_number you are 100% certain about — never guess or invent digits
- city: extract from bio text, pin emoji location, or area name mentions
- state: infer from city if not stated
- is_large_brand: true if followers > 200000 or bio has pvt ltd/llp/franchise/pan india/official page
- confidence: high=WA number found + clear niche, medium=WA signal + probable niche, low=weak
- Return ONLY the JSON, no markdown, no explanation"""

    user_message = f"""Username: @{username}
Name: {full_name}
Followers: {followers:,} | Following: {following:,} | Posts: {post_cnt}
IG Category: {ig_cat or 'not set'} | Is Business Account: {is_biz}
Bio: {bio}
External URL: {url or 'none'}
Bio-link content: {extra or 'none'}"""

    def _call_gemini(contents, use_json_mime=True):
        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 2048,   # was 600 — responses were getting truncated at ~15 tokens
            },
        }
        if use_json_mime:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        resp = requests.post(
            GEMINI_URL,
            params={"key": GEMINI_KEY},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _normalise_confidence(val) -> str:
        """Convert any confidence value Gemini returns into high/medium/low."""
        if isinstance(val, str):
            v = val.lower().strip()
            if v in ("high", "medium", "low", "n/a"):
                return v
            # Gemini sometimes returns "High", "Medium", "Low"
            for s in ("high", "medium", "low"):
                if s in v:
                    return s
            return "low"
        if isinstance(val, (int, float)):
            # Gemini returns 0–1 or 0–100 scale
            f = float(val)
            if f > 1:
                f = f / 100.0   # normalise 0–100 → 0–1
            if f >= 0.7:  return "high"
            if f >= 0.4:  return "medium"
            return "low"
        return "low"

    def _extract_json(raw_text: str) -> dict | None:
        """Try multiple strategies to extract valid JSON from Gemini's response."""
        text = raw_text.strip()

        # Detect obviously truncated responses (< 100 chars is always incomplete)
        if len(text) < 80:
            log.debug(f"Response too short ({len(text)} chars) — definitely truncated")
            return None

        # Strip markdown fences
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
        text = text.strip()

        # Strategy 1: direct parse
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        # Strategy 2: find the outermost {...} block
        start = text.find("{")
        end   = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                result = json.loads(text[start:end + 1])
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

        # Strategy 3: truncated JSON — try to close open braces/brackets
        if start != -1:
            candidate = text[start:]
            opens  = candidate.count("{")
            closes = candidate.count("}")
            if opens > closes:
                candidate += "}" * (opens - closes)
            try:
                result = json.loads(candidate)
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

        # Strategy 4: field-by-field regex extraction (last resort — works even on truncated JSON)
        result = {}
        bool_fields = {
            "valid": False, "whatsapp_signal": False, "sells_on_whatsapp": False,
            "is_small_business": True, "is_large_brand": False, "is_influencer": False,
        }
        str_fields = {
            "city": "", "state": "", "country": "India",
            "whatsapp_number": "", "category": "", "business_type": "unknown",
            "ordering_method": "unknown", "products_or_services": "", "languages": "", "reason": "",
        }
        found_any = False
        for field, default in bool_fields.items():
            m = re.search(rf'"{field}"\s*:\s*(true|false)', text, re.I)
            if m:
                result[field] = m.group(1).lower() == "true"
                found_any = True
            else:
                result[field] = default
        for field, default in str_fields.items():
            m = re.search(rf'"{field}"\s*:\s*"([^"]*)"', text)
            if m:
                result[field] = m.group(1)
                found_any = True
            else:
                result[field] = default
        # confidence separately — handle numeric
        cm = re.search(r'"confidence"\s*:\s*([^\s,}]+)', text)
        if cm:
            raw_conf = cm.group(1).strip().strip('"')
            result["confidence"] = _normalise_confidence(raw_conf)
            found_any = True
        else:
            result["confidence"] = "low"

        if found_any:
            return result
        return None

    def _get_raw_text(raw_response: dict) -> str:
        """Safely extract text from Gemini response, with full debug logging on failure."""
        try:
            candidates = raw_response.get("candidates", [])
            if not candidates:
                # Log the full response so we can see what went wrong
                log.warning(f"Gemini @{username}: no candidates in response. Full response: {json.dumps(raw_response)[:500]}")
                return ""
            candidate = candidates[0]
            # Check for finish reason issues
            finish_reason = candidate.get("finishReason", "")
            if finish_reason not in ("STOP", "MAX_TOKENS", ""):
                log.warning(f"Gemini @{username}: finishReason={finish_reason}. Full candidate: {json.dumps(candidate)[:300]}")
            content = candidate.get("content", {})
            parts   = content.get("parts", [])
            if not parts:
                log.warning(f"Gemini @{username}: empty parts. Full candidate: {json.dumps(candidate)[:300]}")
                return ""
            return parts[0].get("text", "")
        except Exception as e:
            log.warning(f"Gemini @{username}: error extracting text: {e}. Raw: {json.dumps(raw_response)[:300]}")
            return ""

    try:
        # ── Attempt 1: system_instruction + user message ──────────────────────
        payload1 = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_message}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 2048,
                "responseMimeType": "application/json",
            },
        }
        # Exponential backoff on 429 rate-limit — lets us run more workers safely
        for _attempt in range(4):
            resp1 = requests.post(
                GEMINI_URL, params={"key": GEMINI_KEY}, json=payload1, timeout=30
            )
            if resp1.status_code == 429:
                wait = (2 ** _attempt) + random.uniform(0, 1)
                log.warning(f"Gemini @{username}: 429 rate-limit, retrying in {wait:.1f}s (attempt {_attempt+1})")
                time.sleep(wait)
                continue
            resp1.raise_for_status()
            break
        raw1   = resp1.json()
        text1  = _get_raw_text(raw1)
        log.debug(f"Gemini @{username} attempt1 raw ({len(text1)}ch): {text1[:120]}")
        parsed = _extract_json(text1) if text1 else None

        if not parsed:
            # Short delay before retry — avoids hammering a rate-limited API
            time.sleep(0.5)

            # ── Attempt 2: plain single-message, no JSON mime ─────────────────
            simple_prompt = (
                f"Analyze this Instagram profile for the niche: {niche}\n\n"
                f"Username: @{username} | Followers: {followers:,}\n"
                f"Bio: {bio[:300]}\n"
                f"URL: {url or 'none'}\n\n"
                f"IMPORTANT — WhatsApp number: {verified_wa_hint}\n\n"
                f"Return ONLY a complete JSON object with ALL of these keys (no truncation, no markdown):\n"
                f"valid(bool), confidence(\"high\"|\"medium\"|\"low\"), city(str), state(str), "
                f"country(str), whatsapp_number(str), whatsapp_signal(bool), category(str), "
                f"business_type(str), sells_on_whatsapp(bool), is_small_business(bool), "
                f"is_large_brand(bool), is_influencer(bool), ordering_method(str), "
                f"products_or_services(str), languages(str), reason(str)\n\n"
                f"Rules: valid=true only if niche matches AND whatsapp_signal=true AND "
                f"is_small_business=true. confidence must be the string 'high', 'medium' or 'low'. "
                f"Never invent or guess a phone number — only use the verified one above if provided."
            )
            for _attempt2 in range(4):
                resp2 = requests.post(
                    GEMINI_URL,
                    params={"key": GEMINI_KEY},
                    json={
                        "contents": [{"role": "user", "parts": [{"text": simple_prompt}]}],
                        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048},
                    },
                    timeout=30,
                )
                if resp2.status_code == 429:
                    wait = (2 ** _attempt2) + random.uniform(0, 1)
                    log.warning(f"Gemini @{username}: 429 on attempt2, retrying in {wait:.1f}s")
                    time.sleep(wait)
                    continue
                resp2.raise_for_status()
                break
            raw2   = resp2.json()
            text2  = _get_raw_text(raw2)
            log.debug(f"Gemini @{username} attempt2 raw ({len(text2)}ch): {text2[:120]}")
            parsed = _extract_json(text2) if text2 else None

        if not parsed:
            log.warning(
                f"Gemini @{username}: JSON parse failed both attempts. "
                f"attempt1={repr(text1[:120])} | "
                f"attempt2={repr(locals().get('text2','N/A')[:120])}"
            )
            return {**_GEMINI_EMPTY, "reason": "JSON parse failed", "gemini_ran": False}

        parsed["gemini_ran"] = True

        # Normalise confidence — Gemini ignores our "string only" rule sometimes
        parsed["confidence"] = _normalise_confidence(parsed.get("confidence", "low"))

        # Normalise bool fields
        for bfield in ("valid", "whatsapp_signal", "sells_on_whatsapp",
                       "is_small_business", "is_large_brand", "is_influencer"):
            v = parsed.get(bfield)
            if isinstance(v, str):
                parsed[bfield] = v.lower() == "true"
            elif not isinstance(v, bool):
                parsed[bfield] = bool(v)

        # Normalise null → empty string for string fields
        for sfield in ("city", "state", "country", "whatsapp_number", "category",
                       "business_type", "ordering_method", "products_or_services",
                       "languages", "reason"):
            if parsed.get(sfield) is None:
                parsed[sfield] = ""

        # ── WA number safety net ──────────────────────────────────────────────
        # If our regex found a number pre-scan, always prefer it over Gemini's
        # output. Gemini may hallucinate digits or corrupt them during sanitization.
        # If Gemini found something and regex found nothing, keep Gemini's value
        # but only if it looks like a valid Indian mobile number.
        gemini_wa = parsed.get("whatsapp_number", "")
        if regex_wa_number:
            if gemini_wa and gemini_wa != regex_wa_number:
                log.info(
                    f"Gemini @{username}: WA number override — "
                    f"regex={regex_wa_number} vs gemini={gemini_wa} → using regex"
                )
            parsed["whatsapp_number"] = regex_wa_number
            parsed["whatsapp_signal"] = True
        elif gemini_wa:
            # Validate Gemini's number looks like a real Indian mobile
            digits = re.sub(r"[^\d]", "", gemini_wa)
            # Indian mobiles: 10 digits starting 6-9, or with +91 prefix = 12 digits
            if not re.match(r"^(91)?[6-9]\d{9}$", digits):
                log.warning(
                    f"Gemini @{username}: WA number '{gemini_wa}' failed validation "
                    f"(digits={digits}) — clearing"
                )
                parsed["whatsapp_number"] = ""
                # Don't flip whatsapp_signal — bio may still mention WA even without a valid number

        log.info(
            f"Gemini @{username}: valid={parsed.get('valid')} "
            f"conf={parsed.get('confidence')} "
            f"wa={'✓ ' + parsed['whatsapp_number'] if parsed.get('whatsapp_number') else str(parsed.get('whatsapp_signal'))} "
            f"city={parsed.get('city') or '—'} | "
            f"bio_given={repr(bio[:80])}"
        )
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
            # Initial load — wait for first XHR batch
            page.wait_for_timeout(4000)

            def _drain_captured():
                """Parse all captured XHR responses and add usernames."""
                for body in captured:
                    sections = (body.get("data", {}).get("recent", {}).get("sections", [])
                                or body.get("sections", []))
                    for section in sections:
                        for item in section.get("layout_content", {}).get("medias", []):
                            media = item.get("media", {})
                            uname = (media.get("user", {}).get("username", "")
                                     or media.get("owner", {}).get("username", ""))
                            if uname: usernames.add(uname)
                captured.clear()

            # ── Scroll loop: each scroll triggers more XHR batches ───────────
            # Instagram lazy-loads posts as you scroll. Without scrolling
            # you only get the first ~12 posts (one grid row).
            _drain_captured()
            max_scrolls = max(4, (limit // 12) + 2)
            last_count  = -1
            for scroll_n in range(max_scrolls):
                if len(usernames) >= limit:
                    break
                if len(usernames) == last_count:
                    # No new accounts after last scroll — page is exhausted
                    log.info(f"#{tag}: no new users after scroll {scroll_n} — stopping")
                    break
                last_count = len(usernames)
                page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
                page.wait_for_timeout(random.uniform(2000, 3000))
                _drain_captured()
                log.debug(f"#{tag}: scroll {scroll_n+1}/{max_scrolls} → {len(usernames)} users")

            if not usernames:
                log.info(f"#{tag}: XHR intercept got nothing, trying DOM scrape…")
                usernames.update(_parse_usernames_from_content(page.content()))
        finally:
            page.close(); ctx.close()
        log.info(f"#{tag} → {len(usernames)} usernames (after scrolling)")
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

        # Meta-tag fallback gives us follower count + a generic description only.
        # Instagram no longer includes the actual bio in og:description.
        # Do NOT scan page body for phone numbers here — the 50KB of HTML contains
        # OTHER users' data (related accounts, suggested follows) which would give
        # us wrong numbers. The real bio comes from DOM extraction (Strategy 3.5).

        log.info(f"@{username}: meta-tag fallback — {foll:,} followers | bio: {repr(bio_part[:80])}")
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
                            or "/api/v1/users/" in url
                            or "/api/graphql" in url):   # modern IG GraphQL endpoint
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
                # Modern /api/graphql wraps data under data.user or
                # data.xdt_api__v1__users__web_profile_info__connection
                user = (body.get("data", {}).get("user")
                        or body.get("data", {}).get("xdt_api__v1__users__web_profile_info__connection", {}).get("data", {}).get("user")
                        or body.get("graphql", {}).get("user")
                        or body.get("user"))
                if not user:
                    user = body.get("user") or body
                result = _normalise_user(user, username)
                if result and result.get("followers", 0) > 0:
                    return result

            # ── Strategy 3: HTML script-tag + DOM bio extraction ───────────
            content = page.content()
            result  = _extract_user_from_html(content, username)

            # ── Strategy 3.5: DOM bio + links extraction via Playwright ───────
            # ALWAYS run this — even when a bio was already found via HTML/JSON.
            # The DOM JS is the only way to get the links section (wa.me links,
            # linktree, etc.) which is a separate widget below the bio text and
            # is NOT present in the JSON/HTML embed or the meta tags.
            try:
                dom_data = page.evaluate(_DOM_BIO_JS)
                all_links = dom_data.get("allLinks", [])
                ig_links_page = dom_data.get("igLinksPageUrl", "")
                dom_ext_url   = dom_data.get("extUrl", "")
                bio_text      = dom_data.get("bio", "")
                log.debug(
                    f"@{username}: DOM raw → bio={repr(bio_text[:80])} "
                    f"extUrl={repr(dom_ext_url)} allLinks={all_links} "
                    f"igLinksPage={repr(ig_links_page)}"
                )

                # ── Strategy 3.6: navigate to IG /links/ page if present ──────
                # When a profile has multiple links, Instagram collapses them into
                # a pill widget ("wa.me/... and 1 more") whose <a> points to
                # instagram.com/username/links/. Navigate there to get all URLs.
                if ig_links_page and not all_links:
                    try:
                        log.debug(f"@{username}: navigating to IG links page: {ig_links_page}")
                        page.goto(ig_links_page, wait_until="domcontentloaded", timeout=10000)
                        page.wait_for_timeout(2000)
                        # Scrape all <a> tags on the links page
                        links_page_links = page.evaluate(r"""() => {
                            const links = [];
                            document.querySelectorAll('a[href]').forEach(a => {
                                const href = a.href || '';
                                if (!href || href.includes('#')) return;
                                // Resolve l.instagram.com redirects
                                let resolved = href;
                                try {
                                    if (href.includes('l.instagram.com')) {
                                        const u = new URL(href).searchParams.get('u');
                                        if (u) resolved = decodeURIComponent(u);
                                    }
                                } catch(e) {}
                                // Skip internal IG links
                                if (/instagram\.com\/[^/]+\/?$/.test(resolved)) return;
                                if (!links.includes(resolved)) links.push(resolved);
                                // Also scan anchor text for wa.me
                                const txt = (a.innerText || '').trim();
                                const re = /wa\.me\/\+?(\d+)/gi;
                                let m;
                                while ((m = re.exec(txt)) !== null) {
                                    const wu = 'https://wa.me/' + m[1];
                                    if (!links.includes(wu)) links.push(wu);
                                }
                            });
                            // Also scan full page text for wa.me patterns
                            const pageText = document.body ? document.body.innerText : '';
                            const re2 = /wa\.me\/\+?(\d+)/gi;
                            let m2;
                            while ((m2 = re2.exec(pageText)) !== null) {
                                const wu = 'https://wa.me/' + m2[1];
                                if (!links.includes(wu)) links.push(wu);
                            }
                            return links;
                        }""")
                        log.debug(f"@{username}: IG links page → {links_page_links}")
                        all_links = links_page_links or all_links
                    except Exception as e:
                        log.debug(f"@{username}: IG links page navigation failed: {e}")

                # Append all discovered links to bio so extract_wa_number / Gemini see them
                if all_links:
                    links_str = " ".join(all_links)
                    bio_text  = f"{bio_text} {links_str}".strip()

                if result:
                    # Always patch in the links data even if we already have a bio
                    if not result.get("bio") and bio_text:
                        result["bio"] = bio_text
                    elif all_links:
                        # Append links to existing bio so wa.me URLs are visible
                        existing = result.get("bio", "")
                        links_str = " ".join(all_links)
                        result["bio"] = f"{existing} {links_str}".strip()
                    if not result.get("external_url") and dom_ext_url:
                        result["external_url"] = dom_ext_url
                    if not result.get("followers") and dom_data.get("followers", 0):
                        result["followers"] = dom_data["followers"]
                    if not result.get("full_name") and dom_data.get("fullName"):
                        result["full_name"] = dom_data["fullName"]
                else:
                    if bio_text or dom_data.get("followers", 0) > 0:
                        result = {
                            "username":     username,
                            "full_name":    dom_data.get("fullName", ""),
                            "followers":    dom_data.get("followers", 0),
                            "following":    0,
                            "post_count":   0,
                            "bio":          bio_text,
                            "external_url": dom_ext_url,
                            "is_verified":  False,
                            "is_business":  False,
                            "ig_category":  "",
                        }
                log.debug(
                    f"@{username}: after DOM merge → "
                    f"bio_len={len((result or {}).get('bio',''))} "
                    f"external_url={repr((result or {}).get('external_url','')[:80])} "
                    f"wa_in_bio={bool(WA_LINK_RE.search((result or {}).get('bio','')))}"
                )
            except Exception as e:
                log.debug(f"@{username}: DOM bio extract failed: {e}")

            return result

        finally:
            page.close()
            ctx.close()

    try:
        p = browser_run(_job)
        if p and p.get("followers", 0) > 0:
            ext_url = p.get("external_url", "")
            wa_in_bio = bool(WA_LINK_RE.search(p.get("bio", "")))
            wa_in_url = bool(WA_LINK_RE.search(ext_url))
            log.info(
                f"@{username}: {p['followers']:,} followers | bio: {len(p.get('bio',''))} chars | "
                f"external_url={repr(ext_url[:80])} | wa_in_bio={wa_in_bio} | wa_in_url={wa_in_url}"
            )
            log.debug(f"@{username}: full_bio={repr(p.get('bio','')[:200])}")
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
    Tier a profile using Gemini-enriched data.

    Tier 1 — HOT      : small biz ✓ | WA signal ✓ | niche match ✓ | geo ✓ or India-only + no geo info
    Tier 2 — PARTIAL  : WA signal ✓ | niche match ✓ | geo fails (might still be local)
    Tier 3 — WEAK     : in range but no WA signal, OR WA but niche mismatch
    Tier 4 — OUT      : large brand | influencer | verified | follower count clearly wrong
    """
    followers  = profile.get("followers", 0)
    verified   = profile.get("is_verified", False)
    # followers==0 means meta-tag fallback (unknown count) — don't disqualify
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

    # ── WA signal: Gemini AI + regex fallback ─────────────────────────────────
    wa = gem.get("whatsapp_signal", False) or bool(gem.get("whatsapp_number", ""))
    if not wa:
        # Regex fallback — Gemini sometimes misses informal WA signals
        bio = profile.get("bio", "")
        wa  = has_wa_signal(bio, profile.get("external_url", ""), "")
    if wa:
        wa_num = gem.get("whatsapp_number", "")
        met.append(f"WA signal{' — ' + wa_num if wa_num else ''}")
    else:
        missing.append("No WhatsApp signal")

    # ── Niche match: from Gemini ───────────────────────────────────────────────
    # Use gem['valid'] as a guide but don't let it alone kill a lead.
    # A profile with WA signal that Gemini scored low-confidence is still worth seeing.
    niche_match = gem.get("valid", False)
    conf        = gem.get("confidence", "low")
    if niche_match:
        met.append(f"Niche match ({conf} confidence)")
    else:
        missing.append(f"Possible niche mismatch: {gem.get('reason','')[:80]}")

    # ── Geo check ─────────────────────────────────────────────────────────────
    # Logic: if geo_filter is set, check AI city/state + bio text.
    # But: many small Indian home businesses don't mention city at all.
    # If Gemini says country=India and no city found → call it "geo-unknown"
    # rather than "geo-failed", and still allow tier-1 if everything else fits.
    geo_ok      = True   # default: pass if no filter set
    geo_unknown = False  # city not in bio but seems Indian
    if geo_filter:
        ai_city    = (gem.get("city", "") or "").lower()
        ai_state   = (gem.get("state", "") or "").lower()
        ai_country = (gem.get("country", "") or "").lower()
        bio_text   = f"{profile.get('bio','')} {profile.get('full_name','')} {profile.get('username','')}".lower()
        combined   = f"{ai_city} {ai_state} {bio_text}"

        filter_words = [w.strip().lower() for w in geo_filter.split(",") if w.strip()]
        geo_ok = any(w in combined for w in filter_words)

        if not geo_ok:
            # If country is India and no geo info at all, treat as "unknown" not "failed"
            is_india = ai_country in ("india", "") or any(
                w in combined for w in ["india", "indian", "भारत", "🇮🇳"]
            )
            no_foreign_city = not any(
                w in combined for w in [
                    "london", "dubai", "usa", "uk", "canada", "australia",
                    "singapore", "malaysia", "usa", "new york", "california",
                ]
            )
            if is_india and no_foreign_city and not ai_city:
                geo_unknown = True   # Indian but city not in bio
            if geo_ok:
                loc_label = gem.get("city") or gem.get("state") or geo_filter
                met.append(f"Geo match — {loc_label}")
            elif geo_unknown:
                met.append("Geo: India (city not in bio — could be local)")
            else:
                missing.append(f"Geo mismatch — AI detected: {ai_city or ai_state or 'unknown'}")
        else:
            loc_label = gem.get("city") or gem.get("state") or geo_filter
            met.append(f"Geo match — {loc_label}")

    # ── Final tiering ──────────────────────────────────────────────────────────
    # Tier 1: WA + niche match + (geo confirmed OR geo unknown-but-India)
    # Tier 2: WA + niche match + geo clearly somewhere else
    # Tier 3: WA but niche weak, OR no WA but niche match
    if wa and niche_match and (geo_ok or geo_unknown):
        return 1, met, missing
    if wa and niche_match and not geo_ok and not geo_unknown:
        return 2, met, missing
    if wa and not niche_match:
        return 2, met, missing   # has WA, wrong niche — still worth a look
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
        # Always include the external_url itself in extra so that wa.me links
        # set as the profile link (e.g. wa.me/+918951787072) are passed to
        # extract_wa_number and has_wa_signal even when the URL is not a
        # link-in-bio page and no fetch is performed.
        if url:
            extra = f"{url} {extra}".strip()
        # Log what we are actually passing to extract_wa_number / Gemini
        regex_pre = extract_wa_number(prof.get("bio", ""), extra)
        log.debug(
            f"@{uname}: _parse_one — external_url={repr(url[:80])} | "            f"extra_prefix={repr(extra[:80])} | regex_wa={repr(regex_pre)}"
        )
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

    # ── Parallel Gemini calls with rate-limit protection ─────────────────────
    # gemini-2.5-flash free tier: 10 RPM sustained, ~15 concurrent OK in practice.
    # We use 10 workers + a semaphore so we never hammer more than 10 calls at once.
    # Each worker already has exponential backoff on 429, so spikes are handled
    # gracefully. This is ~3× faster than the old 3-worker "sequential-ish" approach.
    _gemini_sem = threading.Semaphore(10)   # max concurrent Gemini HTTP calls

    def _parse_one_throttled(uname_profile):
        with _gemini_sem:
            return _parse_one(uname_profile)

    result_queue: queue.Queue = queue.Queue()

    def _worker(item):
        result = _parse_one_throttled(item)
        result_queue.put(result)

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(_worker, item) for item in all_profiles.items()]
        received = 0
        while received < total_to_parse:
            uname, prof, extra, gem = result_queue.get()
            gem_results[uname] = (prof, extra, gem)
            parsed_count += 1
            received += 1
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

            # Prefer Gemini-extracted fields, fall back to regex.
            # Pass url explicitly so wa.me profile links are parsed even if
            # extra was built from a different source (e.g. a linktree page).
            wa_number = gem.get("whatsapp_number") or extract_wa_number(bio, f"{url} {extra}".strip())
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