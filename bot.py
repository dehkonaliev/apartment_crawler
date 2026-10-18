import asyncio
import hashlib
import html
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import SearchGlobalRequest
from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
USD_UZS = float(os.getenv("USD_UZS", "12500"))
CHANNELS = [
    re.sub(r"^(https?://)?t\.me/|^@", "", c.strip())
    for c in os.getenv("CHANNELS", "").split(",")
    if c.strip()
]

# ---- Settings you can change ----
PER_CHANNEL_LIMIT = 300   # max messages read from each channel
MAX_RESULTS = 15          # ads shown in the answer
MIN_RENT_USD = 15         # prices outside this range are ignored (probably not monthly rent)
MAX_RENT_USD = 5000

user = TelegramClient("user_session", API_ID, API_HASH)  # your account
bot = TelegramClient("bot_session", API_ID, API_HASH)    # the bot


# ======================================================================
# Text cleaning
# ======================================================================
_TABLE = {ord(c): None for c in "'’ʻʼ`‘´"}  # remove apostrophes: Ulug'bek -> ulugbek
_TABLE.update({ord("ў"): "у", ord("ғ"): "г", ord("қ"): "к", ord("ҳ"): "х", ord("ё"): "е"})


def norm(text):
    """For word matching: lowercase, no apostrophes, hyphens become spaces."""
    text = text.lower().translate(_TABLE).replace("-", " ")
    return re.sub(r"\s+", " ", text)


def soft(text):
    """For number reading: like norm, but keeps hyphens (200-250$) and new lines."""
    text = text.lower().translate(_TABLE)
    return re.sub(r"[ \t]+", " ", text)


# ======================================================================
# Word lists - add your own words here
# ======================================================================
LOCATIONS = {
    "Chilonzor": ["chilonzor", "chilanzar", "chilonzar", "чилонзор", "чиланзар"],
    "Yunusobod": ["yunusobod", "yunusabad", "юнусобод", "юнусабад"],
    "Mirzo Ulugbek": ["ulugbek", "ulug bek", "улугбек"],
    "Yakkasaroy": ["yakkasaroy", "yakkasaray", "яккасарой", "яккасарай"],
    "Shayxontohur": ["shayxontohur", "shayxontoxur", "shaykhantakhur", "шайхонтохур", "шайхантахур"],
    "Olmazor": ["olmazor", "almazar", "олмазор", "алмазар"],
    "Mirobod": ["mirobod", "mirabad", "миробод", "мирабад"],
    "Uchtepa": ["uchtepa", "учтепа"],
    "Yashnobod": ["yashnobod", "yashnabad", "яшнобод", "яшнабад"],
    "Sergeli": ["sergeli", "сергели"],
    "Bektemir": ["bektemir", "бектемир"],
    "Yangihayot": ["yangihayot", "yangi hayot", "янгихает", "янгихаает"],
}
LOCATIONS = {k: [norm(a) for a in v] for k, v in LOCATIONS.items()}

RENT_WORDS = [norm(w) for w in [
    "ijara", "ijaraga", "ижара", "arenda", "аренд", "сдает", "сдаю", "сдам", "сдается", "сдаётся",
    "kimlar uchun", "xonalar soni", "kishi kerak",   # ad template: "Manzil / Narxi / Kimlar uchun ..."
]]
SEEKER_START = [norm(w) for w in ["ищу", "сниму", "qidiraman", "qidiryapman"]]
# Ads with these words are removed: daily rent and sale ads
SKIP_WORDS = [norm(w) for w in [
    "kunlik", "sutkalik", "посуточ", "суточн",
    "sotiladi", "sotaman", "продается", "продаётся", "продаю", "продам",
]]

GENDER_PATTERNS = {
    "girls": re.compile(r"\bqiz(?!il)\w*|\bayol\w*|девуш|девоч|студентк|женщин"),
    "boys": re.compile(r"\byigit\w*|\bbol(?:l|al)ar(?:ga|\s+bilan)\b|\bogil\s*bol\w*|парн|парень|мужчин"),
    "family": re.compile(r"\boila\w*|\bjuft\w*|семь[яеюи]|семейн"),
}
GENDER_NAMES = {
    "girls": "girls", "qiz": "girls", "f": "girls", "female": "girls",
    "boys": "boys", "yigit": "boys", "m": "boys", "male": "boys",
    "family": "family", "oila": "family",
}


