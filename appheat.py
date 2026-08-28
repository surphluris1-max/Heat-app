import json
import os
from datetime import datetime, timedelta

import streamlit as st
import pandas as pd
import requests as _http

from dotenv import load_dotenv
load_dotenv()

from fortyguard import FortyGuardClient

WARNING_TEMP = 35.0  # threshold in Celsius
BOX_HALF_SIDE_DEG = 0.002  # ~200 m box around the point

# Check for optional interactive-map dependency (folium is already in
# requirements.txt; only streamlit-folium needs an extra pip install).
try:
    import folium
    from streamlit_folium import st_folium
    HAS_FOLIUM = True
except ImportError:
    HAS_FOLIUM = False


# ──────────────────────────── Client ────────────────────────────
@st.cache_resource
def get_client():
    return FortyGuardClient()


client = get_client()


# ──────────────────────────── Geocoding ─────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def geocode(query):
    """Search for a U.S. place name via OpenStreetMap Nominatim (free, no key)."""
    try:
        resp = _http.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 5, "countrycodes": "us"},
            headers={"User-Agent": "CommunityHeatMonitor/1.0"},
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return []


# ──────────────────────────── AOI helper ────────────────────────
def point_to_small_polygon(lat, lon, half_side=BOX_HALF_SIDE_DEG):
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [lon - half_side, lat - half_side],
                    [lon + half_side, lat - half_side],
                    [lon + half_side, lat + half_side],
                    [lon - half_side, lat + half_side],
                    [lon - half_side, lat - half_side],
                ]],
            },
        }],
    }


# ──────────────────── Temperature (single hour) ────────────────
def get_temperature(lat, lon, date_str, time_str):
    aoi = point_to_small_polygon(lat, lon)
    response = client.create_heatmap(
        polygon_aoi=aoi,
        start_date=date_str,
        start_time=time_str,
        filter_type=1,
        granularity=60,
    )
    features = response["result"]["map_data"]["features"]
    if not features:
        raise ValueError("No tiles returned for this box/time.")
    possible_keys = ["temperature", "average_temperature", "avg_temperature", "temp"]
    temps = []
    for f in features:
        props = f.get("properties", {})
        for key in possible_keys:
            if key in props:
                temps.append(props[key])
                break
    if not temps:
        sample_props = features[0].get("properties", {})
        raise ValueError(f"Unknown temperature key. Keys: {list(sample_props.keys())}")
    return sum(temps) / len(temps)


# ──────────────── Exceedance (hours above threshold) ────────────
def get_exceedance_hours(lat, lon, date_str, end_time_str, threshold=WARNING_TEMP):
    aoi = point_to_small_polygon(lat, lon)
    response = client.create_heatmap(
        polygon_aoi=aoi,
        start_date=date_str,
        start_time="00:00",
        end_time=end_time_str,
        filter_type=2,
        analytic_type="exceedance",
        threshold=threshold,
        direction="above",
        granularity=60,
    )
    stats = response["result"].get("stats_data", {})
    if "mean" in stats:
        return stats["mean"]
    features = response["result"]["map_data"]["features"]
    vals = [f["properties"]["value"] for f in features if "value" in f.get("properties", {})]
    return sum(vals) / len(vals) if vals else 0.0


# ──────────────── Environmental Parameters (full day) ───────────
def get_env_full_day(lat, lon, temperature, date_str):
    """Fetch 24-hour environmental arrays for a single day (filter_type=3)."""
    response = client.environmental_parameters(
        latitude=lat,
        longitude=lon,
        temperature=temperature,
        start_date=date_str,
        filter_type=3,  # full 24-hour day
        analysis=[
            "apparent_temperature_celsius",
            "relative_humidity_percent",
            "air_quality:idx",
        ],
    )
    return response.get("result", {})


# ──────────────────── Display helpers ───────────────────────────
def c_to_f(c):
    return c * 9 / 5 + 32


