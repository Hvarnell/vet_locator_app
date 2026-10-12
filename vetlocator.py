"""Vet locator library - generated from harley_vet_locator.ipynb; edit the notebook, not this file."""
# vetlocator | 1 config
import os, re, json, math, time, html, hashlib, pathlib, datetime, difflib, urllib.parse
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


def _geo_google(q):
    if not GOOGLE_MAPS_API_KEY:
        return None
    j = _get_json(PLACES_TEXT_URL, data=json.dumps({"textQuery": q, "pageSize": 1}), method="POST", timeout=20, tries=1,
                  headers={"Content-Type": "application/json", "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                           "X-Goog-FieldMask": "places.location"})
    pl = j.get("places") or []
    return (pl[0]["location"]["latitude"], pl[0]["location"]["longitude"]) if pl else None


def _geo_census(q):
    """US Census Bureau geocoder: exact for street addresses, no match for bare town names."""
    if not re.match(r"\s*\d", q):
        return None
    j = _get_json("https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
                  params={"address": q, "benchmark": "Public_AR_Current", "format": "json"}, timeout=20, tries=1)
    m = j.get("result", {}).get("addressMatches") or []
    return (m[0]["coordinates"]["y"], m[0]["coordinates"]["x"]) if m else None


def _geo_arcgis(q):
    j = _get_json("https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates",
                  params={"SingleLine": q, "f": "json", "maxLocations": 1, "sourceCountry": "USA"}, timeout=20, tries=1)
    c = j.get("candidates") or []
    return (c[0]["location"]["y"], c[0]["location"]["x"]) if c and c[0].get("score", 100) >= 80 else None


def _geo_photon(q):
    j = _get_json("https://photon.komoot.io/api/", params={"q": q, "limit": 1, "lang": "en"}, timeout=20, tries=1)
    f = j.get("features") or []
    return (f[0]["geometry"]["coordinates"][1], f[0]["geometry"]["coordinates"][0]) if f else None


def _geo_nominatim(q):
    j = _get_json(NOMINATIM_URL, params={"q": q, "format": "json", "limit": 1}, timeout=20, tries=1)
    time.sleep(1.0)                                            # Nominatim's limit: one request per second
    return (float(j[0]["lat"]), float(j[0]["lon"])) if j else None


# Tried in this order; a provider that errors or finds nothing hands over to the next.  Google first when a key is
# set; Census and ArcGIS place street addresses exactly; Photon centres a street rather than the house; Nominatim
# last because it rate-limits shared cloud addresses (HTTP 429).
GEOCODERS = [("Google", _geo_google), ("US Census", _geo_census), ("ArcGIS", _geo_arcgis),
             ("Photon", _geo_photon), ("Nominatim", _geo_nominatim)]


def geocode(query):
    """'lat,lng' text or a (lat, lng) pair passes through; anything else goes through GEOCODERS (cached on disk)."""
    if isinstance(query, (tuple, list)):
        return float(query[0]), float(query[1])
    q = str(query).strip()
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", q)
    if m:
        return float(m.group(1)), float(m.group(2))
    p = _cache_path("geocode", q)
    if p.exists():
        ll = json.loads(p.read_text())
        return float(ll[0]), float(ll[1])
    failures = []
    for name, fn in GEOCODERS:
        try:
            ll = fn(q)
        except Exception as e:
            failures.append(f"{name}: {str(e)[:60]}"); continue
        if ll:
            p.write_text(json.dumps([float(ll[0]), float(ll[1])]))
            return float(ll[0]), float(ll[1])
        failures.append(f"{name}: no match")
    raise ValueError(f"Could not geocode {q!r} - try a fuller address or 'lat,lng' ({'; '.join(failures)})")


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


def waypoints_from_google_link(url):
    """The stops in a Google Maps directions link (long form or a maps.app.goo.gl share link) -> list of places.
    Google does not publish its exact road choice, but the stops you add in Google Maps are the shape of the route."""
    u = str(url).strip()
    if "goo.gl" in u or "maps.app" in u:
        u = requests.get(u, headers={"User-Agent": UA}, timeout=20, allow_redirects=True).url
    m = re.search(r"/maps/dir/([^?#]*)", u)
    if not m:
        raise ValueError("Not a Google Maps directions link (expected .../maps/dir/stop1/stop2/...)")
    stops = [urllib.parse.unquote_plus(seg).strip() for seg in m.group(1).split("/")]
    stops = [x for x in stops if x and not x.startswith(("@", "data=", "am="))]
    if len(stops) < 2:
        raise ValueError("The link has fewer than two stops")
    return stops


def route_from_track(xml_text):
    """A GPX track/route or KML LineString (Google My Maps 'Export to KML', Garmin, Gaia, ...) -> route dict.
    The file's own line is used; nothing is re-routed."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_text)
    pts, wpts = [], []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag in ("trkpt", "rtept"):
            pts.append((float(el.get("lat")), float(el.get("lon"))))
        elif tag == "wpt":
            wpts.append((float(el.get("lat")), float(el.get("lon"))))
        elif tag == "coordinates" and el.text:
            for tok in el.text.split():
                lng, lat = tok.split(",")[:2]; pts.append((float(lat), float(lng)))
    pts = pts or wpts
    if len(pts) < 2:
        raise ValueError("No track, route or LineString found in the file")
    lat = np.array([q[0] for q in pts]); lng = np.array([q[1] for q in pts])
    seg = haversine_mi(lat[:-1], lng[:-1], lat[1:], lng[1:]); cum = np.concatenate([[0.0], np.cumsum(seg)])
    return {"lat": lat, "lng": lng, "cum_mi": cum, "total_mi": float(cum[-1]), "drive_h": float(cum[-1]) / 55.0}


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


def sched_compact(sched):
    """Schedule -> plain lists the phone app can read: 7 days (Mon..Sun), each [[open_min, close_min], ...];
    a 24-hour day is [[0, 1440]]; None when the hours are unknown."""
    if not sched or not any(sched.values()):
        return None
    return [[[0, 1440]] if any(iv[2] for iv in sched.get(d, [])) else [[int(iv[0]), int(iv[1])] for iv in sched.get(d, [])]
            for d in range(7)]


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
CURATED_CSV = """name,town,address,lat,lng,phone,hours,rating,typ,corridor
Sploot Veterinary Care,Arvada,"7705 Wadsworth Blvd, Arvada CO 80003",39.8356,-105.0832,720-780-8874,DAILY 8am-8pm (Sun too),4.8,EXT,home
Arvada Veterinary Hospital,Arvada,"6655 Wadsworth Boulevard, Arvada CO 80003",39.817,-105.0831,303-424-4439,Tu-F 8-6; M 9-5; Sa 8-4,4.6,DAY,home
Church Ranch Veterinary Center,Westminster,"8308 Church Ranch Blvd, Broomfield CO 80021",39.8803,-105.0892,303-732-5819,"OPEN 24/7 (GP + urgent care, surgery)",4.3,ER,home
Olde Town Veterinary Clinic,Arvada,"7709 Ralston Road, Arvada CO 80002",39.8023,-105.0835,720-328-3301,M-F 8-5,4.9,DAY,home
UrgentVet Broomfield,Broomfield,"4700 West 121st Avenue, Broomfield CO 80020",39.9145,-105.0488,303-622-1600,M-F 3-11pm; Sa-Su 10-8,4.7,NIGHT,home
Animal Urgent Care - W 64th Ave,Arvada,"12520 West 64th Avenue, Arvada CO 80004",39.8118,-105.1392,303-420-7387,OPEN 24/7,4.6,ER,home
WRAH - Wheat Ridge Animal Hospital (HIS SPECIALTY HOME),Wheat Ridge,"10140 West 44th Avenue, Wheat Ridge CO 80033",39.7771,-105.111,303-424-3325,"OPEN 24/7 + specialty: neuro (Dr. Djani), IM, surgery",4.1,ER,home
RiNoVet Animal Emergency,Denver,"4495 Washington Street, Denver CO 80216",39.778,-104.9797,303-458-5555,OPEN 24/7,4.2,ER,home
Arvada Flats Veterinary Hospital,Arvada,"6305 Eldridge St, Arvada CO 80004",39.8111,-105.1572,303-467-9212,M-F 8-7; Sa 8-1,4.8,EXT,home
Northside Emergency Pet Clinic,Westminster,"945 West 124th Avenue, Westminster CO 80234",39.9225,-104.9986,303-252-7722,OPEN 24/7,4.2,ER,home
VEG ER - Edgewater,Edgewater,"5485 West 20th Avenue, Edgewater CO 80214",39.7478,-105.0565,720-996-1200,OPEN 24/7,4.8,ER,home
VEG ER - Colfax,Denver,"3845 E Colfax Ave, Denver CO 80206",39.7404,-104.9426,720-574-9834,OPEN 24/7,4.8,ER,home
Evolution Veterinary Specialty & Emergency,Lakewood,"34 Van Gordon St, Lakewood CO 80228",39.7154,-105.1367,720-510-7707,OPEN 24/7,4.0,ER,home
BluePearl Pet Hospital - Lafayette,Lafayette,"2000 West South Boulder Road;W South Boulder Rd, Lafayette CO 80026",39.9874,-105.1167,720-699-7766,OPEN 24/7 specialty + ER,4.6,ER,home
VRCC Veterinary Specialty & Emergency,Englewood,"3550 S Jason St, Englewood CO 80110",39.6524,-104.9987,303-923-5790,OPEN 24/7,4.2,ER,home
VEG ER - Hampden,Denver,"6305 East Hampden Avenue, Denver CO 80222",39.6534,-104.9171,720-739-6003,OPEN 24/7,4.7,ER,home
CSU Veterinary Teaching Hospital - SURGICAL HOME (Dr. Monnet),Fort Collins,"500 West Pitkin Street, Fort Collins CO 80523",40.5719,-105.085,970-297-5000,OPEN 24/7 academic,4.4,ER,home
Animal Care Hospital of Morris,Morris IL,"221 Bedford Road, Morris IL 60450",41.3715,-88.4244,815-941-9924,M-F 8-5; Sa 8-12,4.8,DAY,flint
Pine Bluff Animal Hospital,Morris IL,"3935 N Dresden Rd, Coal City IL 60416",41.3454,-88.2681,815-942-5365,M-F 8-5:30; Sa 8-12,4.8,DAY,flint
Lakewood Animal Hospital,Morris IL,"1301 Lakewood Dr, Morris IL 60450",41.369,-88.4426,815-942-1199,M/Tu/Th/F 8-5:30,4.9,DAY,flint
Parkview Animal Hospital,Newton IA,"2012 1st Avenue East, Newton IA 50208",41.6995,-93.0265,641-792-0340,M-F 8-5,4.7,DAY,flint
Newton Animal Clinic,Newton IA,"2445 Highway F48 W, Newton IA 50208",41.6988,-93.0826,641-433-4815,M-F 8-5,4.6,DAY,flint
Riverside Animal Hospital,Kearney NE,"1115 W 3rd St W, Kearney NE 68845",40.6759,-99.0977,308-234-2617,M-F 8-5:30; Sa 8-12,4.6,DAY,flint
Arbor View Animal Hospital (US-6),Valparaiso IN,"261 Eagle Ridge Drive, Valparaiso IN 46385",41.5498,-87.1141,219-762-7267,M/Tu/Th 8-8pm; W/F 8-6; Sa 8-1,4.7,EXT,flint
Valparaiso Animal Hospital,Valparaiso IN,"902 Calumet Avenue, Valparaiso IN 46383",41.4761,-87.0568,219-462-1862,M-F 7-6; Sa 8-2,4.6,DAY,flint
McAfee Animal Hospital,Valparaiso IN,"950 Transport Drive, Valparaiso IN 46383",41.4617,-87.0175,219-462-5901,M-F 8-12 & 1:30-5:30; Sa 8-12,4.8,DAY,flint
All Creature Features Animal Hospital,La Porte IN,"4034 South US 35, La Porte IN 46350",41.5452,-86.7017,219-393-3558,M-F 7:30-6; Sa 7-1,4.6,DAY,flint
LaPorte Animal Hospital,La Porte IN,"2303 W State Road 2, La Porte IN 46350",41.5967,-86.7442,219-362-2612,M-W 8-5:30; F 8-5,4.5,DAY,flint
Michigan City Animal Hospital,Michigan City IN,"2525 E MI Blvd, MI City IN 46360",41.7079,-86.868,219-872-4191,M-F 7-6; Sa 8-12; SUN 1-7pm,4.8,EXT,flint
St Joseph Animal Wellness Clinic,St Joseph MI,"2610 N Trail Ln, Saint Joseph MI 49085",42.0723,-86.4786,269-429-6966,M-F 8-5:30 (Tu/Th to 6),4.9,DAY,flint
Nickerson Animal Health Center,Benton Harbor MI,"2407 South M 139, Benton Harbor MI 49022",42.0747,-86.436,269-925-8835,M-F 8-6 (midday close),4.7,DAY,flint
Sunset Coast Veterinary Clinic,St Joseph MI,"4133 M-139, St Joseph MI 49085",42.0439,-86.4348,269-408-0468,M-F ~8-5 (Tu/Th to 6),4.7,DAY,flint
Steiner SILS Affordable Vet Care,Fort Morgan CO,"17208 US Highway 34, Fort Morgan CO 80701",40.2537,-103.8257,970-441-1503,M-Tu 7:30-5; W 7:30-4; Th-Su closed,4.6,DAY,flint
Fort Morgan Veterinary Clinic,Fort Morgan CO,"1215 E Burlington Ave, Fort Morgan CO 80701",40.2472,-103.783,970-867-9477,M-F 8-5; Sa 9-12,4.5,DAY,flint
Red Arrow Animal Clinic,Paw Paw MI,"38354 W Red Arrow Hwy, Paw Paw MI 49079",42.2143,-85.9213,269-657-7081,M-W/F 8-5,4.2,DAY,flint
Paw Paw Veterinary Clinic,Paw Paw MI,"31690 Red Arrow Hwy, Paw Paw MI 49079",42.2229,-85.8597,269-657-3114,M-Th 8-7pm; F 8-5; Sa 8-1; Su 9-11am,4.8,EXT,flint
Sterling Animal Clinic,Sterling CO,"1301 West Main Street, Sterling CO 80751",40.6247,-103.2325,970-521-0333,M-F 8-4,4.0,DAY,flint
Veterinary Medical Clinic,Sterling CO,"211 El Camino St, Sterling CO 80751",40.6246,-103.199,970-522-3321,M-F 8-5; Sa 8-12,4.5,DAY,flint
Town & Country Animal Hospital,Charlotte MI,"3110 South Cochran Road, Charlotte MI 48813",42.5226,-84.8365,517-543-2330,M/W 8-7pm; Tu/Th/F 8-5; Sa 8-12,4.6,EXT,flint
Apple Grove Veterinary Care,Charlotte MI,"1800 E Clinton Trl, Charlotte MI 48813",42.5525,-84.8016,517-543-6101,M/Tu/Th 8-7pm; W/F 8-5,4.6,EXT,flint
Baltzell Veterinary Hospital,Ogallala NE,"1710 W 4th St, Ogallala NE 69153",41.126,-101.7386,308-284-4313,M-F 8-5:30; Sa 8-12,4.8,DAY,flint
Animal Clinic & Pharmacy,Ogallala NE,"105 W O St W, Ogallala NE 69153",41.1226,-101.7367,308-284-2182,M-F 8-5; Sa 8-12,4.7,DAY,flint
Westfield Small Animal Clinic,North Platte NE,"1821 W A St W, North Platte NE 69101",41.1347,-100.7851,308-534-4480,M-F 8-6; Sa 8-3,4.5,DAY,flint
Tender Hearts Veterinary Center,North Platte NE,"1208 W A St, North Platte NE 69101",41.1333,-100.7778,308-221-6915,M-F 8-5:30,4.5,DAY,flint
America's Heartland Animal Center,North Platte NE,"220 W Fremont Dr W, North Platte NE 69101",41.0994,-100.7652,308-532-4880,M-F 7:30-5; Sa 8-12,4.4,DAY,flint
Stockman's Veterinary Clinic,North Platte NE,"3666 N Stockman Rd, North Platte NE 69101",41.1658,-100.7583,308-532-7210,M-F 8-5:30; Sa 8-12,4.7,DAY,flint
B & B Veterinary Services,Lexington NE,"3104 N Adams St, Lexington NE 68850",40.8064,-99.7477,308-324-3411,M-F 8-12 & 1-5; Sa 8-10am,4.8,DAY,flint
Lexington Animal Clinic,Lexington NE,"1301 W Pacific Ave, Lexington NE 68850",40.7819,-99.7622,308-324-2411,M-F 8:30-5,4.6,DAY,flint
Overton Veterinary Services,Overton NE,"75348 Road 440, Lexington NE 68850",40.7504,-99.6158,308-324-7202,M-F 8-5:30; Sa 8-12,4.9,DAY,flint
Beebout Veterinary Medical Center,Kearney NE,"2907 W 37th St, Kearney NE 68845",40.7125,-99.1215,308-236-5912,M-Sa 7:30-6,4.6,DAY,flint
West Villa Animal Hospital,Kearney NE,"2518 W 24th St, Kearney NE 68845",40.699,-99.1157,308-244-8143,M-F 7:30-6; Sa 8-12; Su 4:30-6pm,4.5,DAY,flint
Cottonwood Veterinary Clinic,Kearney NE,"5912 2nd Avenue, Kearney NE 68847",40.7322,-99.0855,308-234-8118,M 7:30-8pm; Tu-Th 7:30-6; F 8-6,4.6,EXT,flint
Hilltop Pet Clinic,Kearney NE,"4503 2nd Avenue, Kearney NE 68845",40.7188,-99.0825,308-236-5912,M-F 7:30-9:30a & 3:30-6p; Sa 7:30-12; Su 3-6p,4.7,DAY,flint
Northgate Veterinary Clinic,Kearney NE,"404 East 56th Street, Kearney NE 68847",40.7288,-99.0757,308-234-9512,M-F 8-5,4.7,DAY,flint
Parks Veterinary,Grand Island NE,"3010 N North Rd, Grand Island NE 68803",40.9557,-98.3985,308-384-6272,M-F 7:30-5; Sa 8-12,4.8,DAY,flint
Grand Island Veterinary Hospital,Grand Island NE,"410 South Webb Road, Grand Island NE 68801",40.9139,-98.379,308-384-1641,M-F 8-5,4.5,DAY,flint
Pet Hospital,Grand Island NE,"410 South Webb Road, Grand Island NE 68801",40.9136,-98.3787,308-384-4363,M-F 8-5; Sa 9-12,5.0,DAY,flint
Family Pet Clinic,Grand Island NE,"2620 North Diers Avenue, Grand Island NE 68803",40.9472,-98.3857,308-384-6147,M-F 8-5,4.6,DAY,flint
Animal Medical Clinic,Grand Island NE,"210 E Stolley Park Rd, Grand Island NE 68801",40.9026,-98.338,308-382-6330,M-F 7:30-5; Sa 8-12,4.7,DAY,flint
South Locust Veterinary Clinic,Grand Island NE,"1616 S Locust St, Grand Island NE 68801",40.9049,-98.3404,308-382-6412,M-F 8-5:30,4.7,DAY,flint
York Animal Clinic,York NE,"1529 N Lincoln Ave, York NE 68467",40.8755,-97.5933,402-362-2894,M-F 7:30-5:30; Sa 7:30-12,4.8,DAY,flint
Sullivan Companion Animal Clinic,York NE,"427 North Lincoln Avenue, York NE 68467",40.8656,-97.5928,402-745-6498,M-F 8-5; Sa 8-11am,4.7,DAY,flint
Vondra Veterinary Clinic,Lincoln NE,"2400 West Van Dorn Street, Lincoln NE 68522",40.7851,-96.7537,402-477-1113,M-F 7:30-6; Sa 8-12,4.8,DAY,flint
Veterinary Emergency Services,Lincoln NE,"3700 S 9th St, Lincoln NE 68502",40.7762,-96.7069,402-629-6925,NIGHT ER: M-F 6pm-7am; Sa noon-mid; Su 24h,4.4,NIGHT,flint
Nebraska Animal Medical Center,Lincoln NE,"5720 Old Cheney Road, Lincoln NE 68516",40.7545,-96.643,402-423-9100,M-F 7am-8pm; Sa-Su 8-5 (7 days!),4.3,EXT,flint
Pitts Veterinary Hospital,Lincoln NE,"2225 NE Parkway, Lincoln NE 68502",40.7695,-96.6885,402-423-4120,M-F 7-6; Sa 9-3,4.6,DAY,flint
Animal Care Clinic,Lincoln NE,"255 South 84th Street, Lincoln NE 68510",40.8115,-96.6065,402-566-5056,M-F 7:30-6,4.8,DAY,flint
Clock Tower Animal Clinic,Lincoln NE,"520 North 66th Street, Lincoln NE 68505",40.8178,-96.6299,402-489-6228,M-F 7:30-6,4.8,DAY,flint
Polaris Veterinary Center,Lincoln NE,"2062 N 112th St, Lincoln NE 68527",40.8342,-96.5666,402-467-4469,M/W 7:30-8pm; Tu/Th/F 7:30-6; Sa 7:30-1,4.7,EXT,flint
Urgent Pet Care West,Omaha NE,"4251 South 144th Street, Omaha NE 68137",41.2169,-96.1378,402-234-8834,Daily 8am-MIDNIGHT,4.3,NIGHT,flint
The Pet Clinic,Omaha NE,"2503 South 140th Circle, Omaha NE 68144",41.2365,-96.1339,402-330-3096,M-Th 8-7; F 8-6; Sa 8-2,4.8,DAY,flint
VCA MidWest Referral & Emergency,Omaha NE,"9706 Mockingbird Drive, Omaha NE 68127",41.2094,-96.0652,402-513-8741,M-Th 24h; F to 6am; Sa closed; Su 4pm-mid — CALL FIRST,3.4,NIGHT,flint
VEG ER for Pets - Omaha,Omaha NE,"3645 N 132nd St, Omaha NE 68164",41.2917,-96.1191,402-205-7271,OPEN 24/7,4.8,ER,flint
Omaha Animal Medical Group,Omaha NE,"3316 North 120th Street, Omaha NE 68164",41.2903,-96.1014,402-496-6075,M-F 7:30-6; Sa 8-12,4.8,DAY,flint
VCA 80 Dodge Animal Hospital,Omaha NE,"8022 West Dodge Street, Omaha NE 68114",41.2614,-96.0411,402-399-8100,M-Sa 7am-8pm; Su 10-6 (7 days),4.6,EXT,flint
Lone Tree Animal Care Center,Omaha NE,"524 South 24th Street, Omaha NE 68105",41.255,-95.9474,402-389-3356,M 8-8p; Tu 8-5; W 12-8p; Th 8-5; F 8-4,4.8,DAY,flint
24th Street Animal Clinic,Omaha NE,"3258 S 24th St, Omaha NE 68108",41.2284,-95.9474,402-345-2211,M-F 7:30-6,4.5,DAY,flint
Council Bluffs Veterinary Clinic,Council Bluffs IA,"1229 S 3rd St, Council Bluffs IA 51503",41.2498,-95.8484,712-323-2147,M-F 8-5:30,4.6,DAY,flint
Strohbehn Veterinary Clinic,Council Bluffs IA,"4040 South Expressway, Council Bluffs IA 51501",41.2201,-95.8525,712-366-0556,M-F 8-5:30; Sa 8-12,4.4,DAY,flint
Valley View Veterinary Clinic,Council Bluffs IA,"15025 Valley View Dr, Council Bluffs IA 51503",41.2331,-95.8176,712-256-7387,M-F 8-5:30; Sa 8-12,4.7,DAY,flint
The Animal Clinic,Council Bluffs IA,"1735 McPherson Ave, Council Bluffs IA 51503",41.2596,-95.8132,712-323-0598,M/Tu/Th/F 7-5:30; W 7-12; Sa 7-12,4.4,DAY,flint
VEG ER for Pets - West Des Moines,West Des Moines IA,"6880 #101 E P True Parkway, Boone Township IA 50266",41.5729,-93.8012,515-259-6473,OPEN 24/7,4.9,ER,flint
UrgentVet West Des Moines,West Des Moines IA,"5045 Bentley Drive, West Des Moines IA 50265",41.5611,-93.7719,515-215-9696,M-F 3-11pm; Sa-Su 10-8,4.7,NIGHT,flint
Animal Doctors Veterinary Clinic,West Des Moines IA,"4800 Mills Civic Parkway, West Des Moines IA 50265",41.5581,-93.7691,515-225-9555,M-F 7-6; Sa 8-12,4.8,DAY,flint
Animal Care Clinic West & Metro Cat,West Des Moines IA,"2900 University Avenue, West Des Moines IA 50266",41.6,-93.7443,515-224-4368,M-Th 7-7; F 7-6,4.6,EXT,flint
Iowa Veterinary Specialties,Des Moines IA,"6110 Creston Ave, Des Moines IA 50321",41.5597,-93.7019,515-280-3100,OPEN 24/7,3.6,ER,flint
Grand Avenue Veterinary Hospital,West Des Moines IA,"308-332 Grand Avenue, West Des Moines IA 50265",41.5824,-93.7061,515-274-3489,M-F 7:30-6,4.8,DAY,flint
BluePearl Pet Hospital - Des Moines,Des Moines IA,"4631 Merle Hay Road, Urbandale IA 50310",41.6453,-93.697,515-727-4872,OPEN 24/7,4.3,ER,flint
Grinnell Veterinary Clinic,Grinnell IA,"1630 West Street South, Grinnell IA 50112",41.7092,-92.7281,641-236-4461,M-F 8-5:30; Sa 8-12,4.7,DAY,flint
All Pets Veterinary Hospital,Grinnell IA,"611 West Street, Grinnell IA 50112",41.7394,-92.7278,641-236-6869,M-W/F 8-5; Th 8-8pm; Sa 9-12,4.8,DAY,flint
BluePearl Pet Hospital - Cedar Rapids,Cedar Rapids IA,"755 Capital Dr SW, Cedar Rapids IA 52404",41.8872,-91.6788,319-841-5161,OPEN 24/7 (25 min N of Iowa City via I-380),4.4,ER,flint
Coralville Animal Hospital,Coralville IA,"5th Street, Coralville IA 52241",41.6764,-91.5822,319-351-6848,M-F 7:30-5:30,4.5,DAY,flint
Goosetown Animal Hospital,Coralville IA,"311 2nd Street, Coralville IA 52241",41.6708,-91.5701,319-341-0386,M-F 8-6; Sa 8-12,5.0,DAY,flint
Gentle Heart Pet Clinic,Iowa City IA,"1123 Shirken Dr, IA City IA 52246",41.6467,-91.5457,319-354-6696,M/Tu/F 8-5; W 8-7; Sa 8-12,4.7,DAY,flint
Animal Clinic Inc.,Iowa City IA,"408 Highland Ave, IA City IA 52240",41.6462,-91.5296,319-337-2123,M-F 7:30-5; Sa 8-12,4.8,DAY,flint
Emergency Veterinary Service of Iowa City,Iowa City IA,"3030 Northgate Drive, IA City IA 52245",41.6896,-91.4884,319-338-3605,DAYTIME ER: daily 8am-8pm (wknd to 6),3.2,EXT,flint
Bright Eyes & Bushy Tails (same bldg),Iowa City IA,"3030 Northgate Drive, IA City IA 52245",41.6895,-91.4884,319-351-4256,M-F 8-8; Sa-Su 8-6 (7 days),4.3,EXT,flint
Care Animal Center,Davenport IA,"1502 W Kimberly Rd, Davenport IA 52806",41.5604,-90.5957,563-888-1000,M/F 8-5; Tu/Th 8-6; W 8-2; Sa 8-12,4.7,DAY,flint
Animal Family Veterinary Care,Davenport IA,"6011 N Brady St, Davenport IA 52806",41.5813,-90.5708,563-391-9522,M-F 7-6; Sa 8-2,4.7,DAY,flint
Twin Bridges Animal Hospital,Davenport IA,"4040 E 53rd St, Davenport IA 52807",41.5758,-90.5146,563-355-5311,M-F 7:30-5:30; Sa 8-12,4.7,DAY,flint
Animal Emergency Center Quad Cities,Bettendorf IA,"2810 State St, Bettendorf IA 52722",41.5266,-90.493,563-344-9599,NIGHT ER: M-F 5pm-8am; Sa-Su 24h,3.5,NIGHT,flint
Glenroads Veterinary Clinic,Bettendorf IA,"3515 Middle Road, Bettendorf IA 52722",41.551,-90.483,563-332-2999,M-F 7:45-5:30,4.9,DAY,flint
Ancare Veterinary Hospital,La Salle IL,"24th Street, La Salle IL 61301",41.3536,-89.1055,815-223-1000,M-Th 7:30-7; F -6; Sa 8-5; Su 10-3 (7 days),4.4,EXT,flint
Animal Care Center of Shorewood,Shorewood IL,"150 Brook Forest Avenue, Shorewood IL 60404",41.5228,-88.1994,815-744-1500,M-F 7am-8pm; Sa 7-3,4.2,EXT,flint
VCA Aurora Animal Hospital,Aurora IL,"2600 West Galena Boulevard, Sugar Grove Township IL 60506",41.7648,-88.3855,630-301-6100,OPEN 24/7,3.4,ER,flint
Premier Paws,Joliet IL,"950 NE Frontage Rd, Joliet IL 60431",41.5394,-88.1789,815-729-1555,M 8-4; Tu-F 8-5,4.6,DAY,flint
VCA Joliet Animal Hospital,Joliet IL,"2049 Oneida Street, Joliet IL 60435",41.5267,-88.1315,815-729-0770,M-F 7-7; Sa 8-2,4.2,EXT,flint
Essington Road Animal Hospital,Joliet IL,"1914 Essington Rd, Joliet IL 60435",41.5561,-88.1605,815-439-2323,M/Tu/Th/F 8-12 & 1-6; W 8-12,4.7,DAY,flint
UrgentVet Joliet,Joliet IL,"1801 West Jefferson Street, Joliet IL 60435",41.5246,-88.1251,815-846-4600,M-F 3-11pm; Sa-Su 10-8,4.7,NIGHT,flint
Animal Medical Center of Plainfield,Plainfield IL,"13843 South IL Route 59, Plainfield IL 60544",41.6314,-88.2023,815-436-8387,OPEN 24/7,4.1,ER,flint
VEG ER for Pets - Naperville,Naperville IL,"3204 South IL Route 59, Naperville IL 60564",41.707,-88.2051,630-503-7415,OPEN 24/7,4.7,ER,flint
UrgentVet Naperville,Naperville IL,"602 South IL Route 59, Naperville IL 60540",41.7617,-88.2053,630-686-2400,M-F 3-11pm; Sa-Su 10-8,4.6,NIGHT,flint
Care Animal Clinic Urgent+Wellness,Naperville IL,"531 W 87th St, Naperville IL 60565",41.7257,-88.1492,630-355-6164,M-Sa to 7-8pm; Su 9-6 (7 days),4.7,EXT,flint
VCA Arboretum View Animal Hospital,Downers Grove IL,"2551 Warrenville Road, Downers Grove IL 60515",41.8067,-88.046,630-963-0424,OPEN 24/7,3.6,ER,flint
VEG ER for Pets - Oak Brook,Oak Brook IL,"1765 West 22nd Street, Oak Brook IL 60523",41.8453,-87.9608,331-808-2720,OPEN 24/7,4.6,ER,flint
Premier Veterinary Group - Orland Park,Orland Park IL,"7010 W 159th St, Orland Park IL 60462",41.6032,-87.7902,708-388-3771,OPEN 24/7,4.2,ER,flint
Premier Veterinary Group - Chicago,Chicago IL,"3955 West Belmont Avenue, Jefferson Township IL 60641",41.9389,-87.7251,773-516-5800,OPEN 24/7,4.0,ER,flint
MedVet Chicago,Chicago IL,"3325 North California Avenue, Jefferson Township IL 60625",41.9421,-87.6975,773-281-7110,OPEN 24/7,4.2,ER,flint
Crossroads Animal Hospital,Crown Point IN,"1768 Wedgewood Court, Crown Point IN 46307",41.4756,-87.4072,219-319-0679,OPEN 24/7,3.5,ER,flint
Merrillville Animal Hospital,Merrillville IN,"6226 Cleveland St, Merrillville IN 46410",41.5055,-87.3649,219-980-1531,M/Tu/Th 8-6; W 8-8pm; F-Su closed,4.3,DAY,flint
Broadway Pet Hospital,Merrillville IN,"6690 Broadway, Merrillville IN 46410",41.4963,-87.3376,219-769-5733,M-F 8-6; Sa 8-1,4.8,DAY,flint
EVCC Emergency Vet - Westville,Westville IN,"1645 S US Highway 421, Westville IN 46391",41.581,-86.8926,219-785-7300,OPEN 24/7,4.7,ER,flint
Family Pet Health Center,South Bend IN,"2910 E Mishawaka Aven, South Bend IN 46615",41.6655,-86.2064,574-282-2303,M-F 8-5,4.8,DAY,flint
EVCC Emergency Vet (South Bend metro),Mishawaka IN,"5718 N Main St, Mishawaka IN 46545",41.7133,-86.1779,574-544-6200,OPEN 24/7,4.4,ER,flint
Mishawaka Animal Care Center,Mishawaka IN,"129 W Edison Rd, Mishawaka IN 46545",41.6946,-86.1832,574-255-4130,M/Th/F 8-5:30; Tu 8-6; W 8-12,4.7,DAY,flint
Animal Care Clinic North,Elkhart IN,"327 Northpointe Boulevard, Elkhart IN 46514",41.734,-85.9791,574-264-9521,M-F 7:30-5,4.8,DAY,flint
Animal Aid Clinic South,Elkhart IN,"3718 E Mishawaka Rd, Elkhart IN 46517",41.6391,-85.9312,574-875-5102,M-F 8-6; Sa 8-1,4.5,DAY,flint
Noah's Landing Pet Care Clinic,Elkhart IN,"3929 E Jackson Blvd, Elkhart IN 46516",41.6932,-85.9147,574-293-4555,M-F 8-6; Sa 8-1,4.6,DAY,flint
Pokagon Veterinary Hospital,Angola IN,"2650 W Maumee St, Angola IN 46703",41.6379,-85.0381,260-665-6023,M-Th 8-6; Sa 8-12,4.8,DAY,flint
All Paws & Claws Veterinary Clinic,Angola IN,"2005 N Wayne St, Angola IN 46703",41.6593,-85.0022,260-665-5612,M/W 9-5; Tu/Th 9-8pm; F/Sa 9-12,4.8,EXT,flint
Beechwood Veterinary Clinic,Angola IN,"200 Growth Pkwy, Angola IN 46703",41.6664,-85.0015,260-665-2090,M-F 8-5,4.4,DAY,flint
NIVES 24h Emergency & Specialty,Fort Wayne IN,"5818 Maplecrest Road, Fort Wayne IN 46835",41.134,-85.0613,260-426-1062,OPEN 24/7 (40 min S of I-69 jct),4.3,ER,flint
Care Center Hospital for Animals,Coldwater MI,"649 East Chicago Road, Coldwater MI 49036",41.9372,-84.9514,517-278-5631,M-F 8-5; Sa 8-12,4.5,DAY,flint
Tekonsha Animal Hospital (on I-69),Tekonsha MI,"828 N Main St, Tekonsha MI 49092",42.0995,-84.9855,517-767-3011,M/Tu/F 8-5:30; W 8-7pm; Th 8-5; Sa 8-12,4.7,DAY,flint
VCA Specialty & Emergency Kalamazoo,Portage MI,"5348 S Westnedge Ave, Portage MI 49002",42.2394,-85.5912,269-381-5228,OPEN 24/7,3.4,ER,flint
Southside Veterinary Clinic,Battle Creek MI,"5900 Capital Ave SW, Battle Creek MI 49015",42.218,-85.2038,269-979-1588,M-F 9-5; Sa 9-12,4.8,DAY,flint
Kalamazoo Animal Hospital,Kalamazoo MI,"3301 South Burdick Street, Kalamazoo MI 49001",42.259,-85.5794,269-381-1570,M-Th 8-6; F 8-5,4.5,DAY,flint
Sprinkle Road Veterinary Clinic,Kalamazoo MI,"2659 S Sprinkle Rd, Kalamazoo MI 49001",42.2642,-85.5303,269-349-6060,M-W 7-6; Th/F 8-6; Sa 8-1,4.7,DAY,flint
EVCC Kalamazoo West Main,Kalamazoo MI,"6211 West Main Street, Kalamazoo MI 49009",42.2952,-85.6731,269-548-0400,24h wknds + some weekdays; closed Wed - CALL,4.8,NIGHT,flint
Dickman Road Veterinary Clinic,Battle Creek MI,"455 W Dickman Rd, Battle Creek MI 49037",42.3196,-85.199,269-963-9347,M-F 7:30-6; Sa 8-12,4.4,DAY,flint
DRVC On Columbia,Battle Creek MI,"680 West Columbia Avenue, Battle Creek MI 49015",42.2988,-85.2123,269-441-3013,M-F 8-12 & 1-6,4.6,DAY,flint
VCA Marshall Animal Hospital,Marshall MI,"713 Brewer St, Marshall MI 49068",42.2837,-84.9651,269-781-5114,M-F 7:30-6,4.6,DAY,flint
Brooklyn Road Veterinary Clinic,Jackson MI,"6551 Brooklyn Rd, Jackson MI 49201",42.1723,-84.2741,517-758-2729,M-Tu/Th-F 9-5; W 9-8pm; Sa 9-12,4.9,DAY,flint
Miller Animal Clinic,Lansing MI,"6515 W Saginaw Hwy, Lansing MI 48917",42.7403,-84.6444,517-321-6406,M-F 8am-8pm; Sa 9-5,4.5,EXT,flint
Waverly Animal Hospital,Lansing MI,"233 S Waverly Rd, Lansing MI 48917",42.7319,-84.6021,517-323-4156,M-F 7am-8pm; Sa 8-4,4.3,EXT,flint
Jolly Road Veterinary Hospital,Lansing MI,"3276 E Jolly Rd, Lansing MI 48910",42.6826,-84.5092,517-977-1095,M/Tu/Th 8-5:30; W 10-7; F 8-5,4.6,DAY,flint
Evergreen Veterinary Clinic,Lansing MI,"1114 E Mount Hope Ave, Lansing MI 48910",42.7117,-84.5363,517-507-0053,M-W/F 8-5; Th 12-8pm,4.9,DAY,flint
MSU Veterinary Medical Center,Lansing MI,"824 Wilson Rd, East Lansing MI 48824",42.7228,-84.4708,517-353-5420,OPEN 24/7 - academic hospital ON I-69,3.6,ER,flint
Greater Lansing Veterinary Center,Williamston MI,"1290 W Grand River Ave, Williamston MI 48895",42.6896,-84.3049,517-708-2525,"OPEN 24/7 (E of Lansing, Flint approach)",3.5,ER,flint
Emergency Veterinary Hospital - Ann Arbor,Ann Arbor MI,"5245 Jackson Road, Ann Arbor MI 48103",42.2869,-83.834,734-369-6446,OPEN 24/7 (45 min S of Flint),3.8,ER,flint
Animal Emergency Center - Novi,Novi MI,"24400 Novi Road, Novi MI 48375",42.4687,-83.4743,248-348-1788,OPEN 24/7,3.8,ER,flint
MedVet Commerce (Flint SE backup),Commerce Twp MI,"1120 Welch Rd, Commerce Township MI 48390",42.544,-83.4576,248-960-7200,OPEN 24/7,4.3,ER,flint
Cross Veterinary Clinic,Grand Blanc MI,"5490 Ali Dr, Grand Blanc MI 48439",42.9447,-83.678,810-406-1494,M/Tu 9-6; W-F 9-5,4.8,DAY,flint
Animal Emergency Hospital,Burton MI (Flint),"1148 E Bristol Rd, Burton MI 48529",42.9739,-83.6872,810-238-7557,Daily 8am-MIDNIGHT (W/Th from noon),3.7,NIGHT,flint
Grand Blanc Veterinary Hospital,Grand Blanc MI,"6137 S Saginaw Rd, Grand Blanc MI 48439",42.942,-83.6469,810-694-8241,M-F 8:30-5; Sa 8:30-12,4.5,DAY,flint
Briarwood Veterinary Hospital,Grand Blanc MI,"8213 South Saginaw Road, Grand Blanc MI 48439",42.9126,-83.6083,810-695-6055,M/Th 8-6; Tu/W/F 8-5:30; Sa 8-12,4.8,DAY,flint
UrgentVet Parker,Parker CO,"12239 Pine Bluffs Way, Parker CO 80134",39.4934,-104.7585,720-835-1500,M-F 3-11pm; Sa-Su 10-8,4.6,NIGHT,texas
Animal Care Center of Castle Pines,Castle Rock CO,"562 East Castle Pines Parkway, Castle Pines CO 80108",39.47,-104.8743,303-688-3660,M-Th 7-7; F/Sa 7-6,4.8,EXT,texas
Cherished Companions Animal Clinic,Castle Rock CO,"281 Metzler Dr, Castle Rock CO 80108",39.4096,-104.8594,303-688-3757,M-F 8-6,4.7,DAY,texas
Veterinary Specialists of the Rockies,Castle Rock CO,"774 Maleta Lane, Castle Rock CO 80108",39.4071,-104.8543,303-660-1027,OPEN 24/7 specialty + ER,3.9,ER,texas
North Springs Veterinary Referral Center,Colorado Springs CO,"10520 White Diamond Point, CO Springs CO 80908",38.9838,-104.7958,719-920-4430,OPEN 24/7,4.4,ER,texas
Animal ER Care,Colorado Springs CO,"5520 N Nevada Ave, CO Springs CO 80918",38.9082,-104.8184,719-260-7141,OPEN 24/7,4.2,ER,texas
Southern Rockies Animal Emergency + Specialty,Colorado Springs CO,"5886 Tutt Blvd, CO Springs CO 80923",38.9161,-104.7179,719-473-0482,OPEN 24/7,3.6,ER,texas
Pueblo Animal Urgent Care,Pueblo CO,"704 West Fortino Boulevard, Pueblo CO 81008",38.3114,-104.6175,719-544-7788,NIGHT: M-F 4pm-midnight; Sa-Su noon-midnight,3.1,NIGHT,texas
Pueblo Small Animal Clinic,Pueblo CO,"1400 E 20th St, Pueblo CO 81001",38.2857,-104.5868,719-545-4350,M/Tu/Th/F 8-12 & 1-5:30,4.2,DAY,texas
Veterinary Hospital Associates,Pueblo CO,"651 S Pueblo Blvd, Pueblo CO 81005",38.2547,-104.6621,719-564-0330,M-F 8-5:30,4.5,DAY,texas
Mesa Veterinary Clinic,Pueblo CO,"20th Lane, Blende CO 81006",38.2445,-104.5654,719-542-6075,M-F 7:30-5:30; Sa 7:30-12,4.5,DAY,texas
Trinidad Animal Clinic,Trinidad CO,"1701 East Main Street, Trinidad CO 81082",37.1769,-104.4891,719-846-3212,M-F 8-12 & 1-4; Sa 8:30-12,4.7,DAY,texas
Fisher's Peak Vet Clinic,Trinidad CO,"1605 Santa Fe Trail, Trinidad CO 81082",37.1564,-104.5121,719-846-3211,M-F 8-5; Sa 8-12,4.6,DAY,texas
Mesa Vista Veterinary Hospital,Raton NM,"1600 S 2nd St, Raton NM 87740",36.87,-104.4408,575-445-3912,M-F 8-12 & 1-5,4.8,DAY,texas
Raton Veterinary Hospital,Raton NM,"1101 Frontage Road, Raton NM 87740",36.8864,-104.4291,575-445-2691,M-W/F 8-5; Sa 8-11am,4.8,DAY,texas
Clayton Veterinarian Clinic,Clayton NM,"1406 South 1st Street, Clayton NM 88415",36.4386,-103.1755,575-374-2332,M-F 8-12 & 1-5; Sa 8-12,4.9,DAY,texas
Twist Junction Veterinary Services,Dalhart TX,"12035 FM 3139, Dalhart TX 79022",36.0419,-102.55,806-244-0110,M-F 8-12 & 1-5,4.8,DAY,texas
Circle H Animal Health,Dalhart TX,"3212 US 54 (TX), Dallam TX 79022",36.0777,-102.5014,806-244-7851,M-F 8-5,4.8,DAY,texas
Dumas Veterinary Services,Dumas TX,"324 South Bliss Avenue, Dumas TX 79029",35.8624,-101.9723,806-922-6298,Tu/Th 8:30-5; Sa 8-12 only,5.0,DAY,texas
Animal Medical Center,Amarillo TX,"6201 West Amarillo Boulevard, Amarillo TX 79106",35.1999,-101.9081,806-358-7831,M-F 7:30-6; Sa 8-2; Su 8-1 (7 days),4.8,EXT,texas
Small Animal Emergency Clinic,Amarillo TX,"5127 Canyon Drive, Amarillo TX 79110",35.156,-101.8776,806-352-2277,NIGHT ER: M-Th 6:30pm-7:30am; F 6:30pm-mid; Sa-Su 24h,3.9,NIGHT,texas
Swann Animal Clinic at 45th,Amarillo TX,"3102 Southwest 45th Avenue, Amarillo TX 79109",35.1643,-101.874,806-355-9443,M-F 7am-8:30pm; Sa 8-5,4.7,EXT,texas
Urgent Care at Swann,Amarillo TX,"3102 Southwest 45th Avenue, Amarillo TX 79109",35.1642,-101.8738,806-355-9445,M-W/F 7am-8:30pm; Sa 8-5,4.1,EXT,texas
Amarillo Vet Clinic,Amarillo TX,"941 S Marrs St, Amarillo TX 79104",35.1999,-101.802,806-373-7454,M-F 9-5:30,4.1,DAY,texas
Clarendon Veterinary Hospital,Clarendon TX,"1121 E 2nd St, Clarendon TX 79226",34.9332,-100.8767,806-874-3544,M-F 8-5,4.4,DAY,texas
Childress Veterinary Hospital,Childress TX,"109 Industrial Cir, Childress TX 79201",34.442,-100.2396,940-937-2558,M-F 8-12 & 1:30-5,4.4,DAY,texas
Critter Care Vet Clinic,Childress TX,"300 US Highway 83 North, Childress TX 79201",34.4328,-100.2259,940-937-6065,M-Th 9-12 & 1-5; F 9-12,4.0,DAY,texas
Main St Veterinary Clinic,Vernon TX,"3402 Main St, Vernon TX 76384",34.1375,-99.2843,940-553-3791,M-F 8:30-5:30,4.6,DAY,texas
Lacy Veterinary Clinic,Vernon TX,"219 US Highway 287 E, Vernon TX 76384",34.156,-99.2634,940-552-7755,M-F 8-12 & 1-5,4.5,DAY,texas
PETS Clinic (nonprofit),Wichita Falls TX,"3001 Central Freeway, Wichita Falls TX 76306",33.9548,-98.5266,940-723-7387,M-Th 8:30-5,4.8,DAY,texas
Salt Creek Vet Urgent Care,Wichita Falls TX,"2927 Southwest Parkway, Wichita Falls TX 76308",33.8574,-98.5399,940-247-7207,LIMITED: M 7-7; W 5-8pm; Sa 1-5 - CALL FIRST,4.3,EXT,texas
Animal Hospital of Wichita Falls,Wichita Falls TX,"3206 Holliday Rd, Wichita Falls TX 76302",33.8807,-98.4945,940-767-4369,M-W/F 8-5:30; Th 8-12,4.7,DAY,texas
Chisholm Trail Pet Clinic,Bowie TX,"904 Highway 81 N, Bowie TX 76230",33.5663,-97.8573,940-872-8900,M-Th 8-5:30,4.8,DAY,texas
Bowie Pet Clinic,Bowie TX,"100 North Smythe Street, Bowie TX 76230",33.5588,-97.8505,940-872-3400,M/W/F 9-4; Tu/Th 9-6,4.6,DAY,texas
Cross Timbers Veterinary Hospital,Bowie TX,"1403 Highway 59 N, Bowie TX 76230",33.5671,-97.8373,940-872-2161,M-F 8-5:30; Sa 8-12,4.7,DAY,texas
Pet Health Center (US-287),Decatur TX,"1879 North US Highway 287, Decatur TX 76234",33.2578,-97.611,940-627-8387,M/W/F 8-5; Tu/Th 8-8pm,4.5,EXT,texas
Wise County Animal Clinic,Decatur TX,"1000 E Business 380, Decatur TX 76234",33.2394,-97.5761,940-627-2133,M-F 7-6,4.8,DAY,texas
Decatur Veterinary Clinic,Decatur TX,"2101 S College Ave, Decatur TX 76234",33.2164,-97.5829,940-627-2158,M-F 8-3; Sa 8-10am,4.5,DAY,texas
VEG ER - Fort Worth Alliance (on US-287!),Fort Worth TX,"9001 Tehama Ridge Pkwy, Fort Worth TX 76177",32.9045,-97.3229,817-928-5995,OPEN 24/7,4.7,ER,texas
VEG ER - Camp Bowie,Fort Worth TX,"6201 Camp Bowie Blvd, Fort Worth TX 76116",32.7279,-97.4167,325-484-4240,OPEN 24/7,4.7,ER,texas
Fort Worth Animal Emergency Hospital,Fort Worth TX,"4631 Citylake Blvd W, Fort Worth TX 76132",32.6813,-97.4116,817-263-2900,OPEN 24/7,3.6,ER,texas
MedVet Dallas,Dallas TX,"11343 North Central Expressway, Dallas TX 75243",32.9028,-96.77,972-994-9110,OPEN 24/7,4.1,ER,texas
VEG ER - Dallas Central,Dallas TX,"4500 North Central Expressway, Dallas TX 75205",32.8204,-96.786,972-544-7311,OPEN 24/7,4.8,ER,texas
Hillside Veterinary Clinic,Dallas TX,"6150 East Mockingbird Lane, Dallas TX 75205",32.8363,-96.7591,214-824-0397,OPEN 24/7,4.3,ER,texas
Ellis County Veterinary Clinic,Waxahachie TX,"793 South Interstate 35, Waxahachie TX 75165",32.4015,-96.8723,972-865-7011,M/W/F/Sa/SUN 8am-8pm,4.9,EXT,texas
CityVet Waxahachie,Waxahachie TX,"1975 North US Highway 77, Waxahachie TX 75165",32.4358,-96.8382,214-499-9959,M-F 8-6; Sa 8-12,5.0,DAY,texas
Waxahachie Veterinary Clinic,Waxahachie TX,"2300 Brown Street, Waxahachie TX 75165",32.4161,-96.8235,972-923-1156,M-F 7:30-5:30; Sa 7:30-12,4.3,DAY,texas
Ennis Veterinary Clinic,Ennis TX,"3101 North Kaufman Street, Ennis TX 75119",32.3572,-96.6331,972-875-2647,M-F 8-6; Sa 8-10:30am,4.8,DAY,texas
Animal Hospital of Ennis,Ennis TX,"2001 North Kaufman Street, Ennis TX 75119",32.3452,-96.6341,972-875-4777,M-F 8-5:30,4.1,DAY,texas
Corsicana Veterinary Clinic,Corsicana TX,"1701 West 2nd Avenue, Corsicana TX 75151",32.0883,-96.4837,903-874-7226,M-F 8-6:30; Sa 9-1,4.6,EXT,texas
Fairfield Veterinary Hospital,Fairfield TX,"1320 US - 84, Fairfield TX 75840",31.7029,-96.185,903-389-4255,M-F 8-5; Sa 9-12,4.7,DAY,texas
Buffalo Animal Hospital,Buffalo TX,"511 E US Highway 79, Buffalo TX 75831",31.4618,-96.062,903-322-4239,M-F 8-5; Sa 8-12,4.6,DAY,texas
East Lake Veterinary Hospital,Centerville TX,,31.282,-95.8718,903-536-2424,DAILY 7am-7pm (7 days!),4.7,EXT,texas
Madisonville Veterinary Hospital,Madisonville TX,"409 E Collard St, Madisonville TX 77864",30.9544,-95.9127,936-348-9618,M-F 8-5,4.8,DAY,texas
Easterling Veterinary Services,Madisonville TX,"2233 Highway 90, Madisonville TX 77864",30.9238,-95.9049,936-348-3645,M-F 8-5:30,4.6,DAY,texas
Walker County Veterinary Center,Huntsville TX,"1039 State Highway 75 N, Huntsville TX 77320",30.7413,-95.6035,936-291-2145,M-F 8-12 & 1-5; Sa 8-12,4.8,DAY,texas
Long Veterinary Clinic,Huntsville TX,"1411 14th St, Huntsville TX 77340",30.7201,-95.5549,936-295-8163,M-F 7:30-5:30; Sa 9-12,4.6,DAY,texas
Huntsville Pet Clinic,Huntsville TX,"2902 Montgomery Rd, Huntsville TX 77340",30.6987,-95.5488,936-295-8106,M-F 7:30-5; Sa 8-12,4.7,DAY,texas
Animal Hospital & Emergency Clinic of Conroe,Conroe TX,"505 Interstate 45 North, Conroe TX 77304",30.3131,-95.4733,936-297-5723,OPEN 24/7 - ON I-45,4.3,ER,texas
Emergency Pet Care of Texas,Magnolia TX,"7850 FM 1488 Rd, Magnolia TX 77354",30.2214,-95.5834,832-376-3728,OPEN 24/7,4.2,ER,texas
Greenlight Pet ER,The Woodlands TX,"7901 Research Forest Dr, Spring TX 77382",30.2077,-95.5281,915-400-1010,OPEN 24/7,4.6,ER,texas
BluePearl Pet Hospital - Spring,Spring TX,"1646 Spring Cypress Road, Spring TX 77388",30.0751,-95.4396,832-616-5000,OPEN 24/7 (I-45 N Houston approach),3.8,ER,texas
Gulf Coast Veterinary Specialists (GCVS),Houston TX,"8042 Katy Freeway, Houston TX 77055",29.7853,-95.4815,713-693-1111,OPEN 24/7 - specialty flagship,4.2,ER,texas
Vergi 24/7,Houston TX,"8819 Katy Freeway, Houston TX 77024",29.7832,-95.5069,713-932-9589,OPEN 24/7,4.0,ER,texas
VEG ER - Houston Katy Fwy,Houston TX,"2785 Katy Fwy, Houston TX 77007",29.7762,-95.3878,346-355-6444,OPEN 24/7,4.6,ER,texas
Pearland 288 Animal Emergency (Silverlake),Pearland TX,"10034 West Broadway Street, Pearland TX 77584",29.555,-95.3765,713-482-4592,OPEN 24/7,3.6,ER,texas
VCA Animal Emergency Hospital Southeast (Gulf Fwy),Houston TX,"10331 Gulf Fwy, Houston TX 77034",29.6434,-95.2445,713-941-8460,NIGHT: M-Th 6pm-8am; F 6pm-mid; Sa-Su 24h - ON I-45,3.4,NIGHT,texas
Friendswood Veterinary Hospital,Friendswood TX,"111 Woodland Trail Dr, Friendswood TX 77546",29.5405,-95.2012,281-993-4417,M-F 8-5,5.0,DAY,texas
VEG ER - Friendswood (5 min from Safari),Friendswood TX,"18980 Gulf Fwy, Friendswood TX 77546",29.5453,-95.1473,281-544-0748,OPEN 24/7,4.8,ER,texas
Friendswood Animal Clinic,Friendswood TX,"1401 South Friendswood Drive, Friendswood TX 77546",29.5135,-95.189,281-482-1258,M-F 8-6; Sa 8-12,4.8,DAY,texas
GCVS NASA Parkway,Webster TX,"20610 Gulf Fwy, Webster TX 77598",29.5288,-95.1324,346-788-4103,OPEN 24/7,4.2,ER,texas
League City Animal Clinic,League City TX,"2204 West Main Street, League City TX 77573",29.5015,-95.1224,281-332-0644,M-Th 8-6; F 8-5,4.7,DAY,texas
Bay Creek Animal Clinic,League City TX,"1236 E Main St, League City TX 77573",29.516,-95.0846,281-332-3051,M-F 7:30-6 (W to 5),4.7,DAY,texas
SAFARI Veterinary Care Centers (Dr. Garner) - DESTINATION,League City TX,"2402 Marina Bay Dr, League City TX 77573",29.5264,-95.0748,281-332-5612,M-F 6am-8pm; Sa-Su 9-6,4.5,EXT,texas
Marina Bay Animal Hospital,League City TX,"3030 Marina Bay Dr, League City TX 77573",29.5428,-95.0609,281-612-5858,M-F 7:30-6; Sa 7:30-12,4.8,DAY,texas"""


def public_name(n):
    """Clinic names as the public should see them: the personal annotations from the trip maps removed."""
    n = re.sub(r"\s*\((HIS SPECIALTY HOME|Dr\. [A-Za-z]+|5 min from Safari|Flint SE backup)\)", "", str(n))
    n = re.sub(r"\s*-\s*(SURGICAL HOME \(Dr\. [A-Za-z]+\)|DESTINATION)$", "", n)
    return n.strip()


def public_hours(h):
    """Hours text without the personal notes (which specialist, which service we used)."""
    h = re.sub(r"\s*\(Dr\. [A-Za-z]+\)", "", str(h))
    h = re.sub(r"\+ specialty:.*$", "+ specialty", h)
    return h.strip()


def load_curated(public=True):
    """The hand-verified rows.  public=True (the default, and what the shared builder and phone app use) strips the
    personal trip notes from names and hours; public=False keeps them for the notebook owner's own maps."""
    import io
    df = pd.read_csv(io.StringIO(CURATED_CSV), dtype={"phone": str, "rating": str, "hours": str})
    df["source"] = "curated"
    df["hours_raw"] = df["hours"]
    df["website"] = ""; df["maps_url"] = ""
    if "address" not in df: df["address"] = ""
    df["address"] = df["address"].fillna("")
    df["sched"] = [sched_compact(parse_hours_loose(h)) if t != "ER" else [[[0, 1440]]] * 7 for h, t in zip(df["hours"], df["typ"])]
    df["er_hint"] = df["typ"].isin(["ER", "NIGHT"])
    if public:
        df["name"] = df["name"].map(public_name); df["hours"] = df["hours"].map(public_hours); df["hours_raw"] = df["hours"]
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
        street = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
        address = ", ".join(x for x in (street, " ".join(y for y in (city, t.get("addr:state"), t.get("addr:postcode")) if y)) if x)
        rows.append({"name": name, "lat": float(lat), "lng": float(lng), "address": address, "sched": sched_compact(sched),
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
    if not name:
        return None
    status = pl.get("businessStatus", "OPERATIONAL")
    hrs = pl.get("regularOpeningHours") or {}
    sched = parse_google_periods(hrs.get("periods"))
    wd = hrs.get("weekdayDescriptions") or []
    hours_raw = "; ".join(w.replace(" ", " ").replace(" ", " ") for w in wd)
    typ, hours = classify_row(hours_raw, sched, name)
    addr = pl.get("formattedAddress", "")
    m = re.search(r",\s*([^,]+?),\s*([A-Z]{2})\s*\d{5}", addr)
    town = f"{m.group(1)} {m.group(2)}" if m else addr
    return {"name": name, "lat": pl["location"]["latitude"], "lng": pl["location"]["longitude"], "address": addr.replace(", USA", ""),
            "sched": sched_compact(sched),
            "phone": _fmt_phone(pl.get("nationalPhoneNumber")), "hours": hours, "hours_raw": hours_raw, "typ": typ,
            "rating": (f"{pl['rating']:.1f}" if pl.get("rating") is not None else ""), "town": town,
            "website": pl.get("websiteUri", ""), "maps_url": pl.get("googleMapsUri", ""), "source": "Google",
            "ext_id": pl.get("id", ""), "er_hint": bool(NAME_ER_RE.search(name)),
            "closed": "" if status == "OPERATIONAL" else status.replace("_", " ").lower()}


def fetch_google(centers, radius_mi=15, er_reach_mi=45, er_every=3):
    """Nearby search (veterinary_care, nearest 20 within radius_mi) at every centre, plus a text search for
    '24 hour emergency veterinary hospital' at every er_every-th centre biased to er_reach_mi, so metro ERs are not
    crowded out by the 20-result cap.  Hours/phone/rating fields put every call in Google's Enterprise tier
    (1,000 free calls a month): a 1,000-mile corridor is about 85 calls."""
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
    er_centers = list(centers[::max(1, er_every)])
    if centers[-1] not in er_centers:
        er_centers.append(centers[-1])
    for (lat, lng) in er_centers:
        body = {"textQuery": "24 hour emergency veterinary hospital", "pageSize": 20,
                "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng},
                                            "radius": min(er_reach_mi * 1609.34, 50000.0)}}}
        for pl in _google_post(PLACES_TEXT_URL, body).get("places", []):
            r = _google_row(pl)
            if r: rows[r["ext_id"]] = r
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


def _curated_note(hours_text):
    """The human notes riding on a hand-verified hours string - '(GP + urgent care, surgery)', 'CALL FIRST',
    'LIMITED:' - kept when Google supplies the schedule itself."""
    t = str(hours_text or "")
    parts = []
    lead = re.match(r"\s*([A-Z][A-Z ]{2,}):", t)
    if lead and ("LIMITED" in lead.group(1) or "CALL" in lead.group(1)):
        parts.append(lead.group(1))
    m = re.search(r"\s(\(|-\s|\+\s|—\s)", t)
    if m:
        tail = t[m.start():].strip(" -+—")
        if tail:
            parts.append(tail)
    return " ".join(parts)


def _refresh_from_google(dup, r):
    """A hand-verified row met its Google listing: take Google's current hours, type, rating and phone, keep the
    name, town and notes.  A listing Google marks closed is flagged, not silently kept."""
    note = _curated_note(dup.get("hours_raw") or dup.get("hours"))
    if r.get("closed"):
        dup["hours"] = f"{r['closed']} on Google - verify before relying on this" + (f" · {note}" if note else "")
        dup["typ"] = "UNK"
    elif r.get("typ") != "UNK":
        dup["hours"] = r["hours"] + (f" · {note}" if note else "")
        dup["typ"] = r["typ"]; dup["hours_raw"] = r.get("hours_raw", ""); dup["sched"] = r.get("sched")
    else:
        return                                                  # Google has no hours: the hand-verified text stands
    for c in ("rating", "phone", "website", "maps_url", "address"):
        if r.get(c): dup[c] = r[c]
    dup["source"] = "curated+Google"


def merge_sources(*frames):
    """Earlier frames win (pass the hand-verified rows first).  A later row is dropped when a kept row within
    0.4 mi has a similar name (ratio >= 0.72) or sits within 60 m of it.  Google listings refresh the hand-verified
    row they match (hours, type, rating, phone); OpenStreetMap rows only fill blanks."""
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
                if r.get("source") == "Google" and str(dup.get("source", "")).startswith("curated"):
                    _refresh_from_google(dup, r)
                for c in ("maps_url", "website", "rating", "phone", "address"):
                    if not dup.get(c) and r.get(c): dup[c] = r[c]
                continue
            if r.get("closed"):
                continue                                        # a closed listing with no hand-verified twin: not a door
            kept.append(dict(r)); klat.append(r["lat"]); klng.append(r["lng"]); knorm.append(_norm(r["name"]))
    cols = ["name", "town", "address", "lat", "lng", "phone", "hours", "hours_raw", "typ", "rating", "website", "maps_url", "source", "er_hint", "sched"]
    out = pd.DataFrame(kept)
    for c in cols:
        if c not in out: out[c] = "" if c not in ("er_hint", "sched") else (False if c == "er_hint" else None)
    out["er_hint"] = out["er_hint"].fillna(False).astype(bool)
    out = out.drop(columns=["closed"], errors="ignore")
    out = out[cols + [c for c in out.columns if c not in cols]]
    sched = out["sched"]; out = out.fillna(""); out["sched"] = [s if isinstance(s, list) else None for s in sched]
    return out


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
@media(max-width:700px){td:nth-child(7),th:nth-child(7),td:nth-child(10),th:nth-child(10){display:none}}
</style></head><body>
<header><h1>__TITLE__</h1><div class="sub">__SUB__</div></header>
<div class="bar">
 <button class="btn on" data-f="ALL">All</button><button class="btn" data-f="ER">24/7 ER</button>
 <button class="btn" data-f="NIGHT">Night / late</button><button class="btn" data-f="EXT">Extended</button>
 <button class="btn" data-f="DAY">Daytime</button><button class="btn" data-f="UNK">Unknown hrs</button><input id="q" placeholder="search town or clinic..."><button class="btn" id="near" title="Sort every clinic by distance from where you are right now">&#9673; Near me</button>
</div>
<div class="note" id="nearbox" style="display:none"></div>
<div class="bar legend">
 <span><i class="dot" style="background:#ff5252"></i>24/7 ER</span>
 <span><i class="dot" style="background:#ffab2e"></i>night / late</span>
 <span><i class="dot" style="background:#ffe14d"></i>extended day</span>
 <span><i class="dot" style="background:#4d9bff"></i>daytime GP</span>
 <span><i class="dot" style="background:#9aa3b2"></i>hours unknown - call</span>
 <span><i class="dot" style="background:#0e1116;border:2px solid #fff;width:9px;height:9px"></i>white ring = hand-verified listing (refreshed from Google when a key is set)</span>
</div>
<div id="map"></div>
<div class="note">__NOTE_TOP__</div>
<table id="tbl"><thead><tr>
<th data-s="mile">__MILE_HDR__</th><th data-s="here" class="here" style="display:none">From you</th><th data-s="town">Town</th><th data-s="name">Clinic</th><th data-s="typ">Type</th><th data-s="off">__OFF_HDR__</th><th data-s="hours">Hours</th><th data-s="rating">&#9733;</th><th>Call</th><th data-s="source">Source</th>
</tr></thead><tbody></tbody></table>
<div class="note">__NOTE_BOTTOM__</div>
<script>
const DATA=__DATA__; const MAIN=__MAIN__; const VAR=__VAR__; const HOME=__HOME__; const RINGS=__RINGS__;
const col={ER:'#ff5252',NIGHT:'#ffab2e',EXT:'#ffe14d',DAY:'#4d9bff',UNK:'#9aa3b2'};
let map=null,markers={};
// Basemap tiles: Esri and USGS serve embedded pages (Colab, Streamlit) without a key. OpenStreetMap's own tile
// server refuses those frames ("Access blocked", 403) and CARTO watermarks them ("API KEY REQUIRED").
// If the map engine cannot load (saved page opened with no signal), the table, filters and tap-to-call still work.
try{
map=L.map('map').setView(__CENTER__,__ZOOM__);
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
DATA.forEach((c,i)=>{
 const cur=c.source.indexOf('curated')===0;
 const m=L.circleMarker([c.lat,c.lng],{radius:c.typ==='ER'?9:6,color:cur?'#ffffff':'#0e1116',weight:cur?2:1.5,fillColor:col[c.typ],fillOpacity:.95}).addTo(map);
 const gm=c.maps_url||('https://maps.google.com/?q='+c.lat+','+c.lng);
 m.bindPopup('<div class="pop"><b>'+c.name+'</b><br>'+(c.address||c.town)+' &middot; ~mi '+c.mile+' &middot; '+c.off+' mi off route<br><span class="t-'+c.typ+'">'+c.hours+'</span><br>&#9733; '+(c.rating||'&ndash;')+' &middot; '+(c.phone?'<a href="tel:'+c.phone+'">'+c.phone+'</a>':'no phone listed')+'<br><a target="_blank" href="https://www.google.com/maps/dir/?api=1&destination='+c.lat+','+c.lng+'">Directions</a> &middot; <a target="_blank" href="'+gm+'">Open in Google Maps</a>'+(c.website?' &middot; <a target="_blank" href="'+c.website+'">website</a>':'')+'<br><span class="src">source: '+c.source+'</span></div>');
 markers[i]=m;
});
}catch(err){document.getElementById('map').innerHTML='<div class="note" style="padding:24px 16px">The map picture needs internet the first time it is opened. Everything below still works: filters, search, tap-to-call and Near me.</div>';}
let F='ALL',Q='',sortK='mile',sortA=true,HERE=null,youMarker=null;
function hav(a,b,c,d){const r=Math.PI/180,R=3958.76;const x=Math.sin((c-a)*r/2)**2+Math.cos(a*r)*Math.cos(c*r)*Math.sin((d-b)*r/2)**2;return 2*R*Math.asin(Math.sqrt(x));}
document.getElementById('near').onclick=()=>{
 if(!navigator.geolocation){alert('This browser cannot give a location. Open the map in the phone\'s browser or as the home-screen app.');return;}
 navigator.geolocation.getCurrentPosition(p=>{
  HERE=[p.coords.latitude,p.coords.longitude];
  DATA.forEach(c=>{c.here=hav(HERE[0],HERE[1],c.lat,c.lng)});
  if(map){if(youMarker)map.removeLayer(youMarker);youMarker=L.marker(HERE).addTo(map).bindPopup('<b>You are here</b>');map.setView(HERE,10);}
  const ers=DATA.filter(c=>c.typ==='ER'||c.typ==='NIGHT').sort((a,b)=>a.here-b.here).slice(0,3);
  const nb=document.getElementById('nearbox');nb.style.display='';
  nb.innerHTML='<b>Nearest overnight doors from where you are:</b> '+(ers.map(c=>c.name+' '+(c.phone?'<a href="tel:'+c.phone+'">'+c.phone+'</a>':'no phone')+' ('+c.here.toFixed(1)+' mi straight-line, <span class="t-'+c.typ+'">'+c.typ+'</span>)').join(' - ')||'none in this map')+'. Table is now sorted by distance from you.';
  document.querySelectorAll('.here').forEach(e=>e.style.display='');
  sortK='here';sortA=true;render();
 },()=>alert('Location unavailable - allow location access for this page and try again.'),{enableHighAccuracy:true,timeout:15000,maximumAge:60000});
};
function render(){
 const tb=document.querySelector('#tbl tbody'); tb.innerHTML='';
 let rows=DATA.map((c,i)=>({...c,i})).filter(c=>(F==='ALL'||c.typ===F)&&(!Q||(c.town+' '+c.name).toLowerCase().includes(Q)));
 rows.sort((a,b)=>{let x=a[sortK],y=b[sortK];if(typeof x==='string'){x=x.toLowerCase();y=y.toLowerCase()}return (x<y?-1:x>y?1:0)*(sortA?1:-1)});
 rows.forEach(c=>{
  const tr=document.createElement('tr');
  tr.innerHTML='<td>'+c.mile+'</td><td class="here" style="display:'+(HERE?'':'none')+'">'+(c.here!=null?c.here.toFixed(1)+' mi':'')+'</td><td>'+c.town+'</td><td>'+c.name+(c.address?'<br><span class="src">'+c.address+'</span>':'')+'</td><td class="t-'+c.typ+'">'+c.typ+'</td><td>'+c.off+' mi</td><td>'+c.hours+'</td><td>'+(c.rating||'&ndash;')+'</td><td>'+(c.phone?'<a href="tel:'+c.phone+'">'+c.phone+'</a>':'&ndash;')+'</td><td class="src">'+c.source+'</td>';
  tr.onclick=()=>{if(map&&markers[c.i]){map.setView([c.lat,c.lng],12);markers[c.i].openPopup();window.scrollTo({top:0,behavior:'smooth'})}};
  tb.appendChild(tr);});
 if(map) DATA.forEach((c,i)=>{const s=(F==='ALL'||c.typ===F)&&(!Q||(c.town+' '+c.name).toLowerCase().includes(Q));
  if(s){markers[i].addTo(map)}else{map.removeLayer(markers[i])}});
}
document.querySelectorAll('.btn[data-f]').forEach(b=>b.onclick=()=>{document.querySelectorAll('.btn[data-f]').forEach(x=>x.classList.remove('on'));b.classList.add('on');F=b.dataset.f;render()});
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
    cols = ["name", "town", "address", "lat", "lng", "phone", "hours", "typ", "rating", "off", "mile", "source", "website", "maps_url", "sched"]
    recs = []
    for _, r in df.iterrows():
        rec = {c: r.get(c, "") for c in cols}
        rec["sched"] = rec["sched"] if isinstance(rec["sched"], list) else None
        for c in ("name", "town", "address", "hours", "phone", "rating", "source"): rec[c] = _esc(rec[c] if rec[c] is not None else "")
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


def corridor_map(origin=None, destination=None, *, via=None, variant_via=None, google_link=None, track=None,
                 title=None, provider="auto", corridor_mi=12, er_reach_mi=45, curated=True, out_html=None, public=True):
    """Any driving corridor in the US -> the same map/table as Harley's corridor maps, from live data.
    The road comes from one of: origin/destination (+ via stops) routed by OSRM; google_link, a Google Maps
    directions link whose stops become the route; or track, the text of a GPX/KML file used exactly as drawn.
    corridor_mi: every clinic this close to the road is kept.  er_reach_mi: 24/7, night and emergency-named
    clinics are kept out to this distance (a metro ER 30 mi off the highway is worth knowing about; a daytime GP is not)."""
    prov = _provider_pick(provider)
    if google_link:
        stops = waypoints_from_google_link(google_link)
        origin, destination = stops[0], stops[-1]
        via = stops[1:-1] + list(via or [])
    if track:
        route = route_from_track(track)
        o, d = (route["lat"][0], route["lng"][0]), (route["lat"][-1], route["lng"][-1])
        origin = origin or f"{o[0]:.4f},{o[1]:.4f}"; destination = destination or f"{d[0]:.4f},{d[1]:.4f}"
    else:
        if not origin or not destination:
            raise ValueError("Give origin and destination, a google_link, or a track")
        o, d = geocode(origin), geocode(destination)
        route = route_osrm([o] + [geocode(v) for v in (via or [])] + [d])
    variant = route_osrm([o] + [geocode(v) for v in variant_via] + [d]) if variant_via else None
    frames, warnings = [], []
    if curated:
        frames.append(load_curated(public=public))
    try:
        if prov == "google":
            try:
                live = fetch_google(_centers_along(route, step_mi=18), radius_mi=corridor_mi + 3, er_reach_mi=er_reach_mi)
            except RuntimeError as e:                  # key rejected, quota reached, outage: fall back, do not fail
                warnings.append(f"Google Places unavailable ({e}); live layer from OpenStreetMap instead"); prov = "osm"
        if prov == "osm":
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


def home_map(home, *, label=None, radius_mi=15, rings=(5, 15), title=None, provider="auto", curated=True, out_html=None, public=True):
    """Everything within radius_mi of a home address, sorted by distance, with the rings of Harley's home map.
    24/7, night and emergency-named clinics are kept out to twice the radius."""
    prov = _provider_pick(provider)
    lat, lng = geocode(home)
    frames, warnings = [], []
    if curated:
        frames.append(load_curated(public=public))
    try:
        if prov == "google":
            centers = [(lat, lng)]
            if radius_mi > 10:                      # ring of extra centres so the 20-per-call cap does not truncate a metro
                r_lat = radius_mi * 0.6 / 69.0; r_lng = radius_mi * 0.6 / (69.0 * math.cos(math.radians(lat)))
                centers += [(lat + r_lat * math.cos(a), lng + r_lng * math.sin(a)) for a in np.linspace(0, 2 * math.pi, 6, endpoint=False)]
            try:
                live = fetch_google(centers, radius_mi=min(radius_mi, 31), er_reach_mi=radius_mi * 2)
            except RuntimeError as e:
                warnings.append(f"Google Places unavailable ({e}); live layer from OpenStreetMap instead"); prov = "osm"
        if prov == "osm":
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


# vetlocator | 9 pwa
LEAFLET_CDN = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/"


def _fetch_bytes(url):
    p = CACHE_DIR / ("asset_" + hashlib.md5(url.encode()).hexdigest()[:12])
    if p.exists():
        return p.read_bytes()
    r = requests.get(url, headers={"User-Agent": UA}, timeout=60); r.raise_for_status()
    p.write_bytes(r.content)
    return r.content


# Real street geometry for the icon's little map (OpenStreetMap, a suburban neighbourhood with a highway, parks and
# ponds), simplified and normalized to 0..1 so the icon needs no network and looks like an actual map, not a grid.
_ICON_MAP = json.loads('''{"roads":[["minor",[[0.291,0.037],[0.293,0.026]]],["minor",[[0.303,0.059],[0.303,0.045]]],["minor",[[0.285,0.427],[0.296,0.419],[0.326,0.449],[0.335,0.445],[0.338,0.432],[0.335,0.331],[0.338,0.301]]],["minor",[[0.739,0.498],[0.714,0.465],[0.706,0.465]]],["minor",[[0.419,0.496],[0.422,0.502],[0.438,0.504],[0.448,0.486],[0.467,0.486],[0.471,0.479]]],["minor",[[0.279,0.029],[0.278,0.054]]],["minor",[[0.218,0.552],[0.217,0.582]]],["minor",[[0.627,0.675],[0.58,0.621],[0.574,0.62]]],["minor",[[0.618,0.721],[0.63,0.728],[0.765,0.728],[0.842,0.693]]],["minor",[[0.394,0.64],[0.379,0.642],[0.351,0.667],[0.343,0.679]]],["minor",[[0.392,0.751],[0.387,0.765],[0.378,0.772],[0.352,0.747],[0.317,0.734],[0.307,0.736],[0.299,0.73],[0.292,0.665],[0.282,0.618],[0.276,0.602],[0.261,0.588],[0.206,0.582],[0.176,0.617],[0.167,0.623],[0.147,0.612],[0.131,0.61]]],["mid",[[0.726,0.579],[0.733,0.521],[0.754,0.461],[0.776,0.432],[0.826,0.404],[0.865,0.37],[0.947,0.248]]],["minor",[[0.891,0.107],[0.904,0.067],[0.862,0.0]]],["minor",[[0.031,-0.09],[0.146,-0.092],[0.223,-0.051],[0.252,0.002],[0.279,0.029],[0.303,0.045],[0.313,0.03]]],["main",[[0.612,0.817],[0.578,0.817]]],["mid",[[0.845,0.051],[0.851,0.027],[0.882,-0.046],[0.913,-0.171],[0.937,-0.214],[0.96,-0.227]]],["minor",[[0.501,0.06],[0.506,-0.053],[0.514,-0.081]]],["minor",[[0.778,0.197],[0.783,0.169],[0.79,0.157],[0.81,0.159],[0.821,0.165],[0.867,0.241],[0.899,0.319]]],["minor",[[0.572,-0.066],[0.582,-0.062],[0.599,-0.041],[0.604,0.035],[0.609,0.04],[0.661,0.066],[0.686,0.094],[0.704,0.127],[0.728,0.15],[0.748,0.185],[0.778,0.197]]],["minor",[[0.16,0.559],[0.147,0.428],[0.151,0.374]]],["minor",[[0.154,0.31],[0.156,0.249]]],["minor",[[0.133,0.924],[0.121,0.876]]],["minor",[[0.965,0.466],[0.932,0.435],[0.915,0.367],[0.963,0.292]]],["minor",[[1.057,0.27],[1.053,0.273],[1.019,0.245],[0.999,0.203],[0.951,0.068],[0.915,0.001],[0.882,-0.046]]],["minor",[[0.999,0.203],[1.006,0.184]]],["mid",[[0.268,0.295],[0.26,0.274],[0.253,0.264],[0.245,0.261],[0.131,0.245]]],["minor",[[0.97,0.347],[0.973,0.347],[0.981,0.329]]],["minor",[[0.295,0.686],[0.309,0.682],[0.32,0.673]]],["minor",[[0.35,0.003],[0.358,0.036],[0.363,0.04],[0.371,0.032]]],["minor",[[0.969,0.031],[0.961,0.003]]],["minor",[[0.426,0.778],[0.424,0.642],[0.452,0.591],[0.459,0.588],[0.471,0.597]]],["mid",[[0.467,0.829],[0.467,0.817]]],["minor",[[0.508,0.333],[0.52,0.343]]],["minor",[[0.101,0.663],[0.121,0.667],[0.128,0.657],[0.131,0.644],[0.129,0.494],[0.122,0.439],[0.114,0.418]]],["minor",[[0.389,0.327],[0.386,0.364],[0.39,0.472],[0.385,0.485],[0.376,0.486],[0.367,0.478],[0.365,0.469],[0.36,0.35],[0.363,0.314]]],["minor",[[0.268,0.37],[0.258,0.379],[0.234,0.376],[0.229,0.369],[0.226,0.353],[0.229,0.24],[0.258,0.17],[0.262,0.165],[0.276,0.163]]],["minor",[[0.046,0.967],[0.048,0.941],[0.073,0.885]]],["minor",[[0.573,0.562],[0.575,0.35],[0.596,0.241],[0.603,0.159]]],["minor",[[0.64,0.283],[0.631,0.31],[0.608,0.338],[0.59,0.346],[0.577,0.337]]],["minor",[[0.113,0.742],[0.112,0.775]]],["minor",[[0.084,0.587],[0.08,0.507],[0.067,0.436],[0.069,0.355]]],["minor",[[0.08,0.951],[0.092,0.914]]],["minor",[[0.009,0.457],[-0.001,0.444],[-0.01,0.442],[-0.04,0.465],[-0.048,0.467],[-0.085,0.435],[-0.143,0.44],[-0.167,0.427],[-0.201,0.396],[-0.232,0.394]]],["minor",[[0.103,0.619],[0.101,0.663]]],["minor",[[0.496,0.531],[0.502,0.534],[0.515,0.534],[0.522,0.521],[0.525,0.395]]],["minor",[[1.0,0.559],[1.017,0.491]]],["minor",[[0.983,0.151],[0.996,0.13],[1.008,0.121]]],["minor",[[0.467,0.775],[0.408,0.782]]],["minor",[[-0.042,0.783],[-0.033,0.799],[0.023,0.799]]],["minor",[[0.673,0.772],[0.666,0.781],[0.63,0.78]]],["minor",[[0.966,0.839],[0.973,0.835],[1.002,0.836],[1.012,0.846]]],["mid",[[0.268,0.295],[0.262,0.321],[0.262,0.345],[0.306,0.505],[0.338,0.664],[0.349,0.69],[0.367,0.718]]],["mid",[[0.7,0.567],[0.684,0.541],[0.665,0.48],[0.65,0.448],[0.632,0.425],[0.62,0.415]]],["mid",[[0.956,0.523],[0.97,0.446],[1.0,0.36],[1.02,0.281],[1.036,0.147],[1.033,0.06],[1.024,-0.003],[1.012,-0.049],[0.982,-0.128],[0.967,-0.18],[0.959,-0.236],[0.957,-0.303]]],["minor",[[0.03,0.101],[0.021,0.1],[0.02,0.087],[0.002,0.052]]],["minor",[[0.015,0.182],[0.012,0.138]]],["minor",[[0.01,0.744],[0.028,0.747],[0.029,0.732],[0.035,0.722]]],["mid",[[0.284,1.329],[0.291,1.327],[0.305,1.311],[0.324,1.277],[0.331,1.243],[0.336,1.173],[0.365,1.074],[0.38,1.058],[0.4,1.055],[0.41,1.043],[0.415,1.017],[0.415,0.963]]],["minor",[[0.119,-0.092],[0.118,-0.043],[0.124,-0.027],[0.165,-0.005],[0.223,0.013],[0.232,0.01],[0.243,-0.014]]],["mid",[[0.199,1.183],[0.184,1.158],[0.147,1.142],[0.129,1.113],[0.12,1.069],[0.115,0.96],[0.103,0.929],[0.073,0.885]]],["minor",[[0.674,0.513],[0.681,0.505],[0.682,0.494]]],["minor",[[0.866,0.986],[0.866,0.909]]],["minor",[[0.854,0.534],[0.853,0.5]]],["minor",[[0.016,0.252],[-0.005,0.24]]],["minor",[[0.649,0.524],[0.642,0.548],[0.635,0.553]]],["minor",[[0.658,0.465],[0.677,0.429],[0.681,0.431]]],["minor",[[0.523,0.175],[0.502,0.176],[0.499,0.182],[0.497,0.254],[0.478,0.352],[0.472,0.406],[0.471,0.479]]],["minor",[[0.86,0.82],[0.867,0.827],[0.876,0.824],[0.881,0.815],[0.89,0.759]]],["minor",[[0.951,0.068],[0.969,0.031],[0.993,-0.006]]],["minor",[[0.904,0.516],[0.904,0.454],[0.889,0.404]]],["minor",[[0.977,0.989],[0.977,0.92]]],["minor",[[0.284,0.914],[0.258,0.914]]],["minor",[[0.782,0.908],[0.772,0.916]]],["minor",[[0.801,0.478],[0.808,0.568]]],["minor",[[0.204,0.311],[0.204,0.186],[0.211,0.16]]],["minor",[[0.276,-0.01],[0.267,-0.004],[0.254,0.032]]],["minor",[[0.08,0.733],[0.082,0.776]]],["minor",[[0.057,0.608],[0.057,0.524],[0.045,0.463],[0.042,0.422],[0.043,0.303]]],["minor",[[0.059,0.749],[0.073,0.732],[0.08,0.733],[0.132,0.748],[0.162,0.784],[0.207,0.776],[0.226,0.784]]],["minor",[[0.787,0.718],[0.793,0.764],[0.802,0.772],[0.822,0.761],[0.849,0.725]]],["minor",[[0.448,0.486],[0.439,0.381],[0.445,0.337],[0.472,0.25],[0.474,0.097],[0.468,0.078],[0.446,0.059],[0.4,0.036]]],["minor",[[0.451,0.273],[0.462,0.289]]],["minor",[[0.809,0.887],[0.782,0.908]]],["minor",[[0.392,0.751],[0.397,0.722],[0.39,0.561],[0.387,0.555],[0.361,0.542],[0.32,0.577]]],["minor",[[0.196,0.592],[0.189,0.567]]],["minor",[[0.751,0.583],[0.754,0.543],[0.763,0.52],[0.776,0.503]]],["minor",[[0.701,1.896],[0.702,1.66],[0.696,1.625],[0.692,1.3],[0.682,1.172],[0.683,1.139],[0.694,1.078],[0.736,0.963]]],["minor",[[0.881,0.522],[0.877,0.477],[0.862,0.438]]],["minor",[[0.862,0.768],[0.866,0.746]]],["minor",[[1.031,0.043],[1.011,0.055],[1.007,0.053],[0.993,-0.006],[0.972,-0.066],[0.913,-0.171]]],["minor",[[0.204,0.311],[0.201,0.332],[0.196,0.338],[0.182,0.331],[0.179,0.31],[0.18,0.252]]],["minor",[[0.212,0.862],[0.212,0.904]]],["minor",[[0.832,0.55],[0.827,0.517],[0.831,0.458]]],["minor",[[0.895,0.985],[0.895,0.915]]],["minor",[[0.955,0.807],[0.943,0.836],[0.913,0.834],[0.908,0.817],[0.913,0.775]]],["minor",[[0.247,0.586],[0.248,0.555]]],["minor",[[0.969,0.111],[0.982,0.08]]],["minor",[[0.62,0.415],[0.63,0.374],[0.66,0.337],[0.663,0.324]]],["minor",[[0.599,0.516],[0.597,0.481],[0.603,0.453]]],["mid",[[0.967,0.879],[0.964,0.83],[0.951,0.801],[0.866,0.746],[0.849,0.725],[0.843,0.706],[0.821,0.559]]],["minor",[[0.952,0.985],[0.953,0.922]]],["minor",[[0.98,0.539],[0.993,0.471],[1.008,0.431]]],["minor",[[0.193,1.271],[0.177,1.247],[0.159,1.237],[0.147,1.241],[0.109,1.278],[0.101,1.276],[0.095,1.256],[0.099,1.219],[0.132,1.161],[0.149,1.095],[0.167,1.057],[0.173,0.985],[0.182,0.962],[0.191,0.955],[0.226,0.992],[0.259,1.094]]],["minor",[[0.108,0.58],[0.105,0.508],[0.091,0.431],[0.088,0.366],[0.053,0.341],[0.042,0.34]]],["minor",[[0.1,0.22],[0.129,0.124],[0.135,0.089],[0.137,0.063],[0.132,0.036],[0.123,0.023],[0.109,0.023],[0.069,0.068],[0.046,0.079],[0.03,0.077]]],["minor",[[0.327,0.018],[0.331,0.05]]],["minor",[[0.269,0.594],[0.277,0.566],[0.295,0.556]]],["minor",[[0.469,0.647],[0.45,0.647]]],["minor",[[0.445,0.601],[0.437,0.577],[0.421,0.566]]],["minor",[[0.449,0.214],[0.45,0.12]]],["minor",[[0.073,0.655],[0.074,0.637]]],["minor",[[0.7,0.567],[0.705,0.518]]],["minor",[[0.023,0.799],[0.028,0.785],[0.028,0.747]]],["mid",[[0.612,0.781],[0.618,0.721],[0.628,0.672],[0.64,0.633],[0.669,0.577],[0.676,0.553]]],["minor",[[0.628,0.493],[0.627,0.497]]],["minor",[[0.262,-0.097],[0.296,-0.135],[0.31,-0.139],[0.352,-0.023],[0.35,0.003]]],["minor",[[0.35,0.003],[0.317,0.023],[0.313,0.03]]],["minor",[[0.786,1.048],[0.727,0.948],[0.718,0.943],[0.692,0.953]]],["minor",[[0.136,0.985],[0.118,0.988]]],["minor",[[0.641,0.434],[0.654,0.401],[0.68,0.368],[0.685,0.37]]],["minor",[[0.672,0.728],[0.673,0.772],[0.68,0.782],[0.78,0.781],[0.793,0.764]]],["minor",[[0.468,0.709],[0.448,0.709]]],["minor",[[0.285,0.864],[0.212,0.862]]],["minor",[[0.236,0.876],[0.236,0.863]]],["minor",[[0.058,0.915],[0.042,0.883],[0.05,0.862]]],["minor",[[0.167,0.623],[0.166,0.602]]],["minor",[[0.309,0.432],[0.313,0.408],[0.311,0.301]]],["minor",[[0.311,0.353],[0.299,0.354]]],["minor",[[0.929,0.516],[0.929,0.493]]],["minor",[[0.286,0.636],[0.309,0.614]]],["minor",[[0.776,0.432],[0.767,0.404],[0.738,0.378],[0.705,0.331],[0.656,0.239],[0.638,0.22],[0.623,0.209],[0.578,0.192],[0.572,0.175],[0.573,0.141]]],["minor",[[1.013,0.313],[0.977,0.249]]],["minor",[[1.075,1.003],[1.001,1.002],[0.992,1.001],[0.969,0.985],[0.857,0.985],[0.815,0.95],[0.804,0.953],[0.795,0.963]]],["minor",[[0.212,0.904],[0.275,0.978],[0.284,0.98]]],["minor",[[0.147,0.867],[0.152,0.879]]],["minor",[[0.177,0.873],[0.172,0.865],[0.165,0.864],[0.157,0.87],[0.139,0.916],[0.111,0.947]]],["minor",[[0.149,0.94],[0.139,0.916]]],["minor",[[0.976,0.426],[0.95,0.383],[0.947,0.384]]],["minor",[[0.009,0.457],[0.017,0.437],[0.017,0.328],[0.012,0.312],[-0.012,0.298]]],["minor",[[0.003,0.607],[0.011,0.595],[0.015,0.573],[0.015,0.48],[0.009,0.457]]],["minor",[[0.59,0.271],[0.577,0.255],[0.574,0.244]]],["minor",[[0.614,0.263],[0.613,0.272],[0.608,0.277],[0.601,0.278],[0.59,0.271]]],["minor",[[0.754,0.461],[0.74,0.437],[0.713,0.409],[0.705,0.407]]],["minor",[[0.474,0.215],[0.424,0.214]]],["minor",[[0.253,0.264],[0.277,0.201],[0.276,0.123],[0.272,0.109],[0.243,0.082],[0.233,0.081],[0.224,0.09],[0.22,0.071],[0.213,0.066]]],["minor",[[0.778,0.197],[0.776,0.239],[0.78,0.257],[0.789,0.272],[0.812,0.275],[0.825,0.271],[0.851,0.251],[0.862,0.231]]],["minor",[[0.224,0.09],[0.195,0.119],[0.183,0.153],[0.179,0.186],[0.18,0.252]]],["minor",[[0.184,0.529],[0.179,0.502],[0.171,0.392],[0.163,0.379],[0.127,0.364]]],["minor",[[0.174,0.408],[0.186,0.404],[0.188,0.399]]],["minor",[[0.834,0.968],[0.838,0.9]]],["minor",[[0.115,0.96],[0.103,0.971]]],["minor",[[0.339,0.724],[0.337,0.741]]],["minor",[[0.494,0.113],[0.474,0.113]]],["mid",[[0.306,0.505],[0.289,0.52],[0.281,0.522],[0.212,0.513],[0.201,0.515],[0.184,0.529],[0.16,0.559],[0.141,0.572],[0.093,0.582],[0.067,0.604],[0.057,0.608],[0.033,0.607]]],["minor",[[0.64,0.633],[0.611,0.592],[0.606,0.575]]],["minor",[[0.519,0.589],[0.493,0.59],[0.48,0.575]]],["minor",[[0.003,0.607],[-0.002,0.608],[-0.045,0.588],[-0.059,0.597]]],["minor",[[0.369,0.545],[0.361,0.596],[0.33,0.627]]],["minor",[[0.443,0.43],[0.433,0.438],[0.425,0.439],[0.416,0.429],[0.413,0.405],[0.424,0.285],[0.427,0.049]]],["minor",[[0.101,0.663],[0.033,0.643],[0.013,0.641],[0.005,0.624],[0.003,0.607]]],["minor",[[0.778,0.866],[0.692,0.866]]],["minor",[[0.575,1.351],[0.574,1.24],[0.579,1.21],[0.59,1.184],[0.608,1.158],[0.688,0.983],[0.692,0.922]]],["minor",[[0.198,0.778],[0.195,0.819]]],["mid",[[0.536,0.401],[0.295,0.279]]],["mid",[[0.558,0.423],[0.598,0.447],[0.657,0.534],[0.682,0.558],[0.7,0.567]]],["mid",[[0.268,0.295],[0.28,0.288],[0.288,0.289],[0.532,0.411]]],["main",[[0.064,0.825],[0.284,0.832]]],["mid",[[0.059,0.749],[0.043,0.713],[0.033,0.675],[0.033,0.523],[0.039,0.509],[0.103,0.437],[0.118,0.409],[0.127,0.364],[0.131,0.245]]],["mid",[[0.199,1.183],[0.259,1.094],[0.276,1.056],[0.284,0.996],[0.285,0.864]]],["mid",[[0.806,1.005],[0.8,0.979]]],["minor",[[0.154,0.875],[0.163,0.899]]],["minor",[[0.469,0.484],[0.471,0.486]]],["minor",[[0.441,0.412],[0.445,0.409]]],["minor",[[0.386,0.483],[0.388,0.486]]],["minor",[[0.227,0.362],[0.222,0.366]]],["minor",[[0.333,0.448],[0.336,0.455]]],["minor",[[0.009,0.637],[0.007,0.643]]],["minor",[[0.015,0.317],[0.016,0.313]]],["minor",[[0.051,0.935],[0.053,0.938]]],["minor",[[0.692,0.866],[0.691,0.864]]],["minor",[[0.247,0.193],[0.238,0.173]]],["minor",[[0.225,0.089],[0.229,0.104]]],["minor",[[0.259,0.097],[0.257,0.104]]],["mid",[[0.612,0.817],[0.612,0.827]]],["main",[[0.778,0.826],[0.794,0.831],[0.839,0.858],[0.884,0.874],[0.952,0.88]]],["main",[[0.967,0.87],[0.89,0.866],[0.845,0.85],[0.82,0.837]]],["minor",[[0.175,0.101],[0.154,0.091]]],["minor",[[0.146,0.236],[0.146,0.212],[0.113,0.171]]],["minor",[[0.024,0.003],[0.007,0.001],[-0.016,0.019]]],["minor",[[0.131,0.163],[0.125,0.186]]],["minor",[[0.148,0.138],[0.154,0.091],[0.148,0.06]]],["minor",[[0.114,0.213],[0.126,0.187]]],["minor",[[0.18,0.058],[0.165,0.051],[0.137,0.063]]],["minor",[[0.007,0.001],[0.006,-0.035]]],["minor",[[0.161,0.039],[0.164,0.051]]],["minor",[[0.066,0.123],[0.047,0.129]]],["minor",[[0.136,0.01],[0.151,0.039],[0.152,0.057]]],["minor",[[0.045,0.163],[0.047,0.129],[0.041,0.11],[0.041,0.08]]],["minor",[[0.183,0.033],[0.18,0.058],[0.185,0.081]]],["minor",[[0.046,0.079],[0.048,0.028],[0.062,0.011]]],["minor",[[0.16,0.183],[0.148,0.2],[0.146,0.212]]],["minor",[[0.105,0.198],[0.081,0.162],[0.066,0.123],[0.065,0.07]]],["minor",[[0.121,0.145],[0.114,0.132],[0.114,0.094],[0.121,0.058],[0.106,0.026]]],["minor",[[0.092,0.13],[0.085,0.166]]],["minor",[[0.114,0.132],[0.101,0.136],[0.092,0.13],[0.085,0.104]]],["minor",[[0.036,0.007],[0.048,0.028]]],["minor",[[0.163,0.153],[0.154,0.142],[0.128,0.125]]],["minor",[[0.06,-0.021],[0.062,0.011],[0.077,0.06]]],["minor",[[0.08,-0.012],[0.093,0.039]]],["minor",[[0.107,-0.007],[0.086,0.01]]],["mid",[[0.415,0.963],[0.413,0.818]]],["mid",[[0.285,0.864],[0.284,0.822]]],["mid",[[1.198,0.717],[1.101,0.413],[1.087,0.398],[1.039,0.392],[1.024,0.384],[1.0,0.36],[0.963,0.292],[0.927,0.186],[0.891,0.107],[0.863,0.068],[0.817,0.027],[0.811,0.016],[0.809,-0.019],[0.823,-0.069],[0.834,-0.09],[0.839,-0.117],[0.84,-0.145],[0.831,-0.199],[0.826,-0.206],[0.799,-0.221],[0.742,-0.224],[0.688,-0.271]]],["mid",[[0.073,0.885],[0.065,0.851],[0.065,0.815]]],["mid",[[0.561,0.413],[0.546,0.406]]],["mid",[[0.956,0.523],[0.917,0.515],[0.881,0.522],[0.854,0.534],[0.808,0.568],[0.767,0.583],[0.741,0.583],[0.7,0.567]]],["minor",[[0.925,1.016],[0.925,0.918]]],["mid",[[0.8,0.979],[0.782,0.908],[0.778,0.866]]],["minor",[[0.692,0.906],[0.692,0.866]]],["minor",[[0.692,0.922],[0.692,0.906]]],["minor",[[0.095,0.918],[0.102,0.892]]],["minor",[[0.102,0.892],[0.094,0.882],[0.093,0.86]]],["minor",[[0.104,0.862],[0.08,0.861],[0.073,0.885]]],["minor",[[0.363,1.081],[0.337,1.043],[0.335,1.022],[0.338,0.986],[0.35,0.974],[0.367,1.01]]],["hwy",[[0.548,0.667],[0.546,0.54]]],["main",[[0.778,0.817],[0.638,0.818]]],["main",[[0.795,0.822],[0.778,0.817]]],["main",[[0.612,0.827],[0.76,0.827]]],["main",[[0.952,0.88],[0.967,0.879]]],["main",[[0.976,0.87],[0.967,0.87]]],["main",[[1.074,0.861],[1.043,0.87],[0.976,0.87]]],["main",[[0.967,0.879],[1.043,0.88],[1.074,0.87]]],["main",[[0.212,0.819],[0.195,0.819]]],["main",[[0.252,0.821],[0.212,0.819]]],["main",[[0.284,0.822],[0.252,0.821]]],["main",[[0.195,0.819],[0.085,0.816]]],["main",[[0.284,0.832],[0.331,0.831]]],["main",[[0.506,0.828],[0.515,0.827]]],["main",[[0.085,0.816],[0.065,0.815]]],["main",[[0.032,0.824],[0.064,0.825]]],["main",[[0.065,0.815],[-0.045,0.812]]],["main",[[-0.073,0.821],[0.032,0.824]]],["main",[[0.76,0.827],[0.778,0.826]]],["main",[[0.638,0.818],[0.612,0.817]]],["main",[[0.578,0.817],[0.556,0.817]]],["main",[[0.563,0.827],[0.591,0.827]]],["main",[[0.591,0.827],[0.612,0.827]]],["main",[[0.548,0.827],[0.563,0.827]]],["hwy",[[0.546,0.958],[0.548,0.906]]],["hwy",[[0.542,0.817],[0.542,0.827]]],["hwy",[[0.548,0.846],[0.548,0.827]]],["main",[[0.531,0.817],[0.488,0.817]]],["main",[[0.467,0.829],[0.506,0.828]]],["main",[[0.48,0.817],[0.467,0.817]]],["main",[[0.467,0.817],[0.441,0.818]]],["main",[[0.449,0.829],[0.467,0.829]]],["main",[[0.413,0.829],[0.449,0.829]]],["main",[[0.441,0.818],[0.413,0.818]]],["main",[[0.324,0.821],[0.308,0.822]]],["main",[[0.351,0.82],[0.324,0.821]]],["main",[[0.308,0.822],[0.284,0.822]]],["main",[[0.397,0.819],[0.351,0.82]]],["main",[[0.413,0.818],[0.397,0.819]]],["main",[[0.392,0.83],[0.413,0.829]]],["main",[[0.331,0.831],[0.392,0.83]]],["hwy",[[0.548,0.817],[0.548,0.782]]],["minor",[[0.313,0.03],[0.249,-0.13],[0.233,-0.2]]],["hwy",[[0.548,0.885],[0.548,0.846]]],["hwy",[[0.542,0.86],[0.543,0.909]]],["hwy",[[0.543,0.909],[0.546,0.958]]],["hwy",[[0.548,0.906],[0.548,0.885]]],["main",[[0.533,0.827],[0.542,0.827]]],["main",[[0.556,0.817],[0.548,0.817]]],["main",[[0.542,0.817],[0.531,0.817]]],["hwy",[[0.548,0.782],[0.548,0.76]]],["hwy",[[0.548,0.692],[0.548,0.667]]],["hwy",[[0.548,0.76],[0.548,0.717]]],["hwy",[[0.548,0.717],[0.548,0.692]]],["hwy",[[0.546,0.465],[0.546,0.44]]],["hwy",[[0.546,0.54],[0.546,0.465]]],["hwy",[[0.546,0.435],[0.546,0.418]]],["hwy",[[0.546,0.399],[0.547,0.377]]],["hwy",[[0.547,0.377],[0.547,0.358]]],["hwy",[[0.547,0.358],[0.547,0.317]]],["hwy",[[0.545,0.069],[0.547,0.317]]],["hwy",[[0.548,0.009],[0.548,-0.025]]],["minor",[[0.5,0.179],[0.498,0.175]]],["minor",[[0.416,0.429],[0.415,0.433]]],["minor",[[0.425,0.638],[0.422,0.633]]],["minor",[[0.78,0.559],[0.781,0.58]]],["minor",[[0.573,0.32],[0.579,0.325]]],["minor",[[0.909,0.828],[0.908,0.83]]],["hwy",[[0.546,0.54],[0.543,0.637]]],["hwy",[[0.543,0.637],[0.543,0.686]]],["hwy",[[0.543,0.686],[0.543,0.707]]],["hwy",[[0.543,0.707],[0.542,0.766]]],["hwy",[[0.542,0.766],[0.542,0.8]]],["hwy",[[0.542,0.8],[0.542,0.817]]],["hwy",[[0.547,0.317],[0.55,0.069]]],["minor",[[0.041,0.706],[0.035,0.722]]],["minor",[[0.035,0.722],[0.043,0.713]]],["minor",[[0.09,0.374],[0.097,0.369]]],["hwy",[[0.542,0.827],[0.542,0.86]]],["main",[[0.542,0.827],[0.548,0.827]]],["hwy",[[0.548,0.827],[0.548,0.817]]],["main",[[0.548,0.817],[0.542,0.817]]],["mid",[[0.467,0.775],[0.469,0.608],[0.496,0.531],[0.499,0.395]]],["main",[[0.515,0.827],[0.533,0.827]]],["minor",[[0.266,0.227],[0.273,0.243]]],["minor",[[0.015,0.182],[0.015,0.192]]],["mid",[[0.778,0.826],[0.778,0.817]]],["mid",[[0.778,0.841],[0.778,0.826]]],["mid",[[0.612,0.827],[0.611,1.029],[0.576,1.112],[0.562,1.127]]],["mid",[[0.295,0.279],[0.281,0.277],[0.268,0.295]]],["main",[[0.488,0.817],[0.48,0.817]]],["mid",[[0.534,0.412],[0.546,0.418]]],["mid",[[0.532,0.411],[0.534,0.412]]],["mid",[[0.573,0.415],[0.561,0.413]]],["mid",[[0.612,0.781],[0.612,0.817]]],["mid",[[0.68,0.531],[0.676,0.553]]],["mid",[[0.62,0.415],[0.597,0.409],[0.573,0.415]]],["mid",[[0.551,0.42],[0.558,0.423]]],["mid",[[0.541,0.403],[0.536,0.401]]],["mid",[[0.367,0.718],[0.392,0.751]]],["mid",[[0.392,0.751],[0.408,0.782],[0.413,0.818]]],["minor",[[0.5,0.383],[0.522,0.26],[0.523,0.1]]],["mid",[[0.467,0.817],[0.467,0.775]]],["mid",[[-0.03,0.375],[-0.017,0.341],[0.002,0.205],[0.013,0.183],[0.03,0.178]]],["mid",[[0.065,0.815],[0.064,0.774],[0.059,0.749]]],["mid",[[0.03,0.178],[0.031,-0.031]]],["mid",[[0.778,0.866],[0.778,0.841]]],["minor",[[1.039,0.631],[1.021,0.591],[1.0,0.559],[0.974,0.534],[0.956,0.523]]],["minor",[[0.108,0.898],[0.102,0.892]]],["minor",[[0.947,0.248],[0.966,0.222],[0.967,0.215],[0.95,0.16],[0.928,0.11],[0.925,0.109],[0.909,0.143]]],["hwy",[[0.546,0.418],[0.546,0.406]]],["mid",[[0.546,0.418],[0.551,0.42]]],["hwy",[[0.546,0.44],[0.546,0.435]]],["mid",[[0.546,0.406],[0.541,0.403]]],["hwy",[[0.546,0.406],[0.546,0.399]]],["minor",[[0.598,0.409],[0.597,0.401]]],["minor",[[0.367,0.718],[0.369,0.712]]],["mid",[[0.131,0.245],[0.064,0.188],[0.053,0.181],[0.03,0.178]]],["main",[[0.82,0.837],[0.795,0.822]]],["minor",[[0.283,1.015],[0.295,1.02],[0.314,1.02],[0.322,1.019],[0.325,1.009],[0.322,0.973],[0.284,0.972]]],["hwy",[[0.547,1.015],[0.546,0.958]]],["hwy",[[0.548,0.009],[0.545,0.069]]],["hwy",[[0.55,0.069],[0.548,0.009]]],["mid",[[0.499,0.395],[0.5,0.383]]]],"areas":[["park",[[0.804,0.985],[0.811,0.976],[0.847,1.011],[0.923,1.012],[0.923,1.035],[0.888,1.036],[0.882,1.043],[0.88,1.101],[0.876,1.113],[0.856,1.095],[0.837,1.11],[0.804,0.985]]],["park",[[0.128,0.252],[0.063,0.194],[0.048,0.187],[0.03,0.185],[0.03,0.271],[0.05,0.299],[0.048,0.308],[0.07,0.328],[0.112,0.343],[0.126,0.342],[0.128,0.252]]],["park",[[0.413,-0.164],[0.411,-0.105],[0.393,0.001],[0.394,0.023],[0.388,0.04],[0.371,-0.022],[0.313,-0.197],[0.328,-0.207],[0.342,-0.193],[0.362,-0.219],[0.398,-0.17],[0.413,-0.164]]],["water",[[0.677,-0.055],[0.693,-0.083],[0.707,-0.099],[0.718,-0.026],[0.716,-0.002],[0.713,0.006],[0.707,0.008],[0.701,-0.021],[0.677,-0.055]]],["beige",[[0.934,0.024],[0.969,-0.066],[0.915,-0.162],[0.886,-0.047],[0.934,0.024]]],["park",[[1.069,0.823],[1.072,0.688],[1.062,0.68],[1.042,0.628],[1.038,0.635],[1.014,0.584],[0.974,0.538],[0.933,0.521],[0.889,0.524],[0.856,0.539],[0.826,0.561],[0.825,0.567],[0.845,0.701],[0.854,0.727],[0.86,0.736],[0.959,0.799],[0.984,0.82],[0.984,0.831],[1.052,0.84],[1.069,0.823]]],["water",[[0.268,0.73],[0.276,0.712],[0.277,0.68],[0.269,0.637],[0.26,0.623],[0.218,0.614],[0.209,0.617],[0.173,0.664],[0.143,0.677],[0.145,0.687],[0.172,0.695],[0.209,0.713],[0.268,0.73]]],["park",[[0.278,0.786],[0.24,0.782],[0.241,0.759],[0.203,0.742],[0.198,0.747],[0.196,0.774],[0.195,0.738],[0.166,0.726],[0.07,0.703],[0.052,0.724],[0.037,0.68],[0.072,0.682],[0.128,0.698],[0.124,0.67],[0.132,0.653],[0.134,0.615],[0.144,0.616],[0.165,0.628],[0.165,0.656],[0.174,0.653],[0.201,0.618],[0.214,0.61],[0.263,0.619],[0.273,0.637],[0.28,0.669],[0.284,0.736],[0.278,0.786]]],["beige",[[0.403,0.332],[0.412,0.251],[0.412,0.142],[0.381,0.108],[0.381,0.058],[0.32,0.084],[0.288,0.09],[0.288,0.116],[0.283,0.125],[0.278,0.132],[0.288,0.124],[0.289,0.274],[0.403,0.332]]],["park",[[0.423,0.125],[0.413,0.125],[0.412,0.142],[0.381,0.108],[0.381,0.058],[0.394,0.023],[0.396,0.034],[0.4,0.044],[0.409,0.044],[0.406,0.069],[0.413,0.073],[0.413,0.12],[0.423,0.121],[0.423,0.125]]],["park",[[0.488,0.544],[0.468,0.514],[0.454,0.515],[0.442,0.548],[0.457,0.583],[0.47,0.589],[0.488,0.544]]],["park",[[0.317,0.549],[0.359,0.511],[0.352,0.485],[0.34,0.486],[0.337,0.464],[0.337,0.487],[0.294,0.447],[0.317,0.549]]],["park",[[0.186,0.836],[0.188,0.872],[0.184,0.884],[0.179,0.878],[0.174,0.881],[0.167,0.868],[0.179,0.899],[0.17,0.914],[0.166,0.904],[0.168,0.918],[0.155,0.951],[0.15,0.947],[0.154,0.957],[0.143,0.977],[0.137,0.979],[0.146,0.981],[0.147,1.04],[0.139,1.041],[0.144,1.044],[0.14,1.062],[0.122,1.065],[0.13,1.106],[0.155,1.049],[0.163,1.061],[0.155,1.046],[0.156,0.976],[0.179,0.921],[0.19,0.926],[0.19,0.951],[0.192,0.926],[0.202,0.932],[0.228,0.963],[0.252,1.017],[0.261,1.003],[0.269,1.027],[0.261,1.042],[0.27,1.028],[0.275,1.046],[0.278,1.035],[0.263,0.992],[0.2,0.918],[0.209,0.905],[0.199,0.917],[0.199,0.836],[0.186,0.836]]],["beige",[[0.302,0.501],[0.284,0.516],[0.213,0.507],[0.195,0.513],[0.191,0.487],[0.188,0.432],[0.198,0.425],[0.201,0.404],[0.236,0.414],[0.25,0.423],[0.28,0.424],[0.302,0.501]]],["water",[[0.004,0.917],[0.011,0.906],[0.006,0.892],[-0.003,0.887],[-0.011,0.911],[-0.014,0.938],[-0.01,0.943],[0.004,0.917]]],["park",[[0.633,-0.048],[0.69,0.003],[0.696,-0.003],[0.697,-0.014],[0.685,-0.02],[0.667,-0.042],[0.637,-0.055],[0.633,-0.048]]],["park",[[0.732,-0.106],[0.74,-0.093],[0.754,0.008],[0.75,0.008],[0.734,-0.047],[0.732,-0.106]]],["park",[[0.783,0.196],[0.802,0.204],[0.801,0.227],[0.781,0.223],[0.783,0.196]]],["beige",[[0.412,0.788],[0.418,0.812],[0.534,0.809],[0.537,0.801],[0.538,0.623],[0.472,0.627],[0.471,0.771],[0.464,0.784],[0.419,0.784],[0.412,0.788]]],["beige",[[0.539,0.859],[0.537,0.84],[0.531,0.834],[0.443,0.837],[0.418,0.85],[0.418,1.047],[0.426,1.075],[0.43,1.126],[0.438,1.145],[0.478,1.148],[0.48,1.325],[0.542,1.325],[0.539,0.859]]],["beige",[[0.556,0.656],[0.58,0.656],[0.618,0.699],[0.609,0.808],[0.61,1.009],[0.606,1.039],[0.577,1.104],[0.555,1.121],[0.556,0.656]]],["park",[[0.645,0.723],[0.762,0.722],[0.837,0.688],[0.819,0.567],[0.777,0.586],[0.75,0.59],[0.721,0.584],[0.677,0.558],[0.671,0.578],[0.66,0.598],[0.666,0.612],[0.653,0.64],[0.664,0.657],[0.657,0.681],[0.645,0.682],[0.645,0.723]]],["park",[[0.579,-0.17],[0.579,-0.07],[0.677,0.017],[0.717,0.059],[0.797,0.099],[0.864,0.167],[0.888,0.213],[0.897,0.155],[0.836,0.073],[0.759,0.003],[0.747,-0.104],[0.762,-0.12],[0.829,-0.099],[0.836,-0.13],[0.757,-0.165],[0.692,-0.236],[0.691,-0.26],[0.579,-0.17]]],["park",[[0.252,0.424],[0.268,0.425],[0.267,0.47],[0.251,0.469],[0.252,0.424]]],["park",[[0.933,-0.067],[0.951,-0.036],[0.936,0.003],[0.918,-0.028],[0.933,-0.067]]]]}''')


def _icon_png(size, maskable=False):
    """The icon: a red map pin with a white paw standing on a small rounded map plate, on a white tile.  The plate is a
    real neighbourhood drawn in the familiar light map style (pale grey land, white streets with grey edges, a blue-grey
    highway, green parks, blue water, beige shopping blocks), no labels.  maskable=True draws everything smaller so
    Android's circle/squircle masks never clip it.  Drawn at 1024 px and downsampled so edges stay smooth."""
    import io
    from PIL import Image, ImageDraw, ImageFilter
    S = 1024
    k = 0.78 if maskable else 1.0
    P = lambda x, y: (S * (0.5 + (x - 0.5) * k), S * (0.5 + (y - 0.5) * k))        # scale a point toward the centre
    W = lambda w: max(2, int(S * w * k))
    x0, y0 = P(0.08, 0.53); x1, y1 = P(0.92, 0.93); rad = int(S * 0.08 * k)
    M = lambda u, v: (x0 + u * (x1 - x0), y0 + v * (y1 - y0))                     # map units -> plate pixels
    LAND, PARK, WATER, BEIGE = "#f1f3f4", "#c5e8c2", "#a8d5f6", "#f3eddf"
    style = {"minor": ("#ffffff", "#e3e6ea", 0.008, 0.013), "mid": ("#ffffff", "#d3d8de", 0.013, 0.020),
             "main": ("#ffffff", "#c8ced6", 0.018, 0.027), "hwy": ("#a9b9d3", "#8c9fbe", 0.027, 0.037)}
    im = Image.new("RGBA", (S, S), "#ffffff")
    mp = Image.new("RGBA", (S, S), (0, 0, 0, 0)); m = ImageDraw.Draw(mp)
    m.rounded_rectangle([x0, y0, x1, y1], radius=rad, fill=LAND)
    for kind, pts in _ICON_MAP["areas"]:
        m.polygon([M(u, v) for u, v in pts], fill={"park": PARK, "water": WATER, "beige": BEIGE}[kind])
    for layer in ("edge", "fill"):                                                 # edges first, so crossings merge cleanly
        for cls in ("minor", "mid", "main", "hwy"):
            fill, edge, wf, we = style[cls]
            for c, pts in _ICON_MAP["roads"]:
                if c != cls: continue
                m.line([M(u, v) for u, v in pts], fill=edge if layer == "edge" else fill, width=W(we if layer == "edge" else wf), joint="curve")
    clip = Image.new("L", (S, S), 0); ImageDraw.Draw(clip).rounded_rectangle([x0, y0, x1, y1], radius=rad, fill=255)
    mp.putalpha(clip)
    ps = Image.new("RGBA", (S, S), (0, 0, 0, 0)); ImageDraw.Draw(ps).rounded_rectangle([x0, y0 + S * 0.012 * k, x1, y1 + S * 0.012 * k], radius=rad, fill=(0, 0, 0, 70))
    im.alpha_composite(ps.filter(ImageFilter.GaussianBlur(int(S * 0.014))))                # soft shadow so the plate lifts off the white tile
    im.alpha_composite(mp)
    ImageDraw.Draw(im).rounded_rectangle([x0, y0, x1, y1], radius=rad, outline="#dfe3e8", width=W(0.004))
    # --- the pin, with a soft shadow on the plate ---
    cx, cy = P(0.5, 0.40); R = S * 0.24 * k; tip = P(0.5, 0.80)[1]
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 0)); ImageDraw.Draw(sh).ellipse([cx - S * 0.15 * k, tip - S * 0.05 * k, cx + S * 0.15 * k, tip + S * 0.05 * k], fill=(0, 0, 0, 120))
    sh.putalpha(Image.composite(sh.getchannel("A"), Image.new("L", (S, S), 0), clip))
    im.alpha_composite(sh.filter(ImageFilter.GaussianBlur(int(S * 0.018)))); d = ImageDraw.Draw(im)
    d.ellipse([cx - R, cy - R, cx + R, cy + R], fill="#ff5252")
    d.polygon([(cx - R * 0.72, cy + R * 0.69), (cx + R * 0.72, cy + R * 0.69), (cx, tip)], fill="#ff5252")
    pr, py = R * 0.52, cy + R * 0.02
    d.ellipse([cx - pr * 0.62, py - pr * 0.08, cx + pr * 0.62, py + pr * 0.78], fill="white")
    for dx, dy, rr in [(-0.80, -0.42, 0.26), (-0.32, -0.78, 0.28), (0.32, -0.78, 0.28), (0.80, -0.42, 0.26)]:
        d.ellipse([cx + dx * pr - rr * pr, py + dy * pr - rr * pr, cx + dx * pr + rr * pr, py + dy * pr + rr * pr], fill="white")
    if not maskable:
        mask = Image.new("L", (S, S), 0); ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
        im.putalpha(mask)
    im = im.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()