def detect_genders(n):
    return {g for g, p in GENDER_PATTERNS.items() if p.search(n)}


def gender_ok(tags, wanted, strict):
    if wanted is None or wanted in tags:
        return True
    if not tags:          # gender not written in the ad
        return not strict
    return False


def resolve_location(term):
    """Turn a user word (chilonzor, ulugbek, minor...) into a list of spellings."""
    t = norm(term).replace("_", " ")
    for name, aliases in LOCATIONS.items():
        if t == norm(name) or t in aliases:
            return aliases
    return [t]  # any other word (metro, street, landmark) is used as it is


def find_areas(n):
    return [name for name, aliases in LOCATIONS.items() if any(a in n for a in aliases)]


# ======================================================================
# Reading PRICE from text (no AI)
# ======================================================================
NUM = r"\d{1,3}(?:[ .,]\d{3})+|\d+(?:[.,]\d+)?"
SP = r"[ \t]*"   # spaces only: never jump to the next line
RANGE = rf"(?:{SP}[-–—]{SP}(?:{NUM}))?"   # "200-250$": we take the first (lower) number

P_USD_AFTER = re.compile(
    rf"({NUM}){RANGE}{SP}(?:\$|usd\b|dol\w*|y\.?e\b|у\.?е\b|дол\w*)")
P_USD_BEFORE = re.compile(rf"(?<!\d)(?<!\d\s)\${SP}({NUM})")
P_MLN_MING = re.compile(
    rf"({NUM}){SP}(?:mln\b|mil\b|млн\b){SP}({NUM}){SP}(?:ming\b|минг\b|тыс\b|k\b)")   # 1 mln 800 ming
P_MLN = re.compile(rf"({NUM}){RANGE}{SP}(?:mln\b|mil\b|million\w*|млн\b|миллион\w*)")
P_MING = re.compile(rf"({NUM}){RANGE}{SP}(?:ming\b|минг\b|тыс\b|тысяч\w*|k\b)")
P_SOM = re.compile(rf"({NUM}){RANGE}{SP}(?:som\b|sum\b|сум\b|uzs\b)")
P_KEYWORD = re.compile(rf"(?:narx\w*|цена|стоимост\w*|price|oyiga|oylik\w*|оплата)\s*[:=\-–—]?\s*({NUM})")

# Words just before a number that mean "this is NOT the monthly rent"
EXTRA_WORDS = ["depozit", "депозит", "zalog", "залог", "kommunal", "коммунал",
               "komissi", "комисси", "agent", "агент", "xizmat", "услуг"]
PRICE_WORDS = ["narx", "цена", "стоимост", "price", "oyiga", "oylik", "оплата"]


