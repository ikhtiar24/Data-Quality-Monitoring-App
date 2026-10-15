"""Data profiling, configurable quality rules, history, and report exports."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import pandas as pd
from sqlalchemy import URL, create_engine, text


DEFAULT_DATABASE = Path(os.environ.get("DQM_DB_PATH", "data_quality_history.sqlite3"))
SUPPORTED_RULES = {
    "Never null",
    "Unique values",
    "Between two numbers",
    "Matches a regular expression",
    "Only allowed values",
}


def read_uploaded_file(contents: bytes, filename: str) -> pd.DataFrame:
    """Read a CSV or Excel workbook from uploaded bytes."""
    suffix = Path(filename).suffix.lower()
    if suffix not in {".csv", ".xlsx", ".xls"}:
        raise ValueError("Upload a CSV or Excel file (.csv, .xlsx, or .xls).")
    try:
        if suffix == ".csv":
            frame = pd.read_csv(BytesIO(contents), sep=None, engine="python", encoding="utf-8-sig")
        else:
            frame = pd.read_excel(BytesIO(contents))
    except (UnicodeDecodeError, pd.errors.ParserError, ValueError, OSError) as exc:
        raise ValueError(f"Could not read {filename}: {exc}") from exc

    if frame.empty and len(frame.columns) == 0:
        raise ValueError("The file has no header row or columns.")
    if frame.columns.duplicated().any():
        raise ValueError("Column names must be unique. Rename duplicate columns and upload again.")
    frame.columns = [str(column).strip() for column in frame.columns]
    if any(not column for column in frame.columns):
        raise ValueError("Column names cannot be blank.")
    return frame


def _infer_type(series: pd.Series) -> str:
    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return "Boolean"
    if pd.api.types.is_integer_dtype(dtype):
        return "Integer"
    if pd.api.types.is_float_dtype(dtype):
        return "Number"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "Date / time"

    values = series.dropna()
    if not values.empty:
        sample = values.astype(str).head(500)
        parsed = pd.to_datetime(sample, errors="coerce", format="mixed")
        if parsed.notna().mean() >= 0.9:
            return "Date / time"
    return "Text / category"


def profile_dataframe(frame: pd.DataFrame) -> dict[str, Any]:
    """Summarize completeness, duplicates, inferred types, and value distributions."""
    rows, columns = frame.shape
    null_cells = int(frame.isna().sum().sum())
    column_profiles: list[dict[str, Any]] = []

    for name in frame.columns:
        series = frame[name]
        non_null = series.dropna()
        inferred_type = _infer_type(series)
        distribution: list[dict[str, Any]] = []
        statistics: dict[str, Any] = {}

        if inferred_type in {"Integer", "Number"}:
            numeric = pd.to_numeric(non_null, errors="coerce").dropna()
            if not numeric.empty:
                statistics = {
                    "min": float(numeric.min()),
                    "mean": float(numeric.mean()),
                    "median": float(numeric.median()),
                    "max": float(numeric.max()),
                }
                bins = min(10, max(1, int(numeric.nunique())))
                try:
                    grouped = pd.cut(numeric, bins=bins, duplicates="drop").value_counts(sort=False)
                    distribution = [
                        {"value": str(interval), "count": int(count)}
                        for interval, count in grouped.items()
                    ]
                except ValueError:
                    distribution = [{"value": str(value), "count": int(count)}
                                    for value, count in numeric.value_counts().head(10).items()]
        elif not non_null.empty:
            counts = non_null.astype(str).value_counts(dropna=False).head(10)
            distribution = [
                {"value": str(value), "count": int(count)}
                for value, count in counts.items()
            ]

        missing = int(series.isna().sum())
        column_profiles.append(
            {
                "column": str(name),
                "inferred_type": inferred_type,
                "dtype": str(series.dtype),
                "null_count": missing,
                "null_percentage": (missing / rows * 100) if rows else 0.0,
                "distinct_count": int(series.nunique(dropna=True)),
                "distribution": distribution,
                "statistics": statistics,
            }
        )

    return {
        "row_count": int(rows),
        "column_count": int(columns),
        "duplicate_rows": int(frame.duplicated().sum()),
        "null_cell_count": null_cells,
        "null_percentage": (null_cells / (rows * columns) * 100) if rows and columns else 0.0,
        "columns": column_profiles,
    }


def evaluate_rules(frame: pd.DataFrame, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Evaluate user-defined expectations and return failures with source row numbers."""
    results: list[dict[str, Any]] = []
    for rule in rules:
        column = rule["column"]
        expectation = rule["expectation"]
        values = frame[column]
        failed_mask = pd.Series(False, index=frame.index)

        if expectation == "Never null":
            failed_mask = values.isna()
            detail = "Values must be present."
        elif expectation == "Unique values":
            failed_mask = values.notna() & values.duplicated(keep=False)
            detail = "Non-empty values must not be repeated."
        elif expectation == "Between two numbers":
            numeric = pd.to_numeric(values, errors="coerce")
            failed_mask = numeric.isna() | (numeric < float(rule["minimum"])) | (
                numeric > float(rule["maximum"])
            )
            detail = f"Values must be from {rule['minimum']} to {rule['maximum']} inclusive."
        elif expectation == "Matches a regular expression":
            failed_mask = values.isna() | ~values.astype("string").str.fullmatch(
                rule["pattern"], na=False
            )
            detail = f"Values must match /{rule['pattern']}/."
        elif expectation == "Only allowed values":
            allowed = [str(value).strip() for value in rule["allowed_values"]]
            failed_mask = values.isna() | ~values.astype(str).isin(allowed)
            detail = f"Allowed values: {', '.join(allowed)}."
        else:
            raise ValueError(f"Unsupported rule: {expectation}")

        failed_rows = []
        for position, (value, failed) in enumerate(zip(values.tolist(), failed_mask.tolist())):
            if bool(failed):
                failed_rows.append(
                    {
                        "row_number": position + 2,
                        "column": column,
                        "value": None if pd.isna(value) else str(value),
                        "expectation": expectation,
                        "detail": detail,
                    }
                )
        failed_count = len(failed_rows)
        results.append(
            {
                "column": column,
                "expectation": expectation,
                "parameters": {
                    key: value
                    for key, value in rule.items()
                    if key not in {"column", "expectation"}
                },
                "passed": failed_count == 0,
                "failed_count": failed_count,
                "checked_count": int(len(frame)),
                "detail": detail,
                "exceptions": failed_rows,
            }
        )
    return results