def _leaflet_files():
    """Leaflet as three flat files: CSS with its images folded in as data URIs, the JS, and a marker-icon shim."""
    import base64
    du = lambda name: "data:image/png;base64," + base64.b64encode(_fetch_bytes(LEAFLET_CDN + "images/" + name)).decode()
    css = _fetch_bytes(LEAFLET_CDN + "leaflet.min.css").decode("utf-8")
    for name in ("layers.png", "layers-2x.png", "marker-icon.png"):
        css = css.replace("images/" + name, du(name))
    js = _fetch_bytes(LEAFLET_CDN + "leaflet.min.js").decode("utf-8")
    icons = json.dumps({"iconUrl": du("marker-icon.png"), "iconRetinaUrl": du("marker-icon-2x.png"), "shadowUrl": du("marker-shadow.png")})
    return css, js, f'L.Icon.Default.imagePath="";L.Icon.Default.mergeOptions({icons});'


# A map page built by the notebook or the Streamlit builder loads Leaflet from a CDN; inside the phone app the same
# page is pointed at the app's own copy so it works with no signal.  The same rewrite runs in the browser on import.
_PWA_MYMAPS_JS = r"""// Shared by index.html and live.html: maps a person saves live in this app's own storage, on this phone only.
function localizeMap(t){return t.replace(/<link rel="stylesheet" href="[^"]*leaflet[^"]*\.css"\/?>/,'<link rel="stylesheet" href="./leaflet.css">').replace(/<script src="[^"]*leaflet[^"]*\.js"><\/script>/,'<script src="./leaflet.js"><\/script><script src="./leaflet-icons.js"><\/script>')}
const mmIdx=()=>{try{return JSON.parse(localStorage.getItem('mymaps')||'[]')}catch(e){return []}};
const mmSave=l=>localStorage.setItem('mymaps',JSON.stringify(l));
async function saveMapHtml(t,fallbackName){
 if(t.indexOf('const DATA=')<0)throw new Error('not a map from the builder');
 t=localizeMap(t);const id=(Date.now().toString(36)+Math.random().toString(36).slice(2,7));
 const c=await caches.open('mymaps');await c.put(new Request('./map-'+id+'.html'),new Response(t,{headers:{'Content-Type':'text/html; charset=utf-8'}}));
 const ti=(t.match(/<title>(.*?)<\/title>/)||[])[1]||fallbackName||'Map',su=(t.match(/<div class="sub">(.*?)<\/div>/)||[])[1]||'';
 const l=mmIdx();l.push({id,title:ti,sub:su.replace(/<[^>]+>/g,'').slice(0,120)});mmSave(l);return id;}
async function removeMap(id){const c=await caches.open('mymaps');await c.delete('./map-'+id+'.html');mmSave(mmIdx().filter(m=>m.id!==id));}
function listenForMaps(onSaved){window.addEventListener('message',async e=>{const d=e.data;if(!d||d.type!=='vetmap'||typeof d.html!=='string')return;
 if(APP_ORIGIN&&e.origin!==APP_ORIGIN)return;try{const id=await saveMapHtml(d.html,d.name);onSaved&&onSaved(id,mmIdx().slice(-1)[0]);}catch(err){alert('Could not save that map: '+err.message);}});}"""


