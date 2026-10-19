"""MapAVet - shareable map builder (phone-first layout).

Run locally:   pip install -r requirements.txt && streamlit run app.py
Share online:  push this folder to GitHub and deploy on https://share.streamlit.io (free) - see README.md
Inside the MapAVet phone app it is opened as ...?embed=true&app=1: then the finished map is handed to the app
(saved on the phone, opened full screen) instead of being shown inline.
"""
import os, re, hashlib, pathlib, tempfile, json
import streamlit as st
import streamlit.components.v1 as components


def _secret(name, default=""):
    """Streamlit secrets (Settings > Secrets on Streamlit Cloud); no secrets file = defaults."""
    try:
        return st.secrets.get(name, default) or default
    except Exception:
        return default


# A Google Places key (optional) gives ratings, phones and hours for every clinic; without it the app uses OpenStreetMap.
if _secret("GOOGLE_MAPS_API_KEY"):
    os.environ["GOOGLE_MAPS_API_KEY"] = _secret("GOOGLE_MAPS_API_KEY")
os.environ.setdefault("VET_LOCATOR_CONTACT", _secret("VET_LOCATOR_CONTACT", "MapAVet streamlit app"))
APP_ORIGIN = _secret("MAPAVET_ORIGIN", "https://hvarnell.github.io")   # the phone app that embeds this builder (one-tap save goes only there)

import vetlocator as vl   # noqa: E402  (reads the environment at import time)

APP_DIR = pathlib.Path(__file__).resolve().parent
STATIC_MAPS = APP_DIR / "static" / "maps"          # served at app/static/maps/... when enableStaticServing is on (.streamlit/config.toml)
IN_APP = st.query_params.get("app") == "1"         # opened inside the MapAVet phone app
LL = st.query_params.get("ll", "")                 # "lat,lng" handed over by the phone app after Use my location
LL_OK = bool(re.fullmatch(r"-?\d+(\.\d+)?,-?\d+(\.\d+)?", LL))
MY_LOC = "My location"
MY_LOC_RE = re.compile(r"^My location \((-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\)$")