def fmt_temp(c, use_f=False):
    return f"{c_to_f(c):.1f}°F" if use_f else f"{c:.1f}°C"


def heat_mood(temp):
    if temp < 15:
        return "🥶", "Cold", "#3b82f6"
    if temp < 25:
        return "😊", "Pleasant", "#22c55e"
    if temp < 32:
        return "🙂", "Warm", "#eab308"
    if temp < WARNING_TEMP:
        return "😓", "Hot", "#f97316"
    return "🥵", "Extreme", "#ef4444"


def risk_level(temp, exceedance_hours):
    if temp >= WARNING_TEMP or exceedance_hours >= 4:
        return "High", "#ef4444"
    if temp >= 32 or exceedance_hours >= 1:
        return "Moderate", "#f59e0b"
    return "Low", "#22c55e"


def recommendation(level_text):
    return {
        "High": "🚫 Cancel outdoor activity. Move indoors immediately.",
        "Moderate": "⚠️ Limit sun exposure. Take shade breaks & hydrate.",
        "Low": "✅ Normal activity is safe. Stay hydrated.",
    }[level_text]


# ═══════════════════ PAGE CONFIG & CSS ══════════════════════════
st.set_page_config(
    page_title="Community Heat Safety Monitor",
    page_icon="🌡️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
    #MainMenu, footer {visibility: hidden;}

    /* Hero */
    .hero {
        background: linear-gradient(135deg, #ff6b35 0%, #f72585 50%, #7209b7 100%);
        border-radius: 16px;
        padding: 2.2rem 2rem;
        margin-bottom: 1.2rem;
        color: white;
        position: relative;
        overflow: hidden;
    }
    .hero::before {
        content: '';
        position: absolute;
        top: -50%; right: -20%;
        width: 400px; height: 400px;
        background: rgba(255,255,255,0.08);
        border-radius: 50%;
    }
    .hero h1 { font-size: 2.2rem; font-weight: 800; margin: 0 0 0.2rem 0; }
    .hero p  { font-size: 1rem; opacity: 0.92; margin: 0; }

    /* Cards */
    .metric-card {
        background: white;
        border-radius: 14px;
        padding: 1.2rem;
        box-shadow: 0 2px 12px rgba(0,0,0,0.06);
        border: 1px solid #f0f0f0;
        text-align: center;
        transition: transform 0.2s, box-shadow 0.2s;
    }
    .metric-card:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 20px rgba(0,0,0,0.1);
    }
    .metric-card .value { font-size: 1.9rem; font-weight: 800; margin: 0.2rem 0; }
    .metric-card .label {
        font-size: 0.8rem; color: #888;
        text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600;
    }

    /* Risk badge */
    .risk-badge {
        display: inline-block;
        padding: 0.3rem 0.9rem;
        border-radius: 20px;
        font-weight: 700; font-size: 0.85rem;
        color: white;
    }

    /* Pulse dot */
    @keyframes pulse {
        0%   { box-shadow: 0 0 0 0 rgba(239,68,68,0.5); }
        70%  { box-shadow: 0 0 0 12px rgba(239,68,68,0); }
        100% { box-shadow: 0 0 0 0 rgba(239,68,68,0); }
    }
    .pulse-dot {
        width: 12px; height: 12px;
        background: #ef4444; border-radius: 50%;
        display: inline-block;
        animation: pulse 1.5s infinite;
        vertical-align: middle; margin-right: 6px;
    }

    /* Section titles */
    .section-title {
        font-size: 1.25rem; font-weight: 700; color: #1e293b;
        margin: 1.2rem 0 0.6rem 0;
        display: flex; align-items: center; gap: 8px;
    }

    /* Info strip */
    .info-strip {
        background: linear-gradient(90deg, #dbeafe, #ede9fe);
        border-radius: 10px;
        padding: 0.65rem 1.1rem;
        font-size: 0.88rem; color: #334155;
        margin-bottom: 1rem;
    }

    /* Search result cards */
    .search-result {
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 0.5rem 0.8rem;
        margin-bottom: 0.4rem;
        font-size: 0.9rem;
    }

    /* Nicer tabs */
    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
    .stTabs [data-baseweb="tab"] {
        border-radius: 10px 10px 0 0;
        padding: 0.6rem 1.5rem; font-weight: 600;
    }
</style>
""", unsafe_allow_html=True)


# ── Sidebar ──
with st.sidebar:
    st.markdown("### ⚙️ Settings")
    use_f = st.toggle("Show temperatures in °F", value=False)
    st.divider()
    st.markdown("### 📌 About")
    st.markdown(
        "Real-time **urban heat monitoring** for communities, "
        "powered by the [FortyGuard API](https://api.fortyguard.com).  \n\n"
        "🇺🇸 **U.S. locations only**  \n"
        "📅 Data: **2021 – today**"
    )
    st.divider()
    if not HAS_FOLIUM:
        st.info("💡 Install `streamlit-folium` for click-to-select maps:\n\n"
                "`pip install streamlit-folium`")
    st.caption("FortyGuard Hackathon '26 🏆")


# ── Hero ──
st.markdown("""
<div class="hero">
    <h1>🌡️ Community Heat Safety Monitor</h1>
    <p>Real-time heat risk intelligence for schools, worksites & neighborhoods</p>
</div>
""", unsafe_allow_html=True)

st.markdown(
    '<div class="info-strip">'
    '💡 <b>How to use:</b> Search for a place or click the map → coordinates auto-fill '
    '→ hit <b>Analyze</b> for temperature, risk level, and 24-hour environmental data.'
    '</div>',
    unsafe_allow_html=True,
)


# ── Session-state defaults ──
if "sel_lat" not in st.session_state:
    st.session_state.sel_lat = 40.7128
if "sel_lon" not in st.session_state:
    st.session_state.sel_lon = -74.0060
if "sel_name" not in st.session_state:
    st.session_state.sel_name = ""




# ═══════════════════ TAB 1 — Quick Check ════════════════════════

# ── Location Search ──
st.markdown('<div class="section-title">🔎 Find a Location</div>', unsafe_allow_html=True)

search_col, result_col = st.columns([1, 1.5])

with search_col:
    query = st.text_input(
        "Search for a place",
        placeholder="e.g. Times Square, New York",
        key="search_q",
        label_visibility="collapsed",
    )
    search_btn = st.button("🔍 Search", use_container_width=True)

with result_col:
    if search_btn and query.strip():
        results = geocode(query.strip())
        if results:
            options = {
                f"{r['display_name'][:80]}": r for r in results
            }
            pick = st.radio(
                "Select a result:",
                list(options.keys()),
                key="search_pick",
            )
            if pick:
                chosen = options[pick]
                st.session_state.sel_lat = float(chosen["lat"])
                st.session_state.sel_lon = float(chosen["lon"])
                st.session_state.sel_name = chosen["display_name"].split(",")[0]
        else:
            st.warning("No results found. Try a different search term (U.S. only).")

st.divider()

# ── Map + Coordinates ──
st.markdown('<div class="section-title">📍 Selected Location</div>', unsafe_allow_html=True)

if st.session_state.sel_name:
    st.markdown(f"**📌 {st.session_state.sel_name}**")

map_col, input_col = st.columns([1.6, 1])

with input_col:
    st.markdown("##### Coordinates")
    lat = st.number_input("Latitude", value=st.session_state.sel_lat, format="%.5f", step=0.001, key="inp_lat")
    lon = st.number_input("Longitude", value=st.session_state.sel_lon, format="%.5f", step=0.001, key="inp_lon")
    # Sync back to session state
    st.session_state.sel_lat = lat
    st.session_state.sel_lon = lon

    fetch_env = st.checkbox("Include 24-hour environmental profile", value=True)

    st.markdown("")
    analyze_btn = st.button("🔬 Analyze Heat Risk", type="primary", use_container_width=True)

with map_col:
    if HAS_FOLIUM:
        st.caption("👆 Click anywhere on the map to pick a location")
        m = folium.Map(
            location=[st.session_state.sel_lat, st.session_state.sel_lon],
            zoom_start=13,
            tiles="CartoDB positron",
        )
        folium.Marker(
            [st.session_state.sel_lat, st.session_state.sel_lon],
            popup=st.session_state.sel_name or "Selected",
            icon=folium.Icon(color="red", icon="fire", prefix="fa"),
        ).add_to(m)
        map_out = st_folium(m, height=370, use_container_width=True, returned_objects=[])

        if map_out and map_out.get("last_clicked"):
            clk_lat = map_out["last_clicked"]["lat"]
            clk_lon = map_out["last_clicked"]["lng"]
            if (clk_lat != st.session_state.sel_lat) or (clk_lon != st.session_state.sel_lon):
                st.session_state.sel_lat = clk_lat
                st.session_state.sel_lon = clk_lon
                st.session_state.sel_name = ""
                st.rerun()
    else:
        st.caption("📍 Location preview (install `streamlit-folium` for click-to-select)")
        st.map(
            pd.DataFrame({"lat": [st.session_state.sel_lat], "lon": [st.session_state.sel_lon]}),
            zoom=11, use_container_width=True,
        )

st.divider()

# ── Analysis ──
if analyze_btn:
    now = datetime.now()
    timestamps = [now - timedelta(hours=i) for i in range(3, -1, -1)]
    labels, temps = [], []

    with st.status("🔄 Fetching temperature data …", expanded=True) as status:
        for ts in timestamps:
            d = ts.strftime("%Y-%m-%d")
            t = ts.strftime("%H:00")
            st.write(f"⏳ {d} @ {t}")
            try:
                temp = get_temperature(lat, lon, d, t)
                labels.append(t)
                temps.append(temp)
            except (TaskFailedError, TaskTimeoutError, ValueError) as e:
                st.write(f"⚠️ Skipped {d} {t}: {e}")
        status.update(label="✅ Temperature data ready!", state="complete", expanded=False)

    if temps:
        current = temps[-1]
        emoji, mood, mood_color = heat_mood(current)
        lvl_text, lvl_color = risk_level(current, 0)
        delta = temps[-1] - temps[0] if len(temps) > 1 else 0

        # ── Metric cards ──
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.markdown(f'''
            <div class="metric-card">
                <div class="label">Temperature</div>
                <div class="value" style="color:{mood_color}">{fmt_temp(current, use_f)}</div>
            </div>''', unsafe_allow_html=True)
        with c2:
            st.markdown(f'''
            <div class="metric-card">
                <div class="label">Feels Like</div>
                <div class="value">{emoji} {mood}</div>
            </div>''', unsafe_allow_html=True)
        with c3:
            st.markdown(f'''
            <div class="metric-card">
                <div class="label">Risk Level</div>
                <div class="value"><span class="risk-badge" style="background:{lvl_color}">{lvl_text}</span></div>
            </div>''', unsafe_allow_html=True)
        with c4:
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
            st.markdown(f'''
            <div class="metric-card">
                <div class="label">4 h Trend</div>
                <div class="value" style="color:{'#ef4444' if delta > 0 else '#22c55e'}">{arrow} {abs(delta):.1f}°</div>
            </div>''', unsafe_allow_html=True)

        # ── Alert / safe banner ──
        if current >= WARNING_TEMP:
            st.markdown(
                f'<div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:12px;'
                f'padding:1rem 1.2rem;margin:1rem 0">'
                f'<span class="pulse-dot"></span>'
                f'<b style="color:#991b1b">HEAT WAVE WARNING</b> — '
                f'{fmt_temp(current, use_f)} exceeds {fmt_temp(WARNING_TEMP, use_f)}. '
                f'💧 Stay hydrated, avoid sun 11 AM – 4 PM.</div>',
                unsafe_allow_html=True,
            )
        else:
            st.success(f"✅ Temperature is within safe range ({fmt_temp(current, use_f)})")

        # ── Recommendation ──
        st.markdown(
            f'<div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;'
            f'padding:0.8rem 1rem;margin:0.5rem 0;font-size:0.95rem">'
            f'<b>💬 Recommendation:</b> {recommendation(lvl_text)}</div>',
            unsafe_allow_html=True,
        )

        # ── Temperature trend chart ──
        st.markdown('<div class="section-title">📈 Temperature — Last 4 Hours</div>', unsafe_allow_html=True)
        chart_df = pd.DataFrame({"Time": labels, "Temperature (°C)": temps}).set_index("Time")
        if use_f:
            chart_df["Temperature (°F)"] = chart_df["Temperature (°C)"].apply(c_to_f)
            st.area_chart(chart_df[["Temperature (°F)"]], color="#f72585")
        else:
            st.area_chart(chart_df[["Temperature (°C)"]], color="#f72585")

        # ── 24-hour Environmental Profile ──
        if fetch_env:
            st.markdown('<div class="section-title">🌿 24-Hour Environmental Profile</div>', unsafe_allow_html=True)
            with st.status("🔄 Fetching environmental data …", expanded=True) as env_status:
                try:
                    d = now.strftime("%Y-%m-%d")
                    env = get_env_full_day(lat, lon, current, d)

                    # Build a 24-hour DataFrame from the returned arrays
                    env_data = {}
                    hours = [f"{h:02d}:00" for h in range(24)]

                    if isinstance(env, dict):
                        for key, val in env.items():
                            if isinstance(val, list) and len(val) >= 24:
                                nice_name = (key
                                             .replace("_celsius", " (°C)")
                                             .replace("_percent", " (%)")
                                             .replace(":idx", " Index")
                                             .replace("_", " ")
                                             .title())
                                env_data[nice_name] = val[:24]

                    env_status.update(label="✅ Environmental data ready!", state="complete", expanded=False)

                    if env_data:
                        env_df = pd.DataFrame(env_data, index=hours)
                        env_df.index.name = "Hour"

                        # Mark current hour
                        current_hour = now.hour

                        # Show current-hour values as metric cards
                        env_keys = list(env_data.keys())
                        env_cols = st.columns(min(len(env_keys), 4))
                        for i, key in enumerate(env_keys[:4]):
                            with env_cols[i]:
                                val = env_data[key][current_hour]
                                if use_f and "°C" in key:
                                    display_val = f"{c_to_f(val):.1f}"
                                    display_key = key.replace("(°C)", "(°F)")
                                else:
                                    display_val = f"{val:.1f}" if isinstance(val, float) else str(val)
                                    display_key = key
                                st.metric(f"Now: {display_key}", display_val)

                        # Plot the 24-hour profiles
                        if use_f:
                            for col in env_df.columns:
                                if "°C" in col:
                                    new_col = col.replace("(°C)", "(°F)")
                                    env_df[new_col] = env_df[col].apply(c_to_f)
                                    env_df = env_df.drop(columns=[col])

                        st.line_chart(env_df, color=["#7209b7", "#0891b2", "#059669", "#d97706"][:len(env_df.columns)])
                        st.caption(f"⏰ Current hour highlighted: **{current_hour:02d}:00** — "
                                   f"data covers the full day from midnight to midnight.")

                        with st.expander("📋 Full 24-hour data table"):
                            st.dataframe(env_df, use_container_width=True)
                    else:
                        st.info("No 24-hour arrays returned. The point may be outside supported coverage.")

                except Exception as e:
                    env_status.update(label="⚠️ Error", state="error", expanded=False)
                    st.warning(f"Could not fetch environmental data: {e}")

        with st.expander("📋 Raw temperature readings"):
            st.dataframe(chart_df, use_container_width=True)
    else:
        st.error("❌ No readings fetched. Make sure the location is inside the U.S. "
                 "and the date is between 2021 and today.")


