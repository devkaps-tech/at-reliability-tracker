"""Live map: vehicles on the road right now, coloured by how late they are running."""

import time

import pandas as pd
import pydeck as pdk
import streamlit as st

from common import BASEMAP, CFG, delay_rgb, delay_text, latest_version, query, route_catalog, shape_path
from tracker.config import project_path
from tracker.live import latest_raw_file, read_feed, vehicles_now

st.title("Live: where's my bus now")
st.caption("Every vehicle on a trip, from the collector's latest poll. Refreshes every minute. "
           "Tap a dot for details.")

catalog = route_catalog()
routes = catalog.drop_duplicates("route_id")
labels = dict(zip(routes["route_id"], routes["label"]))
directions = {(r.route_id, r.direction_id): r.direction for r in catalog.itertuples()}
choice = st.selectbox("Show", ["All routes"] + routes["route_id"].tolist(),
                      format_func=lambda r: r if r == "All routes" else labels.get(r, r))


@st.cache_data(ttl=3600, show_spinner=False)
def stop_names() -> dict:
    df = query("SELECT stop_id, stop_name FROM gtfs_stops WHERE gtfs_version = ?", (latest_version(),))
    return dict(zip(df["stop_id"], df["stop_name"]))


@st.fragment(run_every="60s")
def live_view():
    path = latest_raw_file(project_path(CFG["collector"]["raw_dir"]))
    if path is None:
        st.info("No live data yet. Start the collector: ./run_local.sh")
        return
    polled_at, vehicles = vehicles_now(read_feed(path))
    age_min = (time.time() - polled_at) / 60
    when = pd.Timestamp(polled_at, unit="s", tz="UTC").tz_convert("Pacific/Auckland")
    if age_min > 10:
        st.warning(f"The latest poll is {age_min:.0f} minutes old ({when:%-d %b %H:%M}). Is the collector running?")
    else:
        st.caption(f"Data from {when:%H:%M:%S} ({age_min:.0f} min ago).")

    v = pd.DataFrame(vehicles)
    if v.empty:
        st.info("No vehicles on a trip in the latest poll.")
        return
    if choice != "All routes":
        v = v[v["route_id"] == choice]
        if v.empty:
            st.info(f"No {labels.get(choice, choice)} vehicles on the road right now.")

    names = stop_names()
    v["route"] = v["route_id"].map(labels).fillna(v["route_id"])
    v["towards"] = [directions.get((r, d), "") for r, d in zip(v["route_id"], v["direction_id"])]
    v["delay_min"] = v["delay_s"] / 60
    v["status"] = ["no recent update" if pd.isna(s) else delay_text(s) for s in v["delay_s"]]
    v["last_stop"] = v["last_stop_id"].map(names).fillna("")
    v["color"] = [delay_rgb(m) for m in v["delay_min"]]
    v["detail"] = [
        f"{t}<br/>{s}" + (f"<br/>Last stop: {ls}" if ls else "") + (f"<br/>{o}" if o else "")
        for t, s, ls, o in zip(v["towards"], v["status"], v["last_stop"], v["occupancy"])]

    known = v["delay_s"].notna()
    k = st.columns(4)
    k[0].metric("On the road", f"{len(v):,}")
    k[1].metric("On time", f"{((v['delay_s'] >= -60) & (v['delay_s'] <= 300)).sum() / max(known.sum(), 1):.0%}")
    k[2].metric("Late (5+ min)", f"{(v['delay_s'] > 300).sum():,}")
    k[3].metric("Early (1+ min)", f"{(v['delay_s'] < -60).sum():,}")

    layers = []
    if choice != "All routes":
        for shape_id in catalog.loc[catalog["route_id"] == choice, "shape_id"]:
            layers.append(pdk.Layer("PathLayer", data=[{"path": shape_path(shape_id)}], get_path="path",
                                    get_color=[90, 110, 120, 150], width_min_pixels=3))
    layers.append(pdk.Layer("ScatterplotLayer", data=v, get_position=["lon", "lat"], get_fill_color="color",
                            get_line_color=[255, 255, 255, 230], stroked=True, line_width_min_pixels=1,
                            get_radius=60, radius_min_pixels=4 if choice == "All routes" else 7,
                            radius_max_pixels=12, pickable=True))
    view = pdk.data_utils.compute_view(v[["lon", "lat"]].values.tolist()) if len(v) else \
        pdk.ViewState(latitude=-36.85, longitude=174.76, zoom=10)
    st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=view, **BASEMAP,
                             tooltip={"html": "<b>{route}</b><br/>{detail}<br/>Vehicle {vehicle}"}), height=520)
    st.caption("Dot colour: **blue** early · **grey** on time · **red** late (10+ min darkest). "
               "Faint dots: no recent delay update from AT.")

    if choice != "All routes" and len(v):
        st.dataframe(pd.DataFrame({
            "Towards": v["towards"], "Running": v["status"], "Last stop": v["last_stop"],
            "How full": v["occupancy"], "Vehicle": v["vehicle"],
        }).sort_values("Towards"), hide_index=True, width="stretch")


live_view()
