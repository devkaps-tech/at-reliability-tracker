"""Dashboard: how punctual were Auckland's buses, trains and ferries?

Reads only the published scores (the newest run per day that passed its quality checks).
Run with:  .venv/bin/streamlit run dashboard/app.py
"""

import sys
from datetime import timedelta
from pathlib import Path

import altair as alt
import duckdb
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tracker.config import load_config, project_path  # noqa: E402

# Each mode keeps its own colour everywhere, so filtering never repaints a line.
MODE_COLOURS = alt.Scale(domain=["bus", "train", "ferry"], range=["#2a78d6", "#eb6834", "#1baf7a"])
SEQUENTIAL_BLUE = ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]
MIN_STOPS = 30  # ignore routes with too few scored stops to say anything

st.set_page_config(page_title="Auckland transport punctuality", layout="wide")


@st.cache_data(ttl=600)
def query(sql: str, params: tuple = ()):
    path = project_path(load_config()["storage"]["warehouse"])
    with duckdb.connect(str(path), read_only=True) as con:
        con.execute("SET TimeZone = 'Pacific/Auckland'")
        return con.execute(sql, list(params)).df()


try:
    days = query("SELECT min(service_date) AS first, max(service_date) AS last FROM latest_runs")
except duckdb.Error as err:
    st.error(f"Can't open the warehouse yet ({err}). Run the pipeline first: python -m tracker.pipeline run")
    st.stop()
if days["last"].isna().all():
    st.info("No published days yet. Run: python -m tracker.pipeline run --date YYYY-MM-DD")
    st.stop()

punct = load_config()["punctuality"]
st.title("Did Auckland's buses, trains and ferries run on time?")
st.caption(f"On time = from {punct['early_if_more_than_seconds_early'] // 60} min early to "
           f"{punct['late_if_more_than_seconds_late'] // 60} min late, measured at every stop we saw. "
           "Times are Auckland time. Source: Auckland Transport GTFS + realtime feed.")

# ---- Filters, one row ----
first, last = days["first"][0].date(), days["last"][0].date()
c1, c2 = st.columns([2, 1])
date_range = c1.date_input("Days", (max(first, last - timedelta(days=6)), last), min_value=first, max_value=last)
start, end = (date_range if len(date_range) == 2 else (date_range[0], date_range[0]))
modes = c2.multiselect("Mode", ["bus", "train", "ferry"], default=["bus", "train", "ferry"])
if not modes:
    st.stop()
mode_list = ", ".join(f"'{m}'" for m in modes)
where = f"service_date BETWEEN ? AND ? AND mode IN ({mode_list})"

# ---- Headline numbers ----
head = query(f"""SELECT sum(on_time) / sum(stops_scored) AS on_time, sum(late) / sum(stops_scored) AS late,
                        sum(early) / sum(stops_scored) AS early, sum(total_delay_s) / sum(stops_scored) AS avg_delay,
                        sum(stops_scored) AS stops
                 FROM route_metrics WHERE {where}""", (start, end))
trips = query(f"""SELECT sum(planned_trips) AS planned, sum(cancelled_trips) AS cancelled, sum(missing_trips) AS missing
                  FROM route_trips WHERE {where}""", (start, end))
k = st.columns(5)
k[0].metric("On time", f"{head['on_time'][0]:.1%}")
k[1].metric("Late (> 5 min)", f"{head['late'][0]:.1%}")
k[2].metric("Early (> 1 min)", f"{head['early'][0]:.1%}")
k[3].metric("Average delay", f"{head['avg_delay'][0] / 60:+.1f} min")
k[4].metric("Trips cancelled / never seen", f"{int(trips['cancelled'][0] or 0):,} / {int(trips['missing'][0] or 0):,}",
            help=f"Out of {int(trips['planned'][0] or 0):,} planned trips. 'Never seen' trips may have run "
                 "without tracking, or the collector may have been offline — see Pipeline health.")

# ---- Trend ----
st.subheader("On-time rate by day")
trend = query(f"""SELECT service_date, mode, sum(on_time) / sum(stops_scored) AS on_time_rate, sum(stops_scored) AS stops
                  FROM route_metrics WHERE {where} GROUP BY ALL ORDER BY 1""", (start, end))
base = alt.Chart(trend).encode(
    x=alt.X("service_date:T", title=None),
    y=alt.Y("on_time_rate:Q", title="Stops on time", axis=alt.Axis(format="%")),
    color=alt.Color("mode:N", scale=MODE_COLOURS, title="Mode"))
hover = alt.selection_point(fields=["service_date"], nearest=True, on="pointerover", empty=False)
st.altair_chart(
    base.mark_line(strokeWidth=2)
    + base.mark_point(size=60, filled=True).encode(
        opacity=alt.condition(hover, alt.value(1), alt.value(0)),
        tooltip=[alt.Tooltip("service_date:T", title="Day"), alt.Tooltip("mode:N", title="Mode"),
                 alt.Tooltip("on_time_rate:Q", title="On time", format=".1%"),
                 alt.Tooltip("stops:Q", title="Stops scored", format=",")]).add_params(hover),
    width="stretch")

