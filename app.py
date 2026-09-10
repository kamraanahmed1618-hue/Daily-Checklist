from __future__ import annotations

import base64
import csv
import hmac
import html
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
import traceback
import zipfile
from contextlib import contextmanager
from email.message import EmailMessage
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from PIL import Image, ImageOps
from xhtml2pdf import pisa

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
SITE_TZ = ZoneInfo("Asia/Riyadh")


def _b2_endpoint_host() -> str | None:
    endpoint = os.environ.get("B2_ENDPOINT")
    if not endpoint:
        return None
    if not endpoint.startswith("http"):
        endpoint = f"https://{endpoint}"
    return endpoint.split("//", 1)[-1].split("/", 1)[0]


# Photo URLs are presigned against B2's real endpoint host (e.g.
# "s3.us-west-004.backblazeb2.com"), which is two subdomain labels deep — a CSP
# host wildcard like "*.backblazeb2.com" only ever matches one label, so it silently
# blocks the inline <img> (though a direct link to the same URL still opens fine,
# since img-src doesn't govern navigation). Naming the exact configured host avoids
# that mismatch instead of guessing at a wildcard pattern.
_B2_IMG_HOST = _b2_endpoint_host()
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:" + (f" https://{_B2_IMG_HOST}" if _B2_IMG_HOST else "") +
    "; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'"
)


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


def fetch_photo_bytes(key: str) -> tuple[bytes, str] | None:
    """Download one photo's bytes from B2, for bundling into a zip. Extension is taken
    from the stored key (already validated against PHOTO_KEY_PATTERN at upload time)."""
    try:
        obj = b2_client().get_object(Bucket=B2_BUCKET, Key=key)
        data = obj["Body"].read()
    except Exception:
        app.logger.exception("Failed to download photo for zip export: %s", key)
        return None
    extension = key.rsplit(".", 1)[-1] if "." in key else "jpg"
    return data, extension


def safe_archive_folder(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "", text).strip()
    return cleaned[:60] or "record"


def render_pdf(html_content: str) -> bytes:
    buffer = io.BytesIO()
    pisa.CreatePDF(html_content, dest=buffer)
    buffer.seek(0)
    return buffer.getvalue()


def photo_data_uri(data: bytes, extension: str) -> str:
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(extension, "image/jpeg")
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


PDF_PHOTO_MAX_DIMENSION = 900
PDF_PHOTO_JPEG_QUALITY = 70


def resized_photo_for_pdf(data: bytes, extension: str) -> tuple[bytes, str]:
    """Downscale and re-encode a photo before embedding it in a PDF. Uploaded photos can
    be up to 8MB each, so embedding the originals bloated bundle PDFs to hundreds of MB
    and made them slow to generate."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            # A phone photo taken in portrait is often stored with the sensor's native
            # (landscape) pixels plus an EXIF tag saying "rotate this for display" —
            # browsers apply that automatically, but PIL doesn't, so without this the
            # PDF would embed it sideways even though it looks upright everywhere else.
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGB")
            image.thumbnail((PDF_PHOTO_MAX_DIMENSION, PDF_PHOTO_MAX_DIMENSION))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=PDF_PHOTO_JPEG_QUALITY, optimize=True)
            return buffer.getvalue(), "jpg"
    except Exception:
        app.logger.exception("Failed to resize photo for PDF; embedding original")
        return data, extension


def record_pdf_html(
    title: str,
    band_title: str,
    info_rows: list[tuple[str, str]],
    summary: str,
    bullets: list[str],
    photo_sections: list[tuple[str, list[tuple[bytes, str]]]],
    doc_number: str,
) -> str:
    """Builds a self-contained, table-based HTML document for xhtml2pdf. xhtml2pdf
    doesn't implement CSS variables, grid, or flexbox (confirmed by prototyping against
    the real print stylesheet — photos in a CSS grid just stack in one column), so this
    uses a separate, deliberately plain layout rather than reusing the browser templates."""
    esc = html.escape
    info_html = "".join(f'<tr><td class="label">{esc(label)}</td><td>{esc(value)}</td></tr>' for label, value in info_rows)
    summary_html = f'<div class="summary">{esc(summary)}</div>' if summary else ""
    bullets_html = ""
    if bullets:
        items = "".join(f"<li>{esc(item)}</li>" for item in bullets)
        bullets_html = f'<div class="band">Key Lessons and Safe Working Practices</div><ul class="lessons">{items}</ul>'

    photo_sections_html = ""
    for section_title, photos in photo_sections:
        photo_sections_html += f'<div class="band">{esc(section_title)}</div>'
        if not photos:
            photo_sections_html += '<p class="note">No photos attached.</p>'
            continue
        rows = ""
        for index in range(0, len(photos), 2):
            pair = photos[index:index + 2]
            cells = "".join(f'<td><img src="{photo_data_uri(*resized_photo_for_pdf(data, extension))}"></td>' for data, extension in pair)
            if len(pair) == 1:
                cells += "<td></td>"
            rows += f"<tr>{cells}</tr>"
        photo_sections_html += f'<table class="photos">{rows}</table>'

    footer = "Classification: BEC Arabia - External - Unrestricted Use"
    if doc_number:
        footer += f" &middot; {esc(doc_number)}"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body {{ font-family: Helvetica, Arial, sans-serif; font-size: 9pt; color: #111; }}
.title {{ font-size: 15pt; font-weight: bold; color: #0a1f8f; margin: 0 0 10px; text-align: center; }}
table.info {{ width: 100%; border-collapse: collapse; margin-bottom: 10px; }}
table.info td {{ border: 1px solid #000; padding: 4px 8px; font-size: 9pt; }}
table.info td.label {{ background: #dae9f8; font-weight: bold; width: 28%; }}
.band {{ background: #002060; color: #fff; font-weight: bold; text-align: center; padding: 6px; margin: 12px 0 6px; font-size: 10.5pt; }}
.summary {{ background: #eaf2f8; border: 1px solid #c3d8e8; padding: 8px 10px; margin-bottom: 10px; font-size: 9pt; }}
ul.lessons {{ margin: 0 0 10px 18px; padding: 0; font-size: 9pt; }}
ul.lessons li {{ margin-bottom: 3px; }}
table.photos {{ width: 100%; border-collapse: collapse; margin-bottom: 10px; }}
table.photos td {{ border: 1px solid #000; width: 50%; text-align: center; padding: 4px; }}
table.photos img {{ width: 100%; height: auto; }}
.note {{ font-size: 9pt; margin-bottom: 10px; }}
.footer {{ border-top: 1px solid #000; padding-top: 4px; font-size: 7pt; color: #555; margin-top: 10px; }}
</style></head>
<body>
<div class="title">{esc(title)}</div>
<div class="band">{esc(band_title)}</div>
<table class="info">{info_html}</table>
{summary_html}
{bullets_html}
{photo_sections_html}
<div class="footer">{footer}</div>
</body></html>"""


def record_photos(keys: list[str]) -> list[tuple[bytes, str]]:
    fetched = [fetch_photo_bytes(key) for key in keys]
    return [item for item in fetched if item]


def near_miss_pdf_bytes(record: dict[str, Any]) -> bytes:
    return render_pdf(record_pdf_html(
        title="Near Miss Reporting Form",
        band_title="Report Details",
        info_rows=[
            ("Report No.", record["report_no"]), ("Department / Project", record["department_project"]),
            ("Date", record["incident_date"]), ("Time", record["incident_time"]),
            ("Location", record["location"]), ("Reported By", record["reported_by"]),
            ("What Happened", record["what_happened"]), ("Status", record.get("status") or "—"),
        ],
        summary="", bullets=[],
        photo_sections=[("Attached Images", record_photos(safe_json_list(record["photos"])))],
        doc_number="BECCO-COR-OHS-ADD-MMR-000001-R01",
    ))


def violation_pdf_bytes(record: dict[str, Any]) -> bytes:
    return render_pdf(record_pdf_html(
        title="Occupational Health & Safety Violation Notice",
        band_title="Violation Details",
        info_rows=[
            ("Violation No.", record["violation_no"]), ("Date", record["violation_date"]),
            ("Employee Name", record["employee_name"]), ("Company / Contractor", record["company_contractor"]),
            ("Location", record["violation_location"]), ("Type", record["violation_type"]),
            ("Description", record["violation_description"]),
            ("Action Taken", "; ".join(safe_json_list(record["actions"])) or "—"),
            ("Issued By", record["issued_by_name"]),
        ],
        summary="", bullets=[],
        photo_sections=[("Evidence", record_photos(safe_json_list(record["photos"])))],
        doc_number="BECCO-COR-OHS-ADD-VNC-000002-R01",
    ))


def training_pdf_bytes(record: dict[str, Any]) -> bytes:
    type_label = TRAINING_TYPE_LABELS.get(record["session_type"], record["session_type"])
    return render_pdf(record_pdf_html(
        title=record["topic"],
        band_title=f"{type_label} Record",
        info_rows=[
            ("No.", str(record["seq"])), ("Topic", record["topic"]), ("Date", record["session_date"]),
            ("Location", record["location"] or "—"), ("Conducted By", record["trainer"] or "—"),
            ("Duration", record["duration"] or "—"),
            ("Attendees", str(record["attendees_count"]) if record["attendees_count"] is not None else "—"),
        ],
        summary=record["summary"], bullets=safe_json_list(record["key_lessons"]),
        photo_sections=[
            ("Photographic Record", record_photos(safe_json_list(record["photos"]))),
            ("Attendance Record", record_photos(safe_json_list(record["attendance_photos"]))),
        ],
        doc_number="",
    ))


