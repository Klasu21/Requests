# =============================================================================
# amadeus_meteo.py
# =============================================================================
# PURPOSE
# -------
# Streamlit demo that brings together four third‑party services:
#
#   1. Amadeus Self‑Service APIs      – activities catalogue + city search
#   2. Open‑Meteo Archive API         – historical weather for past dates
#   3. Streamlit                      – instant Python‑to‑web GUI framework
#   4. streamlit‑searchbox component  – live type‑ahead city selector
#
# UX HIGHLIGHTS
# -------------
# •  Live search for any city (Amadeus "City Search" endpoint)  
# •  Paginated activities list with page‑size selector  
# •  Sort by Rating / Price (↑ / ↓) or keep Amadeus’ natural order  
# •  Historical weather table for the *same calendar day* back three years  
# •  **Weather‑aware category preset**  
#       – If it rained ≥ 2 of 3 years → "rain" preset  
#       – Else: temperature < 15 °C → "cold/no‑rain" preset  
#       – Else: "warm/no‑rain" preset  
#   The preset is optional; users can edit categories afterwards.  
# •  Everything is held in `st.session_state` so navigation is smooth.
#
# NOTES FOR REAL PROJECTS
# -----------------------
# •  Hard‑coded Amadeus test keys are fine for demos; move them to secrets
#    or environment variables in production.
# •  The Amadeus test base URL is used.  Swap to the production URL +
#    production credentials for live data.
# •  No rate‑limit / retry logic here (simpler code); add if you expect load.
# =============================================================================

# ----------------------------------------------------------------------------- 
# 1. Imports
# -----------------------------------------------------------------------------
import math                                  # integer ceiling for pagination
from datetime import datetime, timedelta     # date arithmetic
from typing import List, Dict                # type annotations

import pandas as pd                          # tabular display for weather table
import requests                              # plain‑old HTTP
import streamlit as st                       # the web framework
from streamlit_searchbox import st_searchbox # 3rd‑party type‑ahead widget


# ----------------------------------------------------------------------------- 
# 2. API Credentials
# -----------------------------------------------------------------------------
# ⚠️  REAL APPS → store these safely (env vars, secrets.toml, vault, …)
API_KEY    = "ED6L9a4Wg81GZZh0U0HYuli8JjZRyEvO"
API_SECRET = "hS91gAmzQYg0aK88"


# ----------------------------------------------------------------------------- 
# 3. Helper functions – Amadeus Auth & API wrappers
# -----------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=1800)
def get_access_token() -> str:
    """
    Retrieve OAuth‑style bearer token (valid ~30 min).
    Uses Streamlit cache so repeated calls reuse the token until expiry.
    """
    resp = requests.post(
        "https://test.api.amadeus.com/v1/security/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": API_KEY,
            "client_secret": API_SECRET,
        },
        timeout=10,
    )
    resp.raise_for_status()                  # surfaces 4xx/5xx as exceptions
    return resp.json()["access_token"]


def _amadeus_city_query(token: str, keyword: str, max_results: int = 8) -> List[Dict]:
    """
    Call Amadeus 'City Search' endpoint.
    Returns a list of dict objects with name, IATA code, lat/lon.

    The Self‑Service test environment returns *synthetic* data but shape matches
    production.  We skip any hit that lacks coordinates (rare).
    """
    resp = requests.get(
        "https://test.api.amadeus.com/v1/reference-data/locations/cities",
        headers={"Authorization": f"Bearer {token}"},
        params={"keyword": keyword, "max": max_results},
        timeout=10,
    )
    if resp.status_code != 200:
        return []  # swallow errors (e.g., rate‑limit) for smoother UX
    hits = []
    for item in resp.json().get("data", []):
        geo = item.get("geoCode", {})
        if "latitude" in geo and "longitude" in geo:
            hits.append(
                {
                    "name": item.get("name", "Unknown"),
                    "iata": item.get("iataCode", ""),
                    "lat":  geo["latitude"],
                    "lon":  geo["longitude"],
                }
            )
    return hits