def _localize_map(page):
    page = re.sub(r'<link rel="stylesheet" href="[^"]*leaflet[^"]*\.css"/?>', '<link rel="stylesheet" href="./leaflet.css">', page, count=1)
    page = re.sub(r'<script src="[^"]*leaflet[^"]*\.js"></script>', '<script src="./leaflet.js"></script><script src="./leaflet-icons.js"></script>', page, count=1)
    return page.replace("<header>", '<header><a href="./index.html" style="color:#8fc1ff;font-size:13px;text-decoration:none">&larr; back</a>', 1)


_PWA_HEAD = r"""<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="manifest" href="./manifest.webmanifest"><meta name="theme-color" content="#151a22">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="apple-mobile-web-app-title" content="__SHORT__">
<link rel="apple-touch-icon" href="./icon-192.png"><link rel="icon" href="./icon-192.png">
<style>
body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Arial,sans-serif;background:#0e1116;color:#e8eaee;padding-top:env(safe-area-inset-top)}
header{padding:14px 16px 10px;background:#151a22;border-bottom:2px solid #2a3140;display:flex;align-items:center;gap:10px}
header img{width:34px;height:34px;border-radius:8px}
h1{margin:0;font-size:20px;color:#ffd94d}.sub{color:#9aa3b2;font-size:12.5px;margin-top:2px}
main{padding:12px 16px 24px;max-width:720px}
.btn{display:block;text-align:center;background:#ff5252;color:#fff;font-weight:700;font-size:17px;border-radius:14px;padding:16px;margin:10px 0;text-decoration:none;border:0;width:100%;box-sizing:border-box;cursor:pointer;font-family:inherit}
.btn.blue{background:#2f6fd6}.btn.grey{background:#1d2430;border:1px solid #3a4356;color:#e8eaee;font-weight:600;font-size:15px;padding:12px}
h2{font-size:14px;color:#9aa3b2;font-weight:600;margin:18px 0 6px}
.card{display:block;background:#1a212c;border:1px solid #2a3140;border-radius:12px;padding:12px 14px;margin:8px 0;color:#e8eaee;text-decoration:none;position:relative}
.card b{display:block;font-size:15px;color:#fff;padding-right:60px}.card span{color:#9aa3b2;font-size:12.5px}
.card .rm{position:absolute;right:12px;top:12px;color:#9aa3b2;font-size:12px;background:none;border:1px solid #3a4356;border-radius:8px;padding:4px 8px;cursor:pointer}
#near{margin:4px 0}
.t-ER{color:#ff6b6b;font-weight:700}.t-NIGHT{color:#ffb84d;font-weight:700}
.status{font-size:12.5px;color:#9aa3b2;padding:10px 0 0;line-height:1.5}.status b{color:#7fe0a0}.status i{color:#ffb84d;font-style:normal}
.status a,.sub a{color:#8fc1ff;text-decoration:none}
.off{display:none;background:#3a2a1a;border:1px solid #ffab2e;color:#ffd9a0;border-radius:10px;padding:10px 12px;font-size:13px;margin:8px 0}
body.offline .off{display:block}body.offline .btn.blue{opacity:.45;pointer-events:none}
.empty{color:#9aa3b2;font-size:13px;line-height:1.5;padding:4px 0}
table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;color:#ffd94d;padding:8px 6px;border-bottom:2px solid #2a3140;position:sticky;top:0;background:#1a212c}
td{padding:7px 6px;border-bottom:1px solid #232a36;vertical-align:top}td a{color:#8fc1ff;text-decoration:none}
.seg{display:flex;gap:6px;margin:6px 0 8px}.seg button{flex:1;background:#1a212c;border:1px solid #3a4356;color:#c9ceda;border-radius:10px;padding:9px 4px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit}
.seg button.on{background:#2f6fd6;border-color:#2f6fd6;color:#fff}
.res{background:#1a212c;border:1px solid #2a3140;border-radius:12px;padding:10px 12px;margin:8px 0;font-size:13.5px;line-height:1.45}
.res b{color:#fff;font-size:15px}.res .d{color:#ffd94d;font-weight:700;white-space:nowrap}.res .addr{color:#c9ceda}.res .hrs{color:#9aa3b2;font-size:12.5px}
.acts{display:flex;gap:8px;margin-top:8px}.act{flex:1;display:flex;align-items:center;justify-content:center;gap:6px;background:#242c3a;border:1px solid #3a4356;color:#fff;border-radius:10px;padding:9px 6px;text-decoration:none;font-weight:700;font-size:13.5px}
.act.go{background:#2f6fd6;border-color:#2f6fd6}.act.call{background:#1f7a4d;border-color:#1f7a4d}.act svg{width:16px;height:16px;flex:0 0 auto}
.badge{display:inline-block;font-size:11px;font-weight:700;border-radius:6px;padding:1px 6px;margin-left:6px;vertical-align:middle;background:#2a3140;color:#c9ceda}.badge.ER{background:#5a2222;color:#ff8a8a}.badge.NIGHT{background:#5a3f14;color:#ffc46b}.badge.EXT{background:#5a5214;color:#ffe97a}.badge.DAY{background:#1b3a5a;color:#8fc1ff}
#nearmap{display:none;height:240px;border-radius:12px;margin:8px 0;border:1px solid #2a3140;background:#1a212c}
.small{font-size:13px;padding:10px}.note{color:#9aa3b2;font-size:12.5px;line-height:1.5}
.toast{position:fixed;left:16px;right:16px;bottom:20px;background:#1b3324;border:1px solid #3fbf74;color:#b9f0cf;border-radius:12px;padding:12px 14px;font-size:14px;z-index:9999}.toast a{color:#fff;font-weight:700}
td .go{display:inline-block;background:#2f6fd6;color:#fff;border-radius:7px;padding:3px 7px;font-size:12px;text-decoration:none}
</style>"""