def build_bundle_zip(csv_filename: str, csv_text: str, pdf_entries: list[tuple[str, bytes]], photo_entries: list[tuple[str, str]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(csv_filename, "﻿" + csv_text)
        for archive_path, pdf_bytes in pdf_entries:
            archive.writestr(f"{archive_path}.pdf", pdf_bytes)
        for archive_path, key in photo_entries:
            fetched = fetch_photo_bytes(key)
            if not fetched:
                continue
            data, extension = fetched
            archive.writestr(f"{archive_path}.{extension}", data)
    buffer.seek(0)
    return buffer.getvalue()


def build_photos_zip(entries: list[tuple[str, str]]) -> bytes:
    """entries: (archive_path_without_extension, b2_key) pairs. A key that fails to
    download is skipped rather than failing the whole export."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for archive_path, key in entries:
            fetched = fetch_photo_bytes(key)
            if not fetched:
                continue
            data, extension = fetched
            archive.writestr(f"{archive_path}.{extension}", data)
    buffer.seek(0)
    return buffer.getvalue()


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
        """CREATE TABLE IF NOT EXISTS ptw_logs (
            id TEXT PRIMARY KEY,
            seq INTEGER,
            ptw_number TEXT NOT NULL,
            issuer TEXT NOT NULL,
            receiver TEXT NOT NULL,
            ptw_type TEXT NOT NULL,
            work_description TEXT NOT NULL DEFAULT '',
            area_hse_personnel TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            shift TEXT NOT NULL DEFAULT '',
            start_date TEXT NOT NULL,
            start_time TEXT NOT NULL DEFAULT '',
            end_date TEXT NOT NULL DEFAULT '',
            end_time TEXT NOT NULL DEFAULT '',
            company TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            workers_count INTEGER,
            reviewed_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS ptw_start_date_idx ON ptw_logs (start_date)",
        "CREATE INDEX IF NOT EXISTS ptw_created_idx ON ptw_logs (created_at)",
        "CREATE INDEX IF NOT EXISTS ptw_status_idx ON ptw_logs (status)",
        """CREATE TABLE IF NOT EXISTS training_logs (
            id TEXT PRIMARY KEY,
            seq INTEGER,
            session_type TEXT NOT NULL,
            topic TEXT NOT NULL,
            session_date TEXT NOT NULL,
            trainer TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            duration TEXT NOT NULL DEFAULT '',
            attendees_count INTEGER,
            objective TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            key_lessons TEXT NOT NULL DEFAULT '[]',
            remarks TEXT NOT NULL DEFAULT '',
            photos TEXT NOT NULL DEFAULT '[]',
            attendance_photos TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS training_date_idx ON training_logs (session_date)",
        "CREATE INDEX IF NOT EXISTS training_created_idx ON training_logs (created_at)",
        "CREATE INDEX IF NOT EXISTS training_type_idx ON training_logs (session_type)",
        """CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            occurred_at TEXT NOT NULL,
            action TEXT NOT NULL,
            record_type TEXT NOT NULL,
            record_ref TEXT NOT NULL,
            actor_name TEXT NOT NULL DEFAULT '',
            ip_address TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS audit_log_occurred_idx ON audit_log (occurred_at)",
        "CREATE INDEX IF NOT EXISTS audit_log_record_type_idx ON audit_log (record_type)",
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
        ensure_column("training_logs", "objective", "TEXT NOT NULL DEFAULT ''")
        ensure_column("training_logs", "summary", "TEXT NOT NULL DEFAULT ''")
        ensure_column("training_logs", "key_lessons", "TEXT NOT NULL DEFAULT '[]'")
        # Backfill sequential numbers for any pre-existing records in submission order.
        for table in ("inspections", "near_miss_reports", "violation_notices", "ptw_logs", "training_logs"):
            cursor.execute(sql(f"SELECT COALESCE(MAX(seq), 0) AS next_seq FROM {table}"))
            next_seq = cursor.fetchone()["next_seq"] or 0
            cursor.execute(sql(f"SELECT id FROM {table} WHERE seq IS NULL ORDER BY created_at ASC"))
            for row in cursor.fetchall():
                next_seq += 1
                cursor.execute(sql(f"UPDATE {table} SET seq = ? WHERE id = ?"), [next_seq, row["id"]])

    # In its own transaction: if pre-existing duplicate ptw_number values make this fail,
    # a Postgres transaction aborts entirely on any statement error, which would otherwise
    # silently roll back every migration above too. The app-level check in submit/edit
    # still catches new duplicates going forward even if this index can't be created yet.
    try:
        with database() as connection:
            connection.cursor().execute("CREATE UNIQUE INDEX IF NOT EXISTS ptw_number_unique_idx ON ptw_logs (ptw_number)")
    except Exception:
        app.logger.exception("Could not create unique index on ptw_logs.ptw_number (likely pre-existing duplicates)")


def log_audit(action: str, record_type: str, record_ref: str, actor_name: str) -> None:
    """Records who edited or deleted a record, and when — there are no individual admin
    accounts (one shared password), so actor_name is whatever the admin typed into the
    "Your name" field at the point of action, not an authenticated identity."""
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql(
            "INSERT INTO audit_log (id, occurred_at, action, record_type, record_ref, actor_name, ip_address) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)"
        ), [
            secrets.token_hex(12), datetime.now(timezone.utc).isoformat(), action, record_type, record_ref,
            (actor_name or "").strip()[:120], (request.remote_addr or "")[:64],
        ])


def send_notification_email(subject: str, body: str) -> None:
    """Best-effort alert for a newly-submitted violation or near-miss. Silently does
    nothing if the NOTIFY_* env vars aren't configured, and never raises — a failed or
    slow email must not break the actual submission, which has already been saved."""
    recipient = os.environ.get("NOTIFY_EMAIL_TO")
    sender = os.environ.get("NOTIFY_SMTP_USER")
    password = os.environ.get("NOTIFY_SMTP_PASSWORD")
    if not (recipient and sender and password):
        return
    try:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = sender
        message["To"] = recipient
        message.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
            smtp.login(sender, password)
            smtp.send_message(message)
    except Exception:
        app.logger.exception("Failed to send notification email")


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
PTW_TYPES = [
    "Cold work", "Hot work", "Electrical", "Lifting", "Excavation", "Scaffolding",
    "Work at Height", "Concrete", "Ground network", "Other",
]
PTW_SHIFTS = ["Day", "Night"]
PTW_STATUSES = ["open", "closed"]
TRAINING_TYPES = ["Induction", "TBT", "Mass TBT", "Specific Training"]
TRAINING_TYPE_LABELS = {
    "Induction": "Induction", "TBT": "TBT (Toolbox Talk)", "Mass TBT": "Mass TBT (large combined session)",
    "Specific Training": "Specific Training",
}


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


def clean_date(value: Any, field: str, required: bool = True) -> str:
    cleaned = clean_text(value, field, 10, required)
    if cleaned and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
        raise ValueError(f"Enter a valid {field}.")
    return cleaned


def clean_time(value: Any, field: str, required: bool = True) -> str:
    cleaned = clean_text(value, field, 5, required)
    if cleaned and not re.fullmatch(r"\d{2}:\d{2}", cleaned):
        raise ValueError(f"Enter a valid {field}.")
    return cleaned


def validate_ptw(payload: dict[str, Any]) -> dict[str, Any]:
    record = {
        "ptw_number": clean_text(payload.get("ptwNumber"), "PTW number", 80),
        "issuer": clean_text(payload.get("issuer"), "PTW issuer"),
        "receiver": clean_text(payload.get("receiver"), "PTW receiver"),
        "ptw_type": clean_text(payload.get("ptwType"), "Type of PTW", 80),
        "work_description": clean_text(payload.get("workDescription"), "Work description", 2000),
        "area_hse_personnel": clean_text(payload.get("areaHsePersonnel"), "Area HSE personnel", 200, False),
        "location": clean_text(payload.get("location"), "Location"),
        "shift": clean_text(payload.get("shift"), "Shift", 20, False),
        "start_date": clean_date(payload.get("startDate"), "PTW start date"),
        "start_time": clean_time(payload.get("startTime"), "PTW start time"),
        "end_date": clean_date(payload.get("endDate"), "PTW end date"),
        "end_time": clean_time(payload.get("endTime"), "PTW end time"),
        "company": clean_text(payload.get("company"), "Company name", 200),
        "status": clean_text(payload.get("status"), "Status", 20, False) or "open",
        "reviewed_by": clean_text(payload.get("reviewedBy"), "Reviewed by", 200, False),
    }
    if record["ptw_type"] not in PTW_TYPES:
        raise ValueError("Select a valid type of PTW.")
    if record["shift"] and record["shift"] not in PTW_SHIFTS:
        raise ValueError("Shift must be Day or Night.")
    if record["status"] not in PTW_STATUSES:
        raise ValueError("Status must be open or closed.")

    workers_raw = payload.get("workersCount")
    workers_count = None
    if workers_raw not in (None, ""):
        try:
            workers_count = int(workers_raw)
        except (TypeError, ValueError):
            raise ValueError("Number of workers must be a whole number.")
        if workers_count < 0 or workers_count > 9999:
            raise ValueError("Number of workers must be between 0 and 9999.")

    return {**record, "workers_count": workers_count}


def validate_training(payload: dict[str, Any]) -> dict[str, Any]:
    record = {
        "session_type": clean_text(payload.get("sessionType"), "Session type", 40),
        "topic": clean_text(payload.get("topic"), "Topic"),
        "session_date": clean_date(payload.get("sessionDate"), "Session date"),
        # Not required: bulk-imported weekly training sheets don't record a trainer name.
        "trainer": clean_text(payload.get("trainer"), "Trainer / conducted by", 200, False),
        "location": clean_text(payload.get("location"), "Location", 200, False),
        "duration": clean_text(payload.get("duration"), "Duration", 60, False),
        "objective": clean_text(payload.get("objective"), "Objective / focus", 300, False),
        "summary": clean_text(payload.get("summary"), "Summary", 3000, False),
        "remarks": clean_text(payload.get("remarks"), "Remarks", 2000, False),
    }
    if record["session_type"] not in TRAINING_TYPES:
        raise ValueError("Select a valid session type.")

    attendees_raw = payload.get("attendeesCount")
    attendees_count = None
    if attendees_raw not in (None, ""):
        try:
            attendees_count = int(attendees_raw)
        except (TypeError, ValueError):
            raise ValueError("Number of attendees must be a whole number.")
        if attendees_count < 0 or attendees_count > 9999:
            raise ValueError("Number of attendees must be between 0 and 9999.")

    return {
        **record,
        "attendees_count": attendees_count,
        "key_lessons": clean_list_text(payload.get("keyLessons"), "Key lesson", 10, 300),
        "photos": clean_photo_keys(payload.get("photoKeys")),
        "attendance_photos": clean_photo_keys(payload.get("attendancePhotoKeys")),
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


def auto_close_expired_ptw() -> None:
    """Flip any 'open' PTW entry whose end date/time has already passed to
    'closed', judged in Saudi local time since that's what the start/end
    fields actually mean to whoever filled in the form. Called at the start
    of every PTW read path (list, overview, detail, exports, counts) instead
    of via a separate scheduled job, since Render's free tier has no cron —
    this keeps the data self-correcting on every page view without needing
    any extra infrastructure."""
    now = datetime.now(SITE_TZ)
    today = now.strftime("%Y-%m-%d")
    now_time = now.strftime("%H:%M")
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql(
            "UPDATE ptw_logs SET status = 'closed', updated_at = ? "
            "WHERE status = 'open' AND (end_date < ? OR (end_date = ? AND end_time <= ?))"
        ), [datetime.now(timezone.utc).isoformat(), today, today, now_time])


def ptw_sort_key(record: dict[str, Any]) -> int:
    # PTW numbers are auto-suggested but the field stays free text, since entries
    # are occasionally logged out of sequence or renumbered later via edit — so
    # the list still needs to read in that number's order rather than submission order.
    match = re.search(r"(\d+)\s*$", record["ptw_number"] or "")
    return int(match.group(1)) if match else -1


def next_ptw_number() -> str:
    """Suggest the next PTW number as one past the highest logged so far, reusing
    whatever prefix that entry used (e.g. "BAJV-840" -> "BAJV-841"), so whoever is
    filling in the form doesn't have to check the log first to avoid clashing."""
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT ptw_number FROM ptw_logs"))
        rows = cursor.fetchall()
    best: tuple[int, str] | None = None
    for row in rows:
        match = re.search(r"^(.*?)(\d+)\s*$", row["ptw_number"] or "")
        if not match:
            continue
        number = int(match.group(2))
        if best is None or number > best[0]:
            best = (number, match.group(1))
    if best is None:
        return "BAJV-1"
    number, prefix = best
    return f"{prefix}{number + 1}"


def filtered_ptw(limit: int = 1000) -> list[dict[str, Any]]:
    auto_close_expired_ptw()
    records = filtered_rows("ptw_logs", ["ptw_number", "issuer", "receiver", "location", "company"], "start_date", limit)
    return sorted(records, key=ptw_sort_key, reverse=True)


def filtered_training(limit: int = 1000) -> list[dict[str, Any]]:
    return filtered_rows("training_logs", ["topic", "trainer", "location", "session_type"], "session_date", limit)


def record_counts() -> dict[str, int]:
    auto_close_expired_ptw()
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT COUNT(*) AS c FROM inspections")
        inspections = cursor.fetchone()["c"]
        cursor.execute("SELECT COUNT(*) AS c FROM near_miss_reports")
        near_miss = cursor.fetchone()["c"]
        cursor.execute("SELECT COUNT(*) AS c FROM violation_notices")
        violations = cursor.fetchone()["c"]
        cursor.execute("SELECT COUNT(*) AS c FROM ptw_logs")
        ptw = cursor.fetchone()["c"]
        cursor.execute(sql("SELECT COUNT(*) AS c FROM ptw_logs WHERE status = ?"), ["open"])
        ptw_open = cursor.fetchone()["c"]
        cursor.execute("SELECT COUNT(*) AS c FROM training_logs")
        training = cursor.fetchone()["c"]
    return {
        "inspections": inspections, "near_miss": near_miss, "violations": violations,
        "ptw": ptw, "ptw_open": ptw_open, "training": training,
    }


def current_work_week_range() -> tuple[str, str]:
    """The on-site work week runs Saturday-Thursday (Friday off) — returns
    [week_start, week_end) as ISO date strings in the site's timezone."""
    today = datetime.now(SITE_TZ).date()
    days_since_saturday = (today.weekday() - 5) % 7
    week_start = today - timedelta(days=days_since_saturday)
    week_end = week_start + timedelta(days=6)  # exclusive upper bound = Friday's date
    return week_start.isoformat(), week_end.isoformat()


def weekly_record_counts() -> dict[str, int]:
    """Homepage tile counts, scoped to the current Sat-Thu work week. Open PTW permits
    stays a live snapshot (currently active on site) rather than week-scoped, since a
    permit opened last week and still running is exactly what that tile should surface."""
    auto_close_expired_ptw()
    week_start, week_end = current_work_week_range()
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT COUNT(*) AS c FROM inspections WHERE inspection_date >= ? AND inspection_date < ?"), [week_start, week_end])
        inspections = cursor.fetchone()["c"]
        cursor.execute(sql("SELECT COUNT(*) AS c FROM near_miss_reports WHERE incident_date >= ? AND incident_date < ?"), [week_start, week_end])
        near_miss = cursor.fetchone()["c"]
        cursor.execute(sql("SELECT COUNT(*) AS c FROM violation_notices WHERE violation_date >= ? AND violation_date < ?"), [week_start, week_end])
        violations = cursor.fetchone()["c"]
        cursor.execute(sql("SELECT COUNT(*) AS c FROM ptw_logs WHERE status = ?"), ["open"])
        ptw_open = cursor.fetchone()["c"]
        cursor.execute(sql("SELECT COUNT(*) AS c FROM training_logs WHERE session_date >= ? AND session_date < ?"), [week_start, week_end])
        training = cursor.fetchone()["c"]
    week_end_display = (date.fromisoformat(week_end) - timedelta(days=1)).isoformat()
    return {
        "inspections": inspections, "near_miss": near_miss, "violations": violations,
        "ptw_open": ptw_open, "training": training,
        "week_start": week_start, "week_end": week_end_display,
    }


def ptw_overview() -> dict[str, Any]:
    """Snapshot of what's currently open on site: which areas have active permits,
    what activity is running in each, and how many of each permit type are open."""
    auto_close_expired_ptw()
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql(
            "SELECT location, ptw_type, COUNT(*) AS c FROM ptw_logs WHERE status = ? "
            "GROUP BY location, ptw_type ORDER BY location, ptw_type"
        ), ["open"])
        area_type_rows = [dict(row) for row in cursor.fetchall()]
        cursor.execute(sql(
            "SELECT ptw_type, COUNT(*) AS c FROM ptw_logs WHERE status = ? GROUP BY ptw_type ORDER BY c DESC"
        ), ["open"])
        by_type = [dict(row) for row in cursor.fetchall()]

    by_area: dict[str, dict[str, Any]] = {}
    for row in area_type_rows:
        area = row["location"] or "Unspecified"
        entry = by_area.setdefault(area, {"location": area, "total": 0, "types": []})
        entry["total"] += row["c"]
        entry["types"].append(f'{row["ptw_type"]} ({row["c"]})')
    by_area_list = sorted(by_area.values(), key=lambda entry: entry["total"], reverse=True)

    total_open = sum(item["c"] for item in by_type)
    hot_work_open = next((item["c"] for item in by_type if item["ptw_type"] == "Hot work"), 0)
    busiest_area = by_area_list[0]["location"] if by_area_list else "—"

    return {
        "total_open": total_open,
        "hot_work_open": hot_work_open,
        "areas_active": len(by_area_list),
        "busiest_area": busiest_area,
        "by_area": by_area_list,
        "by_type": by_type,
    }


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
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    if request.path.startswith("/admin"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def home() -> str:
    return render_template("home.html", counts=weekly_record_counts())


@app.get("/inspection")
def inspection_form() -> str:
    return render_template("index.html", total_items=len(CHECKLIST_ITEMS), sections=CHECKLIST)


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


@app.get("/ptw")
def ptw_form() -> str:
    return render_template("ptw.html", ptw_types=PTW_TYPES, shifts=PTW_SHIFTS, next_ptw_number=next_ptw_number())


@app.get("/training")
def training_form() -> str:
    return render_template("training.html", training_types=TRAINING_TYPES, training_type_labels=TRAINING_TYPE_LABELS)


def detect_image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


PHOTO_MAX_BYTES = 8 * 1024 * 1024


@app.post("/api/uploads/<token>")
def upload_photo(token: str) -> tuple[Response, int] | Response:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,64}", token):
        return jsonify({"error": "Invalid upload session."}), 400
    if not b2_configured():
        return jsonify({"error": "Photo storage is not configured."}), 503
    uploaded = request.files.get("photo")
    if not uploaded:
        return jsonify({"error": "No photo provided."}), 400
    data = uploaded.read(PHOTO_MAX_BYTES + 1)
    if len(data) > PHOTO_MAX_BYTES:
        return jsonify({"error": "Photo is too large (max 8 MB)."}), 400
    ext = detect_image_type(data)
    if not ext:
        return jsonify({"error": "Only JPEG, PNG, or WEBP photos are supported."}), 400
    key = f"uploads/{token}/{secrets.token_hex(10)}.{ext}"
    try:
        b2_client().put_object(Bucket=B2_BUCKET, Key=key, Body=data, ContentType=f"image/{ext}")
    except Exception as error:
        app.logger.exception("Photo upload failed")
        message = "The photo could not be uploaded."
        # Only ever shown to a logged-in admin testing the form, never to a site worker submitting a report.
        if session.get("admin"):
            # botocore's own message is often generic (e.g. ConnectionClosedError); the
            # underlying urllib3/socket error it wraps has the actually useful detail.
            underlying = getattr(error, "kwargs", {}).get("error") if hasattr(error, "kwargs") else None
            detail = f"{error!r} <- {underlying!r}" if underlying else repr(error)
            message = f"{message} ({detail})"
        return jsonify({"error": message}), 500
    return jsonify({"key": key}), 201


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
            columns = (
                "id, seq, report_no, project_name, work_location, contractor, inspected_by, "
                "inspection_date, inspection_time, shift, responses, response_notes, remarks, "
                "signoff_name, signed, total_inspected, compliant, non_compliant, not_applicable, "
                "score, created_at"
            )
            placeholders = ",".join("?" for _ in values)
            # Column names are spelled out (not just positional VALUES) because seq was
            # added to this table later via ALTER TABLE, which appends physically on any
            # database created before that migration ran — a positional INSERT there would
            # silently shift every later value into the wrong column instead of erroring.
            cursor.execute(sql(f"INSERT INTO inspections ({columns}) VALUES ({placeholders})"), values)
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
            columns = (
                "id, seq, report_no, department_project, incident_date, incident_time, "
                "location, reported_by, what_happened, could_have_happened, "
                "near_miss_types, near_miss_type_other, immediate_actions, "
                "hazard_eliminated, hazard_actions_required, investigation_lead, "
                "investigation_date, root_causes, root_cause_other, "
                "root_cause_detail, corrective_actions, preventive_measures, "
                "person_responsible, target_completion_date, reported_by_signoff, "
                "hse_manager_signoff, followup_by, followup_date, status, "
                "status_reason, photos, created_at"
            )
            placeholders = ",".join("?" for _ in values)
            cursor.execute(sql(f"INSERT INTO near_miss_reports ({columns}) VALUES ({placeholders})"), values)
        send_notification_email(
            f"Near-miss reported: {report_no}",
            "A new near-miss report was submitted.\n\n"
            f"Report No.: {report_no}\n"
            f"Location: {record['location']}\n"
            f"Date/time: {record['incident_date']} {record['incident_time']}\n"
            f"Reported by: {record['reported_by']}\n"
            f"What happened: {record['what_happened']}\n\n"
            f"View: {request.host_url.rstrip('/')}{url_for('near_miss_detail', record_id=record_id)}",
        )
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
            columns = (
                "id, seq, violation_no, project_name, violation_date, employee_name, "
                "employee_id, company_contractor, job_title, violation_location, "
                "violation_type, violation_description, actions, "
                "deduction_amount, photos_attached, documents_attached, "
                "issued_by_name, issued_by_position, photos, created_at"
            )
            placeholders = ",".join("?" for _ in values)
            cursor.execute(sql(f"INSERT INTO violation_notices ({columns}) VALUES ({placeholders})"), values)
        send_notification_email(
            f"Violation notice issued: {violation_no}",
            "A new violation notice was submitted.\n\n"
            f"Violation No.: {violation_no}\n"
            f"Employee: {record['employee_name']} ({record['company_contractor']})\n"
            f"Location: {record['violation_location']}\n"
            f"Type: {record['violation_type']}\n"
            f"Description: {record['violation_description']}\n\n"
            f"View: {request.host_url.rstrip('/')}{url_for('violation_detail', record_id=record_id)}",
        )
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


@app.post("/api/ptw")
def submit_ptw() -> tuple[Response, int] | Response:
    try:
        payload = request.get_json(force=True, silent=False)
        if not isinstance(payload, dict):
            raise ValueError("The PTW log data is invalid.")
        record = validate_ptw(payload)
        record_id = secrets.token_hex(16)
        now = datetime.now(timezone.utc).isoformat()
        with database() as connection:
            cursor = connection.cursor()
            cursor.execute(sql("SELECT 1 FROM ptw_logs WHERE ptw_number = ?"), [record["ptw_number"]])
            if cursor.fetchone():
                raise ValueError(f'PTW number "{record["ptw_number"]}" is already in use — choose a different number.')
            cursor.execute(sql("SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM ptw_logs"))
            seq = cursor.fetchone()["next_seq"]
            values = [
                record_id, seq, record["ptw_number"], record["issuer"], record["receiver"], record["ptw_type"],
                record["work_description"], record["area_hse_personnel"], record["location"], record["shift"],
                record["start_date"], record["start_time"], record["end_date"], record["end_time"],
                record["company"], record["status"], record["workers_count"], record["reviewed_by"], now, now,
            ]
            columns = (
                "id, seq, ptw_number, issuer, receiver, ptw_type, "
                "work_description, area_hse_personnel, location, shift, "
                "start_date, start_time, end_date, end_time, "
                "company, status, workers_count, reviewed_by, created_at, updated_at"
            )
            placeholders = ",".join("?" for _ in values)
            cursor.execute(sql(f"INSERT INTO ptw_logs ({columns}) VALUES ({placeholders})"), values)
        return jsonify({"id": record_id, "ptwNumber": record["ptw_number"]}), 201
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except HTTPException:
        raise
    except Exception:
        app.logger.exception("PTW log submission failed")
        return jsonify({"error": "The PTW log entry could not be saved."}), 500


@app.post("/api/training")
def submit_training() -> tuple[Response, int] | Response:
    try:
        payload = request.get_json(force=True, silent=False)
        if not isinstance(payload, dict):
            raise ValueError("The training log data is invalid.")
        record = validate_training(payload)
        record_id = secrets.token_hex(16)
        now = datetime.now(timezone.utc).isoformat()
        with database() as connection:
            cursor = connection.cursor()
            cursor.execute(sql("SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM training_logs"))
            seq = cursor.fetchone()["next_seq"]
            values = [
                record_id, seq, record["session_type"], record["topic"], record["session_date"],
                record["trainer"], record["location"], record["duration"], record["attendees_count"],
                record["objective"], record["summary"], json.dumps(record["key_lessons"]),
                record["remarks"], json.dumps(record["photos"]), json.dumps(record["attendance_photos"]), now,
            ]
            columns = (
                "id, seq, session_type, topic, session_date, "
                "trainer, location, duration, attendees_count, "
                "objective, summary, key_lessons, "
                "remarks, photos, attendance_photos, created_at"
            )
            placeholders = ",".join("?" for _ in values)
            cursor.execute(sql(f"INSERT INTO training_logs ({columns}) VALUES ({placeholders})"), values)
        return jsonify({"id": record_id, "topic": record["topic"]}), 201
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except HTTPException:
        raise
    except Exception:
        app.logger.exception("Training log submission failed")
        return jsonify({"error": "The training log entry could not be saved."}), 500


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
    if view not in {"inspections", "near-miss", "violations", "ptw", "training", "trends"}:
        view = "inspections"

    counts = record_counts()

    records: list[dict[str, Any]] = []
    near_miss_records: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    ptw_logs: list[dict[str, Any]] = []
    training_logs: list[dict[str, Any]] = []
    trends: list[dict[str, Any]] = []
    ptw_stats: dict[str, Any] = {}
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
    elif view == "ptw":
        ptw_logs = filtered_ptw()
        ptw_stats = ptw_overview()
    elif view == "training":
        training_logs = filtered_training()
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
        ptw_logs=ptw_logs,
        ptw_stats=ptw_stats,
        training_logs=training_logs,
        trends=trends,
        trend_charts=trend_charts,
        inspections_count=counts["inspections"],
        near_miss_count=counts["near_miss"],
        violations_count=counts["violations"],
        ptw_count=counts["ptw"],
        training_count=counts["training"],
        training_type_labels=TRAINING_TYPE_LABELS,
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


INSPECTION_HEADER_FIELDS = {
    "projectName": "project_name", "workLocation": "work_location", "contractor": "contractor",
    "inspectedBy": "inspected_by", "shift": "shift", "remarks": "remarks", "signoffName": "signoff_name",
}


def validate_inspection_header(payload: dict[str, Any]) -> dict[str, Any]:
    """A deliberately narrower edit path than validate_inspection: this only covers the
    header/metadata fields (project, location, contractor, date, signoff) — not the 102
    checklist responses themselves, which are far less likely to need a correction and
    would need a much larger dedicated edit form to do properly."""
    record = {
        "project_name": clean_text(payload.get("projectName"), "Project name"),
        "work_location": clean_text(payload.get("workLocation"), "Work location / zone"),
        "contractor": clean_text(payload.get("contractor"), "Contractor / subcontractor"),
        "inspected_by": clean_text(payload.get("inspectedBy"), "Inspected by"),
        "inspection_date": clean_date(payload.get("inspectionDate"), "Date"),
        "inspection_time": clean_time(payload.get("inspectionTime"), "Time"),
        "shift": clean_text(payload.get("shift"), "Shift", 80),
        "remarks": clean_text(payload.get("remarks"), "Remarks", 3000, False),
        "signoff_name": clean_text(payload.get("signoffName"), "Sign-off name"),
    }
    return record


@app.route("/admin/records/<record_id>/edit", methods=["GET", "POST"])
@admin_required
def record_edit(record_id: str) -> str | tuple[str, int] | Response:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT * FROM inspections WHERE id = ?"), [record_id])
        row = cursor.fetchone()
    if not row:
        return "Record not found", 404
    record = dict(row)
    error = ""
    if request.method == "POST":
        edited_by = request.form.get("editedBy", "").strip()
        form_payload = {form_key: request.form.get(form_key) for form_key in INSPECTION_HEADER_FIELDS}
        form_payload["inspectionDate"] = request.form.get("inspectionDate")
        form_payload["inspectionTime"] = request.form.get("inspectionTime")
        try:
            if not edited_by:
                raise ValueError("Your name is required to save changes.")
            updated = validate_inspection_header(form_payload)
            with database() as connection:
                cursor = connection.cursor()
                cursor.execute(sql(
                    "UPDATE inspections SET project_name=?, work_location=?, contractor=?, inspected_by=?, "
                    "inspection_date=?, inspection_time=?, shift=?, remarks=?, signoff_name=? WHERE id=?"
                ), [
                    updated["project_name"], updated["work_location"], updated["contractor"], updated["inspected_by"],
                    updated["inspection_date"], updated["inspection_time"], updated["shift"], updated["remarks"],
                    updated["signoff_name"], record_id,
                ])
            log_audit("updated", "inspection", record["report_no"], edited_by)
            return redirect(url_for("record_detail", record_id=record_id))
        except ValueError as err:
            error = str(err)
            record = {**record, **{db_key: form_payload[form_key] for form_key, db_key in INSPECTION_HEADER_FIELDS.items()}}
            record["inspection_date"] = form_payload["inspectionDate"]
            record["inspection_time"] = form_payload["inspectionTime"]
    return render_template("record_edit.html", record=record, error=error)


@app.post("/admin/records/<record_id>/delete")
@admin_required
def delete_record(record_id: str) -> Response | tuple[str, int]:
    deleted_by = request.form.get("deletedBy", "").strip()
    if not deleted_by:
        return "Your name is required to delete a record.", 400
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT report_no FROM inspections WHERE id = ?"), [record_id])
        row = cursor.fetchone()
        cursor.execute(sql("DELETE FROM inspections WHERE id = ?"), [record_id])
    if row:
        log_audit("deleted", "inspection", row["report_no"], deleted_by)
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


NEAR_MISS_FORM_FIELDS = {
    "departmentProject": "department_project", "incidentDate": "incident_date", "incidentTime": "incident_time",
    "location": "location", "reportedBy": "reported_by", "whatHappened": "what_happened",
    "couldHaveHappened": "could_have_happened", "nearMissTypeOther": "near_miss_type_other",
    "immediateActions": "immediate_actions", "hazardEliminated": "hazard_eliminated",
    "hazardActionsRequired": "hazard_actions_required", "investigationLead": "investigation_lead",
    "investigationDate": "investigation_date", "rootCauseOther": "root_cause_other",
    "rootCauseDetail": "root_cause_detail", "personResponsible": "person_responsible",
    "targetCompletionDate": "target_completion_date", "reportedBySignoff": "reported_by_signoff",
    "hseManagerSignoff": "hse_manager_signoff", "followupBy": "followup_by", "followupDate": "followup_date",
    "status": "status", "statusReason": "status_reason",
}


@app.route("/admin/near-miss/<record_id>/edit", methods=["GET", "POST"])
@admin_required
def near_miss_edit(record_id: str) -> str | tuple[str, int] | Response:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT * FROM near_miss_reports WHERE id = ?"), [record_id])
        row = cursor.fetchone()
    if not row:
        return "Record not found", 404
    record = dict(row)
    for field in ("near_miss_types", "root_causes", "corrective_actions", "preventive_measures", "photos"):
        record[field] = safe_json_list(record[field])
    error = ""
    if request.method == "POST":
        edited_by = request.form.get("editedBy", "").strip()
        form_payload = {form_key: request.form.get(form_key) for form_key in NEAR_MISS_FORM_FIELDS}
        form_payload["nearMissTypes"] = request.form.getlist("nearMissTypes")
        form_payload["rootCauses"] = request.form.getlist("rootCauses")
        form_payload["correctiveActions"] = [line.strip() for line in request.form.get("correctiveActions", "").splitlines()]
        form_payload["preventiveMeasures"] = [line.strip() for line in request.form.get("preventiveMeasures", "").splitlines()]
        try:
            if not edited_by:
                raise ValueError("Your name is required to save changes.")
            updated = validate_near_miss(form_payload)
            with database() as connection:
                cursor = connection.cursor()
                cursor.execute(sql(
                    "UPDATE near_miss_reports SET department_project=?, incident_date=?, incident_time=?, location=?, "
                    "reported_by=?, what_happened=?, could_have_happened=?, near_miss_types=?, near_miss_type_other=?, "
                    "immediate_actions=?, hazard_eliminated=?, hazard_actions_required=?, investigation_lead=?, "
                    "investigation_date=?, root_causes=?, root_cause_other=?, root_cause_detail=?, corrective_actions=?, "
                    "preventive_measures=?, person_responsible=?, target_completion_date=?, reported_by_signoff=?, "
                    "hse_manager_signoff=?, followup_by=?, followup_date=?, status=?, status_reason=? WHERE id=?"
                ), [
                    updated["department_project"], updated["incident_date"], updated["incident_time"], updated["location"],
                    updated["reported_by"], updated["what_happened"], updated["could_have_happened"],
                    json.dumps(updated["near_miss_types"]), updated["near_miss_type_other"],
                    updated["immediate_actions"], updated["hazard_eliminated"], updated["hazard_actions_required"],
                    updated["investigation_lead"], updated["investigation_date"], json.dumps(updated["root_causes"]),
                    updated["root_cause_other"], updated["root_cause_detail"], json.dumps(updated["corrective_actions"]),
                    json.dumps(updated["preventive_measures"]), updated["person_responsible"], updated["target_completion_date"],
                    updated["reported_by_signoff"], updated["hse_manager_signoff"], updated["followup_by"],
                    updated["followup_date"], updated["status"], updated["status_reason"], record_id,
                ])
            log_audit("updated", "near_miss", record["report_no"], edited_by)
            return redirect(url_for("near_miss_detail", record_id=record_id))
        except ValueError as err:
            error = str(err)
            record = {
                **record,
                **{db_key: form_payload[form_key] for form_key, db_key in NEAR_MISS_FORM_FIELDS.items()},
                "near_miss_types": form_payload["nearMissTypes"], "root_causes": form_payload["rootCauses"],
                "corrective_actions": form_payload["correctiveActions"], "preventive_measures": form_payload["preventiveMeasures"],
            }
    return render_template(
        "near_miss_edit.html", record=record, near_miss_types=NEAR_MISS_TYPES, root_causes=ROOT_CAUSES,
        statuses=NEAR_MISS_STATUSES, error=error,
    )


@app.post("/admin/near-miss/<record_id>/delete")
@admin_required
def delete_near_miss(record_id: str) -> Response | tuple[str, int]:
    deleted_by = request.form.get("deletedBy", "").strip()
    if not deleted_by:
        return "Your name is required to delete a record.", 400
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT report_no, photos FROM near_miss_reports WHERE id = ?"), [record_id])
        row = cursor.fetchone()
        cursor.execute(sql("DELETE FROM near_miss_reports WHERE id = ?"), [record_id])
    if row:
        delete_photos(safe_json_list(row["photos"]))
        log_audit("deleted", "near_miss", row["report_no"], deleted_by)
    return redirect(url_for("admin", view="near-miss"))


def photos_zip_response(keys: list[str], filename: str) -> Response | tuple[Response, int]:
    if not b2_configured():
        return jsonify({"error": "Photo storage is not configured."}), 503
    if not keys:
        return jsonify({"error": "No photos to download."}), 404
    entries = [(f"photo-{index}", key) for index, key in enumerate(keys, start=1)]
    zip_bytes = build_photos_zip(entries)
    return Response(zip_bytes, mimetype="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/admin/near-miss/<record_id>/photos.zip")
@admin_required
def near_miss_photos_zip(record_id: str) -> Response | tuple[Response, int]:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT report_no, photos FROM near_miss_reports WHERE id = ?"), [record_id])
        row = cursor.fetchone()
    if not row:
        return jsonify({"error": "Record not found."}), 404
    return photos_zip_response(safe_json_list(row["photos"]), f'{row["report_no"]}-photos.zip')


@app.get("/admin/near-miss/<record_id>/report.pdf")
@admin_required
def near_miss_pdf(record_id: str) -> Response | tuple[Response, int]:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT * FROM near_miss_reports WHERE id = ?"), [record_id])
        row = cursor.fetchone()
    if not row:
        return jsonify({"error": "Record not found."}), 404
    record = dict(row)
    pdf_bytes = near_miss_pdf_bytes(record)
    return Response(pdf_bytes, mimetype="application/pdf", headers={"Content-Disposition": f'attachment; filename="{record["report_no"]}.pdf"'})


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


VIOLATION_FORM_FIELDS = {
    "projectName": "project_name", "violationDate": "violation_date", "employeeName": "employee_name",
    "employeeId": "employee_id", "companyContractor": "company_contractor", "jobTitle": "job_title",
    "violationLocation": "violation_location", "violationType": "violation_type",
    "violationDescription": "violation_description", "deductionAmount": "deduction_amount",
    "issuedByName": "issued_by_name", "issuedByPosition": "issued_by_position",
}


@app.route("/admin/violations/<record_id>/edit", methods=["GET", "POST"])
@admin_required
def violation_edit(record_id: str) -> str | tuple[str, int] | Response:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT * FROM violation_notices WHERE id = ?"), [record_id])
        row = cursor.fetchone()
    if not row:
        return "Record not found", 404
    record = dict(row)
    record["actions"] = safe_json_list(record["actions"])
    record["photos"] = safe_json_list(record["photos"])
    error = ""
    if request.method == "POST":
        edited_by = request.form.get("editedBy", "").strip()
        form_payload = {form_key: request.form.get(form_key) for form_key in VIOLATION_FORM_FIELDS}
        form_payload["actions"] = request.form.getlist("actions")
        try:
            if not edited_by:
                raise ValueError("Your name is required to save changes.")
            updated = validate_violation(form_payload)
            with database() as connection:
                cursor = connection.cursor()
                cursor.execute(sql(
                    "UPDATE violation_notices SET project_name=?, violation_date=?, employee_name=?, employee_id=?, "
                    "company_contractor=?, job_title=?, violation_location=?, violation_type=?, "
                    "violation_description=?, deduction_amount=?, actions=?, issued_by_name=?, issued_by_position=? "
                    "WHERE id=?"
                ), [
                    updated["project_name"], updated["violation_date"], updated["employee_name"], updated["employee_id"],
                    updated["company_contractor"], updated["job_title"], updated["violation_location"],
                    updated["violation_type"], updated["violation_description"], updated["deduction_amount"],
                    json.dumps(updated["actions"]), updated["issued_by_name"], updated["issued_by_position"],
                    record_id,
                ])
            log_audit("updated", "violation", record["violation_no"], edited_by)
            return redirect(url_for("violation_detail", record_id=record_id))
        except ValueError as err:
            error = str(err)
            record = {
                **record,
                **{db_key: form_payload[form_key] for form_key, db_key in VIOLATION_FORM_FIELDS.items()},
                "actions": form_payload["actions"],
            }
    return render_template("violation_edit.html", record=record, violation_actions=VIOLATION_ACTIONS, error=error)


@app.post("/admin/violations/<record_id>/delete")
@admin_required
def delete_violation(record_id: str) -> Response | tuple[str, int]:
    deleted_by = request.form.get("deletedBy", "").strip()
    if not deleted_by:
        return "Your name is required to delete a record.", 400
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT violation_no, photos FROM violation_notices WHERE id = ?"), [record_id])
        row = cursor.fetchone()
        cursor.execute(sql("DELETE FROM violation_notices WHERE id = ?"), [record_id])
    if row:
        delete_photos(safe_json_list(row["photos"]))
        log_audit("deleted", "violation", row["violation_no"], deleted_by)
    return redirect(url_for("admin", view="violations"))


@app.get("/admin/violations/<record_id>/photos.zip")
@admin_required
def violation_photos_zip(record_id: str) -> Response | tuple[Response, int]:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT violation_no, photos FROM violation_notices WHERE id = ?"), [record_id])
        row = cursor.fetchone()
    if not row:
        return jsonify({"error": "Record not found."}), 404
    return photos_zip_response(safe_json_list(row["photos"]), f'{row["violation_no"]}-photos.zip')


@app.get("/admin/violations/<record_id>/notice.pdf")
@admin_required
def violation_pdf(record_id: str) -> Response | tuple[Response, int]:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT * FROM violation_notices WHERE id = ?"), [record_id])
        row = cursor.fetchone()
    if not row:
        return jsonify({"error": "Record not found."}), 404
    record = dict(row)
    pdf_bytes = violation_pdf_bytes(record)
    return Response(pdf_bytes, mimetype="application/pdf", headers={"Content-Disposition": f'attachment; filename="{record["violation_no"]}.pdf"'})


PTW_FORM_FIELDS = {
    "ptwNumber": "ptw_number", "issuer": "issuer", "receiver": "receiver", "ptwType": "ptw_type",
    "workDescription": "work_description", "areaHsePersonnel": "area_hse_personnel", "location": "location",
    "shift": "shift", "startDate": "start_date", "startTime": "start_time", "endDate": "end_date",
    "endTime": "end_time", "company": "company", "status": "status", "workersCount": "workers_count",
    "reviewedBy": "reviewed_by",
}


@app.route("/admin/ptw/<record_id>", methods=["GET", "POST"])
@admin_required
def ptw_detail(record_id: str) -> str | tuple[str, int] | Response:
    if request.method == "GET":
        auto_close_expired_ptw()
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT * FROM ptw_logs WHERE id = ?"), [record_id])
        row = cursor.fetchone()
    if not row:
        return "Record not found", 404
    record = dict(row)
    error = ""
    if request.method == "POST":
        form_payload = {key: request.form.get(key) for key in PTW_FORM_FIELDS}
        edited_by = request.form.get("editedBy", "").strip()
        try:
            if not edited_by:
                raise ValueError("Your name is required to save changes.")
            updated = validate_ptw(form_payload)
            with database() as connection:
                cursor = connection.cursor()
                cursor.execute(sql("SELECT 1 FROM ptw_logs WHERE ptw_number = ? AND id != ?"), [updated["ptw_number"], record_id])
                if cursor.fetchone():
                    raise ValueError(f'PTW number "{updated["ptw_number"]}" is already in use — choose a different number.')
                cursor.execute(sql(
                    "UPDATE ptw_logs SET ptw_number=?, issuer=?, receiver=?, ptw_type=?, work_description=?, "
                    "area_hse_personnel=?, location=?, shift=?, start_date=?, start_time=?, end_date=?, end_time=?, "
                    "company=?, status=?, workers_count=?, reviewed_by=?, updated_at=? WHERE id=?"
                ), [
                    updated["ptw_number"], updated["issuer"], updated["receiver"], updated["ptw_type"],
                    updated["work_description"], updated["area_hse_personnel"], updated["location"], updated["shift"],
                    updated["start_date"], updated["start_time"], updated["end_date"], updated["end_time"],
                    updated["company"], updated["status"], updated["workers_count"], updated["reviewed_by"],
                    datetime.now(timezone.utc).isoformat(), record_id,
                ])
            log_audit("updated", "ptw", updated["ptw_number"], edited_by)
            return redirect(url_for("admin", view="ptw"))
        except ValueError as err:
            error = str(err)
            record = {**record, **{db_key: form_payload[form_key] for form_key, db_key in PTW_FORM_FIELDS.items()}}
    return render_template("ptw_edit.html", record=record, ptw_types=PTW_TYPES, shifts=PTW_SHIFTS, statuses=PTW_STATUSES, error=error)


@app.post("/admin/ptw/<record_id>/delete")
@admin_required
def delete_ptw(record_id: str) -> Response | tuple[str, int]:
    deleted_by = request.form.get("deletedBy", "").strip()
    if not deleted_by:
        return "Your name is required to delete a record.", 400
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT ptw_number FROM ptw_logs WHERE id = ?"), [record_id])
        row = cursor.fetchone()
        cursor.execute(sql("DELETE FROM ptw_logs WHERE id = ?"), [record_id])
    if row:
        log_audit("deleted", "ptw", row["ptw_number"], deleted_by)
    return redirect(url_for("admin", view="ptw"))


@app.get("/admin/training/<record_id>")
@admin_required
def training_detail(record_id: str) -> str | tuple[str, int]:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT * FROM training_logs WHERE id = ?"), [record_id])
        row = cursor.fetchone()
    if not row:
        return "Record not found", 404
    record = dict(row)
    record["photos"] = safe_json_list(record["photos"])
    record["attendance_photos"] = safe_json_list(record["attendance_photos"])
    record["key_lessons"] = safe_json_list(record["key_lessons"])
    return render_template(
        "training_record.html",
        record=record,
        photo_urls=photo_urls(record["photos"]),
        attendance_photo_urls=photo_urls(record["attendance_photos"]),
        training_type_labels=TRAINING_TYPE_LABELS,
    )


TRAINING_FORM_FIELDS = {
    "sessionType": "session_type", "topic": "topic", "sessionDate": "session_date", "trainer": "trainer",
    "location": "location", "duration": "duration", "objective": "objective", "summary": "summary",
    "remarks": "remarks",
}


@app.route("/admin/training/<record_id>/edit", methods=["GET", "POST"])
@admin_required
def training_edit(record_id: str) -> str | tuple[str, int] | Response:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT * FROM training_logs WHERE id = ?"), [record_id])
        row = cursor.fetchone()
    if not row:
        return "Record not found", 404
    record = dict(row)
    record["photos"] = safe_json_list(record["photos"])
    record["attendance_photos"] = safe_json_list(record["attendance_photos"])
    record["key_lessons"] = safe_json_list(record["key_lessons"])
    error = ""
    if request.method == "POST":
        edited_by = request.form.get("editedBy", "").strip()
        form_payload = {form_key: request.form.get(form_key) for form_key in TRAINING_FORM_FIELDS}
        form_payload["attendeesCount"] = request.form.get("attendeesCount")
        form_payload["keyLessons"] = [line.strip() for line in request.form.get("keyLessons", "").splitlines()]
        try:
            if not edited_by:
                raise ValueError("Your name is required to save changes.")
            updated = validate_training(form_payload)
            with database() as connection:
                cursor = connection.cursor()
                cursor.execute(sql(
                    "UPDATE training_logs SET session_type=?, topic=?, session_date=?, trainer=?, location=?, "
                    "duration=?, attendees_count=?, objective=?, summary=?, key_lessons=?, remarks=? WHERE id=?"
                ), [
                    updated["session_type"], updated["topic"], updated["session_date"], updated["trainer"],
                    updated["location"], updated["duration"], updated["attendees_count"], updated["objective"],
                    updated["summary"], json.dumps(updated["key_lessons"]), updated["remarks"], record_id,
                ])
            log_audit("updated", "training", f'{record["seq"]} - {record["topic"]}', edited_by)
            return redirect(url_for("training_detail", record_id=record_id))
        except ValueError as err:
            error = str(err)
            record = {
                **record,
                **{db_key: form_payload[form_key] for form_key, db_key in TRAINING_FORM_FIELDS.items()},
                "attendees_count": form_payload["attendeesCount"], "key_lessons": form_payload["keyLessons"],
            }
    return render_template(
        "training_edit.html", record=record, training_types=TRAINING_TYPES,
        training_type_labels=TRAINING_TYPE_LABELS, error=error,
    )


@app.post("/admin/training/<record_id>/delete")
@admin_required
def delete_training(record_id: str) -> Response | tuple[str, int]:
    deleted_by = request.form.get("deletedBy", "").strip()
    if not deleted_by:
        return "Your name is required to delete a record.", 400
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT topic, seq, photos, attendance_photos FROM training_logs WHERE id = ?"), [record_id])
        row = cursor.fetchone()
        cursor.execute(sql("DELETE FROM training_logs WHERE id = ?"), [record_id])
    if row:
        delete_photos(safe_json_list(row["photos"]) + safe_json_list(row["attendance_photos"]))
        log_audit("deleted", "training", f'{row["seq"]} - {row["topic"]}', deleted_by)
    return redirect(url_for("admin", view="training"))


@app.get("/admin/training/<record_id>/photos.zip")
@admin_required
def training_photos_zip(record_id: str) -> Response | tuple[Response, int]:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT seq, topic, photos FROM training_logs WHERE id = ?"), [record_id])
        row = cursor.fetchone()
    if not row:
        return jsonify({"error": "Record not found."}), 404
    return photos_zip_response(safe_json_list(row["photos"]), f'training-{row["seq"]}-photos.zip')


@app.get("/admin/training/<record_id>/attendance.zip")
@admin_required
def training_attendance_zip(record_id: str) -> Response | tuple[Response, int]:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT seq, topic, attendance_photos FROM training_logs WHERE id = ?"), [record_id])
        row = cursor.fetchone()
    if not row:
        return jsonify({"error": "Record not found."}), 404
    return photos_zip_response(safe_json_list(row["attendance_photos"]), f'training-{row["seq"]}-attendance.zip')


@app.get("/admin/training/<record_id>/record.pdf")
@admin_required
def training_pdf(record_id: str) -> Response | tuple[Response, int]:
    with database() as connection:
        cursor = connection.cursor()
        cursor.execute(sql("SELECT * FROM training_logs WHERE id = ?"), [record_id])
        row = cursor.fetchone()
    if not row:
        return jsonify({"error": "Record not found."}), 404
    record = dict(row)
    pdf_bytes = training_pdf_bytes(record)
    return Response(pdf_bytes, mimetype="application/pdf", headers={"Content-Disposition": f'attachment; filename="training-{record["seq"]}.pdf"'})


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


def ptw_csv(records: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "S.N", "PTW Number", "PTW Issuer", "PTW Receiver", "Type of PTW", "Work Description",
        "Area HSE Personnel", "Location", "Shift", "PTW Start Date & Time", "PTW End Date & Time",
        "Company Name", "Status", "No. of Workers", "Reviewed By",
    ])
    for record in records:
        writer.writerow([
            record["seq"], record["ptw_number"], record["issuer"], record["receiver"], record["ptw_type"],
            record["work_description"], record["area_hse_personnel"], record["location"], record["shift"],
            f'{record["start_date"]} {record["start_time"]}'.strip(), f'{record["end_date"]} {record["end_time"]}'.strip(),
            record["company"], record["status"], record["workers_count"] if record["workers_count"] is not None else "",
            record["reviewed_by"],
        ])
    return output.getvalue()


