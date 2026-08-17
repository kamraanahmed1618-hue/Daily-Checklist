from __future__ import annotations

import csv
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import traceback
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Iterator

from flask import Flask, Response, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from charts import bar_chart_svg, grouped_bar_chart_svg, line_chart_svg

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # Local SQLite mode does not need psycopg.
    psycopg = None
    dict_row = None

try:
    import boto3
    from botocore.client import Config as BotoConfig
except ImportError:  # Photo upload is unavailable until boto3 is installed.
    boto3 = None
    BotoConfig = None


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
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,
)
app.jinja_env.filters["from_json"] = json.loads

B2_BUCKET = os.environ.get("B2_BUCKET_NAME")
PHOTO_MAX_COUNT = 8
PHOTO_KEY_PATTERN = re.compile(r"uploads/[a-zA-Z0-9_-]{8,64}/[0-9a-f]{20}\.(?:jpg|png|webp)")


def is_postgres() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


def b2_configured() -> bool:
    return bool(
        boto3 and B2_BUCKET
        and os.environ.get("B2_KEY_ID")
        and os.environ.get("B2_APPLICATION_KEY")
        and os.environ.get("B2_ENDPOINT")
    )


def b2_client() -> Any:
    endpoint = os.environ["B2_ENDPOINT"]
    if not endpoint.startswith("http"):
        endpoint = f"https://{endpoint}"
    region = "auto"
    host_parts = endpoint.split("//", 1)[-1].split(".")
    if len(host_parts) >= 2 and host_parts[0] == "s3":
        region = host_parts[1]
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["B2_KEY_ID"],
        aws_secret_access_key=os.environ["B2_APPLICATION_KEY"],
        config=BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            # B2's S3-compatible gateway doesn't support the chunked trailing-checksum
            # uploads botocore sends by default, and silently drops the connection instead
            # of returning an error — this puts requests back to plain SigV4 signing.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
        region_name=region,
    )


def photo_url(key: str, expires: int = 3600) -> str:
    return b2_client().generate_presigned_url("get_object", Params={"Bucket": B2_BUCKET, "Key": key}, ExpiresIn=expires)


def photo_urls(keys: list[str]) -> list[str]:
    if not keys or not b2_configured():
        return []
    try:
        return [photo_url(key) for key in keys]
    except Exception:
        app.logger.exception("Failed to generate photo URLs")
        return []


def delete_photos(keys: list[str]) -> None:
    if not keys or not b2_configured():
        return
    try:
        client = b2_client()
        for key in keys:
            client.delete_object(Bucket=B2_BUCKET, Key=key)
    except Exception:
        app.logger.exception("Failed to delete photos from storage")


