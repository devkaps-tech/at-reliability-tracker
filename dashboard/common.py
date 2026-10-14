"""Shared pieces for the dashboard pages: data access, route names, the plain-English summary, colours, charts, maps."""

import math
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import duckdb
import pandas as pd
import pydeck as pdk
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tracker.config import load_config, project_path  # noqa: E402

CFG = load_config()
WAREHOUSE = project_path(CFG["storage"]["warehouse"])
ARRIVALS = project_path(CFG["storage"]["clean_dir"]) / "stop_arrivals" / "service_date=*" / "*.parquet"
EARLY_S = CFG["punctuality"]["early_if_more_than_seconds_early"]
LATE_S = CFG["punctuality"]["late_if_more_than_seconds_late"]
# Free street map (CARTO), no account or key needed.
BASEMAP = {"map_provider": "carto", "map_style": pdk.map_styles.CARTO_LIGHT}
MIN_BUSES = 10  # below this, answers are shown with a "not much data yet" warning

# ---- Colours: early = blue (you might miss it), on time = green, late = red ----
STATUS_LABEL = {"early": "Early", "on_time": "On time", "late": "Late"}
STATUS_SCALE = alt.Scale(domain=["Early", "On time", "Late"], range=["#2a78d6", "#0ca30c", "#d03b3b"])
BLUE, GREY, RED = (42, 120, 214), (150, 150, 145), (208, 59, 59)
DELAY_SCALE = alt.Scale(domain=[-3, 0, 10], range=["#2a78d6", "#a8a7a0", "#d03b3b"], clamp=True)  # minutes

BAND_NAMES = {"am_peak": "Morning peak", "midday": "Daytime", "pm_peak": "Afternoon peak", "evening": "Evening"}
PERIODS = {"Last 7 days": 7, "Last 14 days": 14, "Last 30 days": 30, "All data": None}


# ---------------------------------------------------------------- data access
@st.cache_data(ttl=600, show_spinner=False)
def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        con.execute("SET TimeZone = 'Pacific/Auckland'")
        return con.execute(sql, list(params)).df()


def arrivals_sql() -> str:
    """Scored stops from published days only (days whose blocking quality checks passed)."""
    return (f"(SELECT * FROM read_parquet('{ARRIVALS}', union_by_name = true) "
            f"WHERE service_date IN (SELECT service_date FROM latest_runs) AND status != 'bad_data')")


def stop_if_no_data() -> None:
    """Show a helpful message instead of an error when nothing has been published yet."""
    try:
        days = query("SELECT count(*) AS n FROM latest_runs")["n"][0]
        files = list(ARRIVALS.parent.parent.glob("service_date=*/*.parquet"))
    except duckdb.Error as err:
        st.error(f"Can't open the warehouse ({err}). Run the pipeline first: python -m tracker.pipeline run")
        st.stop()
    if not days or not files:
        st.info("No published days yet. Collect some data, then run: python -m tracker.pipeline run --date YYYY-MM-DD")
        st.stop()


def last_published_day() -> date:
    return query("SELECT max(service_date) AS d FROM latest_runs")["d"][0].date()


def period_start(label: str) -> date:
    days = PERIODS[label]
    return date(2000, 1, 1) if days is None else last_published_day() - timedelta(days=days - 1)


@st.cache_data(ttl=3600, show_spinner=False)
def latest_version() -> str:
    return query("SELECT gtfs_version FROM gtfs_versions ORDER BY loaded_at DESC LIMIT 1")["gtfs_version"][0]


# ---------------------------------------------------------------- route names
def tidy(text: str) -> str:
    """'Botany To Britomart Via Panmure' -> 'Botany to Britomart via Panmure'."""
    return re.sub(r"\s+", " ", (text or "").replace(" To ", " to ").replace(" Via ", " via ")).strip()


def station(name: str) -> str:
    """'Newmarket Train Station 2' -> 'Newmarket'."""
    return re.sub(r"\s*Train Station.*$", "", name or "").strip()


def direction_label(mode: str, headsign: str, first_stop: str, last_stop: str) -> str:
    # Train destination signs are internal codes ("Pukekohe 3 Clockwise To Newmarket 2 Via NKT1"), so use stations.
    if mode == "train":
        return f"{station(first_stop)} to {station(last_stop)}"
    return tidy(headsign)


def ends_label(direction: str) -> str:
    """'Botany to Britomart via Panmure' -> 'Botany ↔ Britomart'. Loop routes keep their name."""
    main = direction.split(" via ")[0]
    return main.replace(" to ", " ↔ ", 1) if " to " in main else main


def natural_key(short_name: str):
    m = re.match(r"(\d+)", short_name)
    return (0, int(m.group(1)), short_name) if m else (1, 0, short_name)