_PWA_INDEX = r"""<!DOCTYPE html><html lang="en"><head><title>__TITLE__</title>__HEAD__<link rel="stylesheet" href="./leaflet.css"></head><body>
<header><img src="./icon-192.png" alt=""><div><h1>__TITLE__</h1><div class="sub">__SUB__</div></div></header>
<main>
<div class="off">No signal - building a new map needs internet. Clinics near me, the built-in list and your saved maps still work.</div>
<h2>Veterinary clinics near me</h2>
<div class="seg" id="mode"><button data-m="er" class="on">24/7 ER</button><button data-m="late">Late night</button><button data-m="open">Open now</button></div>
<button class="btn" id="nearbtn">&#9673; Find clinics near me</button>
<div id="near"></div>
<div id="nearmap"></div>
__LIVE__
__BUILTIN__
<div id="sponsor"></div>
<div id="mmsec" style="display:none"><h2>My maps</h2><div id="mymaps"></div></div>
<div class="status" id="st">Works without signal: clinics near me, the built-in list and your saved maps. Needs signal: building a new map, directions, and map pictures the first time you look at an area. <a href="./install.html" id="howto">Put it on your home screen</a> &middot; <a href="./privacy.html">Privacy</a></div>
<button class="btn grey" id="installbtn" style="display:none">&#11015; Install this app on your phone</button>
<h2>Add a map file</h2>
<label class="btn grey small" for="addmap">+ Add a map file</label><input id="addmap" type="file" accept=".html,text/html" style="display:none">
<div class="note">Maps you build inside this app save to it with one tap. This is for a map file someone sent you or one you built on a computer. Maps stay on this phone only; nobody else can see them.</div>
</main>
<script src="./leaflet.js"></script><script src="./leaflet-icons.js"></script><script>const APP_ORIGIN=__APP_ORIGIN__;</script><script src="./mymaps.js"></script>
<script>
function setNet(){document.body.classList.toggle('offline',!navigator.onLine)}
window.addEventListener('online',setNet);window.addEventListener('offline',setNet);setNet();
if('serviceWorker' in navigator){navigator.serviceWorker.register('./sw.js').then(()=>{document.getElementById('st').insertAdjacentHTML('afterbegin','<b>Saved on this phone.</b> ')}).catch(()=>{});}
let deferredInstall=null;const ib=document.getElementById('installbtn');
window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();deferredInstall=e;ib.style.display='';});
ib.onclick=async()=>{if(!deferredInstall)return;ib.disabled=true;deferredInstall.prompt();await deferredInstall.userChoice;deferredInstall=null;ib.style.display='none';};
window.addEventListener('appinstalled',()=>{ib.style.display='none';});
if(window.matchMedia('(display-mode: standalone)').matches||navigator.standalone){document.getElementById('howto').style.display='none';}
fetch('./sponsor.json',{cache:'no-store'}).then(r=>r.json()).then(sp=>{if(sp&&sp.enabled&&sp.url){document.getElementById('sponsor').innerHTML='<a class="card" href="'+sp.url+'" target="_blank" rel="noopener sponsored" style="border-color:#3a4356"><b>'+sp.title+'</b><span>'+(sp.text||'')+'</span><span style="display:block;font-size:11px;margin-top:4px;color:#6f7886">Sponsored</span></a>';}}).catch(()=>{});
function hav(a,b,c,d){const r=Math.PI/180,R=3958.76;const x=Math.sin((c-a)*r/2)**2+Math.cos(a*r)*Math.cos(c*r)*Math.sin((d-b)*r/2)**2;return 2*R*Math.asin(Math.sqrt(x));}
function renderMine(){const l=mmIdx(),box=document.getElementById('mymaps');box.innerHTML=l.map(m=>'<a class="card" href="./map-'+m.id+'.html"><b>'+m.title+'</b><span>'+m.sub+'</span><button class="rm" data-id="'+m.id+'">remove</button></a>').join('');
 document.getElementById('mmsec').style.display=l.length?'':'none';
 box.querySelectorAll('.rm').forEach(b=>b.onclick=async e=>{e.preventDefault();await removeMap(b.dataset.id);renderMine();});}
document.getElementById('addmap').onchange=async e=>{const f=e.target.files[0];if(!f)return;try{await saveMapHtml(await f.text(),f.name);renderMine();window.scrollTo({top:document.getElementById('mmsec').offsetTop-10,behavior:'smooth'});}catch(err){alert('That is not a map saved from the map builder.');}e.target.value='';};
renderMine();listenForMaps(()=>renderMine());
async function allClinics(){const seen={},out=[];const add=c=>{const k=c.name+'|'+(+c.lat).toFixed(3);if(!seen[k]){seen[k]=1;out.push(c);}};
 try{const b=await (await fetch('./builtin.json')).json();b.forEach(c=>{c.source='verified';add(c)});}catch(e){}
 try{const c=await caches.open('mymaps');for(const m of mmIdx()){const r=await c.match('./map-'+m.id+'.html');if(!r)continue;const t=await r.text();const mm=t.match(/const DATA=(\[[\s\S]*?\]); const MAIN=/);if(mm)JSON.parse(mm[1]).forEach(add);}}catch(e){}
 return out;}
function openNow(c,now){if(c.typ==='ER')return true;const s=c.sched;if(!s)return null;const d=(now.getDay()+6)%7,m=now.getHours()*60+now.getMinutes();
 for(const iv of (s[d]||[]))if(iv[0]<=m&&m<iv[1])return true;for(const iv of (s[(d+6)%7]||[]))if(iv[1]>1440&&m<iv[1]-1440)return true;return false;}
const IOS=/iPhone|iPad|iPod/.test(navigator.userAgent)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
function dirUrl(c){return IOS?'https://maps.apple.com/?daddr='+c.lat+','+c.lng+'&q='+encodeURIComponent(c.name):'https://www.google.com/maps/dir/?api=1&destination='+c.lat+','+c.lng;}
const ICO_GO='<svg viewBox="0 0 24 24" fill="currentColor"><path d="M21 3 3 10.5l8 2.5 2.5 8L21 3z"/></svg>',ICO_CALL='<svg viewBox="0 0 24 24" fill="currentColor"><path d="M6.6 10.8a15 15 0 0 0 6.6 6.6l2.2-2.2a1 1 0 0 1 1-.25c1.1.37 2.3.57 3.6.57a1 1 0 0 1 1 1V20a1 1 0 0 1-1 1A17 17 0 0 1 3 4a1 1 0 0 1 1-1h3.5a1 1 0 0 1 1 1c0 1.25.2 2.45.57 3.6a1 1 0 0 1-.25 1L6.6 10.8z"/></svg>';
let mode='er',nearMap=null,nearLayer=null,lastPos=null;
document.querySelectorAll('#mode button').forEach(b=>b.onclick=()=>{mode=b.dataset.m;document.querySelectorAll('#mode button').forEach(x=>x.classList.toggle('on',x===b));if(lastPos)showNear(lastPos);});
function row(c){const typ=c.typ||'UNK';const badge={ER:'24/7 ER',NIGHT:'late night',EXT:'extended hrs',DAY:'daytime',UNK:'hours unknown'}[typ]||typ;
 return '<div class="res"><div style="display:flex;justify-content:space-between;gap:10px"><div><b>'+c.name+'</b><span class="badge '+typ+'">'+badge+'</span></div><span class="d">'+c.d.toFixed(1)+' mi</span></div>'
 +'<div class="addr">'+(c.address||c.town||'')+'</div><div class="hrs">'+(c.hours||'')+'</div>'
 +'<div class="acts">'+(c.phone?'<a class="act call" href="tel:'+c.phone+'">'+ICO_CALL+' Call</a>':'<span class="act" style="opacity:.5">no phone listed</span>')+'<a class="act go" target="_blank" rel="noopener" href="'+dirUrl(c)+'">'+ICO_GO+' Directions</a></div></div>';}
async function showNear(p){lastPos=p;const la=p.coords.latitude,lo=p.coords.longitude;const all=await allClinics();const now=new Date();
 all.forEach(c=>{c.d=hav(la,lo,c.lat,c.lng)});
 let pick=mode==='er'?all.filter(c=>c.typ==='ER'):mode==='late'?all.filter(c=>c.typ==='ER'||c.typ==='NIGHT'):all.filter(c=>openNow(c,now)===true);
 pick.sort((a,b)=>a.d-b.d);const top=pick.slice(0,6);const box=document.getElementById('near');
 const label={er:'24/7 emergency hospitals',late:'24/7 and late-night clinics',open:'clinics open right now'}[mode];
 box.innerHTML=(top.length?'<div style="color:#ffd94d;font-weight:700;margin:6px 0 2px">Nearest '+label+'</div>'+top.map(row).join(''):'<div class="res">No '+label+' in the built-in list or your saved maps near here. Try another option above, or build a map of your area.</div>')
  +'<div class="note" style="margin-top:6px">Straight-line miles. '+(mode==='open'?'Clinics whose hours are not listed are left out of Open now. ':'')+'Always call before driving - hours change.</div>';
 try{const mapEl=document.getElementById('nearmap');mapEl.style.display='block';
  if(!nearMap){nearMap=L.map('nearmap',{zoomControl:false,attributionControl:false});L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',{maxZoom:19}).addTo(nearMap);}
  if(nearLayer)nearLayer.remove();nearLayer=L.layerGroup().addTo(nearMap);const col={ER:'#ff5252',NIGHT:'#ffab2e',EXT:'#ffe14d',DAY:'#4d9bff',UNK:'#9aa3b2'};
  L.circleMarker([la,lo],{radius:7,color:'#fff',fillColor:'#2f6fd6',fillOpacity:1,weight:2}).addTo(nearLayer).bindPopup('You are here');
  top.forEach(c=>L.circleMarker([c.lat,c.lng],{radius:8,color:'#fff',weight:1.5,fillColor:col[c.typ]||col.UNK,fillOpacity:1}).addTo(nearLayer).bindPopup('<b>'+c.name+'</b><br>'+(c.address||c.town||'')+'<br>'+c.d.toFixed(1)+' mi &middot; <a href="'+dirUrl(c)+'" target="_blank">Directions</a>'));
  const pts=[[la,lo]].concat(top.map(c=>[c.lat,c.lng]));nearMap.fitBounds(L.latLngBounds(pts).pad(0.2));setTimeout(()=>nearMap.invalidateSize(),50);
 }catch(e){}
}
document.getElementById('nearbtn').onclick=()=>{
 const box=document.getElementById('near');box.innerHTML='<div class="res">Getting your location...</div>';
 if(!navigator.geolocation){box.innerHTML='<div class="res">This browser cannot give a location.</div>';return;}
 navigator.geolocation.getCurrentPosition(showNear,()=>{box.innerHTML='<div class="res">Location unavailable - allow location access for this app and try again.</div>'},{enableHighAccuracy:true,timeout:15000,maximumAge:60000});
};
</script></body></html>"""