def clean_photo_keys(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    keys = [item for item in value if isinstance(item, str) and PHOTO_KEY_PATTERN.fullmatch(item)]
    return list(dict.fromkeys(keys))[:PHOTO_MAX_COUNT]


def safe_json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        app.logger.warning("Invalid JSON in stored field, defaulting to empty list: %r", value)
        return []
    return parsed if isinstance(parsed, list) else []


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
            seq INTEGER,
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
        """CREATE TABLE IF NOT EXISTS near_miss_reports (
            id TEXT PRIMARY KEY,
            seq INTEGER,
            report_no TEXT NOT NULL UNIQUE,
            department_project TEXT NOT NULL,
            incident_date TEXT NOT NULL,
            incident_time TEXT NOT NULL,
            location TEXT NOT NULL,
            reported_by TEXT NOT NULL,
            what_happened TEXT NOT NULL,
            could_have_happened TEXT NOT NULL DEFAULT '',
            near_miss_types TEXT NOT NULL DEFAULT '[]',
            near_miss_type_other TEXT NOT NULL DEFAULT '',
            immediate_actions TEXT NOT NULL DEFAULT '',
            hazard_eliminated TEXT NOT NULL DEFAULT '',
            hazard_actions_required TEXT NOT NULL DEFAULT '',
            investigation_lead TEXT NOT NULL DEFAULT '',
            investigation_date TEXT NOT NULL DEFAULT '',
            root_causes TEXT NOT NULL DEFAULT '[]',
            root_cause_other TEXT NOT NULL DEFAULT '',
            root_cause_detail TEXT NOT NULL DEFAULT '',
            corrective_actions TEXT NOT NULL DEFAULT '[]',
            preventive_measures TEXT NOT NULL DEFAULT '[]',
            person_responsible TEXT NOT NULL DEFAULT '',
            target_completion_date TEXT NOT NULL DEFAULT '',
            reported_by_signoff TEXT NOT NULL,
            hse_manager_signoff TEXT NOT NULL DEFAULT '',
            followup_by TEXT NOT NULL DEFAULT '',
            followup_date TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            status_reason TEXT NOT NULL DEFAULT '',
            photos TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS near_miss_date_idx ON near_miss_reports (incident_date)",
        "CREATE INDEX IF NOT EXISTS near_miss_created_idx ON near_miss_reports (created_at)",
        """CREATE TABLE IF NOT EXISTS violation_notices (
            id TEXT PRIMARY KEY,
            seq INTEGER,
            violation_no TEXT NOT NULL UNIQUE,
            project_name TEXT NOT NULL,
            violation_date TEXT NOT NULL,
            employee_name TEXT NOT NULL,
            employee_id TEXT NOT NULL DEFAULT '',
            company_contractor TEXT NOT NULL,
            job_title TEXT NOT NULL DEFAULT '',
            violation_location TEXT NOT NULL,
            violation_type TEXT NOT NULL,
            violation_description TEXT NOT NULL,
            actions TEXT NOT NULL DEFAULT '[]',
            deduction_amount TEXT NOT NULL DEFAULT '',
            photos_attached INTEGER NOT NULL DEFAULT 0,
            documents_attached INTEGER NOT NULL DEFAULT 0,
            issued_by_name TEXT NOT NULL,
            issued_by_position TEXT NOT NULL DEFAULT '',
            photos TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS violations_date_idx ON violation_notices (violation_date)",
        "CREATE INDEX IF NOT EXISTS violations_created_idx ON violation_notices (created_at)",
    ]
    with database() as connection:
        cursor = connection.cursor()
        for statement in statements:
            cursor.execute(statement)

        def ensure_column(table: str, column: str, coltype: str) -> None:
            if is_postgres():
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}")
            else:
                cursor.execute(f"PRAGMA table_info({table})")
                if not any(row[1] == column for row in cursor.fetchall()):
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

        ensure_column("inspections", "seq", "INTEGER")
        ensure_column("near_miss_reports", "photos", "TEXT NOT NULL DEFAULT '[]'")
        ensure_column("violation_notices", "photos", "TEXT NOT NULL DEFAULT '[]'")
        # Backfill sequential numbers for any pre-existing records in submission order.
        for table in ("inspections", "near_miss_reports", "violation_notices"):
            cursor.execute(sql(f"SELECT COALESCE(MAX(seq), 0) AS next_seq FROM {table}"))
            next_seq = cursor.fetchone()["next_seq"] or 0
            cursor.execute(sql(f"SELECT id FROM {table} WHERE seq IS NULL ORDER BY created_at ASC"))
            for row in cursor.fetchall():
                next_seq += 1
                cursor.execute(sql(f"UPDATE {table} SET seq = ? WHERE id = ?"), [next_seq, row["id"]])


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


NEAR_MISS_TYPES = ["Unsafe Act", "Unsafe Condition", "Equipment Failure", "Human Error", "Other"]
ROOT_CAUSES = [
    "Lack of Awareness/Training", "Inadequate Supervision", "Equipment/Tool Defect",
    "Procedural Failure", "Time Pressure/Workload", "Other",
]
NEAR_MISS_STATUSES = ["Completed", "In Progress", "Not Completed"]
VIOLATION_ACTIONS = [
    "First Warning", "Final Warning", "Salary Deduction",
    "Deduction from Subcontractor Payment", "Removal from Site",
]


def clean_choices(value: Any, field: str, allowed: list[str]) -> list[str]:
    if not isinstance(value, list):
        return []
    seen = [item for item in value if isinstance(item, str) and item in allowed]
    return list(dict.fromkeys(seen))


def clean_list_text(value: Any, field: str, max_items: int, maximum: int = 300) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [clean_text(entry, field, maximum, False) for entry in value[:max_items]]
    return [item for item in items if item]


