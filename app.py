from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re

import altair as alt
import pandas as pd
import streamlit as st

from quality_profiler import (
    SUPPORTED_RULES,
    create_excel_report,
    create_pdf_report,
    evaluate_rules,
    load_history,
    load_run,
    profile_dataframe,
    read_uploaded_file,
    save_run,
)


st.set_page_config(
    page_title="ClearCheck | Data quality monitoring",
    page_icon="✓",
    layout="wide",
    initial_sidebar_state="expanded",
)

secret_paths = [
    Path(__file__).resolve().parent / ".streamlit" / "secrets.toml",
    Path.home() / ".streamlit" / "secrets.toml",
]
if not os.environ.get("DATABASE_URL") and any(path.is_file() for path in secret_paths):
    try:
        configured_url = st.secrets.get("DATABASE_URL")
    except FileNotFoundError:
        configured_url = None
    if configured_url:
        os.environ["DATABASE_URL"] = str(configured_url)

st.markdown(
    """
    <style>
      .block-container {max-width: 1440px; padding-top: 2rem; padding-bottom: 3rem;}
      .hero {padding: 1.3rem 1.6rem; border-radius: 16px;
             background: linear-gradient(120deg, #10233f, #184b66); color: white;
             margin-bottom: 1.2rem;}
      .hero p {color: #d6e6f2; margin-bottom: 0;}
      [data-testid="stMetric"] {background: #f5f8fc; border: 1px solid #e4eaf2;
                                padding: 14px 16px; border-radius: 12px;}
    </style>
    """,
    unsafe_allow_html=True,
)

if "rules" not in st.session_state:
    st.session_state.rules = []
if "run_result" not in st.session_state:
    st.session_state.run_result = None
if "frame" not in st.session_state:
    st.session_state.frame = None
if "filename" not in st.session_state:
    st.session_state.filename = None

with st.sidebar:
    st.markdown("## ✓ ClearCheck")
    st.caption("Data quality monitoring")
    page = st.radio("Workspace", ["Profile data", "Rules", "Run history"], label_visibility="collapsed")
    st.divider()
    st.caption("Upload CSV or Excel · Profile · Validate · Monitor")

st.markdown(
    '<div class="hero"><h1>Know your data before it reaches a dashboard.</h1>'
    "<p>Profile a file, define business-friendly expectations, and track quality over time.</p></div>",
    unsafe_allow_html=True,
)


def show_profile(profile: dict) -> None:
    metric_columns = st.columns(4)
    metric_columns[0].metric("Rows", f"{profile['row_count']:,}")
    metric_columns[1].metric("Columns", f"{profile['column_count']:,}")
    metric_columns[2].metric("Duplicate rows", f"{profile['duplicate_rows']:,}")
    metric_columns[3].metric("Missing cells", f"{profile['null_cell_count']:,}", f"{profile['null_percentage']:.1f}% of all cells")

    st.subheader("Column profile")
    profile_rows = [
        {
            "Column": column["column"],
            "Inferred type": column["inferred_type"],
            "Missing": column["null_count"],
            "Missing %": round(column["null_percentage"], 1),
            "Unique": column["distinct_count"],
        }
        for column in profile["columns"]
    ]
    st.dataframe(pd.DataFrame(profile_rows), use_container_width=True, hide_index=True)

    columns = profile["columns"]
    if columns:
        selected = st.selectbox(
            "Explore a column distribution",
            [column["column"] for column in columns],
            key="distribution_column",
        )
        detail = next(column for column in columns if column["column"] == selected)
        if detail["statistics"]:
            stat_cols = st.columns(4)
            for col, label in zip(stat_cols, ["min", "mean", "median", "max"]):
                col.metric(label.title(), f"{detail['statistics'][label]:,.3g}")
        if detail["distribution"]:
            chart_data = pd.DataFrame(detail["distribution"]).set_index("value")
            st.bar_chart(chart_data, y="count", color="#2e8b7a")
        else:
            st.info("No non-empty values are available to chart for this column.")