_PWA_LIVE = r"""<!DOCTYPE html><html lang="en"><head><title>__TITLE__ - map builder</title>__HEAD__
<style>html,body{height:100%}body{display:flex;flex-direction:column}iframe{flex:1 1 auto;border:0;width:100%}
.bar{padding:8px 16px;background:#151a22;border-bottom:1px solid #2a3140;font-size:13px;line-height:1.5}.bar a{color:#8fc1ff;text-decoration:none}.bar .n{color:#9aa3b2}</style></head>
<body><div class="bar"><a href="./index.html">&larr; Home</a> &nbsp;&middot;&nbsp; <a href="__APP__" target="_blank">open in browser</a> <span class="n">&nbsp;&middot;&nbsp; If it says the app is asleep, tap <b>Yes, get this app back up</b> and give it 30 seconds.</span></div>
<div class="off" style="margin:8px 16px">No signal - the builder needs internet. <a href="./index.html" style="color:#ffd94d">Use clinics near me or your saved maps</a>.</div>
<iframe src="__APP__?embed=true" allow="geolocation; clipboard-write" title="Map builder"></iframe>
<script>const APP_ORIGIN=__APP_ORIGIN__;</script><script src="./mymaps.js"></script>
<script>function setNet(){document.body.classList.toggle('offline',!navigator.onLine)}window.addEventListener('online',setNet);window.addEventListener('offline',setNet);setNet();
listenForMaps((id,m)=>{const t=document.createElement('div');t.className='toast';t.innerHTML='Saved: <b>'+(m?m.title:'map')+'</b> is under My maps on the <a href="./index.html">home page</a>.';document.body.appendChild(t);setTimeout(()=>t.remove(),8000);});</script>
</body></html>"""