def validate_near_miss(payload: dict[str, Any]) -> dict[str, Any]:
    record = {
        "department_project": clean_text(payload.get("departmentProject"), "Department / Project"),
        "incident_date": clean_text(payload.get("incidentDate"), "Date of near miss", 10),
        "incident_time": clean_text(payload.get("incidentTime"), "Time of near miss", 5),
        "location": clean_text(payload.get("location"), "Location of near miss"),
        "reported_by": clean_text(payload.get("reportedBy"), "Reported by"),
        "what_happened": clean_text(payload.get("whatHappened"), "What happened", 3000),
        "could_have_happened": clean_text(payload.get("couldHaveHappened"), "What could have happened", 3000, False),
        "near_miss_type_other": clean_text(payload.get("nearMissTypeOther"), "Near miss type (other)", 200, False),
        "immediate_actions": clean_text(payload.get("immediateActions"), "Immediate actions taken", 3000, False),
        "hazard_eliminated": clean_text(payload.get("hazardEliminated"), "Was the hazard eliminated", 10, False),
        "hazard_actions_required": clean_text(payload.get("hazardActionsRequired"), "Required actions to eliminate the hazard", 2000, False),
        "investigation_lead": clean_text(payload.get("investigationLead"), "Investigation lead", 200, False),
        "investigation_date": clean_text(payload.get("investigationDate"), "Date of investigation", 10, False),
        "root_cause_other": clean_text(payload.get("rootCauseOther"), "Root cause (other)", 200, False),
        "root_cause_detail": clean_text(payload.get("rootCauseDetail"), "Detailed description of root cause", 3000, False),
        "person_responsible": clean_text(payload.get("personResponsible"), "Person responsible for action", 200, False),
        "target_completion_date": clean_text(payload.get("targetCompletionDate"), "Target completion date", 10, False),
        "reported_by_signoff": clean_text(payload.get("reportedBySignoff"), "Reported by (sign-off)"),
        "hse_manager_signoff": clean_text(payload.get("hseManagerSignoff"), "HSE Manager (sign-off)", 200, False),
        "followup_by": clean_text(payload.get("followupBy"), "Follow-up conducted by", 200, False),
        "followup_date": clean_text(payload.get("followupDate"), "Follow-up date", 10, False),
        "status_reason": clean_text(payload.get("statusReason"), "Reason (status not completed)", 1000, False),
    }
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", record["incident_date"]):
        raise ValueError("Enter a valid date of near miss.")
    if not re.fullmatch(r"\d{2}:\d{2}", record["incident_time"]):
        raise ValueError("Enter a valid time of near miss.")

    near_miss_types = clean_choices(payload.get("nearMissTypes"), "Type of near miss", NEAR_MISS_TYPES)
    if not near_miss_types:
        raise ValueError("Select at least one type of near miss.")

    hazard_eliminated = record["hazard_eliminated"]
    if hazard_eliminated and hazard_eliminated not in {"Yes", "No"}:
        raise ValueError("Was the hazard eliminated must be Yes or No.")

    root_causes = clean_choices(payload.get("rootCauses"), "Root cause", ROOT_CAUSES)

    status = clean_text(payload.get("status"), "Status of corrective actions", 20, False)
    if status and status not in NEAR_MISS_STATUSES:
        raise ValueError("Select a valid status of corrective actions.")

    return {
        **record,
        "near_miss_types": near_miss_types,
        "root_causes": root_causes,
        "corrective_actions": clean_list_text(payload.get("correctiveActions"), "Corrective action", 4),
        "preventive_measures": clean_list_text(payload.get("preventiveMeasures"), "Preventive measure", 4),
        "status": status,
        "photos": clean_photo_keys(payload.get("photoKeys")),
    }


def validate_violation(payload: dict[str, Any]) -> dict[str, Any]:
    record = {
        "project_name": clean_text(payload.get("projectName"), "Project name"),
        "violation_date": clean_text(payload.get("violationDate"), "Date", 10),
        "employee_name": clean_text(payload.get("employeeName"), "Employee name"),
        "employee_id": clean_text(payload.get("employeeId"), "Employee ID / Iqama No.", 80, False),
        "company_contractor": clean_text(payload.get("companyContractor"), "Company / Contractor"),
        "job_title": clean_text(payload.get("jobTitle"), "Job title", 200, False),
        "violation_location": clean_text(payload.get("violationLocation"), "Violation location"),
        "violation_type": clean_text(payload.get("violationType"), "Type of violation", 200),
        "violation_description": clean_text(payload.get("violationDescription"), "Description of violation", 3000),
        "deduction_amount": clean_text(payload.get("deductionAmount"), "Deduction amount", 80, False),
        "issued_by_name": clean_text(payload.get("issuedByName"), "Issued by (name)"),
        "issued_by_position": clean_text(payload.get("issuedByPosition"), "Issued by (position)", 200, False),
    }
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", record["violation_date"]):
        raise ValueError("Enter a valid date.")
    photos = clean_photo_keys(payload.get("photoKeys"))
    return {
        **record,
        "actions": clean_choices(payload.get("actions"), "Action taken", VIOLATION_ACTIONS),
        "photos_attached": 1 if photos else 0,
        "documents_attached": 1 if payload.get("documentsAttached") is True else 0,
        "photos": photos,
    }


