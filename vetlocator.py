"""Vet locator library - generated from harley_vet_locator.ipynb; edit the notebook, not this file."""
# vetlocator | 1 config
import os, re, json, math, time, html, hashlib, pathlib, datetime, difflib
import numpy as np
import pandas as pd
import requests

CONTACT = os.environ.get("VET_LOCATOR_CONTACT", "vet-locator user")      # OSM services ask for an identifiable User-Agent
UA = f"vet-locator/1.0 ({CONTACT})"
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")           # empty = run on OpenStreetMap only

OSRM_URL = "https://router.project-osrm.org/route/v1/driving/"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = ["https://overpass.openstreetmap.fr/api/interpreter",
                 "https://overpass-api.de/api/interpreter",
                 "https://lz4.overpass-api.de/api/interpreter",
                 "https://overpass.private.coffee/api/interpreter",
                 "https://overpass.kumi.systems/api/interpreter"]
PLACES_NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
PLACES_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"

CACHE_DIR = pathlib.Path("cache"); CACHE_DIR.mkdir(exist_ok=True)

TYPE_ORDER = ["ER", "NIGHT", "EXT", "DAY", "UNK"]
TYPE_COLOR = {"ER": "#ff5252", "NIGHT": "#ffab2e", "EXT": "#ffe14d", "DAY": "#4d9bff", "UNK": "#9aa3b2"}
TYPE_LABEL = {"ER": "24/7 ER", "NIGHT": "night / late", "EXT": "extended day", "DAY": "daytime GP", "UNK": "hours unknown - call"}


def _cache_path(kind, key):
    return CACHE_DIR / f"{kind}_{hashlib.md5(key.encode()).hexdigest()[:12]}.json"


def _get_json(url, *, params=None, data=None, headers=None, method="GET", timeout=60, tries=3, backoff=2.0):
    """HTTP with retries; every call carries the User-Agent the OSM services require."""
    h = {"User-Agent": UA}; h.update(headers or {})
    last = None
    for i in range(tries):
        try:
            r = requests.request(method, url, params=params, data=data, headers=h, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)[:80]
        time.sleep(backoff * (i + 1))
    raise RuntimeError(f"{url.split('/')[2]}: {last}")

# vetlocator | 2 geometry
def haversine_mi(lat1, lon1, lat2, lon2):
    """Great-circle miles; works on scalars or numpy arrays (broadcasting)."""
    R = 3958.7613
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(a))


def geocode(query):
    """'lat,lng' text or a (lat, lng) pair passes through; anything else goes to Nominatim (cached, 1 request/s)."""
    if isinstance(query, (tuple, list)):
        return float(query[0]), float(query[1])
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", str(query))
    if m:
        return float(m.group(1)), float(m.group(2))
    p = _cache_path("geocode", query)
    if p.exists():
        j = json.loads(p.read_text())
    else:
        j = _get_json(NOMINATIM_URL, params={"q": query, "format": "json", "limit": 1})
        p.write_text(json.dumps(j)); time.sleep(1.0)
    if not j:
        raise ValueError(f"Could not geocode: {query!r} - try 'lat,lng' or a fuller address")
    return float(j[0]["lat"]), float(j[0]["lon"])


def route_osrm(points):
    """Driving route through (lat, lng) points -> dict(lat, lng, cum_mi, total_mi, drive_h)."""
    key = ";".join(f"{a:.5f},{b:.5f}" for a, b in points)
    p = _cache_path("route", key)
    if p.exists():
        j = json.loads(p.read_text())
    else:
        coords = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in points)
        j = _get_json(OSRM_URL + coords, params={"overview": "full", "geometries": "geojson"}, timeout=90)
        if j.get("code") != "Ok":
            raise RuntimeError(f"OSRM: {j.get('code')} {j.get('message', '')}")
        p.write_text(json.dumps(j))
    xy = np.array(j["routes"][0]["geometry"]["coordinates"])       # [lng, lat]
    lat, lng = xy[:, 1], xy[:, 0]
    seg = haversine_mi(lat[:-1], lng[:-1], lat[1:], lng[1:])
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    return {"lat": lat, "lng": lng, "cum_mi": cum, "total_mi": float(cum[-1]),
            "drive_h": j["routes"][0]["duration"] / 3600.0}


def resample_route(route, step_mi=0.25):
    """Even spacing along the route, so the nearest vertex is within 1/8 mile of the true nearest point."""
    target = np.arange(0, route["total_mi"], step_mi)
    lat = np.interp(target, route["cum_mi"], route["lat"])
    lng = np.interp(target, route["cum_mi"], route["lng"])
    return {"lat": lat, "lng": lng, "cum_mi": target, "total_mi": route["total_mi"]}


def locate_on_route(df, route, chunk=200):
    """Adds mile (distance along the route at the nearest point) and off (straight-line miles from the route)."""
    rs = resample_route(route)
    miles, offs = np.empty(len(df)), np.empty(len(df))
    lat, lng = df["lat"].to_numpy(float), df["lng"].to_numpy(float)
    for i in range(0, len(df), chunk):
        d = haversine_mi(lat[i:i + chunk, None], lng[i:i + chunk, None], rs["lat"][None, :], rs["lng"][None, :])
        k = d.argmin(axis=1)
        miles[i:i + chunk] = rs["cum_mi"][k]; offs[i:i + chunk] = d[np.arange(len(k)), k]
    out = df.copy(); out["mile"] = np.round(miles).astype(int); out["off"] = np.round(offs, 1)
    return out

# vetlocator | 3 hours
# One schedule format for every source: {day 0..6 (Mon..Sun): [(open_min, close_min, is_24h), ...]}
# close_min > 1440 means the clinic closes after midnight.
DAY_TOKENS = {"mo": 0, "m": 0, "mon": 0, "tu": 1, "t": 1, "tue": 1, "tues": 1, "we": 2, "w": 2, "wed": 2,
              "th": 3, "thu": 3, "thur": 3, "thurs": 3, "fr": 4, "f": 4, "fri": 4, "sa": 5, "sat": 5,
              "su": 6, "sun": 6}


def classify_schedule(sched):
    """The rule behind the colours.  ER = open 24 h every day.  NIGHT = any 24 h day, or closes at/after 10 pm
    or past midnight.  EXT = closes at/after 7 pm on two or more days, or open all 7 days.  DAY = everything else."""
    if not sched or not any(sched.values()):
        return "UNK"
    days = [d for d, v in sched.items() if v]
    if len(days) == 7 and all(any(iv[2] for iv in sched[d]) for d in days):
        return "ER"
    any24 = any(iv[2] for d in days for iv in sched[d])
    latest_close = max((iv[1] for d in days for iv in sched[d] if not iv[2]), default=0)
    if any24 or latest_close >= 22 * 60:
        return "NIGHT"
    late_days = sum(1 for d in days if any(iv[1] >= 19 * 60 for iv in sched[d]))
    if late_days >= 2 or len(days) == 7:
        return "EXT"
    return "DAY"


def _expand_days(tok):
    """'Mo-Fr' / 'Mo,We' / 'M/W/F' / 'Sa-Su' -> list of day indexes; None if the token is not a day spec."""
    tok = tok.strip().lower().replace("/", ",")
    if tok in ("daily", "7 days", "everyday", "every day", "mo-su", "m-su"):
        return list(range(7))
    out = []
    for part in tok.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = [DAY_TOKENS.get(x.strip()) for x in part.split("-", 1)]
            if a is None or b is None:
                return None
            out += [(a + i) % 7 for i in range((b - a) % 7 + 1)]
        else:
            d = DAY_TOKENS.get(part)
            if d is None:
                return None
            out.append(d)
    return sorted(set(out))


def _to_min(h, m, ampm, is_close=False):
    """12-hour clock -> minutes.  Bare hours follow clinic convention: an open before 6 is afternoon ('1-5'),
    other opens are am, closes are pm, 12 is noon."""
    h = int(h); m = int(m or 0)
    if ampm:
        if ampm.startswith("p") and h != 12: h += 12
        if ampm.startswith("a") and h == 12: h = 0
    elif is_close and h < 12:
        h += 12
    elif not is_close and h < 6:
        h += 12
    return h * 60 + m


def parse_hours_loose(text):
    """Parses the shorthand used in the hand-verified maps and free-text listings:
    'M-F 8-6; Sa 8-12', 'Daily 8am-8pm', 'M-Th 6:30pm-7:30am; Sa-Su 24h', '24/7', 'M/Tu/Th 8-6; W 8-8pm; F-Su closed'.
    Returns a schedule dict, or None when nothing parseable is found."""
    if not text:
        return None
    t = str(text).lower().replace("–", "-").replace("—", "-").replace(" to ", "-")
    t = t.replace("midnight", "12am").replace("mid", "12am").replace("noon", "12pm")
    t = re.sub(r"\bw(ee)?k(e)?nds?\b", "sa-su", t); t = re.sub(r"\bweekdays\b", "m-f", t)
    t = re.sub(r"\b(open|hours|limited|call first|call|only|er|urgent|night|daytime|some)\b:?", " ", t)
    if re.search(r"24\s*/\s*7", t) and not re.search(r"\d\s*-\s*\d", t):
        return {d: [(0, 1440, True)] for d in range(7)}
    sched, found = {}, False
    day_re = r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun|mo|tu|we|th|fr|sa|su|m|t|w|f|daily|everyday)(?:day|sday|nesday|rsday|urday|nday)?"
    dayspec_re = rf"((?:{day_re}(?:\s*[-,/]\s*{day_re})*)\s*:?)"
    time_re = r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m?\.?(?![a-z]))?"
    range_re = rf"{time_re}\s*-\s*{time_re}"
    for rule in re.split(r"[;\n]|\band\b", t):
        rule = rule.strip()
        if not rule:
            continue
        days = list(range(7))
        m = re.match(dayspec_re, rule)
        if m:
            ds = re.sub(r"(day|sday|nesday|rsday|urday|nday)\b", "", m.group(1).rstrip(":").strip())
            ex = _expand_days(ds)
            if ex is None:
                continue
            days = ex; rule = rule[m.end():]
        if re.search(r"24\s*h|24\s*/\s*7|24 hours", rule):
            for d in days: sched.setdefault(d, []).append((0, 1440, True))
            found = True; continue
        if re.search(r"\b(off|closed)\b", rule):
            m2 = re.search(dayspec_re, rule)                       # 'closed Wed' / 'F-Su closed'
            if m2 and not m:
                ex = _expand_days(re.sub(r"(day|sday|nesday|rsday|urday|nday)\b", "", m2.group(1).rstrip(":").strip()))
                days = ex if ex is not None else days
            for d in days: sched[d] = []
            found = True; continue
        for r in re.finditer(range_re, rule):
            h1, m1, ap1, h2, m2, ap2 = r.groups()
            o = _to_min(h1, m1, ap1); c = _to_min(h2, m2, ap2, is_close=True)
            if ap1 and ap1.startswith("p") and not ap2 and int(h2) < 12:      # '3pm-11' -> 11 pm
                c = _to_min(h2, m2, "pm")
            if c <= o:
                c += 1440
            for d in days: sched.setdefault(d, []).append((o, c, False))
            found = True
    return sched if found else None