@st.cache_data(ttl=3600, show_spinner=False)
def route_catalog() -> pd.DataFrame:
    """One row per route direction: names, operator, the representative trip and its map line."""
    v = latest_version()
    df = query("""
        WITH trip_len AS (
            SELECT trip_id, count(*) AS n FROM gtfs_stop_times WHERE gtfs_version = $1 GROUP BY trip_id
        ), main_trip AS (   -- the longest trip that follows the route's usual map line
            SELECT p.route_id, p.direction_id, p.headsign, p.shape_id, arg_max(t.trip_id, l.n) AS trip_id
            FROM route_patterns p
            JOIN gtfs_trips t ON t.gtfs_version = p.gtfs_version AND t.route_id = p.route_id
                             AND t.direction_id = p.direction_id AND t.shape_id = p.shape_id
            JOIN trip_len l ON l.trip_id = t.trip_id
            WHERE p.gtfs_version = $1
            GROUP BY ALL
        )
        SELECT m.*, r.route_short_name, r.mode, coalesce(a.agency_name, r.agency_id) AS operator,
               first(s.stop_name ORDER BY st.stop_sequence) AS first_stop,
               last(s.stop_name ORDER BY st.stop_sequence) AS last_stop
        FROM main_trip m
        JOIN gtfs_routes r ON r.gtfs_version = $1 AND r.route_id = m.route_id
        LEFT JOIN gtfs_agency a ON a.gtfs_version = $1 AND a.agency_id = r.agency_id
        JOIN gtfs_stop_times st ON st.gtfs_version = $1 AND st.trip_id = m.trip_id
        JOIN gtfs_stops s ON s.gtfs_version = $1 AND s.stop_id = st.stop_id
        GROUP BY ALL""", (v,))
    df["direction"] = [direction_label(*row) for row in
                       df[["mode", "headsign", "first_stop", "last_stop"]].itertuples(index=False)]
    first_dir = df.sort_values("direction_id").groupby("route_id")["direction"].first()
    df["ends"] = df["route_id"].map(first_dir).map(ends_label)
    df["label"] = df["mode"].str.capitalize() + " " + df["route_short_name"] + " · " + df["ends"]
    df["sort"] = [(["bus", "train", "ferry"].index(m) if m in ("bus", "train", "ferry") else 3,
                   natural_key(s)) for m, s in zip(df["mode"], df["route_short_name"])]
    return df.sort_values(["sort", "direction_id"]).reset_index(drop=True)


def route_labels() -> dict:
    """route_id -> 'Bus 70 · Botany ↔ Britomart'."""
    cat = route_catalog()
    return dict(zip(cat["route_id"], cat["label"]))


@st.cache_data(ttl=3600, show_spinner=False)
def pattern_stops(trip_id: str) -> pd.DataFrame:
    """The stops of one route direction, in order, with map positions."""
    return query("""
        SELECT row_number() OVER (ORDER BY st.stop_sequence) AS stop_number, s.stop_id, s.stop_name, s.stop_code,
               s.stop_lat AS lat, s.stop_lon AS lon, st.arrival_secs
        FROM gtfs_stop_times st JOIN gtfs_stops s ON s.gtfs_version = st.gtfs_version AND s.stop_id = st.stop_id
        WHERE st.gtfs_version = ? AND st.trip_id = ? ORDER BY st.stop_sequence""", (latest_version(), trip_id))


@st.cache_data(ttl=3600, show_spinner=False)
def shape_path(shape_id: str) -> list:
    df = query("SELECT lon, lat FROM gtfs_shapes WHERE gtfs_version = ? AND shape_id = ? ORDER BY seq",
               (latest_version(), shape_id))
    return df.values.tolist()


@st.cache_data(ttl=600, show_spinner=False)
def route_observations(route_id: str, direction_id: int, start: date) -> pd.DataFrame:
    return query(f"""
        SELECT service_date, trip_id, stop_id, stop_sequence, scheduled_time, actual_time, delay_s, status,
               hour, time_band, dayofweek(service_date) AS weekday
        FROM {arrivals_sql()}
        WHERE route_id = ? AND direction_id = ? AND service_date >= ?
        ORDER BY actual_time""", (route_id, int(direction_id), start))


def band_options() -> dict:
    """'Morning peak (7–9 am)' -> 'am_peak', built from the hours in config.toml."""
    def clock(h):
        h %= 24
        return f"{12 if h % 12 == 0 else h % 12} {'am' if h < 12 else 'pm'}"
    options = {"Any time": None}
    for key, hours in CFG["time_bands"].items():
        options[f"{BAND_NAMES.get(key, key)} ({clock(min(hours))}–{clock(max(hours) + 1)})"] = key
    options["Night"] = "night"
    return options