def admin_required(view: Any) -> Any:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not session.get("admin"):
            return redirect(url_for("admin", next=request.full_path))
        return view(*args, **kwargs)
    return wrapped


def filtered_rows(table: str, search_columns: list[str], date_column: str, limit: int = 1000) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    query = request.args.get("q", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    if query:
        pattern = f"%{query.lower().replace('%', '').replace('_', '')}%"
        clauses.append("(" + " OR ".join(f"LOWER({column}) LIKE ?" for column in search_columns) + ")")
        params.extend([pattern] * len(search_columns))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_from):
        clauses.append(f"{date_column} >= ?")
        params.append(date_from)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_to):
        clauses.append(f"{date_column} <= ?")
        params.append(date_to)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql(f"SELECT * FROM {table}{where} ORDER BY created_at DESC LIMIT ?"), [*params, limit])
        rows = cursor.fetchall()
    return [dict(row) for row in rows]


def filtered_records(limit: int = 1000) -> list[dict[str, Any]]:
    return filtered_rows("inspections", ["report_no", "inspected_by", "contractor", "work_location"], "inspection_date", limit)


def filtered_near_miss(limit: int = 1000) -> list[dict[str, Any]]:
    return filtered_rows("near_miss_reports", ["report_no", "reported_by", "department_project", "location"], "incident_date", limit)


def filtered_violations(limit: int = 1000) -> list[dict[str, Any]]:
    return filtered_rows("violation_notices", ["violation_no", "employee_name", "company_contractor", "violation_location"], "violation_date", limit)


def week_start(date_str: str) -> str | None:
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (parsed - timedelta(days=parsed.weekday())).isoformat()