_PWA_VERIFIED = r"""<!DOCTYPE html><html lang="en"><head><title>__TITLE__ - hand-verified clinics</title>__HEAD__</head>
<body><header><div><h1>Hand-verified clinics</h1><div class="sub"><a href="./index.html">&larr; back</a> &middot; __N__ clinics checked by hand against their listings (__STAMP__); __NER__ open 24/7 or late. Call before driving - hours change.</div></div></header>
<main><table><thead><tr><th>Town</th><th>Clinic</th><th>Hours</th><th></th></tr></thead><tbody>__ROWS__</tbody></table></main>
<script>const IOS=/iPhone|iPad|iPod/.test(navigator.userAgent)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);if(IOS)document.querySelectorAll('a[data-ll]').forEach(a=>{a.href='https://maps.apple.com/?daddr='+a.dataset.ll;});</script></body></html>"""

_PWA_PRIVACY = r"""<!DOCTYPE html><html lang="en"><head><title>__TITLE__ - privacy</title>__HEAD__</head>
<body><header><div><h1>Privacy</h1><div class="sub"><a href="./index.html">&larr; back</a></div></div></header>
<main style="font-size:14px;line-height:1.6">
<p><b>__TITLE__</b> does not collect, store or transmit personal information, and has no accounts.</p>
<p><b>Location.</b> When you tap "Find clinics near me" or "Near me", the app asks the phone for its position and uses it on the phone to sort clinics by distance. The position is not sent anywhere and is not kept. Tapping "Directions" opens your phone's own maps app with the clinic's location.</p>
<p><b>Saved maps.</b> Maps you add are stored in this app's own storage on your phone. They are not uploaded. Removing the app removes them.</p>
<p><b>Third-party services.</b> Map background tiles are requested from Esri and the U.S. Geological Survey when you look at a map with internet access; those requests carry the map area being viewed. The live builder (__APPHOST__) is a separate web page: addresses you type there are sent to routing and geocoding services (OSRM, U.S. Census Bureau, ArcGIS, Photon, OpenStreetMap) and, when configured, Google Places, to produce the map. It stores nothing about you beyond temporary caching needed to build the map.</p>
<p><b>Clinic information</b> comes from public listings and is provided as a convenience; always call ahead. Nothing here is veterinary advice.</p>
<p>Contact: __CONTACT__</p>
</main></body></html>"""