# ---------------------------------------------------------------- plain-English summary
def minutes(seconds: float) -> str:
    m = max(1, round(abs(seconds) / 60))
    return f"{m} min"


def stop_summary(delays_s: list[float]) -> dict | None:
    """Turn a list of delays (seconds, negative = early) into the sentences shown to a rider."""
    if not delays_s:
        return None
    s = pd.Series(delays_s, dtype="float64")
    p10, median, p90 = s.quantile(0.1), s.median(), s.quantile(0.9)
    if median >= 60:
        typical = f"usually {minutes(median)} late"
    elif median <= -60:
        typical = f"usually {minutes(median)} early"
    else:
        typical = "usually on time"
    if p10 <= -30:
        early_by = math.ceil(-p10 / 60)
        catch = (f"1 in 10 reached this stop **{early_by}+ min early**, so to be safe, be there {early_by} min "
                 "before the timetable time.")
    else:
        catch = "It rarely comes early: 9 in 10 reached the stop no more than 30 seconds before the timetable time."
    if p90 > LATE_S:
        worst = f"1 in 10 was more than **{minutes(p90)} late**. Leave extra time if you have a connection."
    elif p90 >= 60:
        worst = f"9 in 10 were no more than {minutes(p90)} late."
    else:
        worst = "It was almost never more than a minute late."
    return {
        "n": len(s), "typical": typical, "catch": catch, "worst": worst,
        "median_s": median, "p10_s": p10, "p90_s": p90,
        "early": (s < -EARLY_S).mean(), "on_time": ((s >= -EARLY_S) & (s <= LATE_S)).mean(), "late": (s > LATE_S).mean(),
    }


def status_colour(label: str) -> str:
    """Text colour for an Early / On time / Late cell in a table."""
    colours = {"Early": "#2a78d6", "On time": "#0ca30c", "Late": "#d03b3b"}
    return f"color: {colours[label]}; font-weight: 600" if label in colours else ""


def delay_text(seconds: float) -> str:
    if abs(seconds) < 30:
        return "on time"
    return f"{abs(seconds) / 60:.0f} min {'late' if seconds > 0 else 'early'}" if abs(seconds) >= 90 else \
        f"{abs(seconds):.0f} s {'late' if seconds > 0 else 'early'}"


# ---------------------------------------------------------------- charts
def delay_spread_chart(df: pd.DataFrame) -> alt.Chart:
    """How many buses arrived at each minute early/late."""
    d = df.copy()
    d["minute"] = (d["delay_s"] // 60).clip(-5, 15).astype(int)
    d["Status"] = d["status"].map(STATUS_LABEL)
    counts = d.groupby(["minute", "Status"]).size().reset_index(name="buses")
    counts["label"] = counts["minute"].map(lambda m: "≤ −5" if m == -5 else ("15+" if m == 15 else f"{m:+d}"))
    order = ["≤ −5"] + [f"{m:+d}" for m in range(-4, 15)] + ["15+"]
    return alt.Chart(counts).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
        x=alt.X("label:O", sort=order, scale=alt.Scale(domain=order), title="Minutes late (minus = early)",
                axis=alt.Axis(labelAngle=0, values=["≤ −5", "+0", "+5", "+10", "15+"])),
        y=alt.Y("sum(buses):Q", title="Buses"),
        color=alt.Color("Status:N", scale=STATUS_SCALE, legend=alt.Legend(orient="top", title=None)),
        tooltip=[alt.Tooltip("label:O", title="Minutes late"), "Status:N", alt.Tooltip("buses:Q", title="Buses")],
    ).properties(height=220)