def compute_trends(weeks: int = 12) -> list[dict[str, Any]]:
    today = datetime.now(timezone.utc).date()
    this_monday = today - timedelta(days=today.weekday())
    week_starts = [this_monday - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    cutoff = week_starts[0].isoformat()

    buckets = {
        w.isoformat(): {"inspections": 0, "score_sum": 0.0, "score_count": 0, "non_compliant": 0, "near_miss": 0, "violations": 0}
        for w in week_starts
    }

    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT inspection_date, score, non_compliant FROM inspections WHERE inspection_date >= ?"), [cutoff])
        for row in cursor.fetchall():
            key = week_start(row["inspection_date"])
            if key in buckets:
                buckets[key]["inspections"] += 1
                buckets[key]["score_sum"] += float(row["score"])
                buckets[key]["score_count"] += 1
                buckets[key]["non_compliant"] += int(row["non_compliant"])
        cursor.execute(sql("SELECT incident_date FROM near_miss_reports WHERE incident_date >= ?"), [cutoff])
        for row in cursor.fetchall():
            key = week_start(row["incident_date"])
            if key in buckets:
                buckets[key]["near_miss"] += 1
        cursor.execute(sql("SELECT violation_date FROM violation_notices WHERE violation_date >= ?"), [cutoff])
        for row in cursor.fetchall():
            key = week_start(row["violation_date"])
            if key in buckets:
                buckets[key]["violations"] += 1

    result = []
    for w in week_starts:
        bucket = buckets[w.isoformat()]
        avg_score = round(bucket["score_sum"] / bucket["score_count"], 1) if bucket["score_count"] else None
        result.append({
            "label": w.strftime("%b %-d") if os.name != "nt" else w.strftime("%b %#d"),
            "avg_score": avg_score,
            "non_compliant": bucket["non_compliant"],
            "inspections": bucket["inspections"],
            "near_miss": bucket["near_miss"],
            "violations": bucket["violations"],
        })
    return result


@app.after_request
def security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data: https://*.backblazeb2.com; style-src 'self'; script-src 'self'; connect-src 'self' https://*.backblazeb2.com; base-uri 'self'; frame-ancestors 'none'")
    return response


@app.get("/")
def index() -> str:
    return render_template("index.html", total_items=len(CHECKLIST_ITEMS))


@app.get("/sw.js")
def service_worker() -> Response:
    response = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/near-miss")
def near_miss_form() -> str:
    return render_template("near_miss.html", near_miss_types=NEAR_MISS_TYPES, root_causes=ROOT_CAUSES, statuses=NEAR_MISS_STATUSES)


@app.get("/violation")
def violation_form() -> str:
    return render_template("violation.html", actions=VIOLATION_ACTIONS)


UPLOAD_CONTENT_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


@app.post("/api/uploads/<token>")
def presign_upload(token: str) -> tuple[Response, int] | Response:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,64}", token):
        return jsonify({"error": "Invalid upload session."}), 400
    if not b2_configured():
        return jsonify({"error": "Photo storage is not configured."}), 503
    payload = request.get_json(silent=True) or {}
    ext = UPLOAD_CONTENT_TYPES.get(payload.get("contentType"))
    if not ext:
        return jsonify({"error": "Only JPEG, PNG, or WEBP photos are supported."}), 400
    key = f"uploads/{token}/{secrets.token_hex(10)}.{ext}"
    try:
        # The browser uploads the photo bytes straight to B2 with this URL — our server
        # never proxies the file itself, only signs where it's allowed to go.
        upload_url = b2_client().generate_presigned_url(
            "put_object",
            Params={"Bucket": B2_BUCKET, "Key": key, "ContentType": payload["contentType"]},
            ExpiresIn=300,
        )
    except Exception as error:
        app.logger.exception("Could not create an upload URL")
        message = "The photo could not be uploaded."
        # Only ever shown to a logged-in admin testing the form, never to a site worker submitting a report.
        if session.get("admin"):
            # botocore's own message is often generic (e.g. ConnectionClosedError); the
            # underlying urllib3/socket error it wraps has the actually useful detail.
            underlying = getattr(error, "kwargs", {}).get("error") if hasattr(error, "kwargs") else None
            detail = f"{error!r} <- {underlying!r}" if underlying else repr(error)
            message = f"{message} ({detail})"
        return jsonify({"error": message}), 500
    return jsonify({"uploadUrl": upload_url, "key": key}), 201


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
        created_at = datetime.now(timezone.utc).isoformat()
        with database() as connection:
            cursor = connection.cursor()
            cursor.execute(sql("SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM inspections"))
            seq = cursor.fetchone()["next_seq"]
            report_no = f"OHS-{seq:03d}"
            values = [
                record_id, seq, report_no, record["project_name"], record["work_location"], record["contractor"],
                record["inspected_by"], record["inspection_date"], record["inspection_time"], record["shift"],
                json.dumps(record["responses"]), json.dumps(record["response_notes"]), record["remarks"],
                record["signoff_name"], record["signed"], record["total_inspected"], record["compliant"],
                record["non_compliant"], record["not_applicable"], record["score"], created_at,
            ]
            placeholders = ",".join("?" for _ in values)
            cursor.execute(sql(f"INSERT INTO inspections VALUES ({placeholders})"), values)
        return jsonify({"id": record_id, "reportNo": report_no, "score": record["score"], "nonCompliant": record["non_compliant"]}), 201
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except HTTPException:
        raise
    except Exception as error:
        message = str(error)
        if "unique" in message.lower():
            return jsonify({"error": "That report number already exists. Enter another report number."}), 409
        app.logger.exception("Inspection submission failed")
        return jsonify({"error": "The inspection could not be saved."}), 500


