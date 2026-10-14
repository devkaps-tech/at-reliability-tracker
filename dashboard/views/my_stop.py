"""My bus & stop: how reliable is one route at one stop, in plain English."""

import pandas as pd
import streamlit as st

from common import (MIN_BUSES, PERIODS, STATUS_LABEL, status_colour, band_options, delay_legend, delay_spread_chart, delay_text, period_start,
                    route_catalog, route_map, route_observations, share_chart, shape_path, pattern_stops, stop_stats,
                    stop_summary, stop_if_no_data)

stop_if_no_data()
st.title("My bus & stop")
st.caption("How reliable is your bus at your stop? Pick it below, or tap a stop on the map. "
           "Bookmark the page to come back to the same stop.")

catalog = route_catalog()
routes = catalog.drop_duplicates("route_id")
route_ids = routes["route_id"].tolist()
labels = dict(zip(routes["route_id"], routes["label"]))
qp = st.query_params

# ---- Pick route, direction, stop (remembered in the page address so it can be bookmarked) ----
if "route" not in st.session_state:  # first visit, or coming back from another page
    st.session_state.route = next(r for r in (qp.get("route"), st.session_state.get("chosen_route"), "70-205",
                                              route_ids[0]) if r in route_ids)
route_id = st.selectbox("Bus, train or ferry (type to search)", route_ids, key="route", format_func=labels.get)
st.session_state.chosen_route = route_id  # shared with the Route report page

dirs = catalog[catalog["route_id"] == route_id].sort_values("direction_id")
dir_ids = dirs["direction_id"].tolist()
dir_names = dict(zip(dirs["direction_id"], dirs["direction"]))
if st.session_state.get("dir") not in dir_ids:
    st.session_state.dir = int(qp["dir"]) if qp.get("dir", "").isdigit() and int(qp["dir"]) in dir_ids else dir_ids[0]
direction_id = st.radio("Direction", dir_ids, key="dir", format_func=dir_names.get, horizontal=True)
pattern = dirs[dirs["direction_id"] == direction_id].iloc[0]
stops = pattern_stops(pattern["trip_id"])

c1, c2 = st.columns(2)
bands = band_options()
band_label = c1.selectbox("Time of day", list(bands), key="band")
period_label = c2.selectbox("Period", list(PERIODS), index=1, key="period")
obs = route_observations(route_id, direction_id, period_start(period_label))
if bands[band_label]:
    obs = obs[obs["time_band"] == bands[band_label]]
stats = stop_stats(obs)
stops = stops.merge(stats, on="stop_id", how="left")

stop_ids = stops["stop_id"].drop_duplicates().tolist()
stop_names = {r.stop_id: f"{r.stop_name} (stop {r.stop_code})" for r in stops.itertuples()}
# A stop tapped on the map wins; otherwise the bookmarked stop; otherwise the stop with the most data.
picked = (st.session_state.get("stop_map") or {}).get("selection", {}).get("objects", {}).get("stops", [])
if picked and picked[0].get("stop_id") in stop_ids and picked[0]["stop_id"] != st.session_state.get("last_pick"):
    st.session_state.stop = st.session_state.last_pick = picked[0]["stop_id"]
if st.session_state.get("stop") not in stop_ids:
    busiest = stops.sort_values("buses", ascending=False)["stop_id"].iloc[0] if stats["buses"].sum() else stop_ids[0]
    st.session_state.stop = qp.get("stop") if qp.get("stop") in stop_ids else busiest
stop_id = st.selectbox("Your stop", stop_ids, key="stop", format_func=stop_names.get)
st.query_params.update({"route": route_id, "dir": str(direction_id), "stop": stop_id})

# ---- The answer ----
here = obs[obs["stop_id"] == stop_id]
summary = stop_summary(here["delay_s"].tolist())
stop_name = stops.loc[stops["stop_id"] == stop_id, "stop_name"].iloc[0]
who = f"{pattern['route_short_name']} to {pattern['direction'].split(' to ')[-1].split(' via ')[0]}"
when = "" if bands[band_label] is None else f" in the {band_label.split(' (')[0].lower()}"

with st.container(border=True):
    if summary is None:
        st.markdown(f"<p class='answer'>No {who} buses seen at {stop_name}{when} yet.</p>", unsafe_allow_html=True)
        st.write("The collector catches each trip at a few stops per poll, so a single stop needs a few days of data. "
                 "Try **All data**, **Any time**, or a stop shown in colour on the map below.")
    else:
        st.markdown(f"<p class='answer'>The {who} at {stop_name}{when} is <b>{summary['typical']}</b>.</p>",
                    unsafe_allow_html=True)
        st.markdown(
            f"<p class='shares'><span style='color:#0ca30c'>{summary['on_time']:.0%} on time</span> · "
            f"<span style='color:#d03b3b'>{summary['late']:.0%} late</span> (5+ min) · "
            f"<span style='color:#2a78d6'>{summary['early']:.0%} early</span> (1+ min)</p>", unsafe_allow_html=True)
        st.markdown(f"- {summary['catch']}\n- {summary['worst']}")
        first, last = here["service_date"].min().date(), here["service_date"].max().date()
        span = f"on {first:%-d %b}" if first == last else f"between {first:%-d %b} and {last:%-d %b}"
        note = f"Based on {summary['n']} buses seen {span}."
        if summary["n"] < MIN_BUSES:
            st.warning(note + " That's not many yet, so treat this as a first impression.")
        else:
            st.caption(note)

# ---- Detail ----
if summary is not None:
    left, right = st.columns(2)
    with left:
        st.subheader("How late, bus by bus")
        st.altair_chart(delay_spread_chart(here), width="stretch")
    with right:
        st.subheader("By time of day")
        st.altair_chart(share_chart(here, "hour", "Timetabled hour"), width="stretch")
    if here["service_date"].dt.dayofweek.nunique() >= 3:
        st.subheader("By day of the week")
        st.altair_chart(share_chart(here, "weekday", "Day"), width="stretch")

st.subheader("On the map")
route_map(shape_path(pattern["shape_id"]), stops, highlight=stop_id, key="stop_map", clickable=True)
delay_legend()

if summary is not None:
    st.subheader("Last 20 buses at this stop")
    recent = here.sort_values("actual_time", ascending=False).head(20)
    st.dataframe(pd.DataFrame({
        "Day": recent["service_date"].dt.strftime("%a %-d %b"),
        "Timetable": recent["scheduled_time"].dt.strftime("%H:%M"),
        "Arrived": recent["actual_time"].dt.strftime("%H:%M:%S"),
        "Delay": [delay_text(s) for s in recent["delay_s"]],
        "Status": recent["status"].map(STATUS_LABEL),
    }).style.map(status_colour, subset=["Status"]), hide_index=True, width="stretch", height=35 * (len(recent) + 1) + 3)
    st.caption("Times are when the vehicle reached the stop, from AT's live feed, compared with the timetable.")