def share_chart(df: pd.DataFrame, by: str, title: str) -> alt.Chart:
    """Stacked bars: share early / on time / late for each hour or weekday."""
    d = df.copy()
    if by == "hour":
        d["key"] = d["hour"].astype(int)
        d["sort"] = d["key"].where(d["key"] >= 4, d["key"] + 24)  # 1 am trips sit after 11 pm
        d["group"] = d["key"].map(lambda h: f"{12 if h % 12 == 0 else h % 12}{'a' if h < 12 else 'p'}")
    else:
        names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        d["sort"] = (d["weekday"] - 1) % 7  # Monday first
        d["group"] = d["weekday"].map(lambda w: names[w])
    d["Status"] = d["status"].map(STATUS_LABEL)
    counts = d.groupby(["group", "sort", "Status"]).size().reset_index(name="buses")
    counts["total"] = counts.groupby("group")["buses"].transform("sum")
    counts["share"] = counts["buses"] / counts["total"]
    order = counts.sort_values("sort")["group"].drop_duplicates().tolist()
    return alt.Chart(counts).mark_bar().encode(
        x=alt.X("group:O", sort=order, title=title, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("share:Q", stack="normalize", title="Share of buses", axis=alt.Axis(format="%")),
        color=alt.Color("Status:N", scale=STATUS_SCALE, legend=alt.Legend(orient="top", title=None),
                        sort=["Early", "On time", "Late"]),
        order=alt.Order("Status:N", sort="descending"),
        tooltip=[alt.Tooltip("group:O", title=title), "Status:N", alt.Tooltip("share:Q", format=".0%", title="Share"),
                 alt.Tooltip("buses:Q", title="Buses"), alt.Tooltip("total:Q", title="All buses")],
    ).properties(height=220)


# ---------------------------------------------------------------- maps
def delay_rgb(minutes_late: float | None, alpha: int = 230) -> list:
    """Blue (3+ min early) -> grey (on time) -> red (10+ min late). No data -> faint grey."""
    if minutes_late is None or pd.isna(minutes_late):
        return [180, 180, 175, 120]
    if minutes_late < 0:
        t, a, b = min(1.0, -minutes_late / 3), GREY, BLUE
    else:
        t, a, b = min(1.0, minutes_late / 10), GREY, RED
    return [round(a[i] + (b[i] - a[i]) * t) for i in range(3)] + [alpha]


def stop_stats(obs: pd.DataFrame) -> pd.DataFrame:
    if obs.empty:
        return pd.DataFrame(columns=["stop_id", "buses", "median_min", "p25_min", "p75_min", "late_share",
                                     "on_time_share"])
    g = obs.groupby("stop_id")["delay_s"]
    out = pd.DataFrame({
        "buses": g.size(), "median_min": g.median() / 60, "p25_min": g.quantile(0.25) / 60,
        "p75_min": g.quantile(0.75) / 60,
        "late_share": obs.assign(x=obs["status"] == "late").groupby("stop_id")["x"].mean(),
        "on_time_share": obs.assign(x=obs["status"] == "on_time").groupby("stop_id")["x"].mean(),
    }).reset_index()
    return out


def route_map(path: list, stops: pd.DataFrame, highlight: str | None = None, key: str | None = None,
              clickable: bool = False, height: int = 420):
    """Route line plus stops coloured by typical delay. Returns the map's selection if clickable."""
    s = stops.copy()
    s["color"] = [delay_rgb(m) for m in s.get("median_min", pd.Series([None] * len(s)))]
    s["summary"] = [
        "No buses seen here yet" if pd.isna(n) or not n else
        f"Typically {delay_text(m * 60)} · {o:.0%} on time · {int(n)} buses"
        for n, m, o in zip(s.get("buses", [None] * len(s)), s.get("median_min", [0] * len(s)),
                           s.get("on_time_share", [0] * len(s)))]
    layers = [
        pdk.Layer("PathLayer", data=[{"path": path}], get_path="path", get_color=[90, 110, 120, 170],
                  width_min_pixels=4, width_units="pixels", get_width=4),
        pdk.Layer("ScatterplotLayer", data=s, id="stops", get_position=["lon", "lat"], get_fill_color="color",
                  get_line_color=[255, 255, 255, 230], stroked=True, line_width_min_pixels=1,
                  get_radius=40, radius_min_pixels=5, radius_max_pixels=10, pickable=True),
    ]
    if highlight is not None and (s["stop_id"] == highlight).any():
        layers.append(pdk.Layer("ScatterplotLayer", data=s[s["stop_id"] == highlight], get_position=["lon", "lat"],
                                get_fill_color=[0, 0, 0, 0], get_line_color=[20, 20, 20, 255], stroked=True,
                                filled=False, line_width_min_pixels=3, get_radius=60, radius_min_pixels=12))
    view = pdk.data_utils.compute_view(path or s[["lon", "lat"]].values.tolist())
    deck = pdk.Deck(layers=layers, initial_view_state=view, **BASEMAP,
                    tooltip={"html": "<b>{stop_name}</b><br/>{summary}"})
    if clickable:
        return st.pydeck_chart(deck, height=height, on_select="rerun", selection_mode="single-object", key=key)
    st.pydeck_chart(deck, height=height)
    return None


def delay_legend() -> None:
    st.caption("Stop colour = typical delay: **blue** early · **grey** on time · **red** late (10+ min darkest). "
               "Faint dots: no buses seen there yet.")


def page_style() -> None:
    st.markdown("""<style>
        [data-testid="stMainBlockContainer"] { max-width: 1100px; padding-top: 2.5rem; }
        .answer { font-size: 1.35rem; line-height: 1.35; font-weight: 600; margin: 0 0 .25rem; }
        .shares { font-size: 1.05rem; font-weight: 600; margin: 0 0 .5rem; }
    </style>""", unsafe_allow_html=True)