@app.post("/api/near-miss")
def submit_near_miss() -> tuple[Response, int] | Response:
    try:
        payload = request.get_json(force=True, silent=False)
        if not isinstance(payload, dict):
            raise ValueError("The near-miss report data is invalid.")
        record = validate_near_miss(payload)
        record_id = secrets.token_hex(16)
        created_at = datetime.now(timezone.utc).isoformat()
        with database() as connection:
            cursor = connection.cursor()
            cursor.execute(sql("SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM near_miss_reports"))
            seq = cursor.fetchone()["next_seq"]
            report_no = f"NEAR-MISS-{seq:03d}"
            values = [
                record_id, seq, report_no, record["department_project"], record["incident_date"], record["incident_time"],
                record["location"], record["reported_by"], record["what_happened"], record["could_have_happened"],
                json.dumps(record["near_miss_types"]), record["near_miss_type_other"], record["immediate_actions"],
                record["hazard_eliminated"], record["hazard_actions_required"], record["investigation_lead"],
                record["investigation_date"], json.dumps(record["root_causes"]), record["root_cause_other"],
                record["root_cause_detail"], json.dumps(record["corrective_actions"]), json.dumps(record["preventive_measures"]),
                record["person_responsible"], record["target_completion_date"], record["reported_by_signoff"],
                record["hse_manager_signoff"], record["followup_by"], record["followup_date"], record["status"],
                record["status_reason"], json.dumps(record["photos"]), created_at,
            ]
            placeholders = ",".join("?" for _ in values)
            cursor.execute(sql(f"INSERT INTO near_miss_reports VALUES ({placeholders})"), values)
        return jsonify({"id": record_id, "reportNo": report_no}), 201
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except HTTPException:
        raise
    except Exception as error:
        message = str(error)
        if "unique" in message.lower():
            return jsonify({"error": "That report number already exists. Enter another report number."}), 409
        app.logger.exception("Near-miss report submission failed")
        return jsonify({"error": "The near-miss report could not be saved."}), 500


@app.post("/api/violations")
def submit_violation() -> tuple[Response, int] | Response:
    try:
        payload = request.get_json(force=True, silent=False)
        if not isinstance(payload, dict):
            raise ValueError("The violation notice data is invalid.")
        record = validate_violation(payload)
        record_id = secrets.token_hex(16)
        created_at = datetime.now(timezone.utc).isoformat()
        with database() as connection:
            cursor = connection.cursor()
            cursor.execute(sql("SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM violation_notices"))
            seq = cursor.fetchone()["next_seq"]
            violation_no = f"VIOLATION-{seq:03d}"
            values = [
                record_id, seq, violation_no, record["project_name"], record["violation_date"], record["employee_name"],
                record["employee_id"], record["company_contractor"], record["job_title"], record["violation_location"],
                record["violation_type"], record["violation_description"], json.dumps(record["actions"]),
                record["deduction_amount"], record["photos_attached"], record["documents_attached"],
                record["issued_by_name"], record["issued_by_position"], json.dumps(record["photos"]), created_at,
            ]
            placeholders = ",".join("?" for _ in values)
            cursor.execute(sql(f"INSERT INTO violation_notices VALUES ({placeholders})"), values)
        return jsonify({"id": record_id, "violationNo": violation_no}), 201
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except HTTPException:
        raise
    except Exception as error:
        message = str(error)
        if "unique" in message.lower():
            return jsonify({"error": "That violation number already exists. Enter another violation number."}), 409
        app.logger.exception("Violation notice submission failed")
        return jsonify({"error": "The violation notice could not be saved."}), 500


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

    view = request.args.get("view", "inspections")
    if view not in {"inspections", "near-miss", "violations", "trends"}:
        view = "inspections"

    with database() as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT COUNT(*) AS c FROM inspections")
        inspections_count = cursor.fetchone()["c"]
        cursor.execute("SELECT COUNT(*) AS c FROM near_miss_reports")
        near_miss_count = cursor.fetchone()["c"]
        cursor.execute("SELECT COUNT(*) AS c FROM violation_notices")
        violations_count = cursor.fetchone()["c"]

    records: list[dict[str, Any]] = []
    near_miss_records: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    trends: list[dict[str, Any]] = []
    total = average = non_compliant = 0
    if view == "inspections":
        records = filtered_records()
        total = len(records)
        average = round(sum(float(record["score"]) for record in records) / total, 1) if total else 0
        non_compliant = sum(int(record["non_compliant"]) for record in records)
    elif view == "near-miss":
        near_miss_records = filtered_near_miss()
    elif view == "violations":
        violations = filtered_violations()
    else:
        trends = compute_trends()

    trend_charts = {}
    if view == "trends":
        trend_charts = {
            "score": line_chart_svg(trends, "avg_score", "#0A1F8F"),
            "non_compliant": bar_chart_svg(trends, "non_compliant", "#ad3328"),
            "volume": grouped_bar_chart_svg(trends, [
                ("inspections", "#2a78d6", "Inspections"),
                ("near_miss", "#eb6834", "Near-Miss"),
                ("violations", "#1baf7a", "Violations"),
            ]),
        }

    return render_template(
        "admin.html",
        view=view,
        records=records,
        total=total,
        average=average,
        non_compliant=non_compliant,
        near_miss_records=near_miss_records,
        violations=violations,
        trends=trends,
        trend_charts=trend_charts,
        inspections_count=inspections_count,
        near_miss_count=near_miss_count,
        violations_count=violations_count,
    )


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