PTW_XLSX_HEADERS = [
    "S.N", "PTW Number", "PTW Issuer", "PTW Receiver", "Type of PTW", "Work Description",
    "Area HSE Personnel", "Location", "Shift", "PTW Start Date & Time", "PTW End Date & Time",
    "Company Name", "Status", "No. of Workers", "Reviewed By",
]
PTW_XLSX_COLUMN_WIDTHS = [7, 16, 18, 20, 14, 42, 18, 16, 9, 20, 20, 22, 10, 12, 26]
PTW_XLSX_OPEN_FILL = PatternFill("solid", fgColor="FFFF00")


def ptw_xlsx(records: list[dict[str, Any]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "PTW Log"

    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="0A1F8F")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for col, title in enumerate(PTW_XLSX_HEADERS, start=1):
        cell = sheet.cell(row=1, column=col, value=title)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
    sheet.row_dimensions[1].height = 30
    sheet.freeze_panes = "A2"

    for col, width in enumerate(PTW_XLSX_COLUMN_WIDTHS, start=1):
        sheet.column_dimensions[get_column_letter(col)].width = width

    for row_index, record in enumerate(records, start=2):
        values = [
            record["seq"], record["ptw_number"], record["issuer"], record["receiver"], record["ptw_type"],
            record["work_description"], record["area_hse_personnel"], record["location"], record["shift"],
            f'{record["start_date"]} {record["start_time"]}'.strip(), f'{record["end_date"]} {record["end_time"]}'.strip(),
            record["company"], record["status"], record["workers_count"] if record["workers_count"] is not None else "",
            record["reviewed_by"],
        ]
        for col, value in enumerate(values, start=1):
            cell = sheet.cell(row=row_index, column=col, value=value)
            # Matches the yellow highlight used by hand in the original spreadsheet for open permits.
            if record["status"] == "open":
                cell.fill = PTW_XLSX_OPEN_FILL

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def training_csv(records: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "S.N", "Session Type", "Topic", "Date", "Trainer", "Location", "Duration", "Attendees",
        "Objective", "Summary", "Key Lessons", "Remarks", "Submitted At",
    ])
    for record in records:
        writer.writerow([
            record["seq"], record["session_type"], record["topic"], record["session_date"], record["trainer"],
            record["location"], record["duration"], record["attendees_count"] if record["attendees_count"] is not None else "",
            record["objective"], record["summary"], "; ".join(safe_json_list(record["key_lessons"])),
            record["remarks"], record["created_at"],
        ])
    return output.getvalue()


