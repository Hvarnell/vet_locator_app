# Vet locator app

Every vet door along any US driving corridor, or around any address: 24/7 ERs, night clinics, extended-hours and daytime GPs with phone, hours, rating, mile marker and off-route distance, the anchor chain of overnight doors, and the stretches with no overnight cover. The 231 hand-verified clinics (Denver metro, Denver-Flint, Denver-League City; Google listings Sept 2026) are always included and shown with a white ring.

Generated from `harley_vet_locator.ipynb` - edit the notebook, then re-run its last cell to refresh this folder.

## Share it as a link (free)

1. Put this folder in a GitHub repository (public or private).
2. Go to https://share.streamlit.io, sign in with GitHub, **New app**, pick the repository and `app.py`, **Deploy**.
3. Optional: in the app's *Settings > Secrets* add
   ```
   GOOGLE_MAPS_API_KEY = "your key"
   VET_LOCATOR_CONTACT = "your email"
   ```
   The Google key gives ratings, phones and hours for every clinic (Places API (New) must be enabled on the key). Without it the app uses OpenStreetMap, which is free but lists hours for only about a quarter of clinics and no ratings.
4. Send the URL. Anyone can enter a trip or an address, build the map, and download the HTML or CSV.

Hugging Face Spaces (Streamlit template) works the same way if you prefer it to Streamlit Community Cloud.

## Run it on your own computer

```
pip install -r requirements.txt
streamlit run app.py
```

## Files

| file | what it is |
|---|---|
| `app.py` | the Streamlit page |
| `vetlocator.py` | routing, hours parsing, data providers, merge, map rendering (generated from the notebook) |
| `requirements.txt` | Python packages |

Routing: OSRM (public demo server). Geocoding: Nominatim. Live clinics: Google Places (New) or OpenStreetMap Overpass. All calls are cached on disk for the life of the app instance.
