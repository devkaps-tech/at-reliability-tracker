"""Pipeline health: did the collector get its polls, and did each day pass its quality checks?"""

import altair as alt
import streamlit as st

from common import query

st.title("Pipeline health")
st.caption("One row per scored day (newest run). A day is published only if its blocking checks pass.")

runs = query("""
    SELECT r.service_date AS day, r.polls_received, r.polls_expected, r.stop_observations, r.gtfs_version,
           r.published, r.computed_at,
           string_agg(CASE WHEN NOT q.passed THEN q.check_name || ' (' || q.severity || ')' END, ', ') AS failed
    FROM pipeline_runs r LEFT JOIN quality_checks q USING (run_id)
    WHERE r.run_id IN (SELECT run_id FROM pipeline_runs
                       QUALIFY row_number() OVER (PARTITION BY service_date ORDER BY computed_at DESC) = 1)
    GROUP BY ALL ORDER BY day DESC""")
if runs.empty:
    st.info("No runs yet. Run: python -m tracker.pipeline run --date YYYY-MM-DD")
    st.stop()

runs["coverage"] = runs["polls_received"] / runs["polls_expected"]
st.subheader("Polls received each day")
st.altair_chart(alt.Chart(runs).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3, color="#2a78d6").encode(
    x=alt.X("day:T", title=None, timeUnit="yearmonthdate"),
    y=alt.Y("coverage:Q", title="Share of expected polls", axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0, 1])),
    tooltip=[alt.Tooltip("day:T", title="Day"), alt.Tooltip("polls_received:Q", title="Received"),
             alt.Tooltip("polls_expected:Q", title="Expected"), alt.Tooltip("coverage:Q", format=".0%")],
).properties(height=200), width="stretch")
st.caption("Gaps mean the collector's computer was off or asleep. Below 99% the day is still published, with a "
           "warning.")

st.subheader("Runs and checks")
st.dataframe(runs.assign(day=runs["day"].dt.strftime("%a %-d %b"))[
    ["day", "published", "polls_received", "polls_expected", "stop_observations", "gtfs_version", "failed"]].rename(
    columns={"day": "Day", "published": "Published", "polls_received": "Polls", "polls_expected": "Expected",
             "stop_observations": "Stops observed", "gtfs_version": "Timetable", "failed": "Checks failed"}),
    hide_index=True, width="stretch")