WEEKLY_TRAINING_HEADER = "In-house Training"
WEEKLY_INDUCTION_LABEL = "Total no . of Employees Inducted This Week"


def parse_weekly_training_xlsx(data: bytes) -> list[dict[str, Any]]:
    """Parse the site's own "Safety Training Status" weekly template: a short sheet with a
    Safety Induction summary line, then an In-house Training table (topic, date, attendee
    count, duration) that runs until the first blank topic cell. Scans for those labels by
    text instead of hardcoding row numbers, since the training table can be shorter or
    longer some weeks."""
    workbook = load_workbook(io.BytesIO(data), data_only=True)
    sheet = workbook.worksheets[0]
    records: list[dict[str, Any]] = []

    for row in sheet.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and WEEKLY_INDUCTION_LABEL in cell.value:
                # The count sits in a "Numbers" column further along the same row (its exact
                # position varies with the label's merged cell span), so scan the row for it
                # rather than assuming a fixed offset from the label cell.
                match = None
                for other in row:
                    if other.value and re.search(r"\d+\s*\(\s*\d+\s*Session", str(other.value)):
                        match = re.search(r"(\d+)\s*(?:\(\s*(\d+)\s*Session)?", str(other.value))
                        break
                if match:
                    inducted = int(match.group(1))
                    sessions = int(match.group(2)) if match.group(2) else 1
                    records.append({
                        "session_type": "Induction", "topic": "Weekly Safety Induction",
                        "session_date": "", "trainer": "", "location": "", "duration": "",
                        "attendees_count": inducted,
                        "remarks": f"{sessions} induction session(s) this week (from uploaded weekly file)",
                    })

    header_row = None
    for row in sheet.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and cell.value.strip() == WEEKLY_TRAINING_HEADER:
                header_row = cell.row
                break
        if header_row:
            break
    if header_row:
        for row_index in range(header_row + 1, sheet.max_row + 1):
            topic = sheet.cell(row=row_index, column=2).value
            if not topic or not str(topic).strip():
                break
            date_value = sheet.cell(row=row_index, column=4).value
            session_date = date_value.strftime("%Y-%m-%d") if hasattr(date_value, "strftime") else ""
            attendees = sheet.cell(row=row_index, column=5).value
            duration = sheet.cell(row=row_index, column=6).value
            records.append({
                "session_type": "Specific Training", "topic": str(topic).strip(),
                "session_date": session_date, "trainer": "", "location": "",
                "duration": str(duration).strip() if duration else "",
                "attendees_count": int(attendees) if isinstance(attendees, (int, float)) else None,
                "remarks": "Imported from uploaded weekly training file",
            })
    return records


