"""Vet locator - shareable web app.

Run locally:   pip install -r requirements.txt && streamlit run app.py
Share online:  push this folder to GitHub and deploy on https://share.streamlit.io (free) - see README.md
"""
import os, hashlib, pathlib, tempfile
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
os.environ.setdefault("VET_LOCATOR_CONTACT", _secret("VET_LOCATOR_CONTACT", "vet-locator streamlit app"))

import vetlocator as vl   # noqa: E402  (reads the environment at import time)

st.set_page_config(page_title="Vet locator", page_icon=":dog:", layout="wide")
st.title("Vet locator - every vet door on the road, or around home")
st.caption("24/7 ERs, night clinics, extended-hours and daytime GPs along any US driving corridor or around any address. "
           "Hand-verified listings (Denver metro, Denver-Flint, Denver-League City) always ride on top of the live data.")

with st.sidebar:
    mode = st.radio("What do you need?", ["Driving corridor", "Home base"])
    if mode == "Driving corridor":
        origin = st.text_input("From", "5341 W 82nd Ave, Westminster, CO")
        destination = st.text_input("To", "League City, TX")
        via_txt = st.text_input("Via (optional, comma-separated towns)", "")
        variant_txt = st.text_input("Variant route via (optional, drawn dashed)", "")
        corridor_mi = st.slider("Keep every clinic within (miles of the road)", 3, 30, 12)
        er_reach_mi = st.slider("Keep 24/7 and night clinics out to (miles)", 15, 80, 45)
    else:
        home = st.text_input("Home address", "5341 W 82nd Ave, Westminster, CO")
        radius_mi = st.slider("Radius (miles)", 5, 40, 15)
    provider = st.selectbox("Live data source", ["auto", "google", "osm"],
                            help="auto = Google Places when a key is configured, otherwise OpenStreetMap")
    curated = st.checkbox("Include hand-verified listings", True)
    go = st.button("Build map", type="primary")


@st.cache_data(show_spinner=False, ttl=6 * 3600)
def _build(kind, **kw):
    if kind == "corridor":
        r = vl.corridor_map(**kw)
        return {"html": r["html"], "df": r["df"], "anchors": r["anchors"], "gaps_24_7": r["gaps_24_7"],
                "gaps_overnight": r["gaps_overnight"], "warnings": r["warnings"], "provider": r["provider"],
                "total_mi": r["route"]["total_mi"], "drive_h": r["route"]["drive_h"]}
    r = vl.home_map(**kw)
    return {"html": r["html"], "df": r["df"], "warnings": r["warnings"], "provider": r["provider"]}


def _show_page(page_html, height=1100):
    """Embed the finished map page.  st.iframe (newer Streamlit) needs a file; older versions take the HTML directly."""
    if hasattr(st, "iframe"):
        page_file = pathlib.Path(tempfile.gettempdir()) / f"vetmap_{hashlib.md5(page_html.encode()).hexdigest()[:10]}.html"
        page_file.write_text(page_html, encoding="utf-8")
        st.iframe(page_file, height=height)
    else:
        st.components.v1.html(page_html, height=height, scrolling=True)


if go:
    try:
        with st.spinner("Routing, pulling clinics and building the map (30 s to a few minutes for a long corridor)..."):
            if mode == "Driving corridor":
                via = [s.strip() for s in via_txt.split(",") if s.strip()] or None
                variant_via = [s.strip() for s in variant_txt.split(",") if s.strip()] or None
                res = _build("corridor", origin=origin, destination=destination, via=via, variant_via=variant_via,
                             provider=provider, corridor_mi=corridor_mi, er_reach_mi=er_reach_mi, curated=curated)
                fname = f"vet_corridor_{origin}_{destination}".replace(" ", "_").replace(",", "")[:80]
            else:
                res = _build("home", home=home, radius_mi=radius_mi, provider=provider, curated=curated)
                fname = f"vet_home_{home}".replace(" ", "_").replace(",", "")[:80]
    except Exception as e:
        st.error(f"Could not build the map: {e}")
        st.stop()

    df = res["df"]
    for w in res["warnings"]:
        st.warning(w)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Clinics", len(df))
    c2.metric("24/7 ER", int((df["typ"] == "ER").sum()))
    c3.metric("Night / late", int((df["typ"] == "NIGHT").sum()))
    c4.metric("Hours unknown", int((df["typ"] == "UNK").sum()))
    if mode == "Driving corridor":
        c5.metric("Route", f"{res['total_mi']:.0f} mi / {res['drive_h']:.1f} h")
    else:
        c5.metric("Live source", res["provider"])

    _show_page(res["html"])

    d1, d2 = st.columns(2)
    d1.download_button("Download the map (HTML - opens in any browser, share by email)", res["html"],
                       file_name=fname + ".html", mime="text/html")
    d2.download_button("Download the table (CSV)", df.drop(columns=["er_hint"], errors="ignore").to_csv(index=False),
                       file_name=fname + ".csv", mime="text/csv")

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
    st.info("Set the trip or home address in the sidebar and press **Build map**.")