_PWA_INSTALL = r"""<!DOCTYPE html><html lang="en"><head><title>Get __TITLE__ on your phone</title>__HEAD__
<meta property="og:type" content="website"><meta property="og:title" content="__TITLE__ - get it on your phone"><meta property="og:description" content="__DESC__"><meta property="og:image" content="__SITE__icon-512.png">
<style>
main{max-width:560px;margin:0 auto}
.hero{background:#1a212c;border:1px solid #2a3140;border-radius:16px;padding:18px 16px 14px;margin:4px 0 14px}
.hero h2{font-size:25px;line-height:1.15;color:#fff;font-weight:700;margin:0 0 12px}.hero h2 em{font-style:normal;color:#8fc1ff;display:block;font-size:19px;margin-top:2px}
.hero p{font-size:15px;line-height:1.55;color:#c9ceda;margin:0 0 10px}.hero p u{text-decoration-thickness:2px;text-underline-offset:3px;color:#fff}
.warn{background:#3a2a1a;border:1px solid #ffab2e;color:#ffd9a0;border-radius:12px;padding:12px;font-size:14px;line-height:1.5;margin:12px 0 4px}.warn .btn{margin:10px 0 8px}.warn b{color:#fff}
.ok{background:#1b3324;border:1px solid #3fbf74;color:#b9f0cf;border-radius:12px;padding:12px;font-size:14px;line-height:1.5;margin:12px 0 4px}
.desk{background:#1a212c;border:1px solid #2a3140;border-radius:16px;padding:16px;margin:0 0 14px;text-align:center;font-size:15px;line-height:1.5}
.desk .qr{display:block;width:fit-content;background:#fff;padding:8px;border-radius:12px;margin:10px auto}.desk .qr svg{display:block;width:180px;height:180px}.desk code{display:block;color:#ffd94d;font-size:14px;word-break:break-all;margin-top:4px}
.tabs{display:flex;gap:6px;margin:14px 0 6px}.tabs button{flex:1;background:#1a212c;border:1px solid #3a4356;color:#c9ceda;border-radius:10px;padding:10px 4px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit}
.tabs button.on{background:#2f6fd6;border-color:#2f6fd6;color:#fff}
section{display:none}section.on{display:block}
.step{display:flex;gap:12px;margin:16px 0 4px;align-items:flex-start}.step .n{flex:0 0 30px;height:30px;border-radius:50%;background:#ffd94d;color:#151a22;font-weight:800;font-size:15px;display:flex;align-items:center;justify-content:center;margin-top:1px}
.step div{flex:1;min-width:0}.step p{margin:4px 0 6px;font-size:15px;line-height:1.5}.step p b{color:#fff}.step small{display:block;color:#9aa3b2;font-size:12.5px;line-height:1.45;margin-top:-2px}
.phone{background:#0b0e13;border:2px solid #3a4356;border-radius:22px;padding:10px 8px 8px;width:min(100%,290px);margin:8px 0 2px;font-size:12px;color:#c9ceda;box-sizing:border-box}
.phone .bar{display:flex;align-items:center;gap:8px;padding:4px 6px;color:#9aa3b2}.phone .bar.bottom{justify-content:space-around;font-size:19px;padding-top:8px}
.phone .url{flex:1;background:#1d2430;border-radius:9px;padding:6px 9px;color:#c9ceda;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:11.5px}
.phone .ic{width:24px;height:24px;display:inline-flex;align-items:center;justify-content:center;font-size:17px;color:#c9ceda;border-radius:6px;flex:0 0 auto;font-weight:700}
.phone .ic.hl{outline:2px solid #ff5252;outline-offset:3px;color:#ff5252}
.phone .app{background:#0e1116;border-radius:10px;margin:6px 0;overflow:hidden}
.phone .ah{display:flex;align-items:center;gap:8px;padding:9px 10px;background:#151a22;border-bottom:2px solid #2a3140}.phone .ah img{width:22px;height:22px;border-radius:6px}.phone .ah b{color:#ffd94d;font-size:13px}
.phone .ab{margin:8px 10px;background:#ff5252;color:#fff;border-radius:9px;padding:9px;text-align:center;font-weight:700;font-size:11px}
.phone .al{height:9px;background:#1d2430;border-radius:5px;margin:7px 10px}
.phone .menu{background:#1a212c;border-radius:12px;overflow:hidden;margin:4px 0}
.phone .menu div{display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid #232a36;font-size:12.5px;color:#dfe3ea}.phone .menu div:last-child{border-bottom:0}
.phone .menu div span{width:18px;text-align:center;color:#9aa3b2;flex:0 0 auto}
.phone .menu div.hl{outline:2px solid #ff5252;outline-offset:-2px;border-radius:8px;background:#2b1e1e;color:#fff;font-weight:700}.phone .menu div.hl span{color:#ff5252}
.phone .dlg{background:#1a212c;border-radius:14px;padding:12px;margin:8px 0 4px}.phone .dlg .row{display:flex;align-items:center;gap:10px}.phone .dlg img{width:40px;height:40px;border-radius:10px}
.phone .dlg b{display:block;color:#fff;font-size:14px}.phone .dlg small{color:#9aa3b2;font-size:11.5px}
.phone .dlg .acts{display:flex;justify-content:flex-end;gap:14px;margin-top:12px;font-weight:700;color:#8fc1ff}.phone .dlg .go{background:#2f6fd6;color:#fff;border-radius:9px;padding:7px 16px;outline:2px solid #ff5252;outline-offset:3px}
.tip{color:#ff5252;font-weight:700;font-size:12px;text-align:center;margin-top:8px}
.after{background:#1a212c;border:1px solid #2a3140;border-radius:16px;padding:6px 16px 10px;margin:18px 0 10px}
.help{color:#9aa3b2;font-size:13.5px;line-height:1.5;margin:14px 0}.help a{color:#8fc1ff;text-decoration:none}
</style></head><body>
<header><img src="./icon-192.png" alt=""><div><h1>Get __TITLE__ on your phone</h1><div class="sub">__SLOGAN__ &middot; free &middot; about 30 seconds</div></div></header>
<main>
<div class="hero">
<h2>Not your regular mobile app <em>(good to know)</em></h2>
<p><b>__TITLE__</b> is a web app. Instead of downloading it from a store, you <u>save it to your home screen from Safari or Chrome</u>.</p>
<p>It takes a fraction of the space of a normal app, updates itself, and once it is on your home screen, <b>clinics near me</b>, your saved maps and tap-to-call all work with no signal at all.</p>
<div class="warn" id="inapp" hidden></div>
<button class="btn" id="installbtn" hidden>&#11015; Install __TITLE__ now (one tap)</button>
<div class="ok" id="done" hidden><b>You're all set - __TITLE__ is installed.</b> Open it from your home screen. The first time, tap <b>Find clinics near me</b> and allow location.</div>
</div>
<div class="desk" id="desk" hidden>Open this page on your phone:<br><code id="here"></code>__QR__<br><span style="color:#9aa3b2;font-size:13px">Scan with the phone's camera, or text yourself the link.</span></div>
<div class="tabs"><button data-p="ios">iPhone / iPad</button><button data-p="android">Android (Chrome)</button><button data-p="samsung">Samsung Internet</button></div>

<section id="p-ios">
<div class="step"><div class="n">1</div><div><p>Open this page in <b>Safari</b> and tap the <b>share button</b> at the bottom of the screen.</p>
<small>From Instagram, Facebook or Messages, the page may open inside that app: tap &bull;&bull;&bull; in the corner and choose <b>Open in Safari</b> first.</small>
<div class="phone"><div class="app"><div class="ah"><img src="./icon-192.png" alt=""><b>__TITLE__</b></div><div class="ab">&#9673; Find clinics near me</div><div class="al"></div><div class="al" style="width:55%"></div></div>
<div class="bar"><span class="ic" style="font-size:12px">AA</span><div class="url">&#128274; __HOST__</div><span class="ic">&#8635;</span></div>
<div class="bar bottom"><span class="ic">&lsaquo;</span><span class="ic">&rsaquo;</span><span class="ic hl"><svg width="18" height="21" viewBox="0 0 18 21" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 13V1.5M5 5l4-4 4 4M5 8.5H3v11h12v-11h-2"/></svg></span><span class="ic">&#9783;</span><span class="ic">&#10697;</span></div>
<div class="tip">&uarr; the square with the arrow</div></div></div></div>
<div class="step"><div class="n">2</div><div><p>Scroll down the list and tap <b>Add to Home Screen</b>.</p>
<div class="phone"><div class="menu"><div><span>&#10697;</span>Copy</div><div><span>&#8734;</span>Add to Reading List</div><div><span>&#9783;</span>Add Bookmark</div><div><span>&#9734;</span>Add to Favorites</div><div><span>&#9906;</span>Find on Page</div><div class="hl"><span>&#8853;</span>Add to Home Screen</div><div><span>&#9998;</span>Markup</div><div><span>&#9113;</span>Print</div></div></div></div></div>
<div class="step"><div class="n">3</div><div><p>Tap <b>Add</b> in the top corner.</p>
<div class="phone"><div class="dlg"><div class="acts" style="justify-content:space-between;margin:0 0 10px"><span style="color:#8fc1ff;font-weight:400">Cancel</span><span style="color:#fff">Add to Home Screen</span><span class="go" style="padding:5px 12px">Add</span></div><div class="row"><img src="./icon-192.png" alt=""><div><b>__TITLE__</b><small>__HOST__</small></div></div></div></div></div></div>
</section>

<section id="p-android">
<div class="step"><div class="n">1</div><div><p>Open this page in <b>Chrome</b> and tap the <b>three dots</b> in the top corner.</p>
<small>From Instagram, Facebook or Messenger, the page opens inside that app and the option is missing: tap &#8942; and choose <b>Open in Chrome</b> (or Open in browser) first.</small>
<div class="phone"><div class="bar"><span class="ic">&#8962;</span><div class="url">&#128274; __HOST__</div><span class="ic">+</span><span class="ic" style="font-size:11px;border:1.5px solid #c9ceda;width:16px;height:16px;border-radius:4px">3</span><span class="ic hl">&#8942;</span></div>
<div class="app"><div class="ah"><img src="./icon-192.png" alt=""><b>__TITLE__</b></div><div class="ab">&#9673; Find clinics near me</div><div class="al"></div><div class="al" style="width:55%"></div></div>
<div class="tip">&uarr; the three dots, top right</div></div></div></div>
<div class="step"><div class="n">2</div><div><p>Tap <b>Add to Home screen</b> (on some phones it says <b>Install app</b>).</p>
<div class="phone"><div class="menu"><div><span>&#8862;</span>New tab</div><div><span>&#9673;</span>New Incognito tab</div><div><span>&#9719;</span>History</div><div><span>&#10697;</span>Recent tabs</div><div><span>&#8615;</span>Downloads</div><div><span>&#9734;</span>Bookmarks</div><div><span>&#8679;</span>Share...</div><div><span>&#9906;</span>Find in page</div><div class="hl"><span>&#8853;</span>Add to Home screen</div><div><span>&#9645;</span>Desktop site</div><div><span>&#9881;</span>Settings</div></div></div></div></div>
<div class="step"><div class="n">3</div><div><p>Tap <b>Install</b>.</p><small>If the box says <b>Add</b> instead, tap Add - that works too.</small>
<div class="phone"><div class="dlg"><div class="row"><img src="./icon-192.png" alt=""><div><b>__TITLE__</b><small>__HOST__</small></div></div><div class="acts"><span>Cancel</span><span class="go">Install</span></div></div></div></div></div>
</section>

<section id="p-samsung">
<div class="step"><div class="n">1</div><div><p>Open this page in <b>Samsung Internet</b> and tap the <b>menu</b> (three lines, bottom right).</p>
<small>If a small download-style icon appears at the right end of the address bar, that is an <b>Install</b> shortcut - tapping it does the whole thing.</small>
<div class="phone"><div class="app"><div class="ah"><img src="./icon-192.png" alt=""><b>__TITLE__</b></div><div class="ab">&#9673; Find clinics near me</div><div class="al"></div><div class="al" style="width:55%"></div></div>
<div class="bar"><div class="url">&#128274; __HOST__</div><span class="ic">&#8635;</span></div>
<div class="bar bottom"><span class="ic">&lsaquo;</span><span class="ic">&rsaquo;</span><span class="ic">&#8962;</span><span class="ic">&#9783;</span><span class="ic" style="font-size:11px;border:1.5px solid #c9ceda;width:16px;height:16px;border-radius:4px">3</span><span class="ic hl">&#8801;</span></div>
<div class="tip">&uarr; the three lines, bottom right</div></div></div></div>
<div class="step"><div class="n">2</div><div><p>Tap <b>Add page to</b>, then <b>Home screen</b>.</p>
<div class="phone"><div class="menu"><div><span>&#8615;</span>Downloads</div><div><span>&#9719;</span>History</div><div><span>&#9783;</span>Bookmarks</div><div><span>&#8679;</span>Share</div><div class="hl"><span>&#8853;</span>Add page to</div><div><span>&#9645;</span>Desktop site</div><div><span>&#9881;</span>Settings</div></div>
<div class="menu" style="margin-top:8px"><div style="color:#9aa3b2;font-size:11px">Add current webpage to</div><div><span>&#9783;</span>Bookmarks</div><div><span>&#9889;</span>Quick access</div><div class="hl"><span>&#8853;</span>Home screen</div><div><span>&#8615;</span>Saved pages</div></div></div></div></div>
<div class="step"><div class="n">3</div><div><p>Tap <b>Add</b> (or <b>Install</b>).</p>
<div class="phone"><div class="dlg"><div class="row"><img src="./icon-192.png" alt=""><div><b>__TITLE__</b><small>__HOST__</small></div></div><div class="acts"><span>Cancel</span><span class="go">Add</span></div></div></div></div></div>
</section>

<div class="after">
<div class="step"><div class="n">4</div><div><p>Close the browser and open <b>__TITLE__</b> from your home screen - it opens full screen like any other app.</p></div></div>
<div class="step"><div class="n">5</div><div><p>Tap <b>Find clinics near me</b> and <b>allow location</b> when the phone asks. That is it.</p>
<small>Your location is used on the phone only and never sent anywhere.</small></div></div>
</div>
<p class="help">Trouble getting it on your phone? __CONTACT_HTML__ &nbsp;<a href="./privacy.html">Privacy</a></p>
<a class="btn grey" href="./index.html">Skip for now - use it in the browser</a>
</main>
<script>
const ua=navigator.userAgent||'';
const isIOS=/iPhone|iPad|iPod/.test(ua)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
const isAndroid=/Android/i.test(ua);const isSamsung=/SamsungBrowser/i.test(ua);
const inAppName=(ua.match(/Instagram|FBAN|FBAV|FB_IAB|Messenger|TikTok|musical_ly|Snapchat|LinkedIn|Pinterest|Twitter/i)||[])[0];
const inApp=!!inAppName||(isAndroid&&/; wv\)/.test(ua));
const appLabel={FBAN:'Facebook',FBAV:'Facebook',FB_IAB:'Facebook',musical_ly:'TikTok'}[inAppName]||inAppName||'this app';
const standalone=window.matchMedia('(display-mode: standalone)').matches||navigator.standalone;
const here=location.href.split('#')[0];
function show(p){document.querySelectorAll('section').forEach(s=>s.classList.toggle('on',s.id==='p-'+p));document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.p===p));}
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>show(b.dataset.p));
show(isIOS?'ios':isSamsung?'samsung':'android');
if(!isIOS&&!isAndroid){document.getElementById('desk').hidden=false;document.getElementById('here').textContent=here;}
if(standalone){document.getElementById('done').hidden=false;document.querySelectorAll('.tabs,section,.after,.desk').forEach(x=>x.style.display='none');document.querySelector('.hero h2').style.display='none';document.querySelectorAll('.hero p').forEach(x=>x.style.display='none');}
else if(inApp){const w=document.getElementById('inapp');w.hidden=false;
 if(isAndroid){w.innerHTML='<b>You are inside '+appLabel+'\'s own browser</b>, which cannot save apps to the home screen.<a class="btn" href="intent://'+location.host+location.pathname+'#Intent;scheme=https;package=com.android.chrome;S.browser_fallback_url='+encodeURIComponent(here)+';end">Open this page in Chrome</a>If nothing happens: tap &#8942; in the top corner, then <b>Open in Chrome</b> (or Open in browser), and follow the steps below there.';}
 else if(isIOS){w.innerHTML='<b>You are inside '+appLabel+'\'s own browser</b>, which cannot save apps to the home screen.<a class="btn" href="x-safari-'+here+'">Open this page in Safari</a>If nothing happens: tap &bull;&bull;&bull; in the corner, then <b>Open in Safari</b> (or Open in external browser), and follow the steps below there.';}
 else{w.innerHTML='<b>You are inside '+appLabel+'\'s own browser.</b> Open this page in Safari or Chrome to save it: tap the menu in the corner and choose <b>Open in browser</b>.';}}
let deferredInstall=null;const ib=document.getElementById('installbtn');
window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();deferredInstall=e;if(!standalone&&!inApp)ib.hidden=false;});
ib.onclick=async()=>{if(!deferredInstall)return;ib.disabled=true;deferredInstall.prompt();const r=await deferredInstall.userChoice;deferredInstall=null;ib.hidden=true;ib.disabled=false;if(r&&r.outcome==='accepted'){document.getElementById('done').hidden=false;}};
window.addEventListener('appinstalled',()=>{ib.hidden=true;document.getElementById('done').hidden=false;});
if('serviceWorker' in navigator){navigator.serviceWorker.register('./sw.js').catch(()=>{});}
</script></body></html>"""

