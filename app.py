from __future__ import annotations

import csv
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Iterator

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # Local SQLite mode does not need psycopg.
    psycopg = None
    dict_row = None


BASE_DIR = Path(__file__).resolve().parent
CHECKLIST_SOURCE = BASE_DIR / "checklist_source.ts"
if not CHECKLIST_SOURCE.exists():
    CHECKLIST_SOURCE = BASE_DIR.parent / "lib" / "checklist.ts"


def load_checklist() -> list[dict[str, Any]]:
    source = CHECKLIST_SOURCE.read_text(encoding="utf-8")
    section_pattern = re.compile(
        r'\{\s*id:\s*"([^"]+)",\s*title:\s*"([^"]+)",\s*items:\s*\[(.*?)\]\s*,?\s*\}',
        re.DOTALL,
    )
    item_pattern = re.compile(r'\{\s*id:\s*"([^"]+)",\s*text:\s*"((?:[^"\\]|\\.)*)"\s*\}')
    sections: list[dict[str, Any]] = []
    for section_id, title, items_source in section_pattern.findall(source):
        items = [
            {"id": item_id, "text": json.loads(f'"{text}"')}
            for item_id, text in item_pattern.findall(items_source)
        ]
        sections.append({"id": section_id, "title": title, "items": items})
    if len(sections) != 14 or sum(len(section["items"]) for section in sections) != 102:
        raise RuntimeError("The checklist source could not be loaded safely.")
    return sections


CHECKLIST = load_checklist()
CHECKLIST_ITEMS = [
    {**item, "section_id": section["id"], "section_title": section["title"]}
    for section in CHECKLIST
    for item in section["items"]
]
CHECKLIST_IDS = {item["id"] for item in CHECKLIST_ITEMS}

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
    MAX_CONTENT_LENGTH=512 * 1024,
)


def is_postgres() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