@app.post("/admin/records/<record_id>/delete")
@admin_required
def delete_record(record_id: str) -> Response:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("DELETE FROM inspections WHERE id = ?"), [record_id])
    return redirect(url_for("admin", view="inspections"))


@app.get("/admin/near-miss/<record_id>")
@admin_required
def near_miss_detail(record_id: str) -> str | tuple[str, int]:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT * FROM near_miss_reports WHERE id = ?"), [record_id])
        row = cursor.fetchone()
    if not row:
        return "Record not found", 404
    record = dict(row)
    for field in ("near_miss_types", "root_causes", "corrective_actions", "preventive_measures", "photos"):
        record[field] = safe_json_list(record[field])
    return render_template("near_miss_record.html", record=record, photo_urls=photo_urls(record["photos"]))


@app.post("/admin/near-miss/<record_id>/delete")
@admin_required
def delete_near_miss(record_id: str) -> Response:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT photos FROM near_miss_reports WHERE id = ?"), [record_id])
        row = cursor.fetchone()
        cursor.execute(sql("DELETE FROM near_miss_reports WHERE id = ?"), [record_id])
    if row:
        delete_photos(safe_json_list(row["photos"]))
    return redirect(url_for("admin", view="near-miss"))


@app.get("/admin/violations/<record_id>")
@admin_required
def violation_detail(record_id: str) -> str | tuple[str, int]:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT * FROM violation_notices WHERE id = ?"), [record_id])
        row = cursor.fetchone()
    if not row:
        return "Record not found", 404
    record = dict(row)
    record["actions"] = safe_json_list(record["actions"])
    record["photos"] = safe_json_list(record["photos"])
    return render_template("violation_record.html", record=record, photo_urls=photo_urls(record["photos"]))


@app.post("/admin/violations/<record_id>/delete")
@admin_required
def delete_violation(record_id: str) -> Response:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT photos FROM violation_notices WHERE id = ?"), [record_id])
        row = cursor.fetchone()
        cursor.execute(sql("DELETE FROM violation_notices WHERE id = ?"), [record_id])
    if row:
        delete_photos(safe_json_list(row["photos"]))
    return redirect(url_for("admin", view="violations"))


def inspections_csv(records: list[dict[str, Any]], detailed: bool) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    if detailed:
        writer.writerow(["Report No.", "Date", "Project", "Work Location / Zone", "Contractor / Subcontractor", "Inspected By", "Section", "Requirement", "Result", "Observation / Action"])
        for record in records:
            responses = json.loads(record["responses"])
            notes = json.loads(record["response_notes"])
            for item in CHECKLIST_ITEMS:
                writer.writerow([record["report_no"], record["inspection_date"], record["project_name"], record["work_location"], record["contractor"], record["inspected_by"], item["section_title"], item["text"], responses.get(item["id"], ""), notes.get(item["id"], "")])
    else:
        writer.writerow(["Report No.", "Date", "Time", "Project", "Work Location / Zone", "Contractor / Subcontractor", "Inspected By", "Shift", "Total Inspected", "Compliant", "Non-Compliant", "N/A", "Compliance Score (%)", "Remarks", "Signed By", "Submitted At"])
        for record in records:
            writer.writerow([record["report_no"], record["inspection_date"], record["inspection_time"], record["project_name"], record["work_location"], record["contractor"], record["inspected_by"], record["shift"], record["total_inspected"], record["compliant"], record["non_compliant"], record["not_applicable"], record["score"], record["remarks"], record["signoff_name"], record["created_at"]])
    return output.getvalue()


