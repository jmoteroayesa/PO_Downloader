"""SAP PO Message Explorer - local Streamlit UI. Run: streamlit run app.py"""

from datetime import date, datetime, time as dtime
import json
from pathlib import Path

import streamlit as st

import po_explorer
import sap_po_download as po

SETTINGS = Path.home() / ".sap_po_explorer.json"  # never stores the password


def load_settings():
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    try:
        SETTINGS.write_text(json.dumps(settings), encoding="utf-8")
    except OSError:
        pass


def local_aware(day, clock):
    # Interpreted in this PC's timezone, sent to SAP PO with an explicit offset.
    return datetime.combine(day, clock).astimezone()


st.set_page_config(page_title="SAP PO Message Explorer", layout="wide")
st.title("SAP PO Message Explorer")
settings = load_settings()
state = st.session_state

with st.sidebar:
    st.header("Connection")
    url = st.text_input("SAP PO URL", settings.get("url", "https://"))
    username = st.text_input("Username", settings.get("username", ""))
    password = st.text_input("Password", type="password", help="Never saved.")
    ca_file = st.text_input("CA file (optional)", settings.get("ca_file", ""))
    output_dir = st.text_input("Output folder", settings.get("output", "payloads"))
    limit = st.number_input("Max messages", 1, 10000, po_explorer.DEFAULT_LIMIT)
    load = st.button("Load ICO list")


def make_client():
    if not (url.startswith("https://") and username and password):
        st.error("Enter an HTTPS URL, username and password.")
        return None
    endpoint = url.rstrip("/")
    if "/" not in endpoint.split("://", 1)[1]:
        endpoint += "/AdapterMessageMonitoring/ssl?style=document"
    return po.Monitoring(endpoint, username, password, ca_file or None)


if load:
    client = make_client()
    if client:
        try:
            flows = client.list_integration_flows()
            state["flows"] = sorted({(n, i) for n, i in flows if n and i})
            save_settings({"url": url, "username": username, "ca_file": ca_file, "output": output_dir})
        except (po.DownloadError, OSError, ValueError) as exc:
            st.error(f"Could not load ICO list: {exc}")

flows = state.get("flows", [])
if not flows:
    st.info("Enter connection details in the sidebar and click **Load ICO list**.")
    st.stop()

names = [n for n, _ in flows]
duplicates = {n for n in names if names.count(n) > 1}
ico = st.selectbox("ICO name (type to search)", names, index=None)
now = datetime.now()
c1, c2, c3, c4 = st.columns(4)
start_day = c1.date_input("Start date", now.date())
start_time = c2.time_input("Start time", dtime(0, 0))
end_day = c3.date_input("End date", now.date())
end_time = c4.time_input("End time", dtime(23, 59))
text = st.text_input(
    "Search text (optional)",
    help="Case-insensitive literal match in the first logged payload version only. "
         "Content present only in later versions will not be found. Blank = all messages.",
)
st.caption("Only the first logged version of each message is searched and saved.")

if st.button("Search and save", type="primary", disabled=ico is None):
    start, end = local_aware(start_day, start_time), local_aware(end_day, end_time)
    if ico in duplicates:
        st.error("This ICO name is ambiguous in SAP PO.")
    elif end <= start:
        st.error("End must be later than start.")
    else:
        client = make_client()
        if client:
            scenario_id = next(i for n, i in flows if n == ico)
            bar = st.progress(0.0, text="Searching messages...")
            st.button("Stop")  # clicking reruns the script, aborting this loop

            def progress(done, total, outcome):
                bar.progress(done / total, text=f"{done}/{total} checked, {outcome.matched} matched")

            try:
                result = po_explorer.run_search(
                    client, scenario_id, ico, start, end, Path(output_dir), text,
                    int(limit), progress)
            except (po.DownloadError, OSError, ValueError) as exc:
                st.error(str(exc))
            else:
                (st.success if not result.errors else st.warning)(
                    f"{result.candidates} candidate(s), {result.matched} matched, "
                    f"{len(result.saved_files)} file(s) saved to {Path(output_dir).resolve()}, "
                    f"{len(result.errors)} error(s).")
                for path in result.saved_files:
                    st.text(Path(path).name)
                for key, reason in result.errors:
                    st.error(f"{key}: {reason}")