@lru_cache(maxsize=4)
def _engine_for(database_url: str):
    return create_engine(database_url, pool_pre_ping=True)


def _get_engine(database: Path | str | None = None):
    if database is None:
        configured_url = os.environ.get("DATABASE_URL")
        if configured_url:
            if configured_url.startswith("postgresql://"):
                configured_url = configured_url.replace(
                    "postgresql://", "postgresql+psycopg://", 1
                )
            database_url = configured_url
        else:
            database_url = URL.create(
                "sqlite", database=str(DEFAULT_DATABASE.resolve())
            ).render_as_string(hide_password=False)
    elif str(database).startswith(("sqlite://", "postgresql://", "postgresql+")):
        database_url = str(database)
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace(
                "postgresql://", "postgresql+psycopg://", 1
            )
    else:
        database_url = URL.create(
            "sqlite", database=str(Path(database).resolve())
        ).render_as_string(hide_password=False)
    return _engine_for(database_url)


def init_history(database: Path | str | None = None) -> None:
    engine = _get_engine(database)
    id_column = (
        "INTEGER PRIMARY KEY AUTOINCREMENT"
        if engine.dialect.name == "sqlite"
        else "SERIAL PRIMARY KEY"
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS runs (
                    id {id_column},
                    created_at TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    column_count INTEGER NOT NULL,
                    duplicate_rows INTEGER NOT NULL,
                    null_cell_count INTEGER NOT NULL,
                    quality_score REAL,
                    profile_json TEXT NOT NULL,
                    rules_json TEXT NOT NULL,
                    results_json TEXT NOT NULL
                )
                """
            )
        )


def save_run(
    filename: str,
    profile: dict[str, Any],
    rules: list[dict[str, Any]],
    results: list[dict[str, Any]],
    database: Path | str | None = None,
) -> int:
    engine = _get_engine(database)
    init_history(database)
    passed_count = sum(bool(result["passed"]) for result in results)
    score = (passed_count / len(results) * 100) if results else None
    with engine.begin() as connection:
        run_id = connection.execute(
            text(
                """
            INSERT INTO runs (
                created_at, filename, row_count, column_count, duplicate_rows,
                null_cell_count, quality_score, profile_json, rules_json, results_json
            ) VALUES (
                :created_at, :filename, :row_count, :column_count, :duplicate_rows,
                :null_cell_count, :quality_score, :profile_json, :rules_json, :results_json
            ) RETURNING id
            """
            ),
            {
                "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                "filename": filename,
                "row_count": profile["row_count"],
                "column_count": profile["column_count"],
                "duplicate_rows": profile["duplicate_rows"],
                "null_cell_count": profile["null_cell_count"],
                "quality_score": score,
                "profile_json": json.dumps(profile),
                "rules_json": json.dumps(rules),
                "results_json": json.dumps(results),
            },
        ).scalar_one()
        return int(run_id)


def load_history(database: Path | str | None = None) -> pd.DataFrame:
    init_history(database)
    with _get_engine(database).connect() as connection:
        return pd.read_sql_query(
            text(
                """
            SELECT id, created_at, filename, row_count, column_count, duplicate_rows,
                   null_cell_count, quality_score
            FROM runs ORDER BY id DESC
            """
            ),
            con=connection,
        )


def load_run(run_id: int, database: Path | str | None = None) -> dict[str, Any] | None:
    init_history(database)
    with _get_engine(database).connect() as connection:
        row = connection.execute(
            text(
                "SELECT filename, profile_json, results_json FROM runs WHERE id = :run_id"
            ),
            {"run_id": run_id},
        ).fetchone()
    if row is None:
        return None
    return {
        "filename": row[0],
        "profile": json.loads(row[1]),
        "results": json.loads(row[2]),
    }


def build_exception_frame(results: list[dict[str, Any]]) -> pd.DataFrame:
    exceptions = [item for result in results for item in result["exceptions"]]
    return pd.DataFrame(
        exceptions,
        columns=["row_number", "column", "value", "expectation", "detail"],
    )


def _safe_excel_frame(frame: pd.DataFrame) -> pd.DataFrame:
    def safe(value: Any) -> Any:
        if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    safe_frame = frame.apply(lambda column: column.map(safe))
    safe_frame.columns = [safe(str(column)) for column in safe_frame.columns]
    return safe_frame


def create_excel_report(
    filename: str,
    profile: dict[str, Any],
    results: list[dict[str, Any]],
) -> bytes:
    """Create a multi-sheet Excel report, escaping formula-like uploaded strings."""
    columns = pd.DataFrame(profile["columns"])
    rule_rows = [
        {
            "column": result["column"],
            "expectation": result["expectation"],
            "status": "PASS" if result["passed"] else "FAIL",
            "failed_count": result["failed_count"],
            "checked_count": result["checked_count"],
            "details": result["detail"],
        }
        for result in results
    ]
    summary = pd.DataFrame(
        [
            {"metric": "Source file", "value": filename},
            {"metric": "Rows", "value": profile["row_count"]},
            {"metric": "Columns", "value": profile["column_count"]},
            {"metric": "Duplicate rows", "value": profile["duplicate_rows"]},
            {"metric": "Missing cells", "value": profile["null_cell_count"]},
            {"metric": "Missing cells (%)", "value": round(profile["null_percentage"], 2)},
        ]
    )
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        _safe_excel_frame(summary).to_excel(writer, sheet_name="Summary", index=False)
        _safe_excel_frame(columns).to_excel(writer, sheet_name="Column profile", index=False)
        _safe_excel_frame(pd.DataFrame(rule_rows)).to_excel(
            writer, sheet_name="Rule results", index=False
        )
        _safe_excel_frame(build_exception_frame(results)).to_excel(
            writer, sheet_name="Exceptions", index=False
        )
    return buffer.getvalue()


def create_pdf_report(
    filename: str,
    profile: dict[str, Any],
    results: list[dict[str, Any]],
) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        LongTable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=landscape(A4), rightMargin=14 * mm, leftMargin=14 * mm
    )
    styles = getSampleStyleSheet()
    body = ParagraphStyle("SmallBody", parent=styles["BodyText"], fontSize=8, leading=10, alignment=TA_LEFT)
    story = [
        Paragraph("Data quality exception report", styles["Title"]),
        Paragraph(f"Source: {escape(filename)}", styles["Normal"]),
        Spacer(1, 8),
    ]
    summary = [
        ["Rows", "Columns", "Duplicate rows", "Missing cells", "Missing (%)"],
        [
            str(profile["row_count"]),
            str(profile["column_count"]),
            str(profile["duplicate_rows"]),
            str(profile["null_cell_count"]),
            f"{profile['null_percentage']:.2f}%",
        ],
    ]
    summary_table = Table(summary, hAlign="LEFT")
    summary_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef8")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                ("PADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.extend([summary_table, Spacer(1, 12), Paragraph("Rule results", styles["Heading2"])])
    rule_table = [["Column", "Expectation", "Status", "Failed rows", "Details"]]
    rule_table.extend(
        [
            [
                Paragraph(escape(result["column"]), body),
                Paragraph(escape(result["expectation"]), body),
                "PASS" if result["passed"] else "FAIL",
                str(result["failed_count"]),
                Paragraph(escape(result["detail"]), body),
            ]
            for result in results
        ]
    )
    if len(rule_table) == 1:
        rule_table.append(["No rules configured", "", "", "", ""])
    table = LongTable(rule_table, repeatRows=1, colWidths=[35 * mm, 43 * mm, 20 * mm, 22 * mm, 120 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef8")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("PADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.extend([table, Spacer(1, 12), Paragraph("Exceptions", styles["Heading2"])])
    exceptions = build_exception_frame(results)
    if exceptions.empty:
        story.append(Paragraph("No rule exceptions found.", styles["Normal"]))
    else:
        exception_table = [["Row", "Column", "Value", "Expectation", "Details"]]
        for exception in exceptions.itertuples(index=False):
            exception_table.append(
                [
                    str(exception.row_number),
                    Paragraph(escape(str(exception.column)), body),
                    Paragraph(escape(str(exception.value)), body),
                    Paragraph(escape(str(exception.expectation)), body),
                    Paragraph(escape(str(exception.detail)), body),
                ]
            )
        table = LongTable(
            exception_table,
            repeatRows=1,
            colWidths=[15 * mm, 30 * mm, 45 * mm, 45 * mm, 105 * mm],
        )
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef8")),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("PADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(table)
    document.build(story)
    return output.getvalue()