def city_searchbox_source(user_input: str, **_) -> List[Dict]:
    """
    Adapter function to feed results into streamlit‑searchbox.
    Returns list[dict] where each dict MUST contain a 'label' key.
    """
    token = get_access_token()
    matches = _amadeus_city_query(token, user_input)
    for m in matches:
        m["label"] = f"{m['name']} ({m['iata']})" if m["iata"] else m["name"]
    return matches


def get_activities(token: str, lat: float, lon: float, radius: int) -> List[Dict]:
    """
    Fetch Amadeus 'Activities' near a point, within radius (km 0‑20).
    Returns raw 'data' list.  Errors bubble up to caller.
    """
    resp = requests.get(
        "https://test.api.amadeus.com/v1/shopping/activities",
        headers={"Authorization": f"Bearer {token}"},
        params={"latitude": lat, "longitude": lon, "radius": radius},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["data"]


# ----------------------------------------------------------------------------- 
# 4. Helper functions – Open‑Meteo weather
# -----------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=6000)
def fetch_weather_once(lat: float, lon: float, iso_date: str) -> Dict:
    """
    Retrieve weather for ONE calendar date (iso string) at given coordinates.
    Cached ~100 min to stay under free quota while typing.
    Returns dict with three lists (max/min/precip) or {} if no data.
    """
    resp = requests.get(
        "https://archive-api.open-meteo.com/v1/archive",
        params={
            "latitude": lat,
            "longitude": lon,
            "start_date": iso_date,
            "end_date": iso_date,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
            "timezone": "Europe/Berlin",  # align units with UI
        },
        timeout=10,
    )
    return resp.json().get("daily", {}) if resp.status_code == 200 else {}


def last_three_years_weather(
    lat: float, lon: float, ref_date: datetime
) -> List[Dict]:
    """
    Build a small table for the same calendar day going back 1‑3 years.
    Each row has Year, Max°C, Min°C, Precip mm.
    Missing data rows are skipped.
    """
    rows = []
    for offset in range(1, 4):  # 1, 2, 3 years ago
        dt = ref_date - timedelta(days=365 * offset)
        iso = dt.strftime("%Y-%m-%d")
        daily = fetch_weather_once(lat, lon, iso)
        if daily:
            rows.append(
                {
                    "Year": dt.year,
                    "Max °C": daily["temperature_2m_max"][0],
                    "Min °C": daily["temperature_2m_min"][0],
                    "Precip mm": daily["precipitation_sum"][0],
                }
            )
    return rows


def classify_weather(rows: List[Dict]) -> tuple[bool, float]:
    """
    Decide whether rain is 'expected' and compute average temperature.
    • Rain expected  ⇢  if ≥ 2 rows have precipitation > 0 mm
    • Avg. temp     ⇢  mean of ((max+min)/2) across rows
    Returns (rain_flag, avg_temp)
    """
    rain_flag = sum(row["Precip mm"] > 0 for row in rows) >= 2
    temps = [(r["Max °C"] + r["Min °C"]) / 2 for r in rows]
    avg_temp = sum(temps) / len(temps) if temps else float("nan")
    return rain_flag, avg_temp


def preset_categories(rain: bool, avg_temp: float) -> List[str]:
    """
    Mapping of rain/temperature situation to default activity categories.
      • Rain ...................... → Museums, Restaurants, Historical, Sightseeing
      • No rain & <15 °C .......... → Museums, Historical, Tours, Sightseeing
      • No rain & ≥15 °C .......... → Wine, Historical
    """
    if rain:
        return ["Museums", "Restaurants", "Historical", "Sightseeing"]
    if avg_temp < 15:
        return ["Museums", "Historical", "Tours", "Sightseeing"]
    return ["Wine", "Historical"]


# ----------------------------------------------------------------------------- 
# 5. Streamlit Page Config + session‑state initialisation
# -----------------------------------------------------------------------------
st.set_page_config(page_title="Amadeus Activities", page_icon="🎒")
st.title("🎒 Amadeus Activities & Weather Explorer")

# Ensure keys exist in st.session_state (Streamlit's global dict per browser tab)
for key, default in {
    "page": 1,            # current pagination page
    "have_results": False,# whether user has run a search yet
    "use_preset": False,  # flag: apply weather preset on next refresh
    "active_cats": [],    # list[str] – categories currently in effect
}.items():
    st.session_state.setdefault(key, default)


# ----------------------------------------------------------------------------- 
# 6.  UI – City autocomplete (always visible)
# -----------------------------------------------------------------------------
city_selected = st_searchbox(
    city_searchbox_source,     # callback above
    key="city_search",
    placeholder="Start typing a city …",
    no_results_msg="No city found",
)

# Guard everything else behind 'city selected'
if not city_selected:
    st.info("Start typing a city name to see suggestions.")
    st.stop()                  # terminates re‑run early

lat, lon = city_selected["lat"], city_selected["lon"]
st.success(f"Selected **{city_selected['label']}** → {lat:.2f}, {lon:.2f}")

# ----------------------------------------------------------------------------- 
# 7.  UI – Top‑level controls (radius, date, page size, sorting)
# -----------------------------------------------------------------------------
radius = st.slider("Search radius (km)", 1, 20, 5)
ref_date = st.date_input("Travel date (for weather comparison)", datetime.today())

col_l, col_r = st.columns(2)
page_size = col_l.selectbox("Activities per page", [5, 10, 20], index=1)
sort_option = col_r.selectbox(
    "Sort",
    ["None", "Rating ↓", "Rating ↑", "Price ↓", "Price ↑"],
    index=0,
)

# ----------------------------------------------------------------------------- 
# 8.  Category multiselect  +  preset button
# -----------------------------------------------------------------------------
CATEGORY_LIST = ["Tours", "Museums", "Restaurants", "Wine", "Historical", "Sightseeing"]
KEYWORDS = {
    "Tours":       ["tour"],
    "Museums":     ["museum"],
    "Restaurants": ["restaurant", "food"],
    "Wine":        ["wine"],
    "Historical":  ["castle", "palace", "cathedral", "ruins"],
    "Sightseeing": ["sightseeing", "view", "panorama"],
}

# multiselect is bound to session_state.active_cats so UI and state stay in sync
selection = st.multiselect(
    "Kategorie‑Filter",
    CATEGORY_LIST,
    default=st.session_state.active_cats,
)
if selection != st.session_state.active_cats:
    st.session_state.active_cats = selection
    st.session_state.use_preset = False  # user touched filter, cancel preset

# Preset button is *always* visible
if st.button("🔄 Wetter‑basierten Filter anwenden"):
    st.session_state.use_preset = True   # will activate after weather load

# ----------------------------------------------------------------------------- 
# 9.  Trigger search
# -----------------------------------------------------------------------------
if st.button("Find Activities"):
    st.session_state.update(page=1, have_results=True)  # reset page

# If user never clicked "Find Activities" yet, stop here
if not st.session_state.have_results:
    st.stop()

# ----------------------------------------------------------------------------- 
# 10.  Data fetch & preparation (activities + weather)
# -----------------------------------------------------------------------------
try:
    token = get_access_token()
    activities_raw = get_activities(token, lat, lon, radius)
except requests.exceptions.HTTPError as e:
    # show Amadeus errors nicely, then abort further UI
    st.error("500 Server Error" if e.response.status_code == 500 else f"HTTP Error: {e}")
    st.stop()
except Exception as exc:
    st.error(f"Unexpected error: {exc}")
    st.stop()

# ----------  historical weather ----------
weather_rows = last_three_years_weather(lat, lon, ref_date)
st.subheader("📅 Weather on this date – last 3 years")
if weather_rows:
    st.table(pd.DataFrame(weather_rows).set_index("Year"))
    rain_flag, avg_temp = classify_weather(weather_rows)
    st.markdown(
        f"{'Regen' if rain_flag else 'Kein Regen'} erwartet, "
        f"Durchschnittstemperatur **{avg_temp:.1f} °C**."
    )
else:
    st.warning("No weather data available.")
    rain_flag, avg_temp = False, float("nan")

# ----------  apply weather‑preset if requested ----------
if st.session_state.use_preset:
    st.session_state.active_cats = preset_categories(rain_flag, avg_temp)
    # Update multiselect UI *this run*:
    st.rerun()               # trigger rerun so widget shows new default

# ----------  explanation / help ----------
with st.expander("Wie funktioniert der Wetter‑Filter?"):
    st.markdown(
        """
**So funktioniert der Wetter‑basierte Filter**

1. **Regenerkennung**  
   Ein Tag zählt als *Regen*, wenn an mindestens **2 von 3 Jahren** an diesem Datum Niederschlag > 0 mm gemessen wurde.

2. **Temperatur‑Mittelwert**  
   ⌀ Temp = Mittel aus Tages‑Max und ‑Min jedes Jahres.

---

### Regeln für die Kategorie‑Vorauswahl

| Wetterlage | Vorausgewählte Kategorien |
|------------|---------------------------|
| Regen (≥2 von 3 Jahren) | Museums, Restaurants, Historical, Sightseeing |
| Kein Regen & ⌀ Temp < 15 °C | Museums, Historical, Tours, Sightseeing |
| Kein Regen & ⌀ Temp ≥ 15 °C | Wine, Historical |
"""
    )

# ----------------------------------------------------------------------------- 
# 11.  Filter activities by active categories
# -----------------------------------------------------------------------------
keywords = [
    kw for cat in st.session_state.active_cats for kw in KEYWORDS[cat]
] if st.session_state.active_cats else []

filtered_acts = [
    act for act in activities_raw
    if not keywords or any(
        kw in (act.get("name", "") + " " + act.get("shortDescription", "")).lower()
        for kw in keywords
    )
]

# ----------------------------------------------------------------------------- 
# 12.  Sort activities
# -----------------------------------------------------------------------------
if sort_option != "None":
    reverse = "↓" in sort_option
    if "Rating" in sort_option:
        filtered_acts.sort(key=lambda a: float(a.get("rating") or -1), reverse=reverse)
    else:  # Price chosen
        def price_value(a):
            try:
                return float(a.get("price", {}).get("amount", float("inf")))
            except ValueError:
                return float("inf")
        filtered_acts.sort(key=price_value, reverse=reverse)

# ----------------------------------------------------------------------------- 
# 13.  Pagination calc
# -----------------------------------------------------------------------------
total_hits = len(filtered_acts)
max_pages  = max(1, math.ceil(total_hits / page_size))
# Clamp page number if result‑set changed
st.session_state.page = max(1, min(st.session_state.page, max_pages))
current_page = st.session_state.page

# Create slice for current page
page_slice = filtered_acts[(current_page - 1) * page_size : current_page * page_size]

# ----------------------------------------------------------------------------- 
# 14.  Pagination controls + headline
# -----------------------------------------------------------------------------
st.subheader("🗺️ Activities")

c_prev, c_mid, c_next = st.columns([1, 2, 1])
c_prev.button(
    "⬅️ Prev", disabled=current_page == 1,
    on_click=lambda: st.session_state.__setitem__("page", current_page - 1),
)
c_next.button(
    "Next ➡️", disabled=current_page == max_pages,
    on_click=lambda: st.session_state.__setitem__("page", current_page + 1),
)
c_mid.write(f"Page **{current_page}/{max_pages}** — {len(page_slice)} of {total_hits}")

# ----------------------------------------------------------------------------- 
# 15.  Render each activity on current page
# -----------------------------------------------------------------------------
if not page_slice:
    st.info("No activities match current filters.")

for act in page_slice:
    # Heading
    st.markdown(f"### {act.get('name', 'No Name')}")

    # Key facts
    st.write(f"**Rating:** {act.get('rating', 'N/A')}")
    st.write(f"**Description:** {act.get('shortDescription', 'No description available.')}")

    price_info = act.get("price", {})
    price_txt = (
        f"{float(price_info.get('amount')):,.2f} {price_info.get('currencyCode', '')}"
        if price_info.get("amount") else "N/A"
    )
    st.write(f"**Price:** {price_txt}")

    st.write(f"**Duration:** {act.get('minimumDuration', 'N/A')}")

    # First picture (if any)
    pics = act.get("pictures", [])
    if pics:
        st.image(pics[0], width=400)

    # Link to Amadeus B2C booking partner
    if act.get("bookingLink"):
        st.markdown(
            f"[📅 Book this activity]({act['bookingLink']})",
            unsafe_allow_html=True,
        )

    st.markdown("---")  # visual separator