@app.post("/admin/training/import")
@admin_required
def import_training() -> tuple[Response, int] | Response:
    uploaded = request.files.get("file")
    if not uploaded:
        return jsonify({"error": "No file provided."}), 400
    try:
        parsed = parse_weekly_training_xlsx(uploaded.read())
    except Exception:
        app.logger.exception("Weekly training file import failed")
        return jsonify({"error": "Could not read that file. Make sure it's the weekly training status template."}), 400
    if not parsed:
        return jsonify({"error": "No training sessions or induction summary were found in that file."}), 400

    now = datetime.now(timezone.utc).isoformat()
    imported = 0
    with database() as connection:
        cursor = connection.cursor()
        for entry in parsed:
            try:
                record = validate_training({
                    "sessionType": entry["session_type"], "topic": entry["topic"],
                    "sessionDate": entry["session_date"] or datetime.now(timezone.utc).date().isoformat(),
                    "trainer": entry["trainer"], "location": entry["location"], "duration": entry["duration"],
                    "attendeesCount": entry["attendees_count"], "remarks": entry["remarks"],
                })
            except ValueError:
                continue
            cursor.execute(sql("SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM training_logs"))
            seq = cursor.fetchone()["next_seq"]
            values = [
                secrets.token_hex(16), seq, record["session_type"], record["topic"], record["session_date"],
                record["trainer"], record["location"], record["duration"], record["attendees_count"],
                record["objective"], record["summary"], json.dumps(record["key_lessons"]),
                record["remarks"], json.dumps(record["photos"]), json.dumps(record["attendance_photos"]), now,
            ]
            columns = (
                "id, seq, session_type, topic, session_date, "
                "trainer, location, duration, attendees_count, "
                "objective, summary, key_lessons, "
                "remarks, photos, attendance_photos, created_at"
            )
            placeholders = ",".join("?" for _ in values)
            cursor.execute(sql(f"INSERT INTO training_logs ({columns}) VALUES ({placeholders})"), values)
            imported += 1
    return jsonify({"imported": imported}), 201


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


