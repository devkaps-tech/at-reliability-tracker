"""City overview: the whole network, by day, mode, route and operator."""

from datetime import timedelta

import altair as alt
import streamlit as st

from common import CFG, query, route_labels, stop_if_no_data

MODE_COLOURS = alt.Scale(domain=["bus", "train", "ferry"], range=["#2a78d6", "#eb6834", "#1baf7a"])
SEQUENTIAL_BLUE = ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]
MIN_STOPS = 30  # ignore routes with too few scored stops to say anything

stop_if_no_data()
punct = CFG["punctuality"]
st.title("Did Auckland's buses, trains and ferries run on time?")
st.caption(f"On time = from {punct['early_if_more_than_seconds_early'] // 60} min early to "
           f"{punct['late_if_more_than_seconds_late'] // 60} min late, at every stop we saw. Auckland time. "
           "Source: Auckland Transport timetable and live feed.")

days = query("SELECT min(service_date) AS first, max(service_date) AS last FROM latest_runs")
first, last = days["first"][0].date(), days["last"][0].date()
c1, c2 = st.columns([2, 1])
date_range = c1.date_input("Days", (max(first, last - timedelta(days=6)), last), min_value=first, max_value=last)
start, end = date_range if len(date_range) == 2 else (date_range[0], date_range[0])
modes = c2.multiselect("Mode", ["bus", "train", "ferry"], default=["bus", "train", "ferry"])
if not modes:
    st.stop()
where = f"service_date BETWEEN ? AND ? AND mode IN ({', '.join(repr(m) for m in modes)})"
labels = route_labels()

# ---- Headline numbers ----
head = query(f"""SELECT sum(on_time) / sum(stops_scored) AS on_time, sum(late) / sum(stops_scored) AS late,
                        sum(early) / sum(stops_scored) AS early, sum(total_delay_s) / sum(stops_scored) AS avg_delay
                 FROM route_metrics WHERE {where}""", (start, end))
trips = query(f"""SELECT sum(planned_trips) AS planned, sum(cancelled_trips) AS cancelled,
                         sum(missing_trips) AS missing FROM route_trips WHERE {where}""", (start, end))
k = st.columns(4)
k[0].metric("On time", f"{head['on_time'][0]:.0%}")
k[1].metric("Late (5+ min)", f"{head['late'][0]:.0%}")
k[2].metric("Early (1+ min)", f"{head['early'][0]:.0%}")
k[3].metric("Trips cancelled", f"{int(trips['cancelled'][0] or 0):,}",
            help=f"Out of {int(trips['planned'][0] or 0):,} planned. A further {int(trips['missing'][0] or 0):,} "
                 "were never seen in the live feed; some may have run untracked or while the collector was off.")

# ---- Trend ----
st.subheader("On-time rate by day")
trend = query(f"""SELECT service_date, mode, sum(on_time) / sum(stops_scored) AS on_time_rate,
                         sum(stops_scored) AS stops
                  FROM route_metrics WHERE {where} GROUP BY ALL ORDER BY 1""", (start, end))
base = alt.Chart(trend).encode(
    x=alt.X("service_date:T", title=None),
    y=alt.Y("on_time_rate:Q", title="Stops on time", axis=alt.Axis(format="%")),
    color=alt.Color("mode:N", scale=MODE_COLOURS, title="Mode", legend=alt.Legend(orient="top")))
hover = alt.selection_point(fields=["service_date"], nearest=True, on="pointerover", empty=False)
st.altair_chart(
    (base.mark_line(strokeWidth=2) + base.mark_point(size=60, filled=True).encode(
        opacity=alt.condition(hover, alt.value(1), alt.value(0.35)),
        tooltip=[alt.Tooltip("service_date:T", title="Day"), alt.Tooltip("mode:N", title="Mode"),
                 alt.Tooltip("on_time_rate:Q", title="On time", format=".1%"),
                 alt.Tooltip("stops:Q", title="Stops scored", format=",")]).add_params(hover)).properties(height=240),
    width="stretch")