def show_results(results: list[dict]) -> None:
    st.subheader("Expectation results")
    if not results:
        st.info("No expectations have been added yet. Add rules on the Rules page.")
        return
    for index, result in enumerate(results):
        icon = "✅" if result["passed"] else "❌"
        with st.expander(
            f"{icon} {result['column']} · {result['expectation']} · "
            f"{result['failed_count']} exception(s)",
            expanded=not result["passed"],
        ):
            st.write(result["detail"])
            if result["exceptions"]:
                st.dataframe(
                    pd.DataFrame(result["exceptions"]).drop(columns=["detail"]),
                    use_container_width=True,
                    hide_index=True,
                )


def show_reports(run_result: dict) -> None:
    excel = create_excel_report(
        run_result["filename"], run_result["profile"], run_result["results"]
    )
    pdf = create_pdf_report(
        run_result["filename"], run_result["profile"], run_result["results"]
    )
    safe_name = Path(run_result["filename"]).stem.replace(" ", "_")
    left, right = st.columns(2)
    left.download_button(
        "Download Excel exception report",
        excel,
        file_name=f"{safe_name}_quality_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
    right.download_button(
        "Download PDF exception report",
        pdf,
        file_name=f"{safe_name}_quality_report.pdf",
        mime="application/pdf",
        use_container_width=True,
    )


if page == "Profile data":
    st.subheader("1 · Upload and profile")
    upload = st.file_uploader("Choose a CSV or Excel file", type=["csv", "xlsx", "xls"])
    if upload is None and st.button("Load demo dataset with example checks"):
        demo_path = Path(__file__).resolve().parent / "sample_data.csv"
        try:
            st.session_state.frame = read_uploaded_file(
                demo_path.read_bytes(), demo_path.name
            )
            st.session_state.filename = demo_path.name
            st.session_state.upload_signature = None
            st.session_state.run_result = None
            st.session_state.rules = [
                {"column": "record_id", "expectation": "Unique values"},
                {
                    "column": "loyalty_score",
                    "expectation": "Between two numbers",
                    "minimum": 0.0,
                    "maximum": 100.0,
                },
                {"column": "customer_email", "expectation": "Never null"},
            ]
            st.success("Demo data and three example checks are ready.")
        except (OSError, ValueError) as exc:
            st.error(f"Could not load the included demo dataset: {exc}")
    if upload is not None:
        contents = upload.getvalue()
        upload_signature = (upload.name, hashlib.sha256(contents).hexdigest())
        if st.session_state.get("upload_signature") != upload_signature:
            st.session_state.frame = None
            st.session_state.run_result = None
            st.session_state.rules = []
            try:
                st.session_state.frame = read_uploaded_file(contents, upload.name)
                st.session_state.filename = upload.name
                st.session_state.upload_signature = upload_signature
            except ValueError as exc:
                st.error(str(exc))

    frame = st.session_state.frame
    if frame is not None:
        st.caption(f"Loaded **{st.session_state.filename}** · {len(frame):,} rows · {len(frame.columns):,} columns")
        with st.expander("Preview uploaded data"):
            st.dataframe(frame.head(20), use_container_width=True, hide_index=True)
        if st.button("Analyze data and save this run", type="primary"):
            profile = profile_dataframe(frame)
            results = evaluate_rules(frame, st.session_state.rules)
            run_id = save_run(
                st.session_state.filename,
                profile,
                st.session_state.rules,
                results,
            )
            st.session_state.run_result = {
                "run_id": run_id,
                "filename": st.session_state.filename,
                "profile": profile,
                "results": results,
            }
            st.success(f"Analysis complete. Run #{run_id} is saved to history.")

    if st.session_state.run_result is not None:
        current = st.session_state.run_result
        st.divider()
        show_profile(current["profile"])
        show_results(current["results"])
        st.subheader("4 · Export")
        show_reports(current)

elif page == "Rules":
    st.subheader("2 · Define business expectations")
    frame = st.session_state.frame
    if frame is None:
        st.info("Upload a file on the Profile data page first. Rules are configured against its columns.")
    else:
        with st.form("add_rule", clear_on_submit=True):
            column, expectation = st.columns(2)
            rule_column = column.selectbox("Column", list(frame.columns))
            rule_type = expectation.selectbox("Expectation", sorted(SUPPORTED_RULES))
            minimum = maximum = None
            pattern = ""
            allowed_values = ""
            if rule_type == "Between two numbers":
                lower, upper = st.columns(2)
                minimum = lower.number_input("Minimum (inclusive)", value=0.0)
                maximum = upper.number_input("Maximum (inclusive)", value=100.0)
            elif rule_type == "Matches a regular expression":
                pattern = st.text_input("Regular expression", value=r"^[A-Z]{2}\d{4}$")
            elif rule_type == "Only allowed values":
                allowed_values = st.text_input("Allowed values, separated by commas", placeholder="open, closed, pending")
            add_rule = st.form_submit_button("Add expectation", type="primary")

        if add_rule:
            rule = {"column": rule_column, "expectation": rule_type}
            if rule_type == "Between two numbers":
                if minimum > maximum:
                    st.error("The minimum must not exceed the maximum.")
                else:
                    rule.update({"minimum": minimum, "maximum": maximum})
                    st.session_state.rules.append(rule)
            elif rule_type == "Matches a regular expression":
                try:
                    re.compile(pattern)
                    rule["pattern"] = pattern
                    st.session_state.rules.append(rule)
                except re.error as exc:
                    st.error(f"Invalid regular expression: {exc}")
            elif rule_type == "Only allowed values":
                allowed = [value.strip() for value in allowed_values.split(",") if value.strip()]
                if not allowed:
                    st.error("Enter at least one allowed value.")
                else:
                    rule["allowed_values"] = allowed
                    st.session_state.rules.append(rule)
            else:
                st.session_state.rules.append(rule)

        st.markdown("#### Your expectations")
        if st.session_state.rules:
            for index, rule in enumerate(st.session_state.rules):
                left, right = st.columns([8, 1])
                parameters = {
                    key: value
                    for key, value in rule.items()
                    if key not in {"column", "expectation"}
                }
                left.write(f"**{rule['column']}** · {rule['expectation']} {parameters or ''}")
                if right.button("Remove", key=f"remove_rule_{index}"):
                    st.session_state.rules.pop(index)
                    st.rerun()
            if st.button("Clear all expectations"):
                st.session_state.rules = []
                st.rerun()
        else:
            st.caption("No expectations configured. Add a rule, then run the analysis.")
        st.caption("Expectations are applied on the Profile data page when you save a run.")

elif page == "Run history":
    st.subheader("3 · Quality over time")
    history = load_history()
    if history.empty:
        st.info("No saved runs yet. Upload a file and analyze it to start a monitoring history.")
    else:
        history["created_at"] = pd.to_datetime(
            history["created_at"], errors="coerce", utc=True, format="mixed"
        )
        recent = history.head(100).sort_values("created_at")
        scored = recent.dropna(subset=["quality_score"]).copy()
        if len(scored) >= 2:
            scored["created_at"] = scored["created_at"].dt.tz_localize(None)
            chart = (
                alt.Chart(scored)
                .mark_line(point=alt.OverlayMarkDef(filled=True, size=80))
                .encode(
                    x=alt.X("created_at:T", title="Run date"),
                    y=alt.Y(
                        "quality_score:Q",
                        title="Rule pass rate (%)",
                        scale=alt.Scale(domain=[0, 100]),
                    ),
                    tooltip=[
                        alt.Tooltip("created_at:T", title="Run at"),
                        alt.Tooltip("quality_score:Q", title="Pass rate", format=".1f"),
                    ],
                )
                .properties(height=240)
            )
            st.markdown("#### Rule pass rate")
            st.altair_chart(chart, use_container_width=True)
            st.caption("Each point represents the percentage of configured rules that passed in that run.")
        else:
            st.info("Save at least two runs with expectations enabled to see a quality trend.")
        st.markdown("#### Run snapshots")
        display = history.rename(
            columns={
                "id": "Run",
                "created_at": "When",
                "filename": "File",
                "row_count": "Rows",
                "column_count": "Columns",
                "duplicate_rows": "Duplicate rows",
                "null_cell_count": "Missing cells",
                "quality_score": "Rule pass rate (%)",
            }
        )
        st.dataframe(display, use_container_width=True, hide_index=True)
        selected_run = st.selectbox("Inspect stored run", history["id"].tolist())
        if st.button("Show stored run details"):
            stored_run = load_run(int(selected_run))
            if stored_run is None:
                st.error("The selected run could not be found in the history database.")
            else:
                st.session_state.run_result = {
                    "run_id": int(selected_run),
                    **stored_run,
                }
                st.success("Stored results loaded. Open Profile data to view details and exports.")