def parse_osm_opening_hours(text):
    """Common OSM opening_hours patterns: '24/7', 'Mo-Fr 08:00-18:00; Sa 08:00-12:00', 'Mo-Su 00:00-24:00'.
    Month/week/holiday rules are skipped; unparseable strings return None."""
    if not text:
        return None
    t = text.strip()
    if t == "24/7":
        return {d: [(0, 1440, True)] for d in range(7)}
    sched, found = {}, False
    for rule in t.split(";"):
        rule = rule.strip()
        if not rule or re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|PH|SH|week)\b", rule):
            continue
        days = list(range(7))
        m = re.match(r"((?:Mo|Tu|We|Th|Fr|Sa|Su)(?:\s*[-,]\s*(?:Mo|Tu|We|Th|Fr|Sa|Su))*)\s*", rule)
        if m:
            ex = _expand_days(m.group(1))
            if ex is None:
                continue
            days = ex; rule = rule[m.end():]
        if rule.strip().lower() in ("off", "closed"):
            for d in days: sched[d] = []
            found = True; continue
        if "24/7" in rule or "00:00-24:00" in rule:
            for d in days: sched.setdefault(d, []).append((0, 1440, True))
            found = True; continue
        for r in re.finditer(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", rule):
            o = int(r.group(1)) * 60 + int(r.group(2)); c = int(r.group(3)) * 60 + int(r.group(4))
            if c <= o:
                c += 1440
            for d in days: sched.setdefault(d, []).append((o, c, False))
            found = True
    return sched if found else None


def parse_google_periods(periods):
    """Google Places 'regularOpeningHours.periods' (day 0 = Sunday) -> schedule dict."""
    if not periods:
        return None
    sched = {}
    for p in periods:
        o = p.get("open", {}); c = p.get("close")
        d = (int(o.get("day", 0)) + 6) % 7
        if c is None:                                          # Google encodes 24/7 as one open period with no close
            return {k: [(0, 1440, True)] for k in range(7)}
        omin = int(o.get("hour", 0)) * 60 + int(o.get("minute", 0))
        cmin = int(c.get("hour", 0)) * 60 + int(c.get("minute", 0))
        if int(c.get("day", o.get("day", 0))) != int(o.get("day", 0)):
            cmin += 1440
        if cmin - omin >= 1440:
            sched.setdefault(d, []).append((0, 1440, True))
        else:
            sched.setdefault(d, []).append((omin, cmin, False))
    return sched


_DAY_SHORT = ["M", "Tu", "W", "Th", "F", "Sa", "Su"]


def _fmt_time(mins, is_close=False):
    mins = mins % 1440 if mins != 1440 else 1440
    if mins == 1440:
        return "mid"
    h, m = divmod(mins, 60)
    mm = f":{m:02d}" if m else ""
    if h == 0:
        return f"12{mm}am"
    if h < 12:
        return f"{h}{mm}" + ("am" if is_close else "")
    if h == 12:
        return f"12{mm}" + ("pm" if not is_close else "")
    return f"{h - 12}{mm}" + ("pm" if (not is_close or h >= 19) else "")


def _fmt_days(days):
    """[0,1,2,3,4] -> 'M-F'; [0,2,4] -> 'M/W/F'; [0,1,3] -> 'M/Tu/Th'."""
    runs, i = [], 0
    while i < len(days):
        j = i
        while j + 1 < len(days) and days[j + 1] == days[j] + 1:
            j += 1
        runs.append(_DAY_SHORT[days[i]] if j == i else (f"{_DAY_SHORT[days[i]]}-{_DAY_SHORT[days[j]]}" if j > i + 1 else f"{_DAY_SHORT[days[i]]}/{_DAY_SHORT[days[j]]}"))
        i = j + 1
    return "/".join(runs)


def format_schedule(sched):
    """Schedule dict -> the shorthand of the hand-verified maps: 'M-F 8-6; Sa 8-12', 'M-Th 6pm-7:30am; Sa/Su 24h', 'OPEN 24/7'."""
    if not sched or not any(sched.values()):
        return ""
    if len(sched) == 7 and all(any(iv[2] for iv in v) for v in sched.values()):
        return "OPEN 24/7"
    groups = {}
    for d in range(7):
        ivs = tuple(sorted(sched.get(d, [])))
        if ivs:
            groups.setdefault(ivs, []).append(d)
    parts = []
    for ivs, days in sorted(groups.items(), key=lambda kv: kv[1][0]):
        txt = " & ".join("24h" if iv[2] else f"{_fmt_time(iv[0])}-{_fmt_time(iv[1], True)}" for iv in ivs)
        parts.append(f"{'Daily' if len(days) == 7 else _fmt_days(days)} {txt}")
    return "; ".join(parts)


NAME_ER_RE = re.compile(r"emergenc|\b24[\s/-]*(7|h|hr|hour)|urgent|after[\s-]*hours|\bER\b|critical care", re.I)

# OSM tags amenity=veterinary loosely; these are not places to take a sick dog at 2 am.
NOISE_RE = re.compile(r"groom|day ?care|boarding|kennel|cafe|grill|adoption|shelter|humane society|pet store|petco\b|petsmart\b|"
                      r"transport|equine|dental|eye center|ophthalm|dermatolog|feline fix|spay|neuter|supply|supplies|covetrus|"
                      r"\bdogg\b|dog walk|pet sitting|crematory|cremation|taxiderm|rehab", re.I)


def classify_row(hours_text, sched, name):
    """-> (type, display hours).  Unknown hours on an emergency-sounding name get flagged for a call."""
    typ = classify_schedule(sched)
    shown = format_schedule(sched) if sched else (hours_text or "hours not listed")
    if typ == "UNK" and name and NAME_ER_RE.search(name):
        return "UNK", shown + " - name suggests emergency/urgent: CALL to confirm"
    return typ, shown

# vetlocator | 4 curated
# The 231 clinics hand-verified against Google listings (Sept 2026) for Harley's three maps:
# Denver metro (home), Denver-Flint (I-76/I-80/I-69, with the I-94 variant) and Denver-League City (I-25/US-287/I-45).
CURATED_CSV = """name,town,lat,lng,phone,hours,rating,typ,corridor
Sploot Veterinary Care,Arvada,39.8356,-105.0832,720-780-8874,DAILY 8am-8pm (Sun too),4.8,EXT,home
Arvada Veterinary Hospital,Arvada,39.817,-105.0831,303-424-4439,Tu-F 8-6; M 9-5; Sa 8-4,4.6,DAY,home
Church Ranch Veterinary Center,Westminster,39.8803,-105.0892,303-732-5819,"OPEN 24/7 (GP + urgent care, surgery)",4.3,ER,home
Olde Town Veterinary Clinic,Arvada,39.8023,-105.0835,720-328-3301,M-F 8-5,4.9,DAY,home
UrgentVet Broomfield,Broomfield,39.9145,-105.0488,303-622-1600,M-F 3-11pm; Sa-Su 10-8,4.7,NIGHT,home
Animal Urgent Care - W 64th Ave,Arvada,39.8118,-105.1392,303-420-7387,OPEN 24/7,4.6,ER,home
WRAH - Wheat Ridge Animal Hospital (HIS SPECIALTY HOME),Wheat Ridge,39.7771,-105.111,303-424-3325,"OPEN 24/7 + specialty: neuro (Dr. Djani), IM, surgery",4.1,ER,home
RiNoVet Animal Emergency,Denver,39.778,-104.9797,303-458-5555,OPEN 24/7,4.2,ER,home
Arvada Flats Veterinary Hospital,Arvada,39.8111,-105.1572,303-467-9212,M-F 8-7; Sa 8-1,4.8,EXT,home
Northside Emergency Pet Clinic,Westminster,39.9225,-104.9986,303-252-7722,OPEN 24/7,4.2,ER,home
VEG ER - Edgewater,Edgewater,39.7478,-105.0565,720-996-1200,OPEN 24/7,4.8,ER,home
VEG ER - Colfax,Denver,39.7404,-104.9426,720-574-9834,OPEN 24/7,4.8,ER,home
Evolution Veterinary Specialty & Emergency,Lakewood,39.7154,-105.1367,720-510-7707,OPEN 24/7,4.0,ER,home
BluePearl Pet Hospital - Lafayette,Lafayette,39.9874,-105.1167,720-699-7766,OPEN 24/7 specialty + ER,4.6,ER,home
VRCC Veterinary Specialty & Emergency,Englewood,39.6524,-104.9987,303-923-5790,OPEN 24/7,4.2,ER,home
VEG ER - Hampden,Denver,39.6534,-104.9171,720-739-6003,OPEN 24/7,4.7,ER,home
CSU Veterinary Teaching Hospital - SURGICAL HOME (Dr. Monnet),Fort Collins,40.5719,-105.085,970-297-5000,OPEN 24/7 academic,4.4,ER,home
Animal Care Hospital of Morris,Morris IL,41.3715,-88.4244,815-941-9924,M-F 8-5; Sa 8-12,4.8,DAY,flint
Pine Bluff Animal Hospital,Morris IL,41.3454,-88.2681,815-942-5365,M-F 8-5:30; Sa 8-12,4.8,DAY,flint
Lakewood Animal Hospital,Morris IL,41.369,-88.4426,815-942-1199,M/Tu/Th/F 8-5:30,4.9,DAY,flint
Parkview Animal Hospital,Newton IA,41.6995,-93.0265,641-792-0340,M-F 8-5,4.7,DAY,flint
Newton Animal Clinic,Newton IA,41.6988,-93.0826,641-433-4815,M-F 8-5,4.6,DAY,flint
Riverside Animal Hospital,Kearney NE,40.6759,-99.0977,308-234-2617,M-F 8-5:30; Sa 8-12,4.6,DAY,flint
Arbor View Animal Hospital (US-6),Valparaiso IN,41.5498,-87.1141,219-762-7267,M/Tu/Th 8-8pm; W/F 8-6; Sa 8-1,4.7,EXT,flint
Valparaiso Animal Hospital,Valparaiso IN,41.4761,-87.0568,219-462-1862,M-F 7-6; Sa 8-2,4.6,DAY,flint
McAfee Animal Hospital,Valparaiso IN,41.4617,-87.0175,219-462-5901,M-F 8-12 & 1:30-5:30; Sa 8-12,4.8,DAY,flint
All Creature Features Animal Hospital,La Porte IN,41.5452,-86.7017,219-393-3558,M-F 7:30-6; Sa 7-1,4.6,DAY,flint
LaPorte Animal Hospital,La Porte IN,41.5967,-86.7442,219-362-2612,M-W 8-5:30; F 8-5,4.5,DAY,flint
Michigan City Animal Hospital,Michigan City IN,41.7079,-86.868,219-872-4191,M-F 7-6; Sa 8-12; SUN 1-7pm,4.8,EXT,flint
St Joseph Animal Wellness Clinic,St Joseph MI,42.0723,-86.4786,269-429-6966,M-F 8-5:30 (Tu/Th to 6),4.9,DAY,flint
Nickerson Animal Health Center,Benton Harbor MI,42.0747,-86.436,269-925-8835,M-F 8-6 (midday close),4.7,DAY,flint
Sunset Coast Veterinary Clinic,St Joseph MI,42.0439,-86.4348,269-408-0468,M-F ~8-5 (Tu/Th to 6),4.7,DAY,flint
Steiner SILS Affordable Vet Care,Fort Morgan CO,40.2537,-103.8257,970-441-1503,M-Tu 7:30-5; W 7:30-4; Th-Su closed,4.6,DAY,flint
Fort Morgan Veterinary Clinic,Fort Morgan CO,40.2472,-103.783,970-867-9477,M-F 8-5; Sa 9-12,4.5,DAY,flint
Red Arrow Animal Clinic,Paw Paw MI,42.2143,-85.9213,269-657-7081,M-W/F 8-5,4.2,DAY,flint
Paw Paw Veterinary Clinic,Paw Paw MI,42.2229,-85.8597,269-657-3114,M-Th 8-7pm; F 8-5; Sa 8-1; Su 9-11am,4.8,EXT,flint
Sterling Animal Clinic,Sterling CO,40.6247,-103.2325,970-521-0333,M-F 8-4,4.0,DAY,flint
Veterinary Medical Clinic,Sterling CO,40.6246,-103.199,970-522-3321,M-F 8-5; Sa 8-12,4.5,DAY,flint
Town & Country Animal Hospital,Charlotte MI,42.5226,-84.8365,517-543-2330,M/W 8-7pm; Tu/Th/F 8-5; Sa 8-12,4.6,EXT,flint
Apple Grove Veterinary Care,Charlotte MI,42.5525,-84.8016,517-543-6101,M/Tu/Th 8-7pm; W/F 8-5,4.6,EXT,flint
Baltzell Veterinary Hospital,Ogallala NE,41.126,-101.7386,308-284-4313,M-F 8-5:30; Sa 8-12,4.8,DAY,flint
Animal Clinic & Pharmacy,Ogallala NE,41.1226,-101.7367,308-284-2182,M-F 8-5; Sa 8-12,4.7,DAY,flint
Westfield Small Animal Clinic,North Platte NE,41.1347,-100.7851,308-534-4480,M-F 8-6; Sa 8-3,4.5,DAY,flint
Tender Hearts Veterinary Center,North Platte NE,41.1333,-100.7778,308-221-6915,M-F 8-5:30,4.5,DAY,flint
America's Heartland Animal Center,North Platte NE,41.0994,-100.7652,308-532-4880,M-F 7:30-5; Sa 8-12,4.4,DAY,flint
Stockman's Veterinary Clinic,North Platte NE,41.1658,-100.7583,308-532-7210,M-F 8-5:30; Sa 8-12,4.7,DAY,flint
B & B Veterinary Services,Lexington NE,40.8064,-99.7477,308-324-3411,M-F 8-12 & 1-5; Sa 8-10am,4.8,DAY,flint
Lexington Animal Clinic,Lexington NE,40.7819,-99.7622,308-324-2411,M-F 8:30-5,4.6,DAY,flint
Overton Veterinary Services,Overton NE,40.7504,-99.6158,308-324-7202,M-F 8-5:30; Sa 8-12,4.9,DAY,flint
Beebout Veterinary Medical Center,Kearney NE,40.7125,-99.1215,308-236-5912,M-Sa 7:30-6,4.6,DAY,flint
West Villa Animal Hospital,Kearney NE,40.699,-99.1157,308-244-8143,M-F 7:30-6; Sa 8-12; Su 4:30-6pm,4.5,DAY,flint
Cottonwood Veterinary Clinic,Kearney NE,40.7322,-99.0855,308-234-8118,M 7:30-8pm; Tu-Th 7:30-6; F 8-6,4.6,EXT,flint
Hilltop Pet Clinic,Kearney NE,40.7188,-99.0825,308-236-5912,M-F 7:30-9:30a & 3:30-6p; Sa 7:30-12; Su 3-6p,4.7,DAY,flint
Northgate Veterinary Clinic,Kearney NE,40.7288,-99.0757,308-234-9512,M-F 8-5,4.7,DAY,flint
Parks Veterinary,Grand Island NE,40.9557,-98.3985,308-384-6272,M-F 7:30-5; Sa 8-12,4.8,DAY,flint
Grand Island Veterinary Hospital,Grand Island NE,40.9139,-98.379,308-384-1641,M-F 8-5,4.5,DAY,flint
Pet Hospital,Grand Island NE,40.9136,-98.3787,308-384-4363,M-F 8-5; Sa 9-12,5.0,DAY,flint
Family Pet Clinic,Grand Island NE,40.9472,-98.3857,308-384-6147,M-F 8-5,4.6,DAY,flint
Animal Medical Clinic,Grand Island NE,40.9026,-98.338,308-382-6330,M-F 7:30-5; Sa 8-12,4.7,DAY,flint
South Locust Veterinary Clinic,Grand Island NE,40.9049,-98.3404,308-382-6412,M-F 8-5:30,4.7,DAY,flint
York Animal Clinic,York NE,40.8755,-97.5933,402-362-2894,M-F 7:30-5:30; Sa 7:30-12,4.8,DAY,flint
Sullivan Companion Animal Clinic,York NE,40.8656,-97.5928,402-745-6498,M-F 8-5; Sa 8-11am,4.7,DAY,flint
Vondra Veterinary Clinic,Lincoln NE,40.7851,-96.7537,402-477-1113,M-F 7:30-6; Sa 8-12,4.8,DAY,flint
Veterinary Emergency Services,Lincoln NE,40.7762,-96.7069,402-629-6925,NIGHT ER: M-F 6pm-7am; Sa noon-mid; Su 24h,4.4,NIGHT,flint
Nebraska Animal Medical Center,Lincoln NE,40.7545,-96.643,402-423-9100,M-F 7am-8pm; Sa-Su 8-5 (7 days!),4.3,EXT,flint
Pitts Veterinary Hospital,Lincoln NE,40.7695,-96.6885,402-423-4120,M-F 7-6; Sa 9-3,4.6,DAY,flint
Animal Care Clinic,Lincoln NE,40.8115,-96.6065,402-566-5056,M-F 7:30-6,4.8,DAY,flint
Clock Tower Animal Clinic,Lincoln NE,40.8178,-96.6299,402-489-6228,M-F 7:30-6,4.8,DAY,flint
Polaris Veterinary Center,Lincoln NE,40.8342,-96.5666,402-467-4469,M/W 7:30-8pm; Tu/Th/F 7:30-6; Sa 7:30-1,4.7,EXT,flint
Urgent Pet Care West,Omaha NE,41.2169,-96.1378,402-234-8834,Daily 8am-MIDNIGHT,4.3,NIGHT,flint
The Pet Clinic,Omaha NE,41.2365,-96.1339,402-330-3096,M-Th 8-7; F 8-6; Sa 8-2,4.8,DAY,flint
VCA MidWest Referral & Emergency,Omaha NE,41.2094,-96.0652,402-513-8741,M-Th 24h; F to 6am; Sa closed; Su 4pm-mid — CALL FIRST,3.4,NIGHT,flint
VEG ER for Pets - Omaha,Omaha NE,41.2917,-96.1191,402-205-7271,OPEN 24/7,4.8,ER,flint
Omaha Animal Medical Group,Omaha NE,41.2903,-96.1014,402-496-6075,M-F 7:30-6; Sa 8-12,4.8,DAY,flint
VCA 80 Dodge Animal Hospital,Omaha NE,41.2614,-96.0411,402-399-8100,M-Sa 7am-8pm; Su 10-6 (7 days),4.6,EXT,flint
Lone Tree Animal Care Center,Omaha NE,41.255,-95.9474,402-389-3356,M 8-8p; Tu 8-5; W 12-8p; Th 8-5; F 8-4,4.8,DAY,flint
24th Street Animal Clinic,Omaha NE,41.2284,-95.9474,402-345-2211,M-F 7:30-6,4.5,DAY,flint
Council Bluffs Veterinary Clinic,Council Bluffs IA,41.2498,-95.8484,712-323-2147,M-F 8-5:30,4.6,DAY,flint
Strohbehn Veterinary Clinic,Council Bluffs IA,41.2201,-95.8525,712-366-0556,M-F 8-5:30; Sa 8-12,4.4,DAY,flint
Valley View Veterinary Clinic,Council Bluffs IA,41.2331,-95.8176,712-256-7387,M-F 8-5:30; Sa 8-12,4.7,DAY,flint
The Animal Clinic,Council Bluffs IA,41.2596,-95.8132,712-323-0598,M/Tu/Th/F 7-5:30; W 7-12; Sa 7-12,4.4,DAY,flint
VEG ER for Pets - West Des Moines,West Des Moines IA,41.5729,-93.8012,515-259-6473,OPEN 24/7,4.9,ER,flint
UrgentVet West Des Moines,West Des Moines IA,41.5611,-93.7719,515-215-9696,M-F 3-11pm; Sa-Su 10-8,4.7,NIGHT,flint
Animal Doctors Veterinary Clinic,West Des Moines IA,41.5581,-93.7691,515-225-9555,M-F 7-6; Sa 8-12,4.8,DAY,flint
Animal Care Clinic West & Metro Cat,West Des Moines IA,41.6,-93.7443,515-224-4368,M-Th 7-7; F 7-6,4.6,EXT,flint
Iowa Veterinary Specialties,Des Moines IA,41.5597,-93.7019,515-280-3100,OPEN 24/7,3.6,ER,flint
Grand Avenue Veterinary Hospital,West Des Moines IA,41.5824,-93.7061,515-274-3489,M-F 7:30-6,4.8,DAY,flint
BluePearl Pet Hospital - Des Moines,Des Moines IA,41.6453,-93.697,515-727-4872,OPEN 24/7,4.3,ER,flint
Grinnell Veterinary Clinic,Grinnell IA,41.7092,-92.7281,641-236-4461,M-F 8-5:30; Sa 8-12,4.7,DAY,flint
All Pets Veterinary Hospital,Grinnell IA,41.7394,-92.7278,641-236-6869,M-W/F 8-5; Th 8-8pm; Sa 9-12,4.8,DAY,flint
BluePearl Pet Hospital - Cedar Rapids,Cedar Rapids IA,41.8872,-91.6788,319-841-5161,OPEN 24/7 (25 min N of Iowa City via I-380),4.4,ER,flint
Coralville Animal Hospital,Coralville IA,41.6764,-91.5822,319-351-6848,M-F 7:30-5:30,4.5,DAY,flint
Goosetown Animal Hospital,Coralville IA,41.6708,-91.5701,319-341-0386,M-F 8-6; Sa 8-12,5.0,DAY,flint
Gentle Heart Pet Clinic,Iowa City IA,41.6467,-91.5457,319-354-6696,M/Tu/F 8-5; W 8-7; Sa 8-12,4.7,DAY,flint
Animal Clinic Inc.,Iowa City IA,41.6462,-91.5296,319-337-2123,M-F 7:30-5; Sa 8-12,4.8,DAY,flint
Emergency Veterinary Service of Iowa City,Iowa City IA,41.6896,-91.4884,319-338-3605,DAYTIME ER: daily 8am-8pm (wknd to 6),3.2,EXT,flint
Bright Eyes & Bushy Tails (same bldg),Iowa City IA,41.6895,-91.4884,319-351-4256,M-F 8-8; Sa-Su 8-6 (7 days),4.3,EXT,flint
Care Animal Center,Davenport IA,41.5604,-90.5957,563-888-1000,M/F 8-5; Tu/Th 8-6; W 8-2; Sa 8-12,4.7,DAY,flint
Animal Family Veterinary Care,Davenport IA,41.5813,-90.5708,563-391-9522,M-F 7-6; Sa 8-2,4.7,DAY,flint
Twin Bridges Animal Hospital,Davenport IA,41.5758,-90.5146,563-355-5311,M-F 7:30-5:30; Sa 8-12,4.7,DAY,flint
Animal Emergency Center Quad Cities,Bettendorf IA,41.5266,-90.493,563-344-9599,NIGHT ER: M-F 5pm-8am; Sa-Su 24h,3.5,NIGHT,flint
Glenroads Veterinary Clinic,Bettendorf IA,41.551,-90.483,563-332-2999,M-F 7:45-5:30,4.9,DAY,flint
Ancare Veterinary Hospital,La Salle IL,41.3536,-89.1055,815-223-1000,M-Th 7:30-7; F -6; Sa 8-5; Su 10-3 (7 days),4.4,EXT,flint
Animal Care Center of Shorewood,Shorewood IL,41.5228,-88.1994,815-744-1500,M-F 7am-8pm; Sa 7-3,4.2,EXT,flint
VCA Aurora Animal Hospital,Aurora IL,41.7648,-88.3855,630-301-6100,OPEN 24/7,3.4,ER,flint
Premier Paws,Joliet IL,41.5394,-88.1789,815-729-1555,M 8-4; Tu-F 8-5,4.6,DAY,flint
VCA Joliet Animal Hospital,Joliet IL,41.5267,-88.1315,815-729-0770,M-F 7-7; Sa 8-2,4.2,EXT,flint
Essington Road Animal Hospital,Joliet IL,41.5561,-88.1605,815-439-2323,M/Tu/Th/F 8-12 & 1-6; W 8-12,4.7,DAY,flint
UrgentVet Joliet,Joliet IL,41.5246,-88.1251,815-846-4600,M-F 3-11pm; Sa-Su 10-8,4.7,NIGHT,flint
Animal Medical Center of Plainfield,Plainfield IL,41.6314,-88.2023,815-436-8387,OPEN 24/7,4.1,ER,flint
VEG ER for Pets - Naperville,Naperville IL,41.707,-88.2051,630-503-7415,OPEN 24/7,4.7,ER,flint
UrgentVet Naperville,Naperville IL,41.7617,-88.2053,630-686-2400,M-F 3-11pm; Sa-Su 10-8,4.6,NIGHT,flint
Care Animal Clinic Urgent+Wellness,Naperville IL,41.7257,-88.1492,630-355-6164,M-Sa to 7-8pm; Su 9-6 (7 days),4.7,EXT,flint
VCA Arboretum View Animal Hospital,Downers Grove IL,41.8067,-88.046,630-963-0424,OPEN 24/7,3.6,ER,flint
VEG ER for Pets - Oak Brook,Oak Brook IL,41.8453,-87.9608,331-808-2720,OPEN 24/7,4.6,ER,flint
Premier Veterinary Group - Orland Park,Orland Park IL,41.6032,-87.7902,708-388-3771,OPEN 24/7,4.2,ER,flint
Premier Veterinary Group - Chicago,Chicago IL,41.9389,-87.7251,773-516-5800,OPEN 24/7,4.0,ER,flint
MedVet Chicago,Chicago IL,41.9421,-87.6975,773-281-7110,OPEN 24/7,4.2,ER,flint
Crossroads Animal Hospital,Crown Point IN,41.4756,-87.4072,219-319-0679,OPEN 24/7,3.5,ER,flint
Merrillville Animal Hospital,Merrillville IN,41.5055,-87.3649,219-980-1531,M/Tu/Th 8-6; W 8-8pm; F-Su closed,4.3,DAY,flint
Broadway Pet Hospital,Merrillville IN,41.4963,-87.3376,219-769-5733,M-F 8-6; Sa 8-1,4.8,DAY,flint
EVCC Emergency Vet - Westville,Westville IN,41.581,-86.8926,219-785-7300,OPEN 24/7,4.7,ER,flint
Family Pet Health Center,South Bend IN,41.6655,-86.2064,574-282-2303,M-F 8-5,4.8,DAY,flint
EVCC Emergency Vet (South Bend metro),Mishawaka IN,41.7133,-86.1779,574-544-6200,OPEN 24/7,4.4,ER,flint
Mishawaka Animal Care Center,Mishawaka IN,41.6946,-86.1832,574-255-4130,M/Th/F 8-5:30; Tu 8-6; W 8-12,4.7,DAY,flint
Animal Care Clinic North,Elkhart IN,41.734,-85.9791,574-264-9521,M-F 7:30-5,4.8,DAY,flint
Animal Aid Clinic South,Elkhart IN,41.6391,-85.9312,574-875-5102,M-F 8-6; Sa 8-1,4.5,DAY,flint
Noah's Landing Pet Care Clinic,Elkhart IN,41.6932,-85.9147,574-293-4555,M-F 8-6; Sa 8-1,4.6,DAY,flint
Pokagon Veterinary Hospital,Angola IN,41.6379,-85.0381,260-665-6023,M-Th 8-6; Sa 8-12,4.8,DAY,flint
All Paws & Claws Veterinary Clinic,Angola IN,41.6593,-85.0022,260-665-5612,M/W 9-5; Tu/Th 9-8pm; F/Sa 9-12,4.8,EXT,flint
Beechwood Veterinary Clinic,Angola IN,41.6664,-85.0015,260-665-2090,M-F 8-5,4.4,DAY,flint
NIVES 24h Emergency & Specialty,Fort Wayne IN,41.134,-85.0613,260-426-1062,OPEN 24/7 (40 min S of I-69 jct),4.3,ER,flint
Care Center Hospital for Animals,Coldwater MI,41.9372,-84.9514,517-278-5631,M-F 8-5; Sa 8-12,4.5,DAY,flint
Tekonsha Animal Hospital (on I-69),Tekonsha MI,42.0995,-84.9855,517-767-3011,M/Tu/F 8-5:30; W 8-7pm; Th 8-5; Sa 8-12,4.7,DAY,flint
VCA Specialty & Emergency Kalamazoo,Portage MI,42.2394,-85.5912,269-381-5228,OPEN 24/7,3.4,ER,flint
Southside Veterinary Clinic,Battle Creek MI,42.218,-85.2038,269-979-1588,M-F 9-5; Sa 9-12,4.8,DAY,flint
Kalamazoo Animal Hospital,Kalamazoo MI,42.259,-85.5794,269-381-1570,M-Th 8-6; F 8-5,4.5,DAY,flint
Sprinkle Road Veterinary Clinic,Kalamazoo MI,42.2642,-85.5303,269-349-6060,M-W 7-6; Th/F 8-6; Sa 8-1,4.7,DAY,flint
EVCC Kalamazoo West Main,Kalamazoo MI,42.2952,-85.6731,269-548-0400,24h wknds + some weekdays; closed Wed - CALL,4.8,NIGHT,flint
Dickman Road Veterinary Clinic,Battle Creek MI,42.3196,-85.199,269-963-9347,M-F 7:30-6; Sa 8-12,4.4,DAY,flint
DRVC On Columbia,Battle Creek MI,42.2988,-85.2123,269-441-3013,M-F 8-12 & 1-6,4.6,DAY,flint
VCA Marshall Animal Hospital,Marshall MI,42.2837,-84.9651,269-781-5114,M-F 7:30-6,4.6,DAY,flint
Brooklyn Road Veterinary Clinic,Jackson MI,42.1723,-84.2741,517-758-2729,M-Tu/Th-F 9-5; W 9-8pm; Sa 9-12,4.9,DAY,flint
Miller Animal Clinic,Lansing MI,42.7403,-84.6444,517-321-6406,M-F 8am-8pm; Sa 9-5,4.5,EXT,flint
Waverly Animal Hospital,Lansing MI,42.7319,-84.6021,517-323-4156,M-F 7am-8pm; Sa 8-4,4.3,EXT,flint
Jolly Road Veterinary Hospital,Lansing MI,42.6826,-84.5092,517-977-1095,M/Tu/Th 8-5:30; W 10-7; F 8-5,4.6,DAY,flint
Evergreen Veterinary Clinic,Lansing MI,42.7117,-84.5363,517-507-0053,M-W/F 8-5; Th 12-8pm,4.9,DAY,flint
MSU Veterinary Medical Center,Lansing MI,42.7228,-84.4708,517-353-5420,OPEN 24/7 - academic hospital ON I-69,3.6,ER,flint
Greater Lansing Veterinary Center,Williamston MI,42.6896,-84.3049,517-708-2525,"OPEN 24/7 (E of Lansing, Flint approach)",3.5,ER,flint
Emergency Veterinary Hospital - Ann Arbor,Ann Arbor MI,42.2869,-83.834,734-369-6446,OPEN 24/7 (45 min S of Flint),3.8,ER,flint
Animal Emergency Center - Novi,Novi MI,42.4687,-83.4743,248-348-1788,OPEN 24/7,3.8,ER,flint
MedVet Commerce (Flint SE backup),Commerce Twp MI,42.544,-83.4576,248-960-7200,OPEN 24/7,4.3,ER,flint
Cross Veterinary Clinic,Grand Blanc MI,42.9447,-83.678,810-406-1494,M/Tu 9-6; W-F 9-5,4.8,DAY,flint
Animal Emergency Hospital,Burton MI (Flint),42.9739,-83.6872,810-238-7557,Daily 8am-MIDNIGHT (W/Th from noon),3.7,NIGHT,flint
Grand Blanc Veterinary Hospital,Grand Blanc MI,42.942,-83.6469,810-694-8241,M-F 8:30-5; Sa 8:30-12,4.5,DAY,flint
Briarwood Veterinary Hospital,Grand Blanc MI,42.9126,-83.6083,810-695-6055,M/Th 8-6; Tu/W/F 8-5:30; Sa 8-12,4.8,DAY,flint
UrgentVet Parker,Parker CO,39.4934,-104.7585,720-835-1500,M-F 3-11pm; Sa-Su 10-8,4.6,NIGHT,texas
Animal Care Center of Castle Pines,Castle Rock CO,39.47,-104.8743,303-688-3660,M-Th 7-7; F/Sa 7-6,4.8,EXT,texas
Cherished Companions Animal Clinic,Castle Rock CO,39.4096,-104.8594,303-688-3757,M-F 8-6,4.7,DAY,texas
Veterinary Specialists of the Rockies,Castle Rock CO,39.4071,-104.8543,303-660-1027,OPEN 24/7 specialty + ER,3.9,ER,texas
North Springs Veterinary Referral Center,Colorado Springs CO,38.9838,-104.7958,719-920-4430,OPEN 24/7,4.4,ER,texas
Animal ER Care,Colorado Springs CO,38.9082,-104.8184,719-260-7141,OPEN 24/7,4.2,ER,texas
Southern Rockies Animal Emergency + Specialty,Colorado Springs CO,38.9161,-104.7179,719-473-0482,OPEN 24/7,3.6,ER,texas
Pueblo Animal Urgent Care,Pueblo CO,38.3114,-104.6175,719-544-7788,NIGHT: M-F 4pm-midnight; Sa-Su noon-midnight,3.1,NIGHT,texas
Pueblo Small Animal Clinic,Pueblo CO,38.2857,-104.5868,719-545-4350,M/Tu/Th/F 8-12 & 1-5:30,4.2,DAY,texas
Veterinary Hospital Associates,Pueblo CO,38.2547,-104.6621,719-564-0330,M-F 8-5:30,4.5,DAY,texas
Mesa Veterinary Clinic,Pueblo CO,38.2445,-104.5654,719-542-6075,M-F 7:30-5:30; Sa 7:30-12,4.5,DAY,texas
Trinidad Animal Clinic,Trinidad CO,37.1769,-104.4891,719-846-3212,M-F 8-12 & 1-4; Sa 8:30-12,4.7,DAY,texas
Fisher's Peak Vet Clinic,Trinidad CO,37.1564,-104.5121,719-846-3211,M-F 8-5; Sa 8-12,4.6,DAY,texas
Mesa Vista Veterinary Hospital,Raton NM,36.87,-104.4408,575-445-3912,M-F 8-12 & 1-5,4.8,DAY,texas
Raton Veterinary Hospital,Raton NM,36.8864,-104.4291,575-445-2691,M-W/F 8-5; Sa 8-11am,4.8,DAY,texas
Clayton Veterinarian Clinic,Clayton NM,36.4386,-103.1755,575-374-2332,M-F 8-12 & 1-5; Sa 8-12,4.9,DAY,texas
Twist Junction Veterinary Services,Dalhart TX,36.0419,-102.55,806-244-0110,M-F 8-12 & 1-5,4.8,DAY,texas
Circle H Animal Health,Dalhart TX,36.0777,-102.5014,806-244-7851,M-F 8-5,4.8,DAY,texas
Dumas Veterinary Services,Dumas TX,35.8624,-101.9723,806-922-6298,Tu/Th 8:30-5; Sa 8-12 only,5.0,DAY,texas
Animal Medical Center,Amarillo TX,35.1999,-101.9081,806-358-7831,M-F 7:30-6; Sa 8-2; Su 8-1 (7 days),4.8,EXT,texas
Small Animal Emergency Clinic,Amarillo TX,35.156,-101.8776,806-352-2277,NIGHT ER: M-Th 6:30pm-7:30am; F 6:30pm-mid; Sa-Su 24h,3.9,NIGHT,texas
Swann Animal Clinic at 45th,Amarillo TX,35.1643,-101.874,806-355-9443,M-F 7am-8:30pm; Sa 8-5,4.7,EXT,texas
Urgent Care at Swann,Amarillo TX,35.1642,-101.8738,806-355-9445,M-W/F 7am-8:30pm; Sa 8-5,4.1,EXT,texas
Amarillo Vet Clinic,Amarillo TX,35.1999,-101.802,806-373-7454,M-F 9-5:30,4.1,DAY,texas
Clarendon Veterinary Hospital,Clarendon TX,34.9332,-100.8767,806-874-3544,M-F 8-5,4.4,DAY,texas
Childress Veterinary Hospital,Childress TX,34.442,-100.2396,940-937-2558,M-F 8-12 & 1:30-5,4.4,DAY,texas
Critter Care Vet Clinic,Childress TX,34.4328,-100.2259,940-937-6065,M-Th 9-12 & 1-5; F 9-12,4.0,DAY,texas
Main St Veterinary Clinic,Vernon TX,34.1375,-99.2843,940-553-3791,M-F 8:30-5:30,4.6,DAY,texas
Lacy Veterinary Clinic,Vernon TX,34.156,-99.2634,940-552-7755,M-F 8-12 & 1-5,4.5,DAY,texas
PETS Clinic (nonprofit),Wichita Falls TX,33.9548,-98.5266,940-723-7387,M-Th 8:30-5,4.8,DAY,texas
Salt Creek Vet Urgent Care,Wichita Falls TX,33.8574,-98.5399,940-247-7207,LIMITED: M 7-7; W 5-8pm; Sa 1-5 - CALL FIRST,4.3,EXT,texas
Animal Hospital of Wichita Falls,Wichita Falls TX,33.8807,-98.4945,940-767-4369,M-W/F 8-5:30; Th 8-12,4.7,DAY,texas
Chisholm Trail Pet Clinic,Bowie TX,33.5663,-97.8573,940-872-8900,M-Th 8-5:30,4.8,DAY,texas
Bowie Pet Clinic,Bowie TX,33.5588,-97.8505,940-872-3400,M/W/F 9-4; Tu/Th 9-6,4.6,DAY,texas
Cross Timbers Veterinary Hospital,Bowie TX,33.5671,-97.8373,940-872-2161,M-F 8-5:30; Sa 8-12,4.7,DAY,texas
Pet Health Center (US-287),Decatur TX,33.2578,-97.611,940-627-8387,M/W/F 8-5; Tu/Th 8-8pm,4.5,EXT,texas
Wise County Animal Clinic,Decatur TX,33.2394,-97.5761,940-627-2133,M-F 7-6,4.8,DAY,texas
Decatur Veterinary Clinic,Decatur TX,33.2164,-97.5829,940-627-2158,M-F 8-3; Sa 8-10am,4.5,DAY,texas
VEG ER - Fort Worth Alliance (on US-287!),Fort Worth TX,32.9045,-97.3229,817-928-5995,OPEN 24/7,4.7,ER,texas
VEG ER - Camp Bowie,Fort Worth TX,32.7279,-97.4167,325-484-4240,OPEN 24/7,4.7,ER,texas
Fort Worth Animal Emergency Hospital,Fort Worth TX,32.6813,-97.4116,817-263-2900,OPEN 24/7,3.6,ER,texas
MedVet Dallas,Dallas TX,32.9028,-96.77,972-994-9110,OPEN 24/7,4.1,ER,texas
VEG ER - Dallas Central,Dallas TX,32.8204,-96.786,972-544-7311,OPEN 24/7,4.8,ER,texas
Hillside Veterinary Clinic,Dallas TX,32.8363,-96.7591,214-824-0397,OPEN 24/7,4.3,ER,texas
Ellis County Veterinary Clinic,Waxahachie TX,32.4015,-96.8723,972-865-7011,M/W/F/Sa/SUN 8am-8pm,4.9,EXT,texas
CityVet Waxahachie,Waxahachie TX,32.4358,-96.8382,214-499-9959,M-F 8-6; Sa 8-12,5.0,DAY,texas
Waxahachie Veterinary Clinic,Waxahachie TX,32.4161,-96.8235,972-923-1156,M-F 7:30-5:30; Sa 7:30-12,4.3,DAY,texas
Ennis Veterinary Clinic,Ennis TX,32.3572,-96.6331,972-875-2647,M-F 8-6; Sa 8-10:30am,4.8,DAY,texas
Animal Hospital of Ennis,Ennis TX,32.3452,-96.6341,972-875-4777,M-F 8-5:30,4.1,DAY,texas
Corsicana Veterinary Clinic,Corsicana TX,32.0883,-96.4837,903-874-7226,M-F 8-6:30; Sa 9-1,4.6,EXT,texas
Fairfield Veterinary Hospital,Fairfield TX,31.7029,-96.185,903-389-4255,M-F 8-5; Sa 9-12,4.7,DAY,texas
Buffalo Animal Hospital,Buffalo TX,31.4618,-96.062,903-322-4239,M-F 8-5; Sa 8-12,4.6,DAY,texas
East Lake Veterinary Hospital,Centerville TX,31.282,-95.8718,903-536-2424,DAILY 7am-7pm (7 days!),4.7,EXT,texas
Madisonville Veterinary Hospital,Madisonville TX,30.9544,-95.9127,936-348-9618,M-F 8-5,4.8,DAY,texas
Easterling Veterinary Services,Madisonville TX,30.9238,-95.9049,936-348-3645,M-F 8-5:30,4.6,DAY,texas
Walker County Veterinary Center,Huntsville TX,30.7413,-95.6035,936-291-2145,M-F 8-12 & 1-5; Sa 8-12,4.8,DAY,texas
Long Veterinary Clinic,Huntsville TX,30.7201,-95.5549,936-295-8163,M-F 7:30-5:30; Sa 9-12,4.6,DAY,texas
Huntsville Pet Clinic,Huntsville TX,30.6987,-95.5488,936-295-8106,M-F 7:30-5; Sa 8-12,4.7,DAY,texas
Animal Hospital & Emergency Clinic of Conroe,Conroe TX,30.3131,-95.4733,936-297-5723,OPEN 24/7 - ON I-45,4.3,ER,texas
Emergency Pet Care of Texas,Magnolia TX,30.2214,-95.5834,832-376-3728,OPEN 24/7,4.2,ER,texas
Greenlight Pet ER,The Woodlands TX,30.2077,-95.5281,915-400-1010,OPEN 24/7,4.6,ER,texas
BluePearl Pet Hospital - Spring,Spring TX,30.0751,-95.4396,832-616-5000,OPEN 24/7 (I-45 N Houston approach),3.8,ER,texas
Gulf Coast Veterinary Specialists (GCVS),Houston TX,29.7853,-95.4815,713-693-1111,OPEN 24/7 - specialty flagship,4.2,ER,texas
Vergi 24/7,Houston TX,29.7832,-95.5069,713-932-9589,OPEN 24/7,4.0,ER,texas
VEG ER - Houston Katy Fwy,Houston TX,29.7762,-95.3878,346-355-6444,OPEN 24/7,4.6,ER,texas
Pearland 288 Animal Emergency (Silverlake),Pearland TX,29.555,-95.3765,713-482-4592,OPEN 24/7,3.6,ER,texas
VCA Animal Emergency Hospital Southeast (Gulf Fwy),Houston TX,29.6434,-95.2445,713-941-8460,NIGHT: M-Th 6pm-8am; F 6pm-mid; Sa-Su 24h - ON I-45,3.4,NIGHT,texas
Friendswood Veterinary Hospital,Friendswood TX,29.5405,-95.2012,281-993-4417,M-F 8-5,5.0,DAY,texas
VEG ER - Friendswood (5 min from Safari),Friendswood TX,29.5453,-95.1473,281-544-0748,OPEN 24/7,4.8,ER,texas
Friendswood Animal Clinic,Friendswood TX,29.5135,-95.189,281-482-1258,M-F 8-6; Sa 8-12,4.8,DAY,texas
GCVS NASA Parkway,Webster TX,29.5288,-95.1324,346-788-4103,OPEN 24/7,4.2,ER,texas
League City Animal Clinic,League City TX,29.5015,-95.1224,281-332-0644,M-Th 8-6; F 8-5,4.7,DAY,texas
Bay Creek Animal Clinic,League City TX,29.516,-95.0846,281-332-3051,M-F 7:30-6 (W to 5),4.7,DAY,texas
SAFARI Veterinary Care Centers (Dr. Garner) - DESTINATION,League City TX,29.5264,-95.0748,281-332-5612,M-F 6am-8pm; Sa-Su 9-6,4.5,EXT,texas
Marina Bay Animal Hospital,League City TX,29.5428,-95.0609,281-612-5858,M-F 7:30-6; Sa 7:30-12,4.8,DAY,texas"""


def load_curated():
    import io
    df = pd.read_csv(io.StringIO(CURATED_CSV), dtype={"phone": str, "rating": str, "hours": str})
    df["source"] = "curated"
    df["hours_raw"] = df["hours"]
    df["website"] = ""; df["maps_url"] = ""
    df["er_hint"] = df["typ"].isin(["ER", "NIGHT"])
    return df

# vetlocator | 5 providers
def _fmt_phone(s):
    d = re.sub(r"\D", "", str(s or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return f"{d[:3]}-{d[3:6]}-{d[6:]}" if len(d) == 10 else (str(s).strip() if s else "")


def _bboxes_along(route, chunk_mi=150, margin_mi=45):
    """Rectangles that tile the route, each padded by margin_mi, so every clinic within reach is inside one box."""
    boxes, start = [], 0.0
    while start < route["total_mi"]:
        sel = (route["cum_mi"] >= start - 1) & (route["cum_mi"] <= start + chunk_mi + 1)
        lat, lng = route["lat"][sel], route["lng"][sel]
        dlat = margin_mi / 69.0; dlng = margin_mi / (69.0 * math.cos(math.radians(float(lat.mean()))))
        boxes.append((lat.min() - dlat, lng.min() - dlng, lat.max() + dlat, lng.max() + dlng))
        start += chunk_mi
    return boxes


def _bbox_around(lat, lng, radius_mi):
    dlat = radius_mi / 69.0; dlng = radius_mi / (69.0 * math.cos(math.radians(lat)))
    return (lat - dlat, lng - dlng, lat + dlat, lng + dlng)


def overpass(query_body, timeout=60):
    """Runs an Overpass query against the public mirrors in turn; results are cached on disk by query text."""
    q = f"[out:json][timeout:{timeout}];{query_body}"
    p = _cache_path("overpass", q)
    if p.exists():
        return json.loads(p.read_text())
    last = None
    for url in OVERPASS_URLS:
        try:
            j = _get_json(url, data={"data": q}, method="POST", timeout=timeout + 15, tries=1)
            p.write_text(json.dumps(j)); return j
        except RuntimeError as e:
            last = e
    raise RuntimeError(f"All Overpass mirrors failed ({last}). Re-run later - results are cached as they arrive.")


def osm_places(boxes):
    """City/town/village nodes inside the boxes, used to label a clinic's town when OSM has no addr:city."""
    rows = []
    for (s, w, n, e) in boxes:
        bb = f"({s:.4f},{w:.4f},{n:.4f},{e:.4f})"
        j = overpass(f'node["place"~"^(city|town|village)$"]{bb};out;')
        for el in j["elements"]:
            t = el.get("tags", {})
            if t.get("name"):
                rows.append({"lat": el["lat"], "lng": el["lon"], "place": t["name"], "kind": t["place"],
                             "state": t.get("addr:state") or t.get("is_in:state_code") or ""})
    return pd.DataFrame(rows).drop_duplicates(["place", "lat", "lng"]) if rows else pd.DataFrame(columns=["lat", "lng", "place", "kind", "state"])


def fetch_osm(boxes):
    """amenity=veterinary nodes and ways in the boxes -> DataFrame in the map schema (OSM has no ratings).
    A box whose query fails is skipped and listed in .attrs['failed'] rather than sinking the whole map."""
    els, failed = {}, []
    for (s, w, n, e) in boxes:
        bb = f"({s:.4f},{w:.4f},{n:.4f},{e:.4f})"
        try:
            j = overpass(f'(node["amenity"="veterinary"]{bb};way["amenity"="veterinary"]{bb};);out center tags;')
        except RuntimeError:
            failed.append((s, w, n, e)); continue
        for el in j["elements"]:
            els[(el["type"], el["id"])] = el
    rows = []
    for (typ_, oid), el in els.items():
        t = el.get("tags", {})
        name = t.get("name")
        if not name or (NOISE_RE.search(name) and not NAME_ER_RE.search(name)):
            continue
        lat = el.get("lat") or el.get("center", {}).get("lat"); lng = el.get("lon") or el.get("center", {}).get("lon")
        if lat is None:
            continue
        hours_raw = t.get("opening_hours", "")
        sched = parse_osm_opening_hours(hours_raw)
        typ, hours = classify_row(hours_raw, sched, name)
        if t.get("emergency") == "yes" and typ in ("UNK", "DAY"):
            hours += " (OSM tag emergency=yes)"
        city = t.get("addr:city") or ""
        rows.append({"name": name, "lat": float(lat), "lng": float(lng),
                     "phone": _fmt_phone(t.get("phone") or t.get("contact:phone")),
                     "hours": hours, "hours_raw": hours_raw, "typ": typ, "rating": "",
                     "town": (city + (f" {t['addr:state']}" if city and t.get("addr:state") else "")).strip(),
                     "website": t.get("website") or t.get("contact:website") or "",
                     "maps_url": "", "source": "OSM", "ext_id": f"{typ_}/{oid}",
                     "er_hint": bool(NAME_ER_RE.search(name) or t.get("emergency") == "yes")})
    df = pd.DataFrame(rows)
    df.attrs["failed"] = failed
    return df


def fill_towns(df, places):
    """Nearest city/town (within 12 mi), else the nearest place of any kind, for rows with no town."""
    if df.empty or places.empty:
        return df
    need = df["town"].fillna("").str.strip() == ""
    if not need.any():
        return df
    big = places[places["kind"].isin(["city", "town"])]
    out = df.copy()
    for i in out.index[need]:
        lat, lng = out.at[i, "lat"], out.at[i, "lng"]
        d = haversine_mi(lat, lng, big["lat"].to_numpy(), big["lng"].to_numpy()) if len(big) else np.array([])
        if len(d) and d.min() <= 12:
            r = big.iloc[int(d.argmin())]
        else:
            d = haversine_mi(lat, lng, places["lat"].to_numpy(), places["lng"].to_numpy())
            r = places.iloc[int(d.argmin())]
        out.at[i, "town"] = f"{r['place']} {r['state']}".strip()
    return out


GOOGLE_FIELDS = ("places.id,places.displayName,places.formattedAddress,places.location,places.rating,"
                 "places.userRatingCount,places.nationalPhoneNumber,places.regularOpeningHours,"
                 "places.businessStatus,places.websiteUri,places.googleMapsUri,places.types")


def _google_post(url, body):
    key = f"{url}|{json.dumps(body, sort_keys=True)}"
    p = _cache_path("google", key)
    if p.exists():
        return json.loads(p.read_text())
    j = _get_json(url, data=json.dumps(body), method="POST", timeout=30, tries=2,
                  headers={"Content-Type": "application/json", "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                           "X-Goog-FieldMask": GOOGLE_FIELDS + ",nextPageToken"})
    p.write_text(json.dumps(j))
    return j


def _google_row(pl):
    name = pl.get("displayName", {}).get("text", "")
    if not name or pl.get("businessStatus", "OPERATIONAL") != "OPERATIONAL":
        return None
    hrs = pl.get("regularOpeningHours") or {}
    sched = parse_google_periods(hrs.get("periods"))
    wd = hrs.get("weekdayDescriptions") or []
    hours_raw = "; ".join(w.replace(" ", " ").replace(" ", " ") for w in wd)
    typ, hours = classify_row(hours_raw, sched, name)
    addr = pl.get("formattedAddress", "")
    m = re.search(r",\s*([^,]+?),\s*([A-Z]{2})\s*\d{5}", addr)
    town = f"{m.group(1)} {m.group(2)}" if m else addr
    return {"name": name, "lat": pl["location"]["latitude"], "lng": pl["location"]["longitude"],
            "phone": _fmt_phone(pl.get("nationalPhoneNumber")), "hours": hours, "hours_raw": hours_raw, "typ": typ,
            "rating": (f"{pl['rating']:.1f}" if pl.get("rating") is not None else ""), "town": town,
            "website": pl.get("websiteUri", ""), "maps_url": pl.get("googleMapsUri", ""), "source": "Google",
            "ext_id": pl.get("id", ""), "er_hint": bool(NAME_ER_RE.search(name))}


def fetch_google(centers, radius_mi=15, er_reach_mi=45):
    """Two passes per centre: Nearby (veterinary_care, nearest 20 within radius_mi) and Text ('24 hour emergency
    veterinary hospital', biased to er_reach_mi) so metro ERs are not crowded out by the 20-result cap."""
    if not GOOGLE_MAPS_API_KEY:
        raise RuntimeError("Set GOOGLE_MAPS_API_KEY (environment variable) to use Google Places.")
    rows = {}
    for (lat, lng) in centers:
        body = {"includedTypes": ["veterinary_care"], "maxResultCount": 20, "rankPreference": "DISTANCE",
                "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lng},
                                                   "radius": min(radius_mi * 1609.34, 50000.0)}}}
        for pl in _google_post(PLACES_NEARBY_URL, body).get("places", []):
            r = _google_row(pl)
            if r: rows[r["ext_id"]] = r
    for (lat, lng) in centers[::2] + centers[-1:]:
        token = None
        for _ in range(3):
            body = {"textQuery": "24 hour emergency veterinary hospital", "pageSize": 20,
                    "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng},
                                                "radius": min(er_reach_mi * 1609.34, 50000.0)}}}
            if token: body["pageToken"] = token
            j = _google_post(PLACES_TEXT_URL, body)
            for pl in j.get("places", []):
                r = _google_row(pl)
                if r: rows[r["ext_id"]] = r
            token = j.get("nextPageToken")
            if not token: break
    return pd.DataFrame(list(rows.values()))


def _centers_along(route, step_mi):
    target = np.arange(0, route["total_mi"] + step_mi, step_mi)
    target = target[target <= route["total_mi"]]
    return list(zip(np.interp(target, route["cum_mi"], route["lat"]), np.interp(target, route["cum_mi"], route["lng"])))

# vetlocator | 6 merge
def _norm(s):
    s = re.sub(r"[^a-z0-9 ]", " ", str(s).lower())
    s = re.sub(r"\b(the|of|and|inc|llc|pc|dvm|veterinary|vet|animal|hospital|clinic|center|centre|care|pet|pets)\b", " ", s)
    return " ".join(s.split())


def merge_sources(*frames):
    """Earlier frames win (pass the hand-verified rows first).  A later row is dropped when a kept row within
    0.4 mi has a similar name (ratio >= 0.72) or sits within 60 m of it; its links/rating fill any blanks."""
    kept, klat, klng, knorm = [], [], [], []
    for df in frames:
        if df is None or df.empty:
            continue
        for r in df.to_dict("records"):
            dup = None
            if kept:
                d = haversine_mi(r["lat"], r["lng"], np.array(klat), np.array(klng))
                for k in np.flatnonzero(d <= 0.4):
                    if d[k] <= 0.04 or difflib.SequenceMatcher(None, _norm(r["name"]), knorm[k]).ratio() >= 0.72:
                        dup = kept[k]; break
            if dup is not None:
                for c in ("maps_url", "website", "rating", "phone"):
                    if not dup.get(c) and r.get(c): dup[c] = r[c]
                continue
            kept.append(dict(r)); klat.append(r["lat"]); klng.append(r["lng"]); knorm.append(_norm(r["name"]))
    cols = ["name", "town", "lat", "lng", "phone", "hours", "hours_raw", "typ", "rating", "website", "maps_url", "source", "er_hint"]
    out = pd.DataFrame(kept)
    for c in cols:
        if c not in out: out[c] = "" if c != "er_hint" else False
    out["er_hint"] = out["er_hint"].fillna(False).astype(bool)
    return out[cols + [c for c in out.columns if c not in cols]].fillna("")


def coverage_gaps(df, total_mi, types=("ER", "NIGHT"), reach_mi=45, min_gap_mi=100):
    """Stretches of route longer than min_gap_mi with no clinic of the given types within reach_mi."""
    pts = df[df["typ"].isin(types) & (df["off"] <= reach_mi)].sort_values("mile")
    stations = [(0, "START")] + list(zip(pts["mile"], pts["name"])) + [(int(round(total_mi)), "END")]
    gaps = []
    for (a, na), (b, nb) in zip(stations[:-1], stations[1:]):
        if b - a >= min_gap_mi:
            gaps.append({"from_mile": int(a), "to_mile": int(b), "gap_mi": int(b - a), "from": na, "to": nb})
    return pd.DataFrame(gaps, columns=["from_mile", "to_mile", "gap_mi", "from", "to"])


def anchor_chain(df, reach_mi=12):
    """24/7 and night clinics close to the road, in mile order - the numbers to save before leaving."""
    a = df[df["typ"].isin(["ER", "NIGHT"]) & (df["off"] <= reach_mi)].sort_values(["mile", "typ"])
    return a[["mile", "town", "name", "typ", "off", "hours", "phone"]].reset_index(drop=True)

# vetlocator | 7 render
_TEMPLATE = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
body{margin:0;font-family:system-ui,Segoe UI,Arial,sans-serif;background:#0e1116;color:#e8eaee}
header{padding:12px 16px;background:#151a22;border-bottom:2px solid #2a3140}
h1{margin:0;font-size:20px;color:#ffd94d}.sub{color:#9aa3b2;font-size:13px;margin-top:4px}
#map{height:52vh;min-height:380px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 16px;background:#151a22;border-bottom:1px solid #2a3140}
.btn{border:1px solid #3a4356;background:#1d2430;color:#e8eaee;padding:6px 12px;border-radius:16px;cursor:pointer;font-size:13px}
.btn.on{background:#2f3b52;border-color:#6ea8ff}
input#q{flex:1;min-width:160px;background:#1d2430;border:1px solid #3a4356;border-radius:16px;color:#e8eaee;padding:7px 12px;font-size:13px}
.legend span{display:inline-block;margin-right:14px;font-size:12.5px;color:#c7cdd8}
.dot{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:5px;vertical-align:-1px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{position:sticky;top:0;background:#1a212c;color:#ffd94d;text-align:left;padding:8px 10px;border-bottom:2px solid #2a3140;cursor:pointer}
td{padding:7px 10px;border-bottom:1px solid #232a36;vertical-align:top}
tr:hover td{background:#1a212c;cursor:pointer}
.t-ER{color:#ff6b6b;font-weight:700}.t-NIGHT{color:#ffb84d;font-weight:700}.t-EXT{color:#ffd94d}.t-DAY{color:#7fb3ff}.t-UNK{color:#9aa3b2}
.src{font-size:11px;color:#9aa3b2}
a{color:#8fc1ff;text-decoration:none}.note{padding:10px 16px;font-size:12.5px;color:#9aa3b2}
.pop b{font-size:14px}.pop{font-size:13px;line-height:1.45}
@media(max-width:700px){td:nth-child(6),th:nth-child(6),td:nth-child(9),th:nth-child(9){display:none}}
</style></head><body>
<header><h1>__TITLE__</h1><div class="sub">__SUB__</div></header>
<div class="bar">
 <button class="btn on" data-f="ALL">All</button><button class="btn" data-f="ER">24/7 ER</button>
 <button class="btn" data-f="NIGHT">Night / late</button><button class="btn" data-f="EXT">Extended</button>
 <button class="btn" data-f="DAY">Daytime</button><button class="btn" data-f="UNK">Unknown hrs</button><input id="q" placeholder="search town or clinic...">
</div>
<div class="bar legend">
 <span><i class="dot" style="background:#ff5252"></i>24/7 ER</span>
 <span><i class="dot" style="background:#ffab2e"></i>night / late</span>
 <span><i class="dot" style="background:#ffe14d"></i>extended day</span>
 <span><i class="dot" style="background:#4d9bff"></i>daytime GP</span>
 <span><i class="dot" style="background:#9aa3b2"></i>hours unknown - call</span>
 <span><i class="dot" style="background:#0e1116;border:2px solid #fff;width:9px;height:9px"></i>white ring = hand-verified listing</span>
</div>
<div id="map"></div>
<div class="note">__NOTE_TOP__</div>
<table id="tbl"><thead><tr>
<th data-s="mile">__MILE_HDR__</th><th data-s="town">Town</th><th data-s="name">Clinic</th><th data-s="typ">Type</th><th data-s="off">__OFF_HDR__</th><th data-s="hours">Hours</th><th data-s="rating">&#9733;</th><th>Call</th><th data-s="source">Source</th>
</tr></thead><tbody></tbody></table>
<div class="note">__NOTE_BOTTOM__</div>
<script>
const DATA=__DATA__; const MAIN=__MAIN__; const VAR=__VAR__; const HOME=__HOME__; const RINGS=__RINGS__;
const map=L.map('map').setView(__CENTER__,__ZOOM__);
// Basemap tiles: Esri and USGS serve embedded pages (Colab, Streamlit) without a key. OpenStreetMap's own tile
// server refuses those frames ("Access blocked", 403) and CARTO watermarks them ("API KEY REQUIRED").
const BASEMAPS={
 'Streets (Esri)':L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,attribution:'Tiles &copy; Esri'}),
 'Light gray (Esri)':L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}',{maxZoom:16,attribution:'Tiles &copy; Esri'}),
 'Topo (USGS)':L.tileLayer('https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}',{maxZoom:16,attribution:'USGS The National Map'})
};
let base=BASEMAPS['Streets (Esri)'].addTo(map), tileErrs=0;
base.on('tileerror',()=>{if(++tileErrs>=4&&map.hasLayer(base)){map.removeLayer(base);BASEMAPS['Topo (USGS)'].addTo(map);}});
L.control.layers(BASEMAPS,null,{position:'topright'}).addTo(map);
if(MAIN.length>1) L.polyline(MAIN,{color:'#6ea8ff',weight:4,opacity:.9}).addTo(map);
if(VAR.length>1) L.polyline(VAR,{color:'#6ea8ff',weight:3,opacity:.8,dashArray:'7 7'}).addTo(map);
if(HOME){L.marker(HOME.ll).addTo(map).bindPopup('<b>HOME</b><br>'+HOME.label);RINGS.forEach(r=>L.circle(HOME.ll,{radius:r*1609,color:'#6ea8ff',weight:1.5,fill:false,dashArray:'4 6'}).addTo(map));}
const col={ER:'#ff5252',NIGHT:'#ffab2e',EXT:'#ffe14d',DAY:'#4d9bff',UNK:'#9aa3b2'};
let markers={};
DATA.forEach((c,i)=>{
 const cur=c.source==='curated';
 const m=L.circleMarker([c.lat,c.lng],{radius:c.typ==='ER'?9:6,color:cur?'#ffffff':'#0e1116',weight:cur?2:1.5,fillColor:col[c.typ],fillOpacity:.95}).addTo(map);
 const gm=c.maps_url||('https://maps.google.com/?q='+c.lat+','+c.lng);
 m.bindPopup('<div class="pop"><b>'+c.name+'</b><br>'+c.town+' &middot; ~mi '+c.mile+' &middot; '+c.off+' mi off route<br><span class="t-'+c.typ+'">'+c.hours+'</span><br>&#9733; '+(c.rating||'&ndash;')+' &middot; '+(c.phone?'<a href="tel:'+c.phone+'">'+c.phone+'</a>':'no phone listed')+'<br><a target="_blank" href="'+gm+'">Open in Google Maps</a>'+(c.website?' &middot; <a target="_blank" href="'+c.website+'">website</a>':'')+'<br><span class="src">source: '+c.source+'</span></div>');
 markers[i]=m;
});
let F='ALL',Q='',sortK='mile',sortA=true;
function render(){
 const tb=document.querySelector('#tbl tbody'); tb.innerHTML='';
 let rows=DATA.map((c,i)=>({...c,i})).filter(c=>(F==='ALL'||c.typ===F)&&(!Q||(c.town+' '+c.name).toLowerCase().includes(Q)));
 rows.sort((a,b)=>{let x=a[sortK],y=b[sortK];if(typeof x==='string'){x=x.toLowerCase();y=y.toLowerCase()}return (x<y?-1:x>y?1:0)*(sortA?1:-1)});
 rows.forEach(c=>{
  const tr=document.createElement('tr');
  tr.innerHTML='<td>'+c.mile+'</td><td>'+c.town+'</td><td>'+c.name+'</td><td class="t-'+c.typ+'">'+c.typ+'</td><td>'+c.off+' mi</td><td>'+c.hours+'</td><td>'+(c.rating||'&ndash;')+'</td><td>'+(c.phone?'<a href="tel:'+c.phone+'">'+c.phone+'</a>':'&ndash;')+'</td><td class="src">'+c.source+'</td>';
  tr.onclick=()=>{map.setView([c.lat,c.lng],12);markers[c.i].openPopup();window.scrollTo({top:0,behavior:'smooth'})};
  tb.appendChild(tr);});
 DATA.forEach((c,i)=>{const s=(F==='ALL'||c.typ===F)&&(!Q||(c.town+' '+c.name).toLowerCase().includes(Q));
  if(s){markers[i].addTo(map)}else{map.removeLayer(markers[i])}});
}
document.querySelectorAll('.btn').forEach(b=>b.onclick=()=>{document.querySelectorAll('.btn').forEach(x=>x.classList.remove('on'));b.classList.add('on');F=b.dataset.f;render()});
document.getElementById('q').oninput=e=>{Q=e.target.value.toLowerCase();render()};
document.querySelectorAll('th[data-s]').forEach(h=>h.onclick=()=>{const k=h.dataset.s;if(sortK===k)sortA=!sortA;else{sortK=k;sortA=true}render()});
render();
</script></body></html>"""


def _esc(s):
    return html.escape(str(s), quote=False)


def _thin(route, n=200):
    """Route polyline for the map, thinned to ~n vertices (Leaflet does not need 13,000)."""
    if route is None:
        return []
    idx = np.unique(np.linspace(0, len(route["lat"]) - 1, n).astype(int))
    return [[round(float(route["lat"][i]), 4), round(float(route["lng"][i]), 4)] for i in idx]


def render_html(df, *, title, sub, note_top, note_bottom, route=None, variant=None, home=None, rings=(5, 15), mode="corridor"):
    """Fills the Leaflet template with the clinic table, route line(s) and notes -> one self-contained HTML string."""
    cols = ["name", "town", "lat", "lng", "phone", "hours", "typ", "rating", "off", "mile", "source", "website", "maps_url"]
    recs = []
    for _, r in df.iterrows():
        rec = {c: r.get(c, "") for c in cols}
        for c in ("name", "town", "hours", "phone", "rating", "source"): rec[c] = _esc(rec[c])
        rec["lat"] = round(float(rec["lat"]), 4); rec["lng"] = round(float(rec["lng"]), 4)
        rec["off"] = float(rec["off"]); rec["mile"] = int(rec["mile"]) if mode != "home" else float(rec["mile"])
        recs.append(rec)
    if home is not None:
        center, zoom = [round(home[0], 4), round(home[1], 4)], 10
    else:
        center = [round(float(np.mean(route["lat"])), 3), round(float(np.mean(route["lng"])), 3)]
        span = max(np.ptp(route["lat"]), np.ptp(route["lng"]) * 0.8)
        zoom = 5 if span > 8 else 6 if span > 4 else 7 if span > 2 else 9
    out = (_TEMPLATE.replace("__TITLE__", _esc(title)).replace("__SUB__", sub).replace("__NOTE_TOP__", note_top)
           .replace("__NOTE_BOTTOM__", note_bottom).replace("__DATA__", json.dumps(recs, separators=(",", ":")))
           .replace("__MAIN__", json.dumps(_thin(route))).replace("__VAR__", json.dumps(_thin(variant)))
           .replace("__HOME__", json.dumps({"ll": [round(home[0], 5), round(home[1], 5)], "label": _esc(home[2])} if home else None))
           .replace("__RINGS__", json.dumps(list(rings))).replace("__CENTER__", json.dumps(center)).replace("__ZOOM__", str(zoom))
           .replace("__MILE_HDR__", "Mi from home" if mode == "home" else "~Mile")
           .replace("__OFF_HDR__", "Distance" if mode == "home" else "Off-route"))
    return out

# vetlocator | 8 build
def _provider_pick(provider):
    if provider == "auto":
        return "google" if GOOGLE_MAPS_API_KEY else "osm"
    return provider


def _stamp():
    return datetime.date.today().strftime("%b %d, %Y")


def _tel(p):
    return f'<a href="tel:{p}">{p}</a>' if p else "no phone"


def _live_label(prov):
    return "Google Places" if prov == "google" else "OpenStreetMap (no ratings; hours where mapped)"


def corridor_map(origin, destination, *, via=None, variant_via=None, title=None, provider="auto",
                 corridor_mi=12, er_reach_mi=45, curated=True, out_html=None):
    """Any driving corridor in the US -> the same map/table as Harley's corridor maps, from live data.
    corridor_mi: every clinic this close to the road is kept.  er_reach_mi: 24/7, night and emergency-named
    clinics are kept out to this distance (a metro ER 30 mi off the highway is worth knowing about; a daytime GP is not)."""
    prov = _provider_pick(provider)
    o, d = geocode(origin), geocode(destination)
    pts = [o] + [geocode(v) for v in (via or [])] + [d]
    route = route_osrm(pts)
    variant = route_osrm([o] + [geocode(v) for v in variant_via] + [d]) if variant_via else None
    frames, warnings = [], []
    if curated:
        frames.append(load_curated())
    try:
        if prov == "google":
            live = fetch_google(_centers_along(route, step_mi=18), radius_mi=corridor_mi + 3, er_reach_mi=er_reach_mi)
        else:
            boxes = _bboxes_along(route, chunk_mi=150, margin_mi=er_reach_mi)
            live = fetch_osm(boxes)
            if live.attrs.get("failed"):
                warnings.append(f"OpenStreetMap unavailable for {len(live.attrs['failed'])} of {len(boxes)} route segments - re-run later to fill them")
            try:
                live = fill_towns(live, osm_places(boxes))
            except RuntimeError as e:
                warnings.append(f"town labels incomplete ({e})")
        frames.append(live)
    except RuntimeError as e:
        warnings.append(f"live {prov.upper()} pull failed ({e}); map shows hand-verified listings only")
    df = merge_sources(*frames)
    df = locate_on_route(df, route)
    far_ok = df["typ"].isin(["ER", "NIGHT"]) | ((df["typ"] == "UNK") & df["er_hint"])
    keep = (df["off"] <= corridor_mi) | (far_ok & (df["off"] <= er_reach_mi))
    if variant is not None:
        dv = locate_on_route(df, variant)
        keep |= (dv["off"] <= corridor_mi) | (far_ok & (dv["off"] <= er_reach_mi))
    df = df[keep].sort_values(["mile", "off"]).reset_index(drop=True)
    total = route["total_mi"]
    chain = anchor_chain(df, reach_mi=corridor_mi)
    gaps247 = coverage_gaps(df, total, types=("ER",), reach_mi=er_reach_mi)
    gapsnight = coverage_gaps(df, total, types=("ER", "NIGHT"), reach_mi=er_reach_mi)
    title = title or f"Vet corridor - {origin} to {destination}"
    sub = (f"{len(df)} clinics &middot; {int(round(total))} mi driving &middot; ~{route['drive_h']:.1f} h &middot; "
           f"solid line = route" + (" &middot; dashed = variant" if variant is not None else "") +
           " &middot; map tiles need internet; the table always works")
    chain_txt = " - ".join(f"{_esc(r['name'])} {_tel(r['phone'])} (mi ~{r['mile']})" for _, r in chain.iterrows()) or "none within reach"
    gap_txt = ""
    if len(gaps247):
        gap_txt += " <b>No true 24/7 for:</b> " + "; ".join(f"mi {g['from_mile']}-{g['to_mile']} ({g['gap_mi']} mi, {_esc(g['from'])} to {_esc(g['to'])})" for _, g in gaps247.iterrows()) + "."
    if len(gapsnight):
        gap_txt += " <b>No overnight cover at all for:</b> " + "; ".join(f"mi {g['from_mile']}-{g['to_mile']} ({g['gap_mi']} mi)" for _, g in gapsnight.iterrows()) + "."
    note_top = f"<b>Anchor chain (24/7 and night clinics within {corridor_mi} mi of the road):</b> {chain_txt}.{gap_txt}"
    note_bottom = (f"Hand-verified rows = Google listings Sept 2026; live rows pulled {_stamp()} from {_live_label(prov)}. "
                   f"Call ahead on holidays. Mile markers and off-route distances are straight-line approximations against the drawn route.")
    if warnings:
        note_bottom += " <b>" + "; ".join(_esc(w) for w in warnings) + ".</b>"
    page = render_html(df, title=title, sub=sub, note_top=note_top, note_bottom=note_bottom, route=route, variant=variant)
    if out_html:
        pathlib.Path(out_html).write_text(page, encoding="utf-8")
    return {"df": df, "route": route, "variant": variant, "anchors": chain, "gaps_24_7": gaps247,
            "gaps_overnight": gapsnight, "html": page, "provider": prov, "warnings": warnings}


def home_map(home, *, label=None, radius_mi=15, rings=(5, 15), title=None, provider="auto", curated=True, out_html=None):
    """Everything within radius_mi of a home address, sorted by distance, with the rings of Harley's home map.
    24/7, night and emergency-named clinics are kept out to twice the radius."""
    prov = _provider_pick(provider)
    lat, lng = geocode(home)
    frames, warnings = [], []
    if curated:
        frames.append(load_curated())
    try:
        if prov == "google":
            centers = [(lat, lng)]
            if radius_mi > 10:                      # ring of extra centres so the 20-per-call cap does not truncate a metro
                r_lat = radius_mi * 0.6 / 69.0; r_lng = radius_mi * 0.6 / (69.0 * math.cos(math.radians(lat)))
                centers += [(lat + r_lat * math.cos(a), lng + r_lng * math.sin(a)) for a in np.linspace(0, 2 * math.pi, 6, endpoint=False)]
            live = fetch_google(centers, radius_mi=min(radius_mi, 31), er_reach_mi=radius_mi * 2)
        else:
            boxes = [_bbox_around(lat, lng, radius_mi * 2)]
            live = fetch_osm(boxes)
            if live.attrs.get("failed"):
                warnings.append("OpenStreetMap unavailable - re-run later")
            try:
                live = fill_towns(live, osm_places(boxes))
            except RuntimeError as e:
                warnings.append(f"town labels incomplete ({e})")
        frames.append(live)
    except RuntimeError as e:
        warnings.append(f"live {prov.upper()} pull failed ({e}); map shows hand-verified listings only")
    df = merge_sources(*frames)
    dist = haversine_mi(lat, lng, df["lat"].to_numpy(float), df["lng"].to_numpy(float))
    df["mile"] = np.round(dist, 1); df["off"] = np.round(dist, 1)
    far_ok = df["typ"].isin(["ER", "NIGHT"]) | ((df["typ"] == "UNK") & df["er_hint"])
    keep = (df["off"] <= radius_mi) | (far_ok & (df["off"] <= radius_mi * 2))
    df = df[keep].sort_values("off").reset_index(drop=True)
    er = df[df["typ"] == "ER"].head(3)
    label = label or str(home)
    title = title or f"Home vet network - {label}"
    sub = f"{len(df)} pins around {_esc(label)} &middot; dashed rings = {' and '.join(str(r) for r in rings)} miles &middot; table sorted by distance from home"
    note_top = "<b>Closest 24/7 doors:</b> " + (" - ".join(f"{_esc(r['name'])} {_tel(r['phone'])} ({r['off']} mi)" for _, r in er.iterrows()) or "none within reach") + "."
    note_bottom = (f"Hand-verified rows = Google listings Sept 2026; live rows pulled {_stamp()} from {_live_label(prov)}. "
                   f"Call ahead on holidays. Distances are straight-line from home.")
    if warnings:
        note_bottom += " <b>" + "; ".join(_esc(w) for w in warnings) + ".</b>"
    page = render_html(df, title=title, sub=sub, note_top=note_top, note_bottom=note_bottom, home=(lat, lng, label), rings=rings, mode="home")
    if out_html:
        pathlib.Path(out_html).write_text(page, encoding="utf-8")
    return {"df": df, "home": (lat, lng), "html": page, "provider": prov, "warnings": warnings}