# ---- Least punctual routes ----
st.subheader("10 least punctual routes")
worst = query(f"""SELECT route_id, mode, any_value(operator) AS operator,
                         sum(on_time) / sum(stops_scored) AS on_time_rate, sum(late) / sum(stops_scored) AS late_rate,
                         sum(total_delay_s) / sum(stops_scored) / 60 AS avg_delay_min, sum(stops_scored) AS stops
                  FROM route_metrics WHERE {where} GROUP BY route_id, mode
                  HAVING sum(stops_scored) >= {MIN_STOPS} ORDER BY on_time_rate LIMIT 10""", (start, end))
worst["route"] = worst["route_id"].map(labels).fillna(worst["route_id"])
st.altair_chart(alt.Chart(worst).mark_bar(cornerRadiusEnd=4, height=16).encode(
    x=alt.X("on_time_rate:Q", title="Stops on time", axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0, 1])),
    y=alt.Y("route:N", sort="x", title=None, axis=alt.Axis(labelLimit=320)),
    color=alt.Color("mode:N", scale=MODE_COLOURS, title="Mode", legend=None),
    tooltip=[alt.Tooltip("route:N", title="Route"), alt.Tooltip("operator:N", title="Operator"),
             alt.Tooltip("on_time_rate:Q", title="On time", format=".0%"),
             alt.Tooltip("late_rate:Q", title="Late", format=".0%"),
             alt.Tooltip("avg_delay_min:Q", title="Average delay (min)", format="+.1f"),
             alt.Tooltip("stops:Q", title="Stops scored", format=",")]).properties(height=36 * max(len(worst), 1)),
    width="stretch")
st.caption(f"Routes with at least {MIN_STOPS} scored stops in the period. Open **Route report** to see where along "
           "a route the delay builds up.")

# ---- Route × hour ----
st.subheader("When it goes wrong: route × hour")
heat = query(f"""WITH busiest AS (SELECT route_id FROM route_metrics WHERE {where}
                                  GROUP BY 1 ORDER BY sum(stops_scored) DESC LIMIT 20)
                 SELECT route_id, hour, 1 - sum(on_time) / sum(stops_scored) AS off_rate, sum(stops_scored) AS stops
                 FROM route_metrics WHERE {where} AND route_id IN (SELECT * FROM busiest) GROUP BY ALL""",
             (start, end, start, end))
heat["route"] = heat["route_id"].map(labels).fillna(heat["route_id"])
heat["hour_label"] = heat["hour"].map(lambda h: f"{12 if h % 12 == 0 else h % 12}{'a' if h < 12 else 'p'}")
heat["sort"] = heat["hour"].where(heat["hour"] >= 4, heat["hour"] + 24)
hours = heat.sort_values("sort")["hour_label"].drop_duplicates().tolist()
st.altair_chart(alt.Chart(heat).mark_rect(stroke="white", strokeWidth=1).encode(
    x=alt.X("hour_label:O", sort=hours, title="Timetabled hour", axis=alt.Axis(labelAngle=0)),
    y=alt.Y("route:N", title=None, sort=alt.EncodingSortField("off_rate", op="mean", order="descending"),
            axis=alt.Axis(labelLimit=300)),
    color=alt.Color("off_rate:Q", title="Not on time", scale=alt.Scale(range=SEQUENTIAL_BLUE, domain=[0, 1]),
                    legend=alt.Legend(format="%", orient="top")),
    tooltip=[alt.Tooltip("route:N", title="Route"), alt.Tooltip("hour_label:O", title="Hour"),
             alt.Tooltip("off_rate:Q", title="Not on time", format=".0%"),
             alt.Tooltip("stops:Q", title="Stops scored", format=",")]).properties(height=22 * heat["route"].nunique()),
    width="stretch")
st.caption("The 20 busiest routes. Darker = a bigger share of stops early or late.")

# ---- By operator ----
st.subheader("By operator")
st.dataframe(query(f"""SELECT operator AS Operator, count(DISTINCT route_id) AS Routes,
                              sum(stops_scored) AS "Stops scored",
                              round(100 * sum(on_time) / sum(stops_scored), 1) AS "On time %",
                              round(100 * sum(late) / sum(stops_scored), 1) AS "Late %",
                              round(sum(total_delay_s) / sum(stops_scored) / 60, 1) AS "Avg delay (min)"
                       FROM route_metrics WHERE {where} GROUP BY 1 ORDER BY "On time %" """, (start, end)),
             hide_index=True, width="stretch")