# ---- Worst routes + heat map ----
left, right = st.columns(2)
worst = query(f"""SELECT route_short_name AS route, mode, any_value(operator) AS operator,
                         sum(on_time) / sum(stops_scored) AS on_time_rate, sum(late) / sum(stops_scored) AS late_rate,
                         sum(total_delay_s) / sum(stops_scored) / 60 AS avg_delay_min, sum(stops_scored) AS stops
                  FROM route_metrics WHERE {where} GROUP BY route, mode
                  HAVING sum(stops_scored) >= {MIN_STOPS} ORDER BY on_time_rate LIMIT 10""", (start, end))
with left:
    st.subheader("10 least punctual routes")
    st.altair_chart(alt.Chart(worst).mark_bar(cornerRadiusEnd=4, height=14).encode(
        x=alt.X("on_time_rate:Q", title="Stops on time", axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0, 1])),
        y=alt.Y("route:N", sort="x", title=None),
        color=alt.Color("mode:N", scale=MODE_COLOURS, title="Mode"),
        tooltip=[alt.Tooltip("route:N", title="Route"), alt.Tooltip("operator:N", title="Operator"),
                 alt.Tooltip("on_time_rate:Q", title="On time", format=".1%"),
                 alt.Tooltip("late_rate:Q", title="Late", format=".1%"),
                 alt.Tooltip("avg_delay_min:Q", title="Avg delay (min)", format="+.1f"),
                 alt.Tooltip("stops:Q", title="Stops scored", format=",")]),
        width="stretch")
    st.caption(f"Routes with at least {MIN_STOPS} scored stops in the period.")

heat = query(f"""WITH busiest AS (SELECT route_short_name FROM route_metrics WHERE {where}
                                  GROUP BY 1 ORDER BY sum(stops_scored) DESC LIMIT 25)
                 SELECT route_short_name AS route, hour, 1 - sum(on_time) / sum(stops_scored) AS off_rate,
                        sum(stops_scored) AS stops
                 FROM route_metrics WHERE {where} AND route_short_name IN (SELECT * FROM busiest)
                 GROUP BY ALL""", (start, end, start, end))
with right:
    st.subheader("When it goes wrong: route × hour")
    st.altair_chart(alt.Chart(heat).mark_rect(stroke="white", strokeWidth=1).encode(
        x=alt.X("hour:O", title="Scheduled hour"),
        y=alt.Y("route:N", title=None, sort=alt.EncodingSortField("off_rate", op="mean", order="descending")),
        color=alt.Color("off_rate:Q", title="Not on time", scale=alt.Scale(range=SEQUENTIAL_BLUE, domain=[0, 1]),
                        legend=alt.Legend(format="%")),
        tooltip=[alt.Tooltip("route:N", title="Route"), alt.Tooltip("hour:O", title="Hour"),
                 alt.Tooltip("off_rate:Q", title="Not on time", format=".0%"),
                 alt.Tooltip("stops:Q", title="Stops scored", format=",")]),
        width="stretch")
    st.caption("The 25 busiest routes. Darker = a bigger share of stops early or late.")

# ---- By operator ----
st.subheader("By operator")
st.dataframe(query(f"""SELECT operator AS Operator, count(DISTINCT route_id) AS Routes, sum(stops_scored) AS "Stops scored",
                              round(100 * sum(on_time) / sum(stops_scored), 1) AS "On time %",
                              round(100 * sum(late) / sum(stops_scored), 1) AS "Late %",
                              round(sum(total_delay_s) / sum(stops_scored) / 60, 1) AS "Avg delay (min)"
                       FROM route_metrics WHERE {where} GROUP BY 1 ORDER BY "On time %" """, (start, end)),
             hide_index=True, width="stretch")

# ---- Pipeline health ----
with st.expander("Pipeline health: polls received and quality checks"):
    st.dataframe(query("""SELECT r.service_date AS Day, r.polls_received AS "Polls received",
                                 r.polls_expected AS "Polls expected", r.stop_observations AS "Stops observed",
                                 r.gtfs_version AS Timetable, r.published AS Published,
                                 string_agg(CASE WHEN NOT q.passed THEN q.check_name END, ', ') AS "Checks failed"
                          FROM pipeline_runs r LEFT JOIN quality_checks q USING (run_id)
                          WHERE r.run_id IN (SELECT run_id FROM pipeline_runs
                                             QUALIFY row_number() OVER (PARTITION BY service_date
                                                                        ORDER BY computed_at DESC) = 1)
                          GROUP BY ALL ORDER BY Day DESC"""), hide_index=True, width="stretch")
    st.caption("Polls expected assumes the collector ran all day. Gaps mean the computer was off or asleep.")
