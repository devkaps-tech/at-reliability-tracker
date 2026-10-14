"""Route report: where along a route delays build up, when it is worst, and which trips didn't run."""

import altair as alt
import pandas as pd
import streamlit as st

from common import (LATE_S, PERIODS, delay_legend, delay_text, period_start, query, route_catalog,
                    route_map, route_observations, shape_path, pattern_stops, share_chart, stop_stats,
                    stop_if_no_data)

MIN_PER_STOP = 5  # stops with fewer buses are left out of the "where delay builds up" sentence

stop_if_no_data()
st.title("Route report")
st.caption("Where along the route delays build up, when it runs worst, and how many trips didn't run.")

catalog = route_catalog()
routes = catalog.drop_duplicates("route_id")
route_ids = routes["route_id"].tolist()
labels = dict(zip(routes["route_id"], routes["label"]))
if "route_r" not in st.session_state:
    st.session_state.route_r = next(r for r in (st.query_params.get("route"), st.session_state.get("chosen_route"),
                                                "70-205", route_ids[0]) if r in route_ids)
route_id = st.selectbox("Route (type to search)", route_ids, key="route_r", format_func=labels.get)
st.session_state.chosen_route = route_id
st.query_params.update({"route": route_id})

dirs = catalog[catalog["route_id"] == route_id].sort_values("direction_id")
c1, c2 = st.columns([2, 1])
direction_id = c1.radio("Direction", dirs["direction_id"].tolist(), horizontal=True,
                        format_func=dict(zip(dirs["direction_id"], dirs["direction"])).get)
period_label = c2.selectbox("Period", list(PERIODS), index=1)
start = period_start(period_label)
pattern = dirs[dirs["direction_id"] == direction_id].iloc[0]

obs = route_observations(route_id, direction_id, start)
stops = pattern_stops(pattern["trip_id"]).merge(stop_stats(obs), on="stop_id", how="left")
trips = query("""SELECT sum(planned_trips) AS planned, sum(seen_trips) AS seen, sum(cancelled_trips) AS cancelled,
                        sum(missing_trips) AS missing
                 FROM route_trips WHERE route_id = ? AND service_date >= ?""", (route_id, start)).iloc[0]

# ---- Headline ----
st.markdown(f"**{labels[route_id]}** · {pattern['direction']} · run by {pattern['operator']} · "
            f"{len(stops)} stops")
if obs.empty:
    st.info("No buses seen on this route in this direction yet. Try another period or direction.")
    route_map(shape_path(pattern["shape_id"]), stops)
    st.stop()

k = st.columns(4)
k[0].metric("Stops on time", f"{(obs['status'] == 'on_time').mean():.0%}")
k[1].metric("Late (5+ min)", f"{(obs['status'] == 'late').mean():.0%}")
k[2].metric("Typical delay", delay_text(obs["delay_s"].median()))
k[3].metric("Trips cancelled", f"{int(trips['cancelled'] or 0)} of {int(trips['planned'] or 0)}",
            help="Both directions. Planned trips that were never seen in the live feed are shown below.")

# ---- Map + where delay builds up ----
st.subheader("Where the delay builds up")
route_map(shape_path(pattern["shape_id"]), stops)
delay_legend()

along = stops.dropna(subset=["buses"])
along = along[along["buses"] >= 1]
if len(along) >= 2:
    band = alt.Chart(along).mark_area(opacity=0.18, color="#a8a7a0").encode(
        x=alt.X("stop_number:Q", title="Stop number along the route", scale=alt.Scale(domain=[1, len(stops)])),
        y=alt.Y("p25_min:Q", title="Minutes late (minus = early)"), y2="p75_min:Q")
    line = alt.Chart(along).mark_line(point=alt.OverlayMarkDef(size=45, filled=True), strokeWidth=2,
                                      color="#4b6d7f").encode(
        x="stop_number:Q", y="median_min:Q",
        tooltip=[alt.Tooltip("stop_name:N", title="Stop"), alt.Tooltip("stop_number:Q", title="Stop no."),
                 alt.Tooltip("median_min:Q", title="Typical (min)", format="+.1f"),
                 alt.Tooltip("p25_min:Q", title="Middle half from", format="+.1f"),
                 alt.Tooltip("p75_min:Q", title="Middle half to", format="+.1f"),
                 alt.Tooltip("buses:Q", title="Buses seen")])
    rules = alt.Chart(pd.DataFrame({"y": [0, LATE_S / 60], "what": ["timetable", "5 min late"]})).mark_rule(
        strokeDash=[4, 4]).encode(y="y:Q", color=alt.Color("what:N", scale=alt.Scale(
            domain=["timetable", "5 min late"], range=["#8a8a85", "#d03b3b"]), legend=alt.Legend(orient="top",
                                                                                                    title=None)))
    st.altair_chart((band + rules + line).properties(height=260), width="stretch")
    st.caption("Line = typical delay at each stop. Shaded band = the middle half of buses. "
               "Gaps are stops with no buses seen yet.")

    # One plain sentence: the stretch where the typical delay grows the most.
    solid = along[along["buses"] >= MIN_PER_STOP].sort_values("stop_number")
    if len(solid) >= 3:
        jumps = solid.assign(prev_name=solid["stop_name"].shift(), prev_min=solid["median_min"].shift())
        jumps["rise"] = jumps["median_min"] - jumps["prev_min"]
        worst = jumps.loc[jumps["rise"].idxmax()]
        if worst["rise"] >= 1:
            st.markdown(f"Buses lose the most time between **{worst['prev_name']}** and **{worst['stop_name']}**: "
                        f"typically {delay_text(worst['prev_min'] * 60)} at the first, "
                        f"{delay_text(worst['median_min'] * 60)} at the second.")

# ---- When ----
left, right = st.columns(2)
with left:
    st.subheader("By time of day")
    st.altair_chart(share_chart(obs, "hour", "Timetabled hour"), width="stretch")
with right:
    st.subheader("By day of the week")
    if obs["service_date"].dt.dayofweek.nunique() >= 2:
        st.altair_chart(share_chart(obs, "weekday", "Day"), width="stretch")
    else:
        st.write("Needs at least two different days of data.")

# ---- Worst stops ----
st.subheader("Stops where it's most often late")
worst_stops = along[along["buses"] >= MIN_PER_STOP].sort_values(["late_share", "median_min"], ascending=False).head(8)
if worst_stops.empty:
    st.write(f"Needs at least {MIN_PER_STOP} buses seen at a stop.")
else:
    st.dataframe(pd.DataFrame({
        "Stop": worst_stops["stop_name"], "Buses seen": worst_stops["buses"].astype(int),
        "Late": worst_stops["late_share"].map("{:.0%}".format),
        "On time": worst_stops["on_time_share"].map("{:.0%}".format),
        "Typical": [delay_text(m * 60) for m in worst_stops["median_min"]],
    }), hide_index=True, width="stretch")

# ---- Trips that didn't run ----
st.subheader("Trips that didn't run (both directions)")
daily = query("""SELECT service_date AS Day, sum(planned_trips) AS Planned, sum(seen_trips) AS Seen,
                        sum(cancelled_trips) AS Cancelled, sum(missing_trips) AS "Never seen"
                 FROM route_trips WHERE route_id = ? AND service_date >= ? GROUP BY 1 ORDER BY 1 DESC""",
              (route_id, start))
daily["Day"] = daily["Day"].dt.strftime("%a %-d %b")
st.dataframe(daily, hide_index=True, width="stretch")
st.caption("“Never seen” trips may have run without tracking, or the collector may have been offline. "
           "Check Pipeline health for missed polls before reading too much into them.")