_PWA_SW = r"""const VERSION='__VERSION__';const PRECACHE=__PRECACHE__;
self.addEventListener('install',e=>{e.waitUntil(caches.open('vet-'+VERSION).then(c=>Promise.allSettled(PRECACHE.map(u=>c.add(u)))).then(()=>self.skipWaiting()))});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!=='vet-'+VERSION&&k!=='tiles'&&k!=='mymaps').map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});
async function trim(c){const keys=await c.keys();if(keys.length>2500){for(const k of keys.slice(0,300))await c.delete(k);}}
self.addEventListener('fetch',e=>{
 const u=new URL(e.request.url);
 if(/arcgisonline|nationalmap\.gov/.test(u.host)){
  e.respondWith(caches.open('tiles').then(async c=>{const hit=await c.match(e.request);if(hit)return hit;
   try{const r=await fetch(e.request);c.put(e.request,r.clone());trim(c);return r;}catch(err){return new Response('',{status:504});}}));
  return;}
 if(u.origin===self.location.origin){
  if(u.pathname.endsWith('/sponsor.json')){e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));return;}
  e.respondWith(caches.match(e.request,{ignoreSearch:true}).then(hit=>hit||fetch(e.request).then(r=>{if(r.ok&&u.pathname.indexOf('/map-')<0){const cp=r.clone();caches.open('vet-'+VERSION).then(c=>c.put(e.request,cp));}return r;})));
 }
});"""


def _qr_svg(text):
    """QR code as inline SVG for the install page's desktop view (needs the optional `qrcode` package; blank without it)."""
    try:
        import qrcode, qrcode.image.svg
    except ImportError:
        return ""
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=1)
    svg = img.to_string(encoding="unicode")
    svg = re.sub(r"<\?xml[^>]*\?>", "", svg).replace('width="', 'data-w="', 1).replace('height="', 'data-h="', 1)
    return '<div class="qr">' + svg + "</div>"


def build_pwa(map_files=(), out_dir="pwa", title="MapAVet", subtitle="", app_url="", short_name="MapAVet",
              builtin=None, description="", contact="", stamp="Sept 2026", site_url="", slogan="Never far from an open door."):
    """Packages the phone app as a flat folder (nothing to lose in an upload).  Each person's maps are added on their
    own phone and stay there; map_files are optional preloaded maps for a personal build.  builtin is a DataFrame of
    hand-verified clinics (name, town, address, lat, lng, phone, hours, typ, sched) that powers 'Find clinics near me'
    from the first open.  app_url is the live Streamlit builder (it can save maps straight into the app when opened
    inside it); site_url is where this folder will be hosted (link preview + QR code on install.html, the page to send
    people).  Host the folder on any static site."""
    out = pathlib.Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    site_url = (site_url.rstrip("/") + "/") if site_url else ""
    css, js, icon_js = _leaflet_files()
    (out / "leaflet.css").write_text(css, encoding="utf-8"); (out / "leaflet.js").write_text(js, encoding="utf-8")
    (out / "leaflet-icons.js").write_text(icon_js, encoding="utf-8"); (out / "mymaps.js").write_text(_PWA_MYMAPS_JS, encoding="utf-8")
    head = _PWA_HEAD.replace("__SHORT__", _esc(short_name))
    app_origin = json.dumps(re.sub(r"^(https?://[^/]+).*$", r"\1", app_url) if app_url else "")
    digest = hashlib.md5()
    pre = ["./", "./index.html", "./manifest.webmanifest", "./icon-192.png", "./icon-512.png", "./icon-512-maskable.png", "./leaflet.css", "./leaflet.js",
           "./leaflet-icons.js", "./mymaps.js", "./privacy.html"]
    cards = []
    for mf in map_files:
        src = pathlib.Path(mf); page = _localize_map(src.read_text(encoding="utf-8"))
        (out / src.name).write_text(page, encoding="utf-8"); digest.update(page.encode()); pre.append("./" + src.name)
        t = re.search(r"<title>(.*?)</title>", page); s = re.search(r'<div class="sub">(.*?)</div>', page)
        cards.append(f'<a class="card" href="./{src.name}"><b>{t.group(1) if t else src.name}</b><span>{s.group(1) if s else ""}</span></a>')
    builtin_html = ""
    if builtin is not None and len(builtin):
        b = builtin.copy()
        for c in ("address", "sched"):
            if c not in b: b[c] = "" if c == "address" else None
        rows = []
        for r in b[["name", "town", "address", "lat", "lng", "phone", "hours", "typ", "sched"]].to_dict("records"):
            rec = {k: ("" if (not isinstance(v, list) and pd.isna(v)) else v) for k, v in r.items()}
            rec["sched"] = rec["sched"] if isinstance(rec["sched"], list) else None
            rec["lat"] = round(float(rec["lat"]), 4); rec["lng"] = round(float(rec["lng"]), 4); rows.append(rec)
        (out / "builtin.json").write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8"); pre.append("./builtin.json")
        n_er = sum(1 for r in rows if r["typ"] in ("ER", "NIGHT"))
        trs = "".join(f'<tr><td>{_esc(r["town"])}</td><td><b>{_esc(r["name"])}</b>' + (f'<br><span class="src">{_esc(r["address"])}</span>' if r["address"] else "") + '</td>'
                      f'<td class="t-{r["typ"]}">{_esc(r["hours"])}</td>'
                      f'<td style="white-space:nowrap">{("<a href=tel:" + r["phone"] + ">" + r["phone"] + "</a><br>") if r["phone"] else ""}'
                      f'<a class="go" data-ll="{r["lat"]},{r["lng"]}" target="_blank" href="https://www.google.com/maps/dir/?api=1&destination={r["lat"]},{r["lng"]}">Go</a></td></tr>'
                      for r in sorted(rows, key=lambda r: (str(r["town"])[-2:], str(r["town"]), r["name"])))
        (out / "verified.html").write_text(_PWA_VERIFIED.replace("__TITLE__", _esc(title)).replace("__HEAD__", head).replace("__N__", str(len(rows)))
                                           .replace("__NER__", str(n_er)).replace("__STAMP__", _esc(stamp)).replace("__ROWS__", trs), encoding="utf-8"); pre.append("./verified.html")
        builtin_html = (f'<h2>Built in</h2><a class="card" href="./verified.html"><b>{len(rows)} hand-verified clinics</b>'
                        f'<span>{n_er} open 24/7 or late. Checked against their listings ({_esc(stamp)}): Denver metro, Denver to Michigan (I-80 / I-69) '
                        f'and Denver to Houston (I-25 / US-287 / I-45). Build a map to add any other area.</span></a>')
        digest.update(json.dumps(rows).encode())
    if cards:
        builtin_html = '<h2>Preloaded maps</h2>' + "\n".join(cards) + builtin_html
    live = ""
    if app_url:
        (out / "live.html").write_text(_PWA_LIVE.replace("__TITLE__", _esc(title)).replace("__HEAD__", head).replace("__APP__", _esc(app_url.rstrip("/")))
                                       .replace("__APP_ORIGIN__", app_origin), encoding="utf-8")
        pre.append("./live.html")
        live = '<h2>Any other area</h2><a class="btn blue" href="./live.html">Build a map of a route or an address</a><div class="note" style="margin-top:-4px">Every clinic along any US drive or around any address, with hours and phone. Needs signal to build; the finished map saves to this phone.</div>'
    (out / "index.html").write_text(_PWA_INDEX.replace("__TITLE__", _esc(title)).replace("__HEAD__", head).replace("__SUB__", subtitle or _esc(slogan))
                                    .replace("__LIVE__", live).replace("__BUILTIN__", builtin_html).replace("__APP_ORIGIN__", app_origin), encoding="utf-8")
    apphost = re.sub(r"^https?://", "", app_url).rstrip("/") if app_url else "the builder"
    (out / "privacy.html").write_text(_PWA_PRIVACY.replace("__TITLE__", _esc(title)).replace("__HEAD__", head).replace("__APPHOST__", _esc(apphost))
                                      .replace("__CONTACT__", _esc(contact or "the app publisher")), encoding="utf-8")
    sitehost = re.sub(r"^https?://", "", site_url).rstrip("/") if site_url else "your-site.github.io"
    (out / "install.html").write_text(_PWA_INSTALL.replace("__TITLE__", _esc(title)).replace("__HEAD__", head).replace("__SITE__", _esc(site_url or "./"))
                                      .replace("__HOST__", _esc(sitehost)).replace("__DESC__", _esc(description or subtitle or slogan)).replace("__SLOGAN__", _esc(slogan))
                                      .replace("__QR__", _qr_svg(site_url + "install.html") if site_url else "")
                                      .replace("__CONTACT_HTML__", (f'Email <a href="mailto:{_esc(contact)}">{_esc(contact)}</a>.' if contact else "Reply to whoever sent you this link.")), encoding="utf-8"); pre.append("./install.html")
    (out / "manifest.webmanifest").write_text(json.dumps({
        "id": "./", "name": title, "short_name": short_name, "description": description or subtitle or slogan,
        "start_url": "./index.html", "scope": "./", "display": "standalone", "orientation": "portrait", "lang": "en-US",
        "background_color": "#0e1116", "theme_color": "#151a22", "categories": ["medical", "navigation", "travel"],
        "icons": [{"src": "./icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                  {"src": "./icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                  {"src": "./icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}, indent=1), encoding="utf-8")
    (out / "icon-192.png").write_bytes(_icon_png(192)); (out / "icon-512.png").write_bytes(_icon_png(512))
    (out / "icon-512-maskable.png").write_bytes(_icon_png(512, maskable=True))
    (out / ".nojekyll").write_text("", encoding="utf-8")
    if not (out / "sponsor.json").exists():                 # one sponsored card on the home page; edit this file on the site to run it
        (out / "sponsor.json").write_text(json.dumps({"enabled": False, "title": "Sponsor title", "text": "One line of text.", "url": "https://example.com"}, indent=1), encoding="utf-8")
    digest.update(app_url.encode()); digest.update(_PWA_INDEX.encode()); digest.update(_PWA_MYMAPS_JS.encode()); digest.update(_PWA_LIVE.encode())
    (out / "sw.js").write_text(_PWA_SW.replace("__VERSION__", digest.hexdigest()[:10]).replace("__PRECACHE__", json.dumps(pre)), encoding="utf-8")
    (out / "README.md").write_text(
        f"# {title} - phone app\n\nA static web app: host this folder and open it on a phone. Everyone's maps are added on their own phone and stay there.\n\n"
        "## Put it online with GitHub Pages (free)\n\n"
        "1. Create a **public** repository on GitHub and upload every file in this folder (loose files, no sub-folders) with **Add file -> Upload files**. "
        "`.nojekyll` is a hidden file; if the upload box skips it, create it with Add file -> Create new file (empty).\n"
        "2. Repository **Settings -> Pages -> Build and deployment -> Source: Deploy from a branch**, branch `main`, folder `/ (root)`, **Save**.\n"
        "3. After a minute the same page shows the address, e.g. `https://<user>.github.io/<repo>/`.\n\n"
        "## The link to send people\n\n`.../install.html` - it explains that this is a web app, detects the phone (and the Instagram/Facebook in-app browser, which cannot install apps), "
        "offers a one-tap Install button where the browser supports it, and shows illustrated Add-to-Home-Screen steps for Safari, Chrome and Samsung Internet. "
        "On a computer it shows a QR code. Pass `site_url` to `build_pwa` so the QR code and link preview point at the right address.\n\n"
        "## Clinics near me\n\nOne button, three choices: 24/7 ER (default), late night, or any clinic open right now (from each clinic's hours and the phone's clock). "
        "Every result has the address, tap-to-call and a Directions button that opens the phone's own maps app. It uses the built-in hand-verified list plus every map saved on the phone, and works without signal.\n\n"
        "## Saving a map\n\nOpen the builder from inside the app, build the map and tap **Save to this phone** - it appears under My maps. "
        "**Add a map file** on the home page is for a map built on a computer or sent by someone else. Maps stay on that phone only.\n\n"
        "## Sponsored card\n\n`sponsor.json` controls one sponsored card on the home page: set `enabled` to true and fill in `title`, `text` and `url` "
        "(an affiliate or sponsor link). It is fetched fresh on every online open, so editing the file on the site changes it immediately. "
        "If you run it, declare 'Contains ads' in the Play listing.\n\n"
        "## Store listing\n\nhttps://www.pwabuilder.com packages this hosted app for Google Play and the App Store; `privacy.html` is the privacy policy page the stores ask for.\n",
        encoding="utf-8")
    return sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