@app.get("/admin/export/near-miss/bundle.zip")
@admin_required
def export_near_miss_bundle() -> Response:
    records = filtered_near_miss(limit=5000)
    pdf_entries = []
    photo_entries = []
    for record in records:
        folder = safe_archive_folder(record["report_no"])
        try:
            pdf_entries.append((f"{folder}/{record['report_no']}", near_miss_pdf_bytes(record)))
        except Exception:
            app.logger.exception("Failed to render near-miss PDF for %s", record["report_no"])
        for index, key in enumerate(safe_json_list(record["photos"]), start=1):
            photo_entries.append((f"{folder}/photo-{index}", key))
    zip_bytes = build_bundle_zip(
        "near-miss.csv", near_miss_csv(records), pdf_entries,
        photo_entries if b2_configured() else [],
    )
    filename = f'diriyah-near-miss-bundle-{datetime.now(timezone.utc).date().isoformat()}.zip'
    return Response(zip_bytes, mimetype="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/admin/export/violations")
@admin_required
def export_violations() -> Response:
    csv_text = violations_csv(filtered_violations(limit=5000))
    filename = f'diriyah-violations-{datetime.now(timezone.utc).date().isoformat()}.csv'
    return Response("\ufeff" + csv_text, mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/admin/export/violations/bundle.zip")
@admin_required
def export_violations_bundle() -> Response:
    records = filtered_violations(limit=5000)
    pdf_entries = []
    photo_entries = []
    for record in records:
        folder = safe_archive_folder(record["violation_no"])
        try:
            pdf_entries.append((f"{folder}/{record['violation_no']}", violation_pdf_bytes(record)))
        except Exception:
            app.logger.exception("Failed to render violation PDF for %s", record["violation_no"])
        for index, key in enumerate(safe_json_list(record["photos"]), start=1):
            photo_entries.append((f"{folder}/photo-{index}", key))
    zip_bytes = build_bundle_zip(
        "violations.csv", violations_csv(records), pdf_entries,
        photo_entries if b2_configured() else [],
    )
    filename = f'diriyah-violations-bundle-{datetime.now(timezone.utc).date().isoformat()}.zip'
    return Response(zip_bytes, mimetype="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/admin/export/ptw")
@admin_required
def export_ptw() -> Response:
    csv_text = ptw_csv(filtered_ptw(limit=5000))
    filename = f'diriyah-ptw-log-{datetime.now(timezone.utc).date().isoformat()}.csv'
    return Response("\ufeff" + csv_text, mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/admin/export/ptw.xlsx")
@admin_required
def export_ptw_xlsx() -> Response:
    workbook_bytes = ptw_xlsx(filtered_ptw(limit=5000))
    filename = f'diriyah-ptw-log-{datetime.now(timezone.utc).date().isoformat()}.xlsx'
    return Response(
        workbook_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/admin/export/training")
@admin_required
def export_training() -> Response:
    csv_text = training_csv(filtered_training(limit=5000))
    filename = f'diriyah-training-log-{datetime.now(timezone.utc).date().isoformat()}.csv'
    return Response("﻿" + csv_text, mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/admin/export/training/bundle.zip")
@admin_required
def export_training_bundle() -> Response:
    records = filtered_training(limit=5000)
    pdf_entries = []
    photo_entries = []
    for record in records:
        folder = safe_archive_folder(f'{record["seq"]}-{record["topic"]}')
        try:
            pdf_entries.append((f"{folder}/{folder}", training_pdf_bytes(record)))
        except Exception:
            app.logger.exception("Failed to render training PDF for %s", record["topic"])
        for index, key in enumerate(safe_json_list(record["photos"]), start=1):
            photo_entries.append((f"{folder}/photos/photo-{index}", key))
        for index, key in enumerate(safe_json_list(record["attendance_photos"]), start=1):
            photo_entries.append((f"{folder}/attendance/photo-{index}", key))
    zip_bytes = build_bundle_zip(
        "training-log.csv", training_csv(records), pdf_entries,
        photo_entries if b2_configured() else [],
    )
    filename = f'diriyah-training-bundle-{datetime.now(timezone.utc).date().isoformat()}.zip'
    return Response(zip_bytes, mimetype="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


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
        archive.writestr(f"ptw-log-{today}.csv", "\ufeff" + ptw_csv(filtered_ptw(limit=5000)))
        archive.writestr(f"training-log-{today}.csv", "\ufeff" + training_csv(filtered_training(limit=5000)))
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