def parse_number(s):
    s = s.strip()
    if re.fullmatch(r"\d{1,3}(?:[ .,]\d{3})+", s):   # 3 000 000 / 3.000.000 / 3,000,000
        return float(re.sub(r"[ .,]", "", s))
    try:
        return float(s.replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


def _before(t, pos, size=14):
    """Text just before a number, cut at the last comma / new line."""
    window = t[max(0, pos - size):pos]
    return re.split(r"[,;\n|•]", window)[-1]


def extract_price_usd(t):
    """t = soft(text). Returns monthly rent in dollars, or None."""
    cands = []  # (position, usd)
    combo = []  # "1 mln 800 ming" -> (start, end, usd)
    for m in P_MLN_MING.finditer(t):
        a, b = parse_number(m.group(1)), parse_number(m.group(2))
        if a is not None and b is not None:
            combo.append((m.start(), m.end(), (a * 1_000_000 + b * 1_000) / USD_UZS))

    def inside_combo(pos):
        return any(s <= pos < e for s, e, _ in combo)

    for rx in (P_USD_AFTER, P_USD_BEFORE):
        for m in rx.finditer(t):
            v = parse_number(m.group(1))
            if v is not None and not inside_combo(m.start()):
                cands.append((m.start(), v))
    for rx, mult in ((P_MLN, 1_000_000), (P_MING, 1_000), (P_SOM, 1)):
        for m in rx.finditer(t):
            v = parse_number(m.group(1))
            if v is not None and not inside_combo(m.start()):
                cands.append((m.start(), v * mult / USD_UZS))
    cands += [(s, usd) for s, _, usd in combo]

    cands = sorted(c for c in cands if MIN_RENT_USD <= c[1] <= MAX_RENT_USD)
    good = [c for c in cands if not any(w in _before(t, c[0]) for w in EXTRA_WORDS)]
    if good:
        for pos, usd in good:                      # prefer a number written after "narx", "цена"...
            if any(w in _before(t, pos) for w in PRICE_WORDS):
                return usd
        return good[0][1]

    # Last try: number after a price word, with no currency ("Narxi: 250")
    m = P_KEYWORD.search(t)
    if m:
        v = parse_number(m.group(1))
        if v:
            usd = v if v < 5000 else v / USD_UZS
            if MIN_RENT_USD <= usd <= MAX_RENT_USD:
                return usd
    return None


# ======================================================================
# Reading ROOMS from text (no AI)
# ======================================================================
ROOMS_LABEL = re.compile(
    r"xonalar(?:[ \t]+soni)?[ \t]*[:=\-]?[ \t]*(\d{1,2})"                       # Xonalar soni: 3
    r"|(?:количество|кол[. \-]*во)[ \t]+комнат[ \t]*[:=\-]?[ \t]*(\d{1,2})")   # Количество комнат: 3
ROOMS_DIGIT = re.compile(r"(?<!\d)(\d{1,2})[ \t]*-?[ \t]*(?:xona|хона|комнат|комн\b|ком\b|room|bedroom|к\b)")
ROOMS_WORD = re.compile(
    r"\b(bir|ikki|iki|uch|tort|besh)[ \t]*-?[ \t]*xona"
    r"|\b(одно|дву|трех|четырех|пяти)\w*[ \t]*комн"
    r"|\b(однушк|двушк|трешк)")
STUDIO = re.compile(r"studiya|студи|\bкомнат[ауы]?\b")
WORD_ROOMS = {"bir": 1, "ikki": 2, "iki": 2, "uch": 3, "tort": 4, "besh": 5,
              "одно": 1, "дву": 2, "трех": 3, "четырех": 4, "пяти": 5,
              "однушк": 1, "двушк": 2, "трешк": 3}


def extract_rooms(t):
    """t = soft(text). Returns number of rooms, or None."""
    m = ROOMS_LABEL.search(t)
    if m:
        v = int(m.group(1) or m.group(2))
        if 1 <= v <= 8:
            return v
    m = ROOMS_DIGIT.search(t)
    if m and 1 <= int(m.group(1)) <= 8:
        return int(m.group(1))
    m = ROOMS_WORD.search(t)
    if m:
        return WORD_ROOMS[next(g for g in m.groups() if g)]
    if STUDIO.search(t):
        return 1
    return None


# ======================================================================
# Reading number of people (shared flats)
# ======================================================================
PEOPLE_TOTAL = re.compile(r"kishi[ \t]+turadi[ \t]*[:=\-]?[ \t]*(\d{1,2})")    # Umumiy necha kishi turadi: 7
PEOPLE_NEEDED = re.compile(r"kishi[ \t]+kerak[ \t]*[:=\-]?[ \t]*(\d{1,2})")    # Nechi kishi kerak: 2


def extract_people(t):
    """Returns (people living there in total, people still needed)."""
    a, b = PEOPLE_TOTAL.search(t), PEOPLE_NEEDED.search(t)
    return (int(a.group(1)) if a else None), (int(b.group(1)) if b else None)


# ======================================================================
# Data
# ======================================================================
@dataclass
class Ad:
    channel: str
    msg_id: int
    text: str
    date: datetime
    genders: set = field(default_factory=set)
    areas: list = field(default_factory=list)
    rooms: int | None = None
    price_usd: float | None = None
    people_total: int | None = None
    people_needed: int | None = None

    @property
    def link(self):
        return f"https://t.me/{self.channel}/{self.msg_id}"


@dataclass
class Filters:
    loc_aliases: list = field(default_factory=list)   # list of alias lists
    price_min: float | None = None
    price_max: float | None = None
    rooms_min: int | None = None
    rooms_max: int | None = None
    gender: str | None = None
    strict: bool = False
    days: int = 3


def ad_key(n):
    return hashlib.sha1(re.sub(r"\W+", "", n)[:400].encode("utf-8")).hexdigest()


# ======================================================================
# Fetching and the full pipeline
# ======================================================================
async def fetch_channel(name, cutoff, sem):
    out, error = [], None
    async with sem:
        try:
            async for m in user.iter_messages(name, limit=PER_CHANNEL_LIMIT):
                if m.date < cutoff:
                    break
                if m.message and len(m.message) >= 40:
                    out.append((name, m.id, m.message, m.date))
        except Exception as e:
            error = f"{name}: {e}"
    return out, error


async def find_rentals(f):
    stats = {"fetched": 0, "basic": 0, "unique": 0, "unknown": 0, "errors": []}
    cutoff = datetime.now(timezone.utc) - timedelta(days=f.days)
    sem = asyncio.Semaphore(3)
    fetched = await asyncio.gather(*(fetch_channel(c, cutoff, sem) for c in CHANNELS))

    results, seen = [], set()
    for raw, error in fetched:
        if error:
            stats["errors"].append(error)
        for channel, msg_id, text, date in raw:
            stats["fetched"] += 1
            n = norm(text)

            # Step 1: word filters
            if not any(w in n for w in RENT_WORDS):
                continue
            if any(w in n for w in SKIP_WORDS):
                continue
            if any(w in n[:40] for w in SEEKER_START):
                continue
            if f.loc_aliases and not any(a in n for aliases in f.loc_aliases for a in aliases):
                continue
            genders = detect_genders(n)
            if not gender_ok(genders, f.gender, f.strict):
                continue
            stats["basic"] += 1

            # Step 2: remove duplicates
            key = ad_key(n)
            if key in seen:
                continue
            seen.add(key)
            stats["unique"] += 1

            # Step 3: read price and rooms
            s = soft(text)
            total, needed = extract_people(s)
            ad = Ad(channel, msg_id, text, date, genders, find_areas(n),
                    rooms=extract_rooms(s), price_usd=extract_price_usd(s),
                    people_total=total, people_needed=needed)

            # Step 4: price and rooms filters
            if f.price_min is not None or f.price_max is not None:
                if ad.price_usd is None:
                    stats["unknown"] += 1
                    continue
                if f.price_min is not None and ad.price_usd < f.price_min:
                    continue
                if f.price_max is not None and ad.price_usd > f.price_max:
                    continue
            if f.rooms_min is not None or f.rooms_max is not None:
                if ad.rooms is None:
                    stats["unknown"] += 1
                    continue
                if f.rooms_min is not None and ad.rooms < f.rooms_min:
                    continue
                if f.rooms_max is not None and ad.rooms > f.rooms_max:
                    continue
            results.append(ad)

    results.sort(key=lambda a: (a.price_usd is None, a.price_usd or 0, -a.date.timestamp()))
    return results, stats


# ======================================================================
# Reading the /rent command
# ======================================================================
def parse_pairs(args):
    """'loc=a, b price=80-95$' -> {'loc': 'a, b', 'price': '80-95$'}  (a value runs until the next key=)"""
    parts = re.split(r"(\b\w+)=", args or "")
    if parts[0].strip():
        raise ValueError("Write filters like this: loc=chilonzor price=100-300. Send /rent help to see all of them.")
    return {parts[i].lower(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def parse_range(s, cast, single_is_max):
    s = re.sub(r"[^\d.\-]", "", s.replace("–", "-").replace("—", "-"))   # "80-95$" -> "80-95"
    if "-" in s:
        lo, hi = s.split("-", 1)
        return (cast(lo) if lo else None), (cast(hi) if hi else None)
    v = cast(s)
    return (None, v) if single_is_max else (v, v)


def parse_filters(args):
    f = Filters()
    pairs = parse_pairs(args)
    unknown = set(pairs) - {"loc", "price", "rooms", "gender", "days", "strict"}
    if unknown:
        raise ValueError(f"Unknown filter: {', '.join(sorted(unknown))}. Send /rent help to see the filters.")
    try:
        if "loc" in pairs:
            f.loc_aliases = [resolve_location(t) for t in re.split(r"[,\s]+", pairs["loc"]) if t]
        if "price" in pairs:
            f.price_min, f.price_max = parse_range(pairs["price"], float, True)
        if "rooms" in pairs:
            f.rooms_min, f.rooms_max = parse_range(pairs["rooms"], int, False)
        if "days" in pairs:
            f.days = max(1, min(30, int(re.sub(r"\D", "", pairs["days"]))))
    except ValueError:
        raise ValueError("I could not read price, rooms or days. Example: price=150-300 rooms=2 days=5")
    if "gender" in pairs:
        g = pairs["gender"].lower().strip(" ,")
        if g not in GENDER_NAMES and g != "any":
            raise ValueError("gender must be: girls, boys, family or any")
        f.gender = GENDER_NAMES.get(g)
    f.strict = pairs.get("strict", "").strip() == "1"
    return f


RENT_HELP = (
    "All filters are optional. /rent alone shows the newest ads of the last 3 days.\n\n"
    "Example:\n"
    "/rent loc=chilonzor,yunusobod price=150-300 rooms=2 gender=girls days=5\n\n"
    "loc - district or any place word (use _ for spaces: loc=mirzo_ulugbek)\n"
    "price - in dollars: 150-300, or 300 (up to 300)\n"
    "rooms - 2, or 1-2\n"
    "gender - girls, boys, family, any\n"
    "days - how many days back to look (default 3, max 30)\n"
    "strict=1 - hide ads that do not write any gender\n\n"
    "/check <ad text> - shows what I understand from one ad"
)


# ======================================================================
# Output
# ======================================================================
def format_ad(a):
    price = f"${a.price_usd:,.0f}" if a.price_usd is not None else "price ?"
    rooms = f"{a.rooms} rooms" if a.rooms else "rooms ?"
    gender = ", ".join(sorted(a.genders)) or "gender not stated"
    area = ", ".join(a.areas) or "area not named"
    snippet = a.text.replace("\n", " ")[:180]
    people = ""
    if a.people_needed:
        people = f" | needs {a.people_needed}" + (f" (of {a.people_total} living there)" if a.people_total else "")
    return (f"<b>{price}</b> | {rooms} | {gender} | {html.escape(area)}{people} | {a.date:%d.%m}\n"
            f"{html.escape(snippet)}...\n{a.link}")


async def send_results(event, lines):
    if not lines:
        await event.reply("Nothing found.")
        return
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 2 > 3800:
            await event.reply(chunk, parse_mode="html", link_preview=False)
            chunk = ""
        chunk += line + "\n\n"
    if chunk:
        await event.reply(chunk, parse_mode="html", link_preview=False)


# ======================================================================
# Old commands: /search and /find
# ======================================================================
def make_link(chat, msg_id):
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    if getattr(chat, "broadcast", False) or getattr(chat, "megagroup", False):
        return f"https://t.me/c/{chat.id}/{msg_id}"
    return None


def chat_title(chat):
    if chat is None:
        return "Unknown"
    title = getattr(chat, "title", None)
    if title:
        return title
    name = " ".join(filter(None, [getattr(chat, "first_name", None), getattr(chat, "last_name", None)]))
    return name or "Unknown"


def format_result(chat, msg):
    text = (msg.message or "[media / no text]").replace("\n", " ")
    if len(text) > 200:
        text = text[:200] + "..."
    link = make_link(chat, msg.id)
    line = f"<b>{html.escape(chat_title(chat))}</b> ({msg.date:%Y-%m-%d %H:%M})\n{html.escape(text)}"
    return line + (f"\n{link}" if link else "")


async def global_search(query):
    res = await user(SearchGlobalRequest(
        q=query, filter=InputMessagesFilterEmpty(), min_date=None, max_date=None,
        offset_rate=0, offset_peer=InputPeerEmpty(), offset_id=0, limit=10))
    peers = {c.id: c for c in res.chats}
    peers.update({u.id: u for u in res.users})
    lines = []
    for m in res.messages:
        pid = (getattr(m.peer_id, "channel_id", None)
               or getattr(m.peer_id, "chat_id", None)
               or getattr(m.peer_id, "user_id", None))
        lines.append(format_result(peers.get(pid), m))
    return lines


async def channel_search(target, query):
    entity = await user.get_entity(target)
    return [format_result(entity, m) async for m in user.iter_messages(entity, search=query, limit=10)]


# ======================================================================
# Bot handlers
# ======================================================================
START_TEXT = (
    "Commands:\n"
    "/rent - find apartments in Tashkent (filters are optional, /rent help shows them)\n"
    "/check text - see what I understand from one ad\n"
    "/search text - search all my chats and public channels\n"
    "/find @channel text - search inside one channel"
)


@bot.on(events.NewMessage(pattern=r"^/start", incoming=True))
async def start_handler(event):
    if event.sender_id == OWNER_ID:
        await event.reply(START_TEXT)


@bot.on(events.NewMessage(pattern=r"(?s)^/rent\b(.*)", incoming=True))
async def rent_handler(event):
    if event.sender_id != OWNER_ID:
        return
    args = event.pattern_match.group(1).strip()
    if args == "help":
        await event.reply(RENT_HELP)
        return
    if not CHANNELS:
        await event.reply("Add your channels to CHANNELS in the .env file first.")
        return
    try:
        f = parse_filters(args)
    except ValueError as e:
        await event.reply(str(e))
        return

    await event.reply(f"Looking at {len(CHANNELS)} channels, last {f.days} days...")
    try:
        results, s = await find_rentals(f)
    except FloodWaitError as e:
        await event.reply(f"Telegram says wait {e.seconds} seconds, then try again.")
        return
    except Exception as e:
        await event.reply(f"Error: {e}")
        return

    summary = (f"Checked {s['fetched']} messages -> {s['basic']} passed word filters -> "
               f"{s['unique']} unique -> {len(results)} match.")
    if s["unknown"]:
        summary += f"\n{s['unknown']} ads hidden because price or rooms was not written."
    for err in s["errors"]:
        summary += f"\nChannel problem: {err}"
    await event.reply(summary)
    await send_results(event, [format_ad(a) for a in results[:MAX_RESULTS]])


@bot.on(events.NewMessage(pattern=r"(?s)^/check\s+(.+)", incoming=True))
async def check_handler(event):
    """Paste one ad to see what the rules understand. Good for finding mistakes."""
    if event.sender_id != OWNER_ID:
        return
    text = event.pattern_match.group(1)
    n, s = norm(text), soft(text)
    price = extract_price_usd(s)
    rooms = extract_rooms(s)
    people = extract_people(s)
    genders = ", ".join(sorted(detect_genders(n))) or "not stated"
    areas = ", ".join(find_areas(n)) or "not found"
    is_rent = any(w in n for w in RENT_WORDS)
    skipped = [w for w in SKIP_WORDS if w in n]
    await event.reply(
        f"Rent ad: {'yes' if is_rent else 'NO (no rent word)'}\n"
        f"Removed by: {', '.join(skipped) if skipped else '-'}\n"
        f"Price: {f'${price:,.0f}' if price is not None else 'not found'}\n"
        f"Rooms: {rooms if rooms else 'not found'}\n"
        f"People: {'%s living there' % people[0] if people[0] else 'total not found'}, "
        f"{'%s needed' % people[1] if people[1] else 'needed not found'}\n"
        f"Gender: {genders}\n"
        f"Area: {areas}"
    )


@bot.on(events.NewMessage(pattern=r"^/search\s+(.+)", incoming=True))
async def search_handler(event):
    if event.sender_id != OWNER_ID:
        return
    query = event.pattern_match.group(1).strip()
    await event.reply(f"Searching for: {query} ...")
    try:
        await send_results(event, await global_search(query))
    except FloodWaitError as e:
        await event.reply(f"Telegram says wait {e.seconds} seconds, then try again.")
    except Exception as e:
        await event.reply(f"Error: {e}")


@bot.on(events.NewMessage(pattern=r"^/find\s+(\S+)\s+(.+)", incoming=True))
async def find_handler(event):
    if event.sender_id != OWNER_ID:
        return
    target, query = event.pattern_match.group(1).strip(), event.pattern_match.group(2).strip()
    await event.reply(f"Searching {target} for: {query} ...")
    try:
        await send_results(event, await channel_search(target, query))
    except FloodWaitError as e:
        await event.reply(f"Telegram says wait {e.seconds} seconds, then try again.")
    except Exception as e:
        await event.reply(f"Error: {e}")


async def main():
    await user.start(phone=lambda: input("Your phone number (+998...): "))
    await bot.start(bot_token=BOT_TOKEN)
    print("Bot is running. Press Ctrl+C to stop.")
    await asyncio.gather(user.run_until_disconnected(), bot.run_until_disconnected())


if __name__ == "__main__":
    asyncio.run(main())