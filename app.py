"""VetAlong - shareable map builder (phone-first layout).

Run locally:   pip install -r requirements.txt && streamlit run app.py
Share online:  push this folder to GitHub and deploy on https://share.streamlit.io (free) - see README.md
"""
import os, re, hashlib, pathlib, tempfile
import streamlit as st


def _secret(name, default=""):
    """Streamlit secrets (Settings > Secrets on Streamlit Cloud); no secrets file = defaults."""
    try:
        return st.secrets.get(name, default) or default
    except Exception:
        return default


# A Google Places key (optional) gives ratings, phones and hours for every clinic; without it the app uses OpenStreetMap.
if _secret("GOOGLE_MAPS_API_KEY"):
    os.environ["GOOGLE_MAPS_API_KEY"] = _secret("GOOGLE_MAPS_API_KEY")
os.environ.setdefault("VET_LOCATOR_CONTACT", _secret("VET_LOCATOR_CONTACT", "VetAlong streamlit app"))

import vetlocator as vl   # noqa: E402  (reads the environment at import time)

APP_DIR = pathlib.Path(__file__).resolve().parent
STATIC_MAPS = APP_DIR / "static" / "maps"          # served at app/static/maps/... when enableStaticServing is on (.streamlit/config.toml)

st.set_page_config(page_title="VetAlong", page_icon=":dog:", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""<style>
.block-container{padding-top:1.2rem;padding-bottom:2rem}
h1{font-size:1.5rem !important;margin-bottom:0.2rem}
div[data-testid="stMetric"]{background:#1a212c;border-radius:10px;padding:8px 12px}
</style>""", unsafe_allow_html=True)
st.title("VetAlong")
st.caption("Every vet door along a drive or around an address: 24/7 ERs, night clinics, extended-hours and daytime GPs, "
           "with phone, hours and rating. Hand-verified listings (Denver metro, Denver-Flint, Denver-League City) always included.")

mode = st.radio("What do you need?", ["Driving corridor", "Home base"], horizontal=True, label_visibility="collapsed")

with st.form("inputs", border=True):
    if mode == "Driving corridor":
        origin = st.text_input("From", "", placeholder="Street address or town, e.g. Denver, CO")
        destination = st.text_input("To", "", placeholder="e.g. Flint, MI")
        with st.expander("Shape the route (optional)"):
            via_txt = st.text_input("Via (comma-separated towns)", "")
            variant_txt = st.text_input("Variant route via (drawn dashed)", "")
            google_link = st.text_input("Google Maps directions link", "",
                                        help="In Google Maps, set the drive up with any stops you want, Share -> Copy link, paste here. Its stops replace From/To/Via.")
            track_file = st.file_uploader("GPX or KML file (used exactly as drawn)", type=["gpx", "kml"])
            corridor_mi = st.slider("Keep every clinic within (miles of the road)", 3, 30, 12)
            er_reach_mi = st.slider("Keep 24/7 and night clinics out to (miles)", 15, 80, 45)
    else:
        home = st.text_input("Home address", "", placeholder="Street address or town")
        radius_mi = st.slider("Radius (miles)", 5, 40, 15)
    with st.expander("Data settings"):
        provider = st.selectbox("Live data source", ["auto", "google", "osm"],
                                help="auto = Google Places when a key is configured, otherwise OpenStreetMap")
        curated = st.checkbox("Include hand-verified listings", True)
    go = st.form_submit_button("Build map", type="primary", use_container_width=True)


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


def _show_page(page_html, height=900):
    """Embed the finished map page.  st.iframe (newer Streamlit) needs a file; older versions take the HTML directly."""
    if hasattr(st, "iframe"):
        page_file = pathlib.Path(tempfile.gettempdir()) / f"vetmap_{hashlib.md5(page_html.encode()).hexdigest()[:10]}.html"
        page_file.write_text(page_html, encoding="utf-8")
        st.iframe(page_file, height=height)
    else:
        st.components.v1.html(page_html, height=height, scrolling=True)


if go:
    needed = [origin, destination] if mode == "Driving corridor" else [home]
    if mode == "Driving corridor" and (google_link.strip() or track_file is not None):
        needed = []
    if any(not v.strip() for v in needed):
        st.warning("Enter the address or town first." if mode == "Home base" else "Enter both From and To (or a Google Maps link / GPX file under Shape the route).")
        st.stop()
    try:
        with st.spinner("Routing, pulling clinics and building the map (30 s to a few minutes for a long corridor)..."):
            if mode == "Driving corridor":
                via = [s.strip() for s in via_txt.split(",") if s.strip()] or None
                variant_via = [s.strip() for s in variant_txt.split(",") if s.strip()] or None
                track = track_file.getvalue().decode("utf-8", "ignore") if track_file is not None else None
                res = _build("corridor", origin=origin, destination=destination, via=via, variant_via=variant_via,
                             google_link=google_link.strip() or None, track=track,
                             provider=provider, corridor_mi=corridor_mi, er_reach_mi=er_reach_mi, curated=curated)
                fname = "vet_corridor_" + (track_file.name.rsplit(".", 1)[0] if track_file is not None else f"{origin}_{destination}")
            else:
                res = _build("home", home=home, radius_mi=radius_mi, provider=provider, curated=curated)
                fname = f"vet_home_{home}"
            fname = re.sub(r"[^A-Za-z0-9]+", "_", fname).strip("_")[:80]
    except Exception as e:
        st.error(f"Could not build the map: {e}")
        st.stop()

    df = res["df"]
    for w in res["warnings"]:
        st.warning(w)
    c1, c2, c3 = st.columns(3)
    c1.metric("Clinics", len(df))
    c2.metric("24/7 ER", int((df["typ"] == "ER").sum()))
    c3.metric("Night / late", int((df["typ"] == "NIGHT").sum()))
    if mode == "Driving corridor":
        st.caption(f"{res['total_mi']:.0f} mi, about {res['drive_h']:.1f} h driving. Live source: {res['provider']}.")
    else:
        st.caption(f"Live source: {res['provider']}.")

    url = _publish(res["html"])
    if url:
        st.link_button("Open the map full screen (tap-to-call and Near me work there)", url, use_container_width=True)
    _show_page(res["html"])

    d1, d2 = st.columns(2)
    d1.download_button("Save the map (HTML - add it to the VetAlong phone app under My maps)", res["html"], file_name=fname + ".html", mime="text/html", use_container_width=True)
    d2.download_button("Save the table (CSV)", df.drop(columns=["er_hint"], errors="ignore").to_csv(index=False),
                       file_name=fname + ".csv", mime="text/csv", use_container_width=True)

    if mode == "Driving corridor":
        st.subheader("Anchor chain - the numbers to save before leaving")
        st.dataframe(res["anchors"], hide_index=True)
        if len(res["gaps_24_7"]):
            st.subheader("Stretches with no true 24/7 within reach")
            st.dataframe(res["gaps_24_7"], hide_index=True)
        if len(res["gaps_overnight"]):
            st.subheader("Stretches with no overnight cover at all")
            st.dataframe(res["gaps_overnight"], hide_index=True)
else:
    st.info("Enter the trip or the home address and press **Build map**.")