st.set_page_config(page_title="MapAVet", page_icon=":dog:", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""<style>
.block-container{padding-top:1rem;padding-bottom:4rem}
h1{font-size:1.5rem !important;margin-bottom:0.2rem}
div[data-testid="InputInstructions"]{display:none !important}
</style>""", unsafe_allow_html=True)
(st.subheader if IN_APP else st.title)("Build a map" if IN_APP else "MapAVet")
st.caption("Every veterinary clinic along a drive or around an address: 24/7 ERs, night clinics, extended-hours and daytime "
           "practices with address, hours, phone and directions.")

mode = st.radio("What do you need?", ["Driving corridor", "Home base"], index=1 if LL_OK else 0, horizontal=True, label_visibility="collapsed")


def _html_button(label, color, onclick_js, height=64, key=""):
    """A real button in a small iframe: the only way a Streamlit page can run JavaScript on a tap (geolocation,
    handing the map to the phone app).  Text and data are JSON-escaped so the page HTML can never break out of
    the script."""
    snippet = ("<!DOCTYPE html><html><head><meta charset='utf-8'><style>body{margin:0;font-family:system-ui,sans-serif}"
               "button{width:100%;background:" + color + ";color:#fff;border:0;border-radius:12px;padding:14px;font-size:16px;font-weight:700;cursor:pointer;font-family:inherit}"
               ".ok{display:none;color:#7fe0a0;font-size:14px;padding:8px 0}</style></head><body>"
               "<button id='b'>" + label + "</button><div class='ok' id='ok'></div><script>" + onclick_js + "</script></body></html>")
    if hasattr(st, "iframe"):
        st.iframe(snippet, height=height)
    else:
        components.html(snippet, height=height)


def _js(v):
    """JSON for inline JavaScript: '</' and '<!--' inside the data must not end the script block."""
    return json.dumps(v).replace("</", "<\\/").replace("<!--", "<\\!--")


# ---------------------------------------------------------------- inputs
with st.container(border=True):
    if mode == "Driving corridor":
        origin = st.text_input("From", "", placeholder="Street address or town, e.g. Denver, CO")
        destination = st.text_input("To", "", placeholder="e.g. Flint, MI")
        custom = st.toggle("Customize route")
        st.caption("e.g., avoid highways, add planned stops along the route")
        via_txt = variant_txt = google_link = ""; track_file = None; corridor_mi = 12; er_reach_mi = 45
        if custom:
            via_txt = st.text_input("Planned stops along the route (comma-separated towns)", "")
            variant_txt = st.text_input("Alternative route via (drawn dashed)", "")
            google_link = st.text_input("Google Maps directions link", "",
                                        help="In Google Maps, set the drive up the way you want it (avoid highways, add stops), Share -> Copy link, paste here. Its stops replace From/To.")
            track_file = st.file_uploader("GPX or KML file (used exactly as drawn)", type=["gpx", "kml"])
            corridor_mi = st.slider("Keep every clinic within (miles of the road)", 3, 30, 12)
            er_reach_mi = st.slider("Keep 24/7 and night clinics out to (miles)", 15, 80, 45)
    else:
        home = st.text_input("Home address", f"{MY_LOC} ({LL.replace(',', ', ')})" if LL_OK else "", placeholder="Street address or town")
        # Use my location: inside the phone app the app itself asks the phone (its own permission pop-up) and reopens
        # this page with ?ll=; in a plain browser the button asks here and writes "My location (lat, lng)" into the
        # address box (the page and this button share an origin), or opens the page with ?ll= in a new tab as a fallback.
        _html_button("&#128205; Use my location", "#2f6fd6",
                     "const b=document.getElementById('b');"
                     "function fill(t){try{const d=window.parent.document,i=d.querySelector('input[aria-label=\"Home address\"]');if(!i)return false;"
                     "const W=window.parent;Object.getOwnPropertyDescriptor(W.HTMLInputElement.prototype,'value').set.call(i,t);i.dispatchEvent(new W.Event('input',{bubbles:true}));"
                     "i.dispatchEvent(new W.KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,bubbles:true}));i.blur();return true;}catch(e){return false;}}"
                     "b.onclick=()=>{b.disabled=true;b.textContent='Getting your location...';"
                     "if(" + _js(IN_APP) + "){window.top.postMessage({type:'vetloc'}," + _js(APP_ORIGIN) + ");return;}"
                     "if(!navigator.geolocation){b.textContent='This browser cannot give a location';return;}"
                     "navigator.geolocation.getCurrentPosition(p=>{const ll=p.coords.latitude.toFixed(5)+','+p.coords.longitude.toFixed(5);"
                     "if(fill('My location ('+ll.replace(',',', ')+')')){b.textContent='Location filled in - tap Build map';return;}"
                     "const u=new URL(window.parent.location.href);u.searchParams.set('ll',ll);window.open(u.toString(),'_blank');b.textContent='Opened with your location';},"
                     "e=>{b.disabled=false;b.textContent=(e&&e.code===1)?'Location is blocked for this site - allow it, or type the address':'Could not get your location - type the address instead';},"
                     "{enableHighAccuracy:false,timeout:12000,maximumAge:600000});};", height=56)
        radius_mi = st.slider("Radius (miles)", 5, 40, 15)
    go = st.button("Build map", type="primary", use_container_width=True)


@st.cache_data(show_spinner=False, ttl=6 * 3600)
def _build(kind, **kw):
    if kind == "corridor":
        r = vl.corridor_map(**kw)
        return {"html": r["html"], "df": r["df"], "anchors": r["anchors"], "gaps_24_7": r["gaps_24_7"],
                "gaps_overnight": r["gaps_overnight"], "warnings": r["warnings"], "provider": r["provider"],
                "total_mi": r["route"]["total_mi"], "drive_h": r["route"]["drive_h"]}
    r = vl.home_map(**kw)
    return {"html": r["html"], "df": r["df"], "warnings": r["warnings"], "provider": r["provider"]}


def _publish(page_html):
    """Write the finished page where the app can serve it full-screen (Near me needs a top-level page)."""
    name = f"vetmap_{hashlib.md5(page_html.encode()).hexdigest()[:10]}.html"
    try:
        STATIC_MAPS.mkdir(parents=True, exist_ok=True)
        (STATIC_MAPS / name).write_text(page_html, encoding="utf-8")
        return f"app/static/maps/{name}"
    except OSError:
        return None


def _goto_map_button(page_html, name, fresh):
    """Inside the phone app: hands the finished page to the app, which saves it under My maps and opens it full screen.
    Right after a build the page scrolls down to this button so it is the next thing the person sees."""
    _html_button("&#128506; Go to map", "#ff5252",
                 "const H=" + _js(page_html) + ",N=" + _js(name) + ",O=" + _js(APP_ORIGIN) + ";"
                 "const b=document.getElementById('b');b.onclick=()=>{window.top.postMessage({type:'vetmap',html:H,name:N,open:true},O);"
                 "b.textContent='Opening the map...';};"
                 + ("try{setTimeout(()=>window.frameElement.scrollIntoView({behavior:'smooth',block:'center'}),300);}catch(e){}" if fresh else ""), height=64)


def _show_page(page_html, height=900):
    """Embed the finished map page (plain-browser use).  st.iframe (newer Streamlit) needs a file; older versions take the HTML."""
    if hasattr(st, "iframe"):
        page_file = pathlib.Path(tempfile.gettempdir()) / f"vetmap_{hashlib.md5(page_html.encode()).hexdigest()[:10]}.html"
        page_file.write_text(page_html, encoding="utf-8")
        st.iframe(page_file, height=height)
    else:
        components.html(page_html, height=height, scrolling=True)


# ---------------------------------------------------------------- build
if go:
    m_loc = MY_LOC_RE.match(home.strip()) if mode == "Home base" else None
    if m_loc:
        home_q, home_label = f"{m_loc.group(1)},{m_loc.group(2)}", "my location"
    else:
        home_q = home_label = home.strip() if mode == "Home base" else ""
    needed = [origin, destination] if mode == "Driving corridor" else [home_q]
    if mode == "Driving corridor" and (google_link.strip() or track_file is not None):
        needed = []
    if any(not v.strip() for v in needed):
        st.warning("Enter the address or town first, or tap Use my location." if mode == "Home base"
                   else "Enter both From and To (or a Google Maps link / GPX file under Customize route).")
        st.stop()
    try:
        with st.spinner("Routing, pulling clinics and building the map (30 s to a few minutes for a long corridor)..."):
            if mode == "Driving corridor":
                via = [s.strip() for s in via_txt.split(",") if s.strip()] or None
                variant_via = [s.strip() for s in variant_txt.split(",") if s.strip()] or None
                track = track_file.getvalue().decode("utf-8", "ignore") if track_file is not None else None
                res = _build("corridor", origin=origin, destination=destination, via=via, variant_via=variant_via,
                             google_link=google_link.strip() or None, track=track, corridor_mi=corridor_mi, er_reach_mi=er_reach_mi)
                fname = "vet_corridor_" + (track_file.name.rsplit(".", 1)[0] if track_file is not None else f"{origin}_{destination}")
            else:
                res = _build("home", home=home_q, label=home_label, radius_mi=radius_mi)
                fname = f"vet_home_{home_label}"
            fname = re.sub(r"[^A-Za-z0-9]+", "_", fname).strip("_")[:80]
    except Exception as e:
        st.error(f"Could not build the map: {e}")
        st.stop()
    st.session_state["result"] = {"res": res, "fname": fname, "mode": mode}

if "result" in st.session_state:
    res, fname, built_mode = (st.session_state["result"][k] for k in ("res", "fname", "mode"))
    df = res["df"]
    for w in res["warnings"]:
        st.warning(w)
    if IN_APP:
        _goto_map_button(res["html"], fname, fresh=bool(go))
        st.caption("It is saved under My maps on the app's home page, so it opens again without signal.")
    else:
        st.caption(f"{len(df)} clinics - {int((df['typ'] == 'ER').sum())} open 24/7, {int((df['typ'] == 'NIGHT').sum())} late night"
                   + (f" - {res['total_mi']:.0f} mi, about {res['drive_h']:.1f} h driving" if built_mode == "Driving corridor" else "") + ".")
        url = _publish(res["html"])
        if url:
            st.link_button("Open the map full screen", url, use_container_width=True, type="primary")
        _show_page(res["html"])
        d1, d2 = st.columns(2)
        d1.download_button("Save the map file (HTML)", res["html"], file_name=fname + ".html", mime="text/html", use_container_width=True)
        d2.download_button("Save the table (CSV)", df.drop(columns=["er_hint"], errors="ignore").to_csv(index=False),
                           file_name=fname + ".csv", mime="text/csv", use_container_width=True)
        if built_mode == "Driving corridor":
            st.subheader("Anchor chain - the numbers to save before leaving")
            st.dataframe(res["anchors"], hide_index=True)
            if len(res["gaps_24_7"]):
                st.subheader("Stretches with no true 24/7 within reach")
                st.dataframe(res["gaps_24_7"], hide_index=True)
            if len(res["gaps_overnight"]):
                st.subheader("Stretches with no overnight cover at all")
                st.dataframe(res["gaps_overnight"], hide_index=True)
elif not go:
    st.info("Enter the trip or the home address and press **Build map**.")
