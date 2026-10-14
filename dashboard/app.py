"""Dashboard: how reliable are Auckland's buses, trains and ferries, for one rider and for the whole city.

Run with:  .venv/bin/streamlit run dashboard/app.py
"""

import streamlit as st

from common import page_style

st.set_page_config(page_title="Auckland transport punctuality", page_icon=":material/directions_bus:", layout="wide")
page_style()

st.navigation([
    st.Page("views/my_stop.py", title="My bus & stop", icon=":material/directions_bus:", default=True),
    st.Page("views/route_report.py", title="Route report", icon=":material/route:"),
    st.Page("views/live.py", title="Live map", icon=":material/my_location:"),
    st.Page("views/overview.py", title="City overview", icon=":material/insights:"),
    st.Page("views/health.py", title="Pipeline health", icon=":material/monitor_heart:"),
]).run()