def near_miss_csv(records: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Report No.", "Date", "Time", "Department / Project", "Location", "Reported By", "Type of Near Miss",
        "What Happened", "What Could Have Happened", "Immediate Actions", "Hazard Eliminated",
        "Root Causes", "Corrective Actions", "Preventive Measures", "Person Responsible",
        "Target Completion Date", "Status", "Submitted At",
    ])
    for record in records:
        writer.writerow([
            record["report_no"], record["incident_date"], record["incident_time"], record["department_project"],
            record["location"], record["reported_by"], "; ".join(safe_json_list(record["near_miss_types"])),
            record["what_happened"], record["could_have_happened"], record["immediate_actions"],
            record["hazard_eliminated"], "; ".join(safe_json_list(record["root_causes"])),
            "; ".join(safe_json_list(record["corrective_actions"])), "; ".join(safe_json_list(record["preventive_measures"])),
            record["person_responsible"], record["target_completion_date"], record["status"], record["created_at"],
        ])
    return output.getvalue()


def violations_csv(records: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Violation No.", "Date", "Project", "Employee Name", "Employee ID", "Company / Contractor", "Job Title",
        "Location", "Type of Violation", "Description", "Action Taken", "Deduction Amount",
        "Issued By", "Position", "Submitted At",
    ])
    for record in records:
        writer.writerow([
            record["violation_no"], record["violation_date"], record["project_name"], record["employee_name"],
            record["employee_id"], record["company_contractor"], record["job_title"], record["violation_location"],
            record["violation_type"], record["violation_description"], "; ".join(safe_json_list(record["actions"])),
            record["deduction_amount"], record["issued_by_name"], record["issued_by_position"], record["created_at"],
        ])
    return output.getvalue()


@app.get("/admin/export")
@admin_required
def export_records() -> Response:
    kind = "detailed" if request.args.get("kind") == "detailed" else "summary"
    csv_text = inspections_csv(filtered_records(limit=5000), detailed=(kind == "detailed"))
    filename = f'diriyah-ohs-{kind}-{datetime.now(timezone.utc).date().isoformat()}.csv'
    return Response("\ufeff" + csv_text, mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/admin/export/near-miss")
@admin_required
def export_near_miss() -> Response:
    csv_text = near_miss_csv(filtered_near_miss(limit=5000))
    filename = f'diriyah-near-miss-{datetime.now(timezone.utc).date().isoformat()}.csv'
    return Response("\ufeff" + csv_text, mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/admin/export/violations")
@admin_required
def export_violations() -> Response:
    csv_text = violations_csv(filtered_violations(limit=5000))
    filename = f'diriyah-violations-{datetime.now(timezone.utc).date().isoformat()}.csv'
    return Response("\ufeff" + csv_text, mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/admin/backup")
def backup_all() -> Response | tuple[Response, int]:
    expected = os.environ.get("EXPORT_TOKEN")
    supplied = request.args.get("token") or request.headers.get("X-Export-Token", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        return jsonify({"error": "Unauthorized"}), 401

    today = datetime.now(timezone.utc).date().isoformat()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"inspections-summary-{today}.csv", "\ufeff" + inspections_csv(filtered_records(limit=5000), detailed=False))
        archive.writestr(f"inspections-detailed-{today}.csv", "\ufeff" + inspections_csv(filtered_records(limit=5000), detailed=True))
        archive.writestr(f"near-miss-{today}.csv", "\ufeff" + near_miss_csv(filtered_near_miss(limit=5000)))
        archive.writestr(f"violations-{today}.csv", "\ufeff" + violations_csv(filtered_violations(limit=5000)))
    buffer.seek(0)
    filename = f"diriyah-ohs-backup-{today}.zip"
    return Response(buffer.getvalue(), mimetype="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/health")
def health() -> Response:
    return jsonify({"status": "ok"})


@app.errorhandler(Exception)
def handle_unexpected_error(error: Exception) -> Any:
    if isinstance(error, HTTPException):
        # JSON API routes (fetch()-driven, e.g. photo upload) need a parseable body even
        # for errors Werkzeug raises itself, like a 413 for a request over MAX_CONTENT_LENGTH —
        # otherwise the client tries to JSON.parse() Werkzeug's HTML error page and breaks.
        if request.path.startswith("/api/"):
            return jsonify({"error": error.description}), error.code
        return error
    app.logger.exception("Unhandled exception")
    # Only ever shown to an authenticated admin, so a raw traceback is safe here
    # and lets a real crash be diagnosed straight from the page instead of the host's logs.
    if session.get("admin"):
        return Response(f"Internal Server Error\n\n{traceback.format_exc()}", mimetype="text/plain"), 500
    return jsonify({"error": "Internal Server Error"}), 500


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