@contextmanager
def database() -> Iterator[Any]:
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        if psycopg is None:
            raise RuntimeError("PostgreSQL support is unavailable. Install the project requirements.")
        connection = psycopg.connect(database_url, row_factory=dict_row)
    else:
        db_path = Path(os.environ.get("OHS_DB_PATH", BASE_DIR / "data" / "ohs.db"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def sql(statement: str) -> str:
    return statement.replace("?", "%s") if is_postgres() else statement


def init_db() -> None:
    statements = [
        """CREATE TABLE IF NOT EXISTS inspections (
            id TEXT PRIMARY KEY,
            report_no TEXT NOT NULL UNIQUE,
            project_name TEXT NOT NULL,
            work_location TEXT NOT NULL,
            contractor TEXT NOT NULL,
            inspected_by TEXT NOT NULL,
            inspection_date TEXT NOT NULL,
            inspection_time TEXT NOT NULL,
            shift TEXT NOT NULL,
            responses TEXT NOT NULL,
            response_notes TEXT NOT NULL DEFAULT '{}',
            remarks TEXT NOT NULL DEFAULT '',
            signoff_name TEXT NOT NULL,
            signed INTEGER NOT NULL DEFAULT 0,
            total_inspected INTEGER NOT NULL,
            compliant INTEGER NOT NULL,
            non_compliant INTEGER NOT NULL,
            not_applicable INTEGER NOT NULL,
            score REAL NOT NULL,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS inspections_date_idx ON inspections (inspection_date)",
        "CREATE INDEX IF NOT EXISTS inspections_created_idx ON inspections (created_at)",
        "CREATE INDEX IF NOT EXISTS inspections_contractor_idx ON inspections (contractor)",
    ]
    with database() as connection:
        cursor = connection.cursor()
        for statement in statements:
            cursor.execute(statement)


def clean_text(value: Any, field: str, maximum: int = 200, required: bool = True) -> str:
    cleaned = value.strip() if isinstance(value, str) else ""
    if required and not cleaned:
        raise ValueError(f"{field} is required.")
    if len(cleaned) > maximum:
        raise ValueError(f"{field} is too long.")
    return cleaned


def validate_inspection(payload: dict[str, Any]) -> dict[str, Any]:
    record = {
        "project_name": clean_text(payload.get("projectName"), "Project name"),
        "work_location": clean_text(payload.get("workLocation"), "Work location / zone"),
        "contractor": clean_text(payload.get("contractor"), "Contractor / subcontractor"),
        "inspected_by": clean_text(payload.get("inspectedBy"), "Inspected by"),
        "inspection_date": clean_text(payload.get("inspectionDate"), "Date", 10),
        "inspection_time": clean_text(payload.get("inspectionTime"), "Time", 5),
        "shift": clean_text(payload.get("shift"), "Shift", 80),
        "report_no": clean_text(payload.get("reportNo"), "Report number", 80, False),
        "remarks": clean_text(payload.get("remarks"), "Remarks", 3000, False),
        "signoff_name": clean_text(payload.get("signoffName"), "Sign-off name"),
    }
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", record["inspection_date"]):
        raise ValueError("Enter a valid inspection date.")
    if not re.fullmatch(r"\d{2}:\d{2}", record["inspection_time"]):
        raise ValueError("Enter a valid inspection time.")
    if payload.get("signed") is not True:
        raise ValueError("Confirm the digital sign-off before submitting.")

    raw_responses = payload.get("responses")
    if not isinstance(raw_responses, dict):
        raise ValueError("Checklist responses are required.")
    responses = {
        key: value for key, value in raw_responses.items()
        if key in CHECKLIST_IDS and value in {"Y", "N", "NA"}
    }
    if len(responses) != len(CHECKLIST_ITEMS):
        raise ValueError(f"Complete all {len(CHECKLIST_ITEMS)} checklist items before submitting.")

    raw_notes = payload.get("responseNotes")
    notes: dict[str, str] = {}
    if isinstance(raw_notes, dict):
        for key, value in raw_notes.items():
            if key in CHECKLIST_IDS:
                notes[key] = clean_text(value, "Observation", 600, False)
    for item in CHECKLIST_ITEMS:
        if responses[item["id"]] == "N" and not notes.get(item["id"]):
            raise ValueError(f'Add an observation for the non-compliant item: {item["text"]}')

    compliant = sum(value == "Y" for value in responses.values())
    non_compliant = sum(value == "N" for value in responses.values())
    not_applicable = sum(value == "NA" for value in responses.values())
    total_inspected = compliant + non_compliant
    score = round((compliant / total_inspected * 100) if total_inspected else 0, 1)
    return {
        **record,
        "responses": responses,
        "response_notes": notes,
        "signed": 1,
        "compliant": compliant,
        "non_compliant": non_compliant,
        "not_applicable": not_applicable,
        "total_inspected": total_inspected,
        "score": score,
    }


def admin_required(view: Any) -> Any:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not session.get("admin"):
            return redirect(url_for("admin", next=request.full_path))
        return view(*args, **kwargs)
    return wrapped


def filtered_records(limit: int = 1000) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    query = request.args.get("q", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    if query:
        pattern = f"%{query.lower().replace('%', '').replace('_', '')}%"
        clauses.append("(LOWER(report_no) LIKE ? OR LOWER(inspected_by) LIKE ? OR LOWER(contractor) LIKE ? OR LOWER(work_location) LIKE ?)")
        params.extend([pattern] * 4)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_from):
        clauses.append("inspection_date >= ?")
        params.append(date_from)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_to):
        clauses.append("inspection_date <= ?")
        params.append(date_to)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql(f"SELECT * FROM inspections{where} ORDER BY created_at DESC LIMIT ?"), [*params, limit])
        rows = cursor.fetchall()
    return [dict(row) for row in rows]


@app.after_request
def security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'")
    return response


@app.get("/")
def index() -> str:
    return render_template("index.html", total_items=len(CHECKLIST_ITEMS))


@app.get("/api/checklist")
def checklist_api() -> Response:
    return jsonify({"sections": CHECKLIST, "total": len(CHECKLIST_ITEMS)})


@app.post("/api/inspections")
def submit_inspection() -> tuple[Response, int] | Response:
    try:
        payload = request.get_json(force=True, silent=False)
        if not isinstance(payload, dict):
            raise ValueError("The inspection data is invalid.")
        record = validate_inspection(payload)
        record_id = secrets.token_hex(16)
        report_no = record["report_no"] or f'OHS-{record["inspection_date"].replace("-", "")}-{record_id[:6].upper()}'
        created_at = datetime.now(timezone.utc).isoformat()
        values = [
            record_id, report_no, record["project_name"], record["work_location"], record["contractor"],
            record["inspected_by"], record["inspection_date"], record["inspection_time"], record["shift"],
            json.dumps(record["responses"]), json.dumps(record["response_notes"]), record["remarks"],
            record["signoff_name"], record["signed"], record["total_inspected"], record["compliant"],
            record["non_compliant"], record["not_applicable"], record["score"], created_at,
        ]
        placeholders = ",".join("?" for _ in values)
        with database() as connection:
            cursor = connection.cursor()
            cursor.execute(sql(f"INSERT INTO inspections VALUES ({placeholders})"), values)
        return jsonify({"id": record_id, "reportNo": report_no, "score": record["score"], "nonCompliant": record["non_compliant"]}), 201
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        message = str(error)
        if "unique" in message.lower():
            return jsonify({"error": "That report number already exists. Enter another report number."}), 409
        app.logger.exception("Inspection submission failed")
        return jsonify({"error": "The inspection could not be saved."}), 500


@app.route("/admin", methods=["GET", "POST"])
def admin() -> str | Response:
    error = ""
    configured = bool(os.environ.get("ADMIN_PASSWORD"))
    if request.method == "POST":
        supplied = request.form.get("password", "")
        expected = os.environ.get("ADMIN_PASSWORD", "")
        if expected and hmac.compare_digest(supplied.encode(), expected.encode()):
            session.clear()
            session["admin"] = True
            session.permanent = True
            return redirect(url_for("admin"))
        error = "Incorrect admin password."
    if not session.get("admin"):
        return render_template("login.html", error=error, configured=configured)

    records = filtered_records()
    total = len(records)
    average = round(sum(float(record["score"]) for record in records) / total, 1) if total else 0
    non_compliant = sum(int(record["non_compliant"]) for record in records)
    return render_template("admin.html", records=records, total=total, average=average, non_compliant=non_compliant)


@app.post("/admin/logout")
def logout() -> Response:
    session.clear()
    return redirect(url_for("admin"))


@app.get("/admin/records/<record_id>")
@admin_required
def record_detail(record_id: str) -> str | tuple[str, int]:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT * FROM inspections WHERE id = ?"), [record_id])
        row = cursor.fetchone()
    if not row:
        return "Record not found", 404
    record = dict(row)
    record["responses"] = json.loads(record["responses"])
    record["response_notes"] = json.loads(record["response_notes"])
    return render_template("record.html", record=record, sections=CHECKLIST)


@app.get("/admin/export")
@admin_required
def export_records() -> Response:
    records = filtered_records(limit=5000)
    detailed = request.args.get("kind") == "detailed"
    output = io.StringIO()
    writer = csv.writer(output)
    if detailed:
        writer.writerow(["Report No.", "Date", "Project", "Work Location / Zone", "Contractor / Subcontractor", "Inspected By", "Section", "Requirement", "Result", "Observation / Action"])
        for record in records:
            responses = json.loads(record["responses"])
            notes = json.loads(record["response_notes"])
            for item in CHECKLIST_ITEMS:
                writer.writerow([record["report_no"], record["inspection_date"], record["project_name"], record["work_location"], record["contractor"], record["inspected_by"], item["section_title"], item["text"], responses.get(item["id"], ""), notes.get(item["id"], "")])
        kind = "detailed"
    else:
        writer.writerow(["Report No.", "Date", "Time", "Project", "Work Location / Zone", "Contractor / Subcontractor", "Inspected By", "Shift", "Total Inspected", "Compliant", "Non-Compliant", "N/A", "Compliance Score (%)", "Remarks", "Signed By", "Submitted At"])
        for record in records:
            writer.writerow([record["report_no"], record["inspection_date"], record["inspection_time"], record["project_name"], record["work_location"], record["contractor"], record["inspected_by"], record["shift"], record["total_inspected"], record["compliant"], record["non_compliant"], record["not_applicable"], record["score"], record["remarks"], record["signoff_name"], record["created_at"]])
        kind = "summary"
    filename = f'diriyah-ohs-{kind}-{datetime.now(timezone.utc).date().isoformat()}.csv'
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/health")
def health() -> Response:
    return jsonify({"status": "ok"})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
