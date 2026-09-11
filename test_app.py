import csv
import io
import json
import os
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import openpyxl


TEST_FILES = tempfile.TemporaryDirectory()
os.environ["OHS_DB_PATH"] = os.path.join(TEST_FILES.name, "test.db")
os.environ["ADMIN_PASSWORD"] = "test-admin-password"
os.environ["SECRET_KEY"] = "test-secret-key-that-is-only-used-by-the-automated-suite"
os.environ["EXPORT_TOKEN"] = "test-export-token"

from app import CHECKLIST, CHECKLIST_ITEMS, app, database  # noqa: E402


class ChecklistApplicationTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        self.client = app.test_client()
        with database() as connection:
            connection.execute("DELETE FROM inspections")
            connection.execute("DELETE FROM near_miss_reports")
            connection.execute("DELETE FROM violation_notices")
            connection.execute("DELETE FROM ptw_logs")
            connection.execute("DELETE FROM training_logs")
            connection.execute("DELETE FROM audit_log")

    def payload(self):
        return {
            "projectName": "1 Hotel Diriyah",
            "workLocation": "Zone 3",
            "contractor": "Test Contractor",
            "inspectedBy": "QA Inspector",
            "inspectionDate": "2026-08-15",
            "inspectionTime": "14:30",
            "shift": "Day",
            "remarks": "Automated verification record.",
            "signoffName": "QA Inspector",
            "signed": True,
            "responses": {item["id"]: "Y" for item in CHECKLIST_ITEMS},
            "responseNotes": {},
        }

    def login(self):
        return self.client.post("/admin", data={"password": "test-admin-password"})

    def near_miss_payload(self):
        return {
            "departmentProject": "Zone 3",
            "incidentDate": "2026-08-16",
            "incidentTime": "10:00",
            "location": "Podium Level 2",
            "reportedBy": "Foreman A",
            "whatHappened": "Ladder slipped on a wet floor.",
            "nearMissTypes": ["Unsafe Condition"],
            "reportedBySignoff": "Foreman A",
        }

    def violation_payload(self):
        return {
            "projectName": "1 Hotel Diriyah",
            "violationDate": "2026-08-16",
            "employeeName": "John Doe",
            "companyContractor": "BEC Arabia Contracting",
            "violationLocation": "Zone 2",
            "violationType": "No PPE",
            "violationDescription": "Worker observed without a hard hat in an active work zone.",
            "actions": ["First Warning"],
            "issuedByName": "Site HSE Officer",
        }

    def ptw_payload(self):
        # A day out (rather than a fixed date) so this stays "open" under the
        # auto-close-on-expiry feature no matter when the suite actually runs.
        tomorrow = (datetime.now(ZoneInfo("Asia/Riyadh")) + timedelta(days=1)).strftime("%Y-%m-%d")
        return {
            "ptwNumber": "BAJV-829",
            "ptwType": "Hot work",
            "issuer": "Faisal",
            "receiver": "Sayed",
            "company": "BAJV",
            "location": "Basement",
            "workDescription": "Drilling, grinding & pipe installation",
            "startDate": tomorrow,
            "startTime": "08:00",
            "endDate": tomorrow,
            "endTime": "17:00",
        }

    def training_payload(self):
        return {
            "sessionType": "TBT",
            "topic": "Work at Height",
            "sessionDate": "2026-08-22",
            "trainer": "Faisal Raza",
            "location": "Zone 3",
            "duration": "One Hour",
            "attendeesCount": "12",
            "objective": "Preventing falls from height during scaffold and edge work",
            "summary": "A Toolbox Talk was conducted on Work at Height, covering anchor points and harness inspection.",
            "keyLessons": ["Always inspect harness before use.", "Use 100% tie-off at height."],
            "remarks": "Covered anchor points and harness inspection.",
        }

    def test_homepage_links_to_all_systems(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        for path in ("/inspection", "/near-miss", "/violation", "/ptw", "/training", "/admin"):
            self.assertIn(path.encode(), response.data)

    def test_homepage_stats_only_count_current_work_week(self):
        from datetime import date, timedelta

        from app import current_work_week_range

        week_start, week_end = current_work_week_range()
        week_end_display = (date.fromisoformat(week_end) - timedelta(days=1)).isoformat()
        payload = self.near_miss_payload()
        payload["incidentDate"] = week_start  # inside this Sat-Thu week
        self.client.post("/api/near-miss", json=payload)

        old_payload = self.near_miss_payload()
        old_payload["incidentDate"] = "2020-01-01"  # long before this week
        self.client.post("/api/near-miss", json=old_payload)

        html = self.client.get("/").get_data(as_text=True)
        self.assertIn(">1<", html)  # only the in-week record is counted
        self.assertIn(week_start, html)
        self.assertIn(week_end_display, html)

    def test_current_work_week_range_excludes_friday(self):
        from datetime import date
        from app import current_work_week_range

        with patch("app.datetime") as mock_datetime:
            mock_datetime.now.return_value.date.return_value = date(2026, 9, 11)  # a Friday
            week_start, week_end = current_work_week_range()
        # Week runs Sat 9/5 through Thu 9/10; Friday itself falls just outside it.
        self.assertEqual(week_start, "2026-09-05")
        self.assertEqual(week_end, "2026-09-11")

    def test_inspection_form_moved_from_root(self):
        response = self.client.get("/inspection")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"OHS Team Inspection Checklist", response.data)

    def test_inspection_form_includes_a_section_toggle_for_every_section(self):
        html = self.client.get("/inspection").get_data(as_text=True)
        self.assertEqual(html.count("data-section-toggle="), len(CHECKLIST))
        for section in CHECKLIST:
            self.assertIn(f'data-section-toggle="{section["id"]}"', html)

    def test_checklist_has_all_source_requirements(self):
        response = self.client.get("/api/checklist")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["total"], 102)
        self.assertEqual(len(response.json["sections"]), 14)

    def test_submit_review_and_export_record(self):
        response = self.client.post("/api/inspections", json=self.payload())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["score"], 100.0)
        self.assertEqual(response.json["reportNo"], "OHS-001")
        record_id = response.json["id"]

        self.assertEqual(self.login().status_code, 302)
        dashboard = self.client.get("/admin")
        self.assertIn(b"OHS-001", dashboard.data)
        detail = self.client.get(f"/admin/records/{record_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"GENERAL BEST PRACTICE", detail.data)

        summary = self.client.get("/admin/export?kind=summary")
        self.assertEqual(summary.status_code, 200)
        self.assertIn("attachment", summary.headers["Content-Disposition"])
        self.assertIn("OHS-001", summary.get_data(as_text=True))

        detailed = self.client.get("/admin/export?kind=detailed")
        rows = list(csv.reader(io.StringIO(detailed.get_data(as_text=True).lstrip("\ufeff"))))
        self.assertEqual(len(rows), 103)
        self.assertEqual(rows[-1][6], "General Best Practice")

    def test_non_compliance_requires_observation(self):
        payload = self.payload()
        first = CHECKLIST_ITEMS[0]
        payload["responses"][first["id"]] = "N"
        response = self.client.post("/api/inspections", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("observation", response.json["error"].lower())

    def test_admin_requires_correct_password(self):
        response = self.client.get("/admin/export")
        self.assertEqual(response.status_code, 302)
        response = self.client.post("/admin", data={"password": "wrong"})
        self.assertIn(b"Incorrect admin password", response.data)

    def test_trends_tab_renders_with_and_without_data(self):
        self.login()
        empty = self.client.get("/admin?view=trends")
        self.assertEqual(empty.status_code, 200)
        self.assertIn(b"<svg", empty.data)

        self.client.post("/api/inspections", json=self.payload())
        with_data = self.client.get("/admin?view=trends")
        self.assertEqual(with_data.status_code, 200)
        self.assertIn(b"<svg", with_data.data)

    def test_trends_legend_uses_css_classes_not_inline_style(self):
        # Inline style="" attributes are silently dropped by the strict
        # style-src 'self' CSP — the legend color swatches must use CSS
        # classes instead, or they render invisible with no error shown.
        self.login()
        response = self.client.get("/admin?view=trends")
        self.assertNotIn(b'<i style=', response.data)

    def test_near_miss_form_pages_load(self):
        self.assertEqual(self.client.get("/near-miss").status_code, 200)
        self.assertEqual(self.client.get("/violation").status_code, 200)
        self.assertEqual(self.client.get("/ptw").status_code, 200)
        self.assertEqual(self.client.get("/training").status_code, 200)

    def test_near_miss_requires_at_least_one_type(self):
        payload = self.near_miss_payload()
        payload["nearMissTypes"] = []
        response = self.client.post("/api/near-miss", json=payload)
        self.assertEqual(response.status_code, 400)

    def test_submit_review_and_export_near_miss(self):
        response = self.client.post("/api/near-miss", json=self.near_miss_payload())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["reportNo"], "NEAR-MISS-001")
        record_id = response.json["id"]

        self.login()
        dashboard = self.client.get("/admin?view=near-miss")
        self.assertIn(b"NEAR-MISS-001", dashboard.data)
        detail = self.client.get(f"/admin/near-miss/{record_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"NEAR MISS REPORTING FORM", detail.data)
        self.assertIn(b"Unsafe Condition", detail.data)

        export = self.client.get("/admin/export/near-miss")
        self.assertEqual(export.status_code, 200)
        self.assertIn("NEAR-MISS-001", export.get_data(as_text=True))

    def test_near_miss_pdf_download(self):
        response = self.client.post("/api/near-miss", json=self.near_miss_payload())
        record_id = response.json["id"]
        self.login()

        pdf_response = self.client.get(f"/admin/near-miss/{record_id}/report.pdf")
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response.mimetype, "application/pdf")
        self.assertIn("NEAR-MISS-001.pdf", pdf_response.headers["Content-Disposition"])
        self.assertTrue(pdf_response.data.startswith(b"%PDF"))

    def test_violation_requires_employee_name(self):
        payload = self.violation_payload()
        payload["employeeName"] = ""
        response = self.client.post("/api/violations", json=payload)
        self.assertEqual(response.status_code, 400)

    def test_submit_review_and_export_violation(self):
        response = self.client.post("/api/violations", json=self.violation_payload())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["violationNo"], "VIOLATION-001")
        record_id = response.json["id"]

        self.login()
        dashboard = self.client.get("/admin?view=violations")
        self.assertIn(b"VIOLATION-001", dashboard.data)
        detail = self.client.get(f"/admin/violations/{record_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"VIOLATION NOTICE", detail.data.upper())
        self.assertIn(b"First Warning", detail.data)

        export = self.client.get("/admin/export/violations")
        self.assertEqual(export.status_code, 200)
        self.assertIn("VIOLATION-001", export.get_data(as_text=True))

    def test_violation_submission_sends_notification_email_when_configured(self):
        fake_smtp = MagicMock()
        fake_smtp.__enter__.return_value = fake_smtp
        with patch.dict(os.environ, {
            "NOTIFY_EMAIL_TO": "safety@example.com", "NOTIFY_SMTP_USER": "alerts@example.com",
            "NOTIFY_SMTP_PASSWORD": "app-password",
        }), patch("app.smtplib.SMTP_SSL", return_value=fake_smtp) as mock_smtp_ssl:
            response = self.client.post("/api/violations", json=self.violation_payload())
        self.assertEqual(response.status_code, 201)
        mock_smtp_ssl.assert_called_once()
        fake_smtp.login.assert_called_once_with("alerts@example.com", "app-password")
        sent_message = fake_smtp.send_message.call_args[0][0]
        self.assertEqual(sent_message["To"], "safety@example.com")
        self.assertIn("VIOLATION-001", sent_message["Subject"])

    def test_notification_email_supports_third_party_smtp_provider(self):
        fake_smtp = MagicMock()
        fake_smtp.__enter__.return_value = fake_smtp
        with patch.dict(os.environ, {
            "NOTIFY_EMAIL_TO": "safety@example.com", "NOTIFY_SMTP_USER": "98a1b2c3d4e5f6@smtp-brevo.com",
            "NOTIFY_SMTP_PASSWORD": "brevo-smtp-key", "NOTIFY_EMAIL_FROM": "alerts@example.com",
            "NOTIFY_SMTP_HOST": "smtp-relay.brevo.com", "NOTIFY_SMTP_PORT": "587",
        }), patch("app.smtplib.SMTP_SSL", return_value=fake_smtp) as mock_smtp_ssl:
            response = self.client.post("/api/violations", json=self.violation_payload())
        self.assertEqual(response.status_code, 201)
        mock_smtp_ssl.assert_called_once_with("smtp-relay.brevo.com", 587, timeout=10)
        fake_smtp.login.assert_called_once_with("98a1b2c3d4e5f6@smtp-brevo.com", "brevo-smtp-key")
        sent_message = fake_smtp.send_message.call_args[0][0]
        self.assertEqual(sent_message["From"], "alerts@example.com")

    def test_near_miss_submission_sends_notification_email_when_configured(self):
        fake_smtp = MagicMock()
        fake_smtp.__enter__.return_value = fake_smtp
        with patch.dict(os.environ, {
            "NOTIFY_EMAIL_TO": "safety@example.com", "NOTIFY_SMTP_USER": "alerts@example.com",
            "NOTIFY_SMTP_PASSWORD": "app-password",
        }), patch("app.smtplib.SMTP_SSL", return_value=fake_smtp) as mock_smtp_ssl:
            response = self.client.post("/api/near-miss", json=self.near_miss_payload())
        self.assertEqual(response.status_code, 201)
        mock_smtp_ssl.assert_called_once()
        sent_message = fake_smtp.send_message.call_args[0][0]
        self.assertIn("NEAR-MISS-001", sent_message["Subject"])

    def test_submission_succeeds_even_if_notification_email_fails(self):
        with patch.dict(os.environ, {
            "NOTIFY_EMAIL_TO": "safety@example.com", "NOTIFY_SMTP_USER": "alerts@example.com",
            "NOTIFY_SMTP_PASSWORD": "app-password",
        }), patch("app.smtplib.SMTP_SSL", side_effect=OSError("connection refused")):
            response = self.client.post("/api/violations", json=self.violation_payload())
        self.assertEqual(response.status_code, 201)

    def test_notification_email_not_sent_when_unconfigured(self):
        with patch("app.smtplib.SMTP_SSL") as mock_smtp_ssl:
            response = self.client.post("/api/violations", json=self.violation_payload())
        self.assertEqual(response.status_code, 201)
        mock_smtp_ssl.assert_not_called()

    def test_violation_pdf_download(self):
        response = self.client.post("/api/violations", json=self.violation_payload())
        record_id = response.json["id"]
        self.login()

        pdf_response = self.client.get(f"/admin/violations/{record_id}/notice.pdf")
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response.mimetype, "application/pdf")
        self.assertIn("VIOLATION-001.pdf", pdf_response.headers["Content-Disposition"])
        self.assertTrue(pdf_response.data.startswith(b"%PDF"))

    def test_ptw_requires_valid_type(self):
        payload = self.ptw_payload()
        payload["ptwType"] = "Not a real type"
        response = self.client.post("/api/ptw", json=payload)
        self.assertEqual(response.status_code, 400)

    def test_ptw_requires_ptw_number(self):
        payload = self.ptw_payload()
        payload["ptwNumber"] = ""
        response = self.client.post("/api/ptw", json=payload)
        self.assertEqual(response.status_code, 400)

    def test_ptw_form_suggests_next_permit_number(self):
        blank = self.client.get("/ptw")
        self.assertIn(b'value="BAJV-1"', blank.data)

        payload = self.ptw_payload()
        payload["ptwNumber"] = "BAJV-840"
        self.client.post("/api/ptw", json=payload)

        after = self.client.get("/ptw")
        self.assertIn(b'value="BAJV-841"', after.data)

    def test_submit_review_and_export_ptw(self):
        response = self.client.post("/api/ptw", json=self.ptw_payload())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["ptwNumber"], "BAJV-829")
        record_id = response.json["id"]

        self.login()
        dashboard = self.client.get("/admin?view=ptw")
        self.assertIn(b"BAJV-829", dashboard.data)
        detail = self.client.get(f"/admin/ptw/{record_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"BAJV-829", detail.data)
        # New entries default to open until a coordinator closes them out.
        self.assertIn(b"open", detail.data.lower())

        export = self.client.get("/admin/export/ptw")
        self.assertEqual(export.status_code, 200)
        self.assertIn("BAJV-829", export.get_data(as_text=True))

    def test_ptw_xlsx_export_matches_original_columns_and_highlights_open(self):
        open_id = self.client.post("/api/ptw", json=self.ptw_payload()).json["id"]
        closed_payload = self.ptw_payload()
        closed_payload["ptwNumber"] = "BAJV-830"
        closed_id = self.client.post("/api/ptw", json=closed_payload).json["id"]

        self.login()
        self.client.post(f"/admin/ptw/{closed_id}", data={
            "ptwNumber": "BAJV-830", "issuer": "Faisal", "receiver": "Sayed", "ptwType": "Hot work",
            "workDescription": "Drilling", "areaHsePersonnel": "", "location": "Basement", "shift": "",
            "startDate": "2026-08-18", "startTime": "08:00", "endDate": "2026-08-18", "endTime": "17:00",
            "company": "BAJV", "status": "closed", "workersCount": "", "reviewedBy": "",
            "editedBy": "QA Inspector",
        })

        response = self.client.get("/admin/export/ptw.xlsx")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.content_type,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        workbook = openpyxl.load_workbook(io.BytesIO(response.data))
        sheet = workbook.active
        header = [cell.value for cell in sheet[1]]
        self.assertEqual(header, [
            "S.N", "PTW Number", "PTW Issuer", "PTW Receiver", "Type of PTW", "Work Description",
            "Area HSE Personnel", "Location", "Shift", "PTW Start Date & Time", "PTW End Date & Time",
            "Company Name", "Status", "No. of Workers", "Reviewed By",
        ])

        rows = {row[1].value: row for row in sheet.iter_rows(min_row=2)}
        self.assertIn("BAJV-829", rows)
        self.assertIn("BAJV-830", rows)
        # Open permit row is highlighted; the closed one is not.
        self.assertEqual(rows["BAJV-829"][1].fill.fgColor.rgb, "00FFFF00")
        self.assertNotEqual(rows["BAJV-830"][1].fill.fgColor.rgb, "00FFFF00")

    def test_ptw_auto_closes_once_end_time_has_passed(self):
        yesterday = (datetime.now(ZoneInfo("Asia/Riyadh")) - timedelta(days=1)).strftime("%Y-%m-%d")
        payload = self.ptw_payload()
        payload["ptwNumber"] = "BAJV-900"
        payload["startDate"] = yesterday
        payload["endDate"] = yesterday
        record_id = self.client.post("/api/ptw", json=payload).json["id"]

        with database() as connection:
            row = connection.execute("SELECT status FROM ptw_logs WHERE id = ?", [record_id]).fetchone()
            self.assertEqual(dict(row)["status"], "open")

        self.login()
        # Merely loading the list (or overview, export, etc.) is what triggers
        # the self-correction — there's no separate scheduled job to wait for.
        self.client.get("/admin?view=ptw")

        with database() as connection:
            row = connection.execute("SELECT status FROM ptw_logs WHERE id = ?", [record_id]).fetchone()
            self.assertEqual(dict(row)["status"], "closed")

    def test_ptw_list_orders_by_permit_number_not_submission_order(self):
        # Submitted out of numeric order (834 first, then 832, then 833) to
        # simulate someone backfilling or correcting an entry after the fact.
        for number in ("BAJV-834", "BAJV-832", "BAJV-833"):
            payload = self.ptw_payload()
            payload["ptwNumber"] = number
            response = self.client.post("/api/ptw", json=payload)
            self.assertEqual(response.status_code, 201)

        self.login()
        dashboard = self.client.get("/admin?view=ptw")
        body = dashboard.data.decode()
        # Highest number first, regardless of submission order.
        self.assertLess(body.index("BAJV-834"), body.index("BAJV-833"))
        self.assertLess(body.index("BAJV-833"), body.index("BAJV-832"))

    def test_ptw_overview_breaks_down_open_permits_by_area_and_type(self):
        # Two open in Basement (one Hot work, one Lifting), one open in Zone A
        # (Hot work), and one closed in Basement that should be excluded entirely.
        basement_hot = self.ptw_payload()
        self.client.post("/api/ptw", json=basement_hot)

        basement_lift = self.ptw_payload()
        basement_lift.update({"ptwNumber": "BAJV-830", "ptwType": "Lifting"})
        self.client.post("/api/ptw", json=basement_lift)

        zone_a_hot = self.ptw_payload()
        zone_a_hot.update({"ptwNumber": "BAJV-831", "location": "Zone A"})
        self.client.post("/api/ptw", json=zone_a_hot)

        closed_payload = self.ptw_payload()
        closed_payload["ptwNumber"] = "BAJV-832"
        closed_id = self.client.post("/api/ptw", json=closed_payload).json["id"]
        self.login()
        self.client.post(f"/admin/ptw/{closed_id}", data={
            "ptwNumber": "BAJV-832", "issuer": "Faisal", "receiver": "Sayed", "ptwType": "Hot work",
            "workDescription": "Drilling", "areaHsePersonnel": "", "location": "Basement", "shift": "",
            "startDate": "2026-08-18", "startTime": "08:00", "endDate": "2026-08-18", "endTime": "17:00",
            "company": "BAJV", "status": "closed", "workersCount": "", "reviewedBy": "",
            "editedBy": "QA Inspector",
        })

        overview = self.client.get("/admin?view=ptw")
        self.assertEqual(overview.status_code, 200)
        body = overview.data.decode()
        # 3 open total (the closed one excluded); Basement busiest with 2; Hot work count is 2.
        self.assertIn("Basement", body)
        self.assertIn("Hot work (1)", body)
        self.assertIn("Lifting (1)", body)

    def test_ptw_edit_updates_status_and_persists(self):
        record_id = self.client.post("/api/ptw", json=self.ptw_payload()).json["id"]
        self.login()
        payload = self.ptw_payload()
        payload["status"] = "closed"
        payload["reviewedBy"] = "Faisal"
        response = self.client.post(f"/admin/ptw/{record_id}", data={
            "ptwNumber": payload["ptwNumber"], "issuer": payload["issuer"], "receiver": payload["receiver"],
            "ptwType": payload["ptwType"], "workDescription": payload["workDescription"], "areaHsePersonnel": "",
            "location": payload["location"], "shift": "", "startDate": payload["startDate"],
            "startTime": payload["startTime"], "endDate": payload["endDate"], "endTime": payload["endTime"],
            "company": payload["company"], "status": "closed", "workersCount": "", "reviewedBy": "Faisal",
            "editedBy": "QA Inspector",
        })
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(f"/admin/ptw/{record_id}")
        self.assertIn(b'value="Faisal"', detail.data)
        export = self.client.get("/admin/export/ptw")
        self.assertIn("closed", export.get_data(as_text=True))

        with database() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT * FROM audit_log WHERE record_ref = ?", [payload["ptwNumber"]])
            row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["action"], "updated")
        self.assertEqual(row["actor_name"], "QA Inspector")

    def test_ptw_edit_rejects_invalid_update_and_keeps_entered_values(self):
        record_id = self.client.post("/api/ptw", json=self.ptw_payload()).json["id"]
        self.login()
        response = self.client.post(f"/admin/ptw/{record_id}", data={
            "ptwNumber": "", "issuer": "Faisal", "receiver": "Sayed", "ptwType": "Hot work",
            "workDescription": "Drilling", "areaHsePersonnel": "", "location": "Basement", "shift": "",
            "startDate": "2026-08-18", "startTime": "08:00", "endDate": "2026-08-18", "endTime": "17:00",
            "company": "BAJV", "status": "open", "workersCount": "", "reviewedBy": "",
            "editedBy": "QA Inspector",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"PTW number is required", response.data)

    def test_ptw_edit_requires_actor_name(self):
        record_id = self.client.post("/api/ptw", json=self.ptw_payload()).json["id"]
        self.login()
        payload = self.ptw_payload()
        response = self.client.post(f"/admin/ptw/{record_id}", data={
            "ptwNumber": payload["ptwNumber"], "issuer": payload["issuer"], "receiver": payload["receiver"],
            "ptwType": payload["ptwType"], "workDescription": payload["workDescription"], "areaHsePersonnel": "",
            "location": payload["location"], "shift": "", "startDate": payload["startDate"],
            "startTime": payload["startTime"], "endDate": payload["endDate"], "endTime": payload["endTime"],
            "company": payload["company"], "status": "open", "workersCount": "", "reviewedBy": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Your name is required", response.data)

    def test_ptw_number_must_be_unique_on_create(self):
        self.client.post("/api/ptw", json=self.ptw_payload())
        response = self.client.post("/api/ptw", json=self.ptw_payload())
        self.assertEqual(response.status_code, 400)
        self.assertIn("already in use", response.json["error"])

    def test_ptw_number_must_be_unique_on_edit(self):
        first_id = self.client.post("/api/ptw", json=self.ptw_payload()).json["id"]
        second_payload = self.ptw_payload()
        second_payload["ptwNumber"] = "BAJV-830"
        second_id = self.client.post("/api/ptw", json=second_payload).json["id"]

        self.login()
        payload = self.ptw_payload()
        response = self.client.post(f"/admin/ptw/{second_id}", data={
            "ptwNumber": payload["ptwNumber"], "issuer": payload["issuer"], "receiver": payload["receiver"],
            "ptwType": payload["ptwType"], "workDescription": payload["workDescription"], "areaHsePersonnel": "",
            "location": payload["location"], "shift": "", "startDate": payload["startDate"],
            "startTime": payload["startTime"], "endDate": payload["endDate"], "endTime": payload["endTime"],
            "company": payload["company"], "status": "open", "workersCount": "", "reviewedBy": "",
            "editedBy": "QA Inspector",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"already in use", response.data)
        # Editing a permit and keeping its own (unchanged) number must still work.
        unchanged = self.client.post(f"/admin/ptw/{second_id}", data={
            "ptwNumber": "BAJV-830", "issuer": payload["issuer"], "receiver": payload["receiver"],
            "ptwType": payload["ptwType"], "workDescription": payload["workDescription"], "areaHsePersonnel": "",
            "location": payload["location"], "shift": "", "startDate": payload["startDate"],
            "startTime": payload["startTime"], "endDate": payload["endDate"], "endTime": payload["endTime"],
            "company": payload["company"], "status": "open", "workersCount": "", "reviewedBy": "",
            "editedBy": "QA Inspector",
        })
        self.assertEqual(unchanged.status_code, 302)

    def test_delete_ptw(self):
        record_id = self.client.post("/api/ptw", json=self.ptw_payload()).json["id"]
        self.login()
        response = self.client.post(f"/admin/ptw/{record_id}/delete", data={"deletedBy": "QA Inspector"})
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(f"/admin/ptw/{record_id}")
        self.assertEqual(detail.status_code, 404)

    def test_delete_ptw_requires_actor_name(self):
        record_id = self.client.post("/api/ptw", json=self.ptw_payload()).json["id"]
        self.login()
        response = self.client.post(f"/admin/ptw/{record_id}/delete")
        self.assertEqual(response.status_code, 400)
        detail = self.client.get(f"/admin/ptw/{record_id}")
        self.assertEqual(detail.status_code, 200)  # not deleted

    def test_training_requires_valid_session_type(self):
        payload = self.training_payload()
        payload["sessionType"] = "Not a real type"
        response = self.client.post("/api/training", json=payload)
        self.assertEqual(response.status_code, 400)

    def test_submit_review_and_export_training(self):
        response = self.client.post("/api/training", json=self.training_payload())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["topic"], "Work at Height")
        record_id = response.json["id"]

        self.login()
        dashboard = self.client.get("/admin?view=training")
        self.assertIn(b"Work at Height", dashboard.data)
        detail = self.client.get(f"/admin/training/{record_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Work at Height", detail.data)
        self.assertIn(b"Faisal Raza", detail.data)
        self.assertIn(b"Preventing falls from height", detail.data)
        self.assertIn(b"A Toolbox Talk was conducted", detail.data)
        self.assertIn(b"Always inspect harness before use.", detail.data)
        self.assertIn(b"Use 100% tie-off at height.", detail.data)

        export = self.client.get("/admin/export/training")
        self.assertEqual(export.status_code, 200)
        export_text = export.get_data(as_text=True)
        self.assertIn("Work at Height", export_text)
        self.assertIn("Always inspect harness before use.", export_text)

    def test_delete_training(self):
        record_id = self.client.post("/api/training", json=self.training_payload()).json["id"]
        self.login()
        response = self.client.post(f"/admin/training/{record_id}/delete", data={"deletedBy": "QA Inspector"})
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(f"/admin/training/{record_id}")
        self.assertEqual(detail.status_code, 404)

    def test_photos_zip_requires_storage_configured(self):
        record_id = self.client.post("/api/training", json=self.training_payload()).json["id"]
        self.login()
        response = self.client.get(f"/admin/training/{record_id}/photos.zip")
        self.assertEqual(response.status_code, 503)

    def test_photos_zip_404s_for_missing_record(self):
        self.login()
        response = self.client.get("/admin/training/does-not-exist/photos.zip")
        self.assertEqual(response.status_code, 404)

    def test_photos_zip_requires_admin(self):
        record_id = self.client.post("/api/training", json=self.training_payload()).json["id"]
        response = self.client.get(f"/admin/training/{record_id}/photos.zip")
        self.assertEqual(response.status_code, 302)

    def test_training_photos_zip_bundles_actual_photo_bytes(self):
        payload = self.training_payload()
        payload["photoKeys"] = ["uploads/tok12345/aaaaaaaaaaaaaaaaaaaa.jpg", "uploads/tok12345/bbbbbbbbbbbbbbbbbbbb.png"]
        payload["attendancePhotoKeys"] = ["uploads/tok12345/cccccccccccccccccccc.jpg"]
        with patch("app.B2_BUCKET", "test-bucket"), \
             patch.dict(os.environ, {"B2_KEY_ID": "k", "B2_APPLICATION_KEY": "s", "B2_ENDPOINT": "s3.test.backblazeb2.com"}):
            record_id = self.client.post("/api/training", json=payload).json["id"]
            self.login()

            fake_client = MagicMock()
            fake_client.get_object.return_value = {"Body": io.BytesIO(b"fake-photo-bytes")}
            with patch("app.b2_client", return_value=fake_client):
                photos_response = self.client.get(f"/admin/training/{record_id}/photos.zip")
                attendance_response = self.client.get(f"/admin/training/{record_id}/attendance.zip")

        self.assertEqual(photos_response.status_code, 200)
        self.assertEqual(photos_response.mimetype, "application/zip")
        photos_zip = zipfile.ZipFile(io.BytesIO(photos_response.data))
        self.assertEqual(sorted(photos_zip.namelist()), ["photo-1.jpg", "photo-2.png"])
        self.assertEqual(photos_zip.read("photo-1.jpg"), b"fake-photo-bytes")

        self.assertEqual(attendance_response.status_code, 200)
        attendance_zip = zipfile.ZipFile(io.BytesIO(attendance_response.data))
        self.assertEqual(attendance_zip.namelist(), ["photo-1.jpg"])

    def test_training_pdf_download(self):
        payload = self.training_payload()
        response = self.client.post("/api/training", json=payload)
        record_id = response.json["id"]
        self.login()

        pdf_response = self.client.get(f"/admin/training/{record_id}/record.pdf")
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response.mimetype, "application/pdf")
        self.assertIn("training-1.pdf", pdf_response.headers["Content-Disposition"])
        self.assertTrue(pdf_response.data.startswith(b"%PDF"))

    def test_resized_photo_for_pdf_shrinks_large_photos(self):
        from PIL import Image
        from app import resized_photo_for_pdf

        buffer = io.BytesIO()
        Image.new("RGB", (4032, 3024), color=(120, 140, 160)).save(buffer, format="JPEG", quality=95)
        original = buffer.getvalue()

        resized, extension = resized_photo_for_pdf(original, "jpg")
        self.assertEqual(extension, "jpg")
        self.assertLess(len(resized), len(original))
        with Image.open(io.BytesIO(resized)) as image:
            self.assertLessEqual(max(image.size), 900)

    def test_resized_photo_for_pdf_applies_exif_rotation(self):
        from PIL import Image
        from app import resized_photo_for_pdf

        # A portrait phone photo is often stored with landscape sensor pixels plus an
        # EXIF tag telling viewers to rotate it 90deg for display.
        exif = Image.Exif()
        exif[274] = 6
        buffer = io.BytesIO()
        Image.new("RGB", (400, 300), color=(120, 140, 160)).save(buffer, format="JPEG", exif=exif)
        original = buffer.getvalue()

        resized, _ = resized_photo_for_pdf(original, "jpg")
        with Image.open(io.BytesIO(resized)) as image:
            self.assertEqual(image.size, (300, 400))

    def test_resized_photo_for_pdf_falls_back_on_invalid_image(self):
        from app import resized_photo_for_pdf

        resized, extension = resized_photo_for_pdf(b"not-an-image", "png")
        self.assertEqual(resized, b"not-an-image")
        self.assertEqual(extension, "png")

    def test_export_training_bundle_includes_csv_pdfs_and_photos(self):
        first = self.training_payload()
        first["photoKeys"] = ["uploads/tok11111/aaaaaaaaaaaaaaaaaaaa.jpg"]
        second = self.training_payload()
        second["topic"] = "PPE Refresher"
        second["attendancePhotoKeys"] = ["uploads/tok22222/bbbbbbbbbbbbbbbbbbbb.jpg"]
        with patch("app.B2_BUCKET", "test-bucket"), \
             patch.dict(os.environ, {"B2_KEY_ID": "k", "B2_APPLICATION_KEY": "s", "B2_ENDPOINT": "s3.test.backblazeb2.com"}):
            self.client.post("/api/training", json=first)
            self.client.post("/api/training", json=second)
            self.login()

            fake_client = MagicMock()
            fake_client.get_object.return_value = {"Body": io.BytesIO(b"fake-photo-bytes")}
            with patch("app.b2_client", return_value=fake_client):
                response = self.client.get("/admin/export/training/bundle.zip")

        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.data))
        names = archive.namelist()
        self.assertIn("training-log.csv", names)
        self.assertTrue(any(name.endswith(".pdf") for name in names))
        self.assertEqual(sum(name.endswith(".pdf") for name in names), 2)
        self.assertTrue(any("photos/photo-1.jpg" in name for name in names))
        self.assertTrue(any("attendance/photo-1.jpg" in name for name in names))
        self.assertTrue(any("PPE Refresher" in name for name in names))

    def test_export_training_bundle_works_without_photo_storage(self):
        self.client.post("/api/training", json=self.training_payload())
        self.login()
        response = self.client.get("/admin/export/training/bundle.zip")
        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.data))
        names = archive.namelist()
        self.assertIn("training-log.csv", names)
        self.assertEqual(sum(name.endswith(".pdf") for name in names), 1)
        self.assertFalse(any(name.endswith((".jpg", ".png")) for name in names))

    def test_training_weekly_import_parses_induction_and_sessions(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet["A2"] = "Week No.# Date: From 22-08-2026 To 27-08-2026"
        sheet["A3"] = "SAFETY INDUCTION"
        sheet["A4"] = "Sl. No."
        sheet["B4"] = "Employee's Inducted"
        sheet["E4"] = "Numbers"
        sheet["A5"] = 1
        sheet["B5"] = "Total no . of Employees Inducted This Week"
        sheet["E5"] = "70  ( 13 Session ) "
        sheet["A6"] = "In-house Trainings"
        sheet["A7"] = "Sl. No."
        sheet["B7"] = "In-house Training"
        sheet["D7"] = "Date"
        sheet["E7"] = "Number of Attendees"
        sheet["F7"] = "Time Duration"
        sheet["A8"] = 1
        sheet["B8"] = "Work Permit System"
        sheet["D8"] = datetime(2026, 8, 22)
        sheet["E8"] = 12
        sheet["F8"] = "One Hour"
        sheet["A9"] = 2
        sheet["B9"] = "Emergency Response"
        sheet["D9"] = datetime(2026, 8, 23)
        sheet["E9"] = 14
        sheet["F9"] = "One Hour"
        buffer = io.BytesIO()
        workbook.save(buffer)
        buffer.seek(0)

        self.login()
        response = self.client.post(
            "/admin/training/import",
            data={"file": (buffer, "weekly.xlsx")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["imported"], 3)

        dashboard = self.client.get("/admin?view=training")
        self.assertIn(b"Weekly Safety Induction", dashboard.data)
        self.assertIn(b"Work Permit System", dashboard.data)
        self.assertIn(b"Emergency Response", dashboard.data)

    def test_sequential_report_numbers(self):
        first = self.client.post("/api/inspections", json=self.payload())
        ignored_payload = self.payload()
        ignored_payload["reportNo"] = "CUSTOM-IGNORED"
        second = self.client.post("/api/inspections", json=ignored_payload)
        self.assertEqual(first.json["reportNo"], "OHS-001")
        self.assertEqual(second.json["reportNo"], "OHS-002")

    def test_photo_upload_rejects_bad_token(self):
        response = self.client.post("/api/uploads/bad token!", data={}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)

    def test_photo_upload_without_storage_configured(self):
        response = self.client.post(
            "/api/uploads/abcdef1234567890",
            data={"photo": (io.BytesIO(b"not-a-real-image"), "photo.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 503)

    def test_b2_endpoint_host_matches_the_exact_presigned_url_host(self):
        # A CSP host wildcard like "*.backblazeb2.com" only ever matches one DNS
        # label, but B2's real endpoint is two labels deep (e.g. "s3.<region>.
        # backblazeb2.com") — so the CSP has to name the exact host instead of
        # guessing with a wildcard, or the browser silently blocks the photo <img>.
        from app import _b2_endpoint_host
        with patch.dict(os.environ, {"B2_ENDPOINT": "s3.us-west-004.backblazeb2.com"}):
            self.assertEqual(_b2_endpoint_host(), "s3.us-west-004.backblazeb2.com")
        with patch.dict(os.environ, {"B2_ENDPOINT": "https://s3.eu-central-003.backblazeb2.com"}):
            self.assertEqual(_b2_endpoint_host(), "s3.eu-central-003.backblazeb2.com")

    def test_b2_client_uses_path_style_and_disables_chunked_checksums(self):
        with patch.dict(os.environ, {
            "B2_ENDPOINT": "s3.us-west-004.backblazeb2.com",
            "B2_KEY_ID": "test-key-id",
            "B2_APPLICATION_KEY": "test-app-key",
        }):
            from app import b2_client
            client = b2_client()
        config = client.meta.config
        self.assertEqual(config.s3["addressing_style"], "path")
        self.assertEqual(config.request_checksum_calculation, "when_required")
        self.assertEqual(config.response_checksum_validation, "when_required")
        self.assertEqual(client.meta.region_name, "us-west-004")

    def test_over_max_content_length_returns_json_not_html_on_api_routes(self):
        payload = self.near_miss_payload()
        payload["whatHappened"] = "x" * (13 * 1024 * 1024)
        response = self.client.post("/api/near-miss", json=payload)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.content_type, "application/json")
        self.assertIn("error", response.json)

    def test_delete_requires_admin(self):
        record_id = self.client.post("/api/inspections", json=self.payload()).json["id"]
        response = self.client.post(f"/admin/records/{record_id}/delete")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin", response.headers["Location"])
        self.login()
        detail = self.client.get(f"/admin/records/{record_id}")
        self.assertEqual(detail.status_code, 200)

    def test_delete_record(self):
        record_id = self.client.post("/api/inspections", json=self.payload()).json["id"]
        self.login()
        response = self.client.post(f"/admin/records/{record_id}/delete", data={"deletedBy": "QA Inspector"})
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(f"/admin/records/{record_id}")
        self.assertEqual(detail.status_code, 404)

    def test_delete_near_miss(self):
        record_id = self.client.post("/api/near-miss", json=self.near_miss_payload()).json["id"]
        self.login()
        response = self.client.post(f"/admin/near-miss/{record_id}/delete", data={"deletedBy": "QA Inspector"})
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(f"/admin/near-miss/{record_id}")
        self.assertEqual(detail.status_code, 404)

    def test_delete_violation(self):
        record_id = self.client.post("/api/violations", json=self.violation_payload()).json["id"]
        self.login()
        response = self.client.post(f"/admin/violations/{record_id}/delete", data={"deletedBy": "QA Inspector"})
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(f"/admin/violations/{record_id}")
        self.assertEqual(detail.status_code, 404)

    def test_delete_requires_actor_name(self):
        record_id = self.client.post("/api/near-miss", json=self.near_miss_payload()).json["id"]
        self.login()
        response = self.client.post(f"/admin/near-miss/{record_id}/delete")
        self.assertEqual(response.status_code, 400)
        detail = self.client.get(f"/admin/near-miss/{record_id}")
        self.assertEqual(detail.status_code, 200)  # not deleted

    def test_delete_logs_audit_entry(self):
        from app import database

        response = self.client.post("/api/near-miss", json=self.near_miss_payload())
        record_id = response.json["id"]
        report_no = response.json["reportNo"]
        self.login()
        self.client.post(f"/admin/near-miss/{record_id}/delete", data={"deletedBy": "QA Inspector"})

        with database() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT * FROM audit_log WHERE record_ref = ?", [report_no])
            row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["action"], "deleted")
        self.assertEqual(row["record_type"], "near_miss")
        self.assertEqual(row["actor_name"], "QA Inspector")

    def test_near_miss_and_violation_detail_pages_render(self):
        near_miss_id = self.client.post("/api/near-miss", json=self.near_miss_payload()).json["id"]
        violation_id = self.client.post("/api/violations", json=self.violation_payload()).json["id"]
        self.login()
        near_miss_detail = self.client.get(f"/admin/near-miss/{near_miss_id}")
        self.assertEqual(near_miss_detail.status_code, 200)
        violation_detail = self.client.get(f"/admin/violations/{violation_id}")
        self.assertEqual(violation_detail.status_code, 200)

    def test_near_miss_edit_page_prefills_existing_values(self):
        record_id = self.client.post("/api/near-miss", json=self.near_miss_payload()).json["id"]
        self.login()
        response = self.client.get(f"/admin/near-miss/{record_id}/edit")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode()
        self.assertIn('value="Zone 3"', body)
        self.assertIn('value="Podium Level 2"', body)
        self.assertIn('name="nearMissTypes" value="Unsafe Condition" checked', body)

    def test_near_miss_edit_saves_changes_and_logs_audit(self):
        response = self.client.post("/api/near-miss", json=self.near_miss_payload())
        record_id = response.json["id"]
        report_no = response.json["reportNo"]
        self.login()
        edit_response = self.client.post(f"/admin/near-miss/{record_id}/edit", data={
            "departmentProject": "Zone 3B", "location": "Podium Level 3", "incidentDate": "2026-08-16",
            "incidentTime": "10:00", "reportedBy": "Foreman A", "whatHappened": "Ladder slipped on wet floor.",
            "nearMissTypes": "Unsafe Condition", "reportedBySignoff": "Foreman A", "editedBy": "QA Tester",
        })
        self.assertEqual(edit_response.status_code, 302)
        detail = self.client.get(f"/admin/near-miss/{record_id}")
        self.assertIn(b"Zone 3B", detail.data)
        self.assertIn(b"Podium Level 3", detail.data)
        self.assertIn(b"wet floor", detail.data)

        with database() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT * FROM audit_log WHERE record_ref = ?", [report_no])
            row = cursor.fetchone()
        self.assertEqual(row["action"], "updated")
        self.assertEqual(row["actor_name"], "QA Tester")

    def test_near_miss_edit_requires_actor_name(self):
        record_id = self.client.post("/api/near-miss", json=self.near_miss_payload()).json["id"]
        self.login()
        response = self.client.post(f"/admin/near-miss/{record_id}/edit", data={
            "departmentProject": "Zone 3", "location": "Podium Level 2", "incidentDate": "2026-08-16",
            "incidentTime": "10:00", "reportedBy": "Foreman A", "whatHappened": "Ladder slipped.",
            "nearMissTypes": "Unsafe Condition", "reportedBySignoff": "Foreman A",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Your name is required", response.data)

    def test_near_miss_edit_does_not_touch_photos(self):
        payload = self.near_miss_payload()
        payload["photoKeys"] = ["uploads/tok11111/aaaaaaaaaaaaaaaaaaaa.jpg"]
        record_id = self.client.post("/api/near-miss", json=payload).json["id"]
        self.login()
        self.client.post(f"/admin/near-miss/{record_id}/edit", data={
            "departmentProject": "Zone 3", "location": "Podium Level 2", "incidentDate": "2026-08-16",
            "incidentTime": "10:00", "reportedBy": "Foreman A", "whatHappened": "Ladder slipped.",
            "nearMissTypes": "Unsafe Condition", "reportedBySignoff": "Foreman A", "editedBy": "QA Tester",
        })
        with database() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT photos FROM near_miss_reports WHERE id = ?", [record_id])
            row = cursor.fetchone()
        self.assertIn("aaaaaaaaaaaaaaaaaaaa.jpg", row["photos"])

    def test_violation_edit_page_prefills_existing_values(self):
        record_id = self.client.post("/api/violations", json=self.violation_payload()).json["id"]
        self.login()
        response = self.client.get(f"/admin/violations/{record_id}/edit")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode()
        self.assertIn('value="John Doe"', body)
        self.assertIn('name="actions" value="First Warning" checked', body)

    def test_violation_edit_saves_changes_and_logs_audit(self):
        response = self.client.post("/api/violations", json=self.violation_payload())
        record_id = response.json["id"]
        violation_no = response.json["violationNo"]
        self.login()
        payload = self.violation_payload()
        edit_response = self.client.post(f"/admin/violations/{record_id}/edit", data={
            "projectName": payload["projectName"], "violationDate": payload["violationDate"],
            "employeeName": "Jane Doe", "companyContractor": payload["companyContractor"],
            "violationLocation": payload["violationLocation"], "violationType": payload["violationType"],
            "violationDescription": "Worker observed without safety glasses.",
            "actions": "Final Warning", "issuedByName": payload["issuedByName"], "editedBy": "QA Tester",
        })
        self.assertEqual(edit_response.status_code, 302)
        detail = self.client.get(f"/admin/violations/{record_id}")
        self.assertIn(b"Jane Doe", detail.data)
        self.assertIn(b"safety glasses", detail.data)

        with database() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT * FROM audit_log WHERE record_ref = ?", [violation_no])
            row = cursor.fetchone()
        self.assertEqual(row["action"], "updated")
        self.assertEqual(row["actor_name"], "QA Tester")

    def test_violation_edit_requires_actor_name(self):
        record_id = self.client.post("/api/violations", json=self.violation_payload()).json["id"]
        self.login()
        payload = self.violation_payload()
        response = self.client.post(f"/admin/violations/{record_id}/edit", data={
            "projectName": payload["projectName"], "violationDate": payload["violationDate"],
            "employeeName": payload["employeeName"], "companyContractor": payload["companyContractor"],
            "violationLocation": payload["violationLocation"], "violationType": payload["violationType"],
            "violationDescription": payload["violationDescription"],
            "issuedByName": payload["issuedByName"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Your name is required", response.data)

    def test_violation_edit_does_not_touch_photos(self):
        payload = self.violation_payload()
        payload["photoKeys"] = ["uploads/tok11111/aaaaaaaaaaaaaaaaaaaa.jpg"]
        record_id = self.client.post("/api/violations", json=payload).json["id"]
        self.login()
        edit_payload = self.violation_payload()
        self.client.post(f"/admin/violations/{record_id}/edit", data={
            "projectName": edit_payload["projectName"], "violationDate": edit_payload["violationDate"],
            "employeeName": edit_payload["employeeName"], "companyContractor": edit_payload["companyContractor"],
            "violationLocation": edit_payload["violationLocation"], "violationType": edit_payload["violationType"],
            "violationDescription": edit_payload["violationDescription"],
            "issuedByName": edit_payload["issuedByName"], "editedBy": "QA Tester",
        })
        with database() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT photos FROM violation_notices WHERE id = ?", [record_id])
            row = cursor.fetchone()
        self.assertIn("aaaaaaaaaaaaaaaaaaaa.jpg", row["photos"])

    def test_training_edit_page_prefills_existing_values(self):
        record_id = self.client.post("/api/training", json=self.training_payload()).json["id"]
        self.login()
        response = self.client.get(f"/admin/training/{record_id}/edit")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode()
        self.assertIn('value="Work at Height"', body)
        self.assertIn('value="Faisal Raza"', body)
        self.assertIn("Always inspect harness before use.", body)

    def test_training_edit_saves_changes_and_logs_audit(self):
        response = self.client.post("/api/training", json=self.training_payload())
        record_id = response.json["id"]
        self.login()
        payload = self.training_payload()
        edit_response = self.client.post(f"/admin/training/{record_id}/edit", data={
            "sessionType": payload["sessionType"], "topic": "Work at Height (Revised)",
            "sessionDate": payload["sessionDate"], "trainer": payload["trainer"], "location": payload["location"],
            "duration": payload["duration"], "attendeesCount": payload["attendeesCount"],
            "objective": payload["objective"], "summary": "Updated summary text.",
            "keyLessons": "Inspect harness daily.\nUse tie-off above shoulder height.",
            "remarks": payload["remarks"], "editedBy": "QA Tester",
        })
        self.assertEqual(edit_response.status_code, 302)
        detail = self.client.get(f"/admin/training/{record_id}")
        self.assertIn(b"Work at Height (Revised)", detail.data)
        self.assertIn(b"Updated summary text.", detail.data)
        self.assertIn(b"Inspect harness daily.", detail.data)

        with database() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT * FROM audit_log WHERE record_ref LIKE '%Work at Height%'")
            row = cursor.fetchone()
        self.assertEqual(row["action"], "updated")
        self.assertEqual(row["actor_name"], "QA Tester")

    def test_training_edit_requires_actor_name(self):
        record_id = self.client.post("/api/training", json=self.training_payload()).json["id"]
        self.login()
        payload = self.training_payload()
        response = self.client.post(f"/admin/training/{record_id}/edit", data={
            "sessionType": payload["sessionType"], "topic": payload["topic"], "sessionDate": payload["sessionDate"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Your name is required", response.data)

    def test_training_edit_does_not_touch_photos(self):
        payload = self.training_payload()
        payload["photoKeys"] = ["uploads/tok11111/aaaaaaaaaaaaaaaaaaaa.jpg"]
        payload["attendancePhotoKeys"] = ["uploads/tok22222/bbbbbbbbbbbbbbbbbbbb.jpg"]
        record_id = self.client.post("/api/training", json=payload).json["id"]
        self.login()
        edit_payload = self.training_payload()
        self.client.post(f"/admin/training/{record_id}/edit", data={
            "sessionType": edit_payload["sessionType"], "topic": edit_payload["topic"],
            "sessionDate": edit_payload["sessionDate"], "editedBy": "QA Tester",
        })
        with database() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT photos, attendance_photos FROM training_logs WHERE id = ?", [record_id])
            row = cursor.fetchone()
        self.assertIn("aaaaaaaaaaaaaaaaaaaa.jpg", row["photos"])
        self.assertIn("bbbbbbbbbbbbbbbbbbbb.jpg", row["attendance_photos"])

    def test_inspection_edit_page_prefills_existing_values(self):
        record_id = self.client.post("/api/inspections", json=self.payload()).json["id"]
        self.login()
        response = self.client.get(f"/admin/records/{record_id}/edit")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode()
        self.assertIn('value="Zone 3"', body)
        self.assertIn('value="Test Contractor"', body)

    def test_inspection_edit_saves_header_changes_and_logs_audit(self):
        response = self.client.post("/api/inspections", json=self.payload())
        record_id = response.json["id"]
        self.login()
        payload = self.payload()
        edit_response = self.client.post(f"/admin/records/{record_id}/edit", data={
            "projectName": payload["projectName"], "workLocation": "Zone 5",
            "contractor": payload["contractor"], "inspectedBy": payload["inspectedBy"],
            "inspectionDate": payload["inspectionDate"], "inspectionTime": payload["inspectionTime"],
            "shift": payload["shift"], "remarks": "Corrected zone after review.",
            "signoffName": payload["signoffName"], "editedBy": "QA Tester",
        })
        self.assertEqual(edit_response.status_code, 302)
        detail = self.client.get(f"/admin/records/{record_id}")
        self.assertIn(b"Zone 5", detail.data)
        self.assertIn(b"Corrected zone after review.", detail.data)

        with database() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT * FROM audit_log WHERE record_type = 'inspection'")
            row = cursor.fetchone()
        self.assertEqual(row["action"], "updated")
        self.assertEqual(row["actor_name"], "QA Tester")

    def test_inspection_edit_requires_actor_name(self):
        record_id = self.client.post("/api/inspections", json=self.payload()).json["id"]
        self.login()
        payload = self.payload()
        response = self.client.post(f"/admin/records/{record_id}/edit", data={
            "projectName": payload["projectName"], "workLocation": payload["workLocation"],
            "contractor": payload["contractor"], "inspectedBy": payload["inspectedBy"],
            "inspectionDate": payload["inspectionDate"], "inspectionTime": payload["inspectionTime"],
            "shift": payload["shift"], "signoffName": payload["signoffName"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Your name is required", response.data)

    def test_inspection_edit_does_not_touch_checklist_responses(self):
        record_id = self.client.post("/api/inspections", json=self.payload()).json["id"]
        self.login()
        payload = self.payload()
        self.client.post(f"/admin/records/{record_id}/edit", data={
            "projectName": payload["projectName"], "workLocation": payload["workLocation"],
            "contractor": payload["contractor"], "inspectedBy": payload["inspectedBy"],
            "inspectionDate": payload["inspectionDate"], "inspectionTime": payload["inspectionTime"],
            "shift": payload["shift"], "signoffName": payload["signoffName"], "editedBy": "QA Tester",
        })
        with database() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT responses, compliant, score FROM inspections WHERE id = ?", [record_id])
            row = cursor.fetchone()
        responses = json.loads(row["responses"])
        self.assertTrue(all(value == "Y" for value in responses.values()))
        self.assertEqual(row["score"], 100.0)

    def test_detail_pages_survive_malformed_legacy_json(self):
        near_miss_id = self.client.post("/api/near-miss", json=self.near_miss_payload()).json["id"]
        violation_id = self.client.post("/api/violations", json=self.violation_payload()).json["id"]
        with database() as connection:
            connection.execute(
                "UPDATE near_miss_reports SET near_miss_types = ?, photos = ? WHERE id = ?",
                ["not-json", "not-json", near_miss_id],
            )
            connection.execute(
                "UPDATE violation_notices SET actions = ?, photos = ? WHERE id = ?",
                ["not-json", "not-json", violation_id],
            )
        self.login()
        near_miss_detail = self.client.get(f"/admin/near-miss/{near_miss_id}")
        self.assertEqual(near_miss_detail.status_code, 200)
        violation_detail = self.client.get(f"/admin/violations/{violation_id}")
        self.assertEqual(violation_detail.status_code, 200)

    def test_backup_rejects_missing_or_wrong_token(self):
        self.assertEqual(self.client.get("/admin/backup").status_code, 401)
        self.assertEqual(self.client.get("/admin/backup?token=wrong").status_code, 401)

    def test_backup_returns_zip_with_all_record_types(self):
        self.client.post("/api/inspections", json=self.payload())
        self.client.post("/api/near-miss", json=self.near_miss_payload())
        self.client.post("/api/violations", json=self.violation_payload())
        self.client.post("/api/ptw", json=self.ptw_payload())
        self.client.post("/api/training", json=self.training_payload())

        response = self.client.get("/admin/backup?token=test-export-token")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")

        # Kept CSV-only on purpose (fast/lightweight) — full PDFs+photos live in the
        # separate /admin/backup/<type>.zip endpoints, so one heavy combined request
        # can't time out or crash the whole backup.
        archive = zipfile.ZipFile(io.BytesIO(response.data))
        names = archive.namelist()
        self.assertEqual(len(names), 6)
        self.assertTrue(any(name.startswith("inspections-summary-") for name in names))
        self.assertTrue(any(name.startswith("inspections-detailed-") for name in names))
        self.assertTrue(any(name.startswith("near-miss-") for name in names))
        self.assertTrue(any(name.startswith("violations-") for name in names))
        self.assertTrue(any(name.startswith("ptw-log-") for name in names))
        self.assertTrue(any(name.startswith("training-log-") for name in names))

    def test_per_type_backup_endpoints_reject_missing_or_wrong_token(self):
        for path in ("/admin/backup/near-miss.zip", "/admin/backup/violations.zip", "/admin/backup/training.zip"):
            self.assertEqual(self.client.get(path).status_code, 401)
            self.assertEqual(self.client.get(f"{path}?token=wrong").status_code, 401)

    def test_per_type_backup_endpoints_include_pdfs_and_photos(self):
        near_miss_payload = self.near_miss_payload()
        near_miss_payload["photoKeys"] = ["uploads/tok11111/aaaaaaaaaaaaaaaaaaaa.jpg"]
        violation_payload = self.violation_payload()
        violation_payload["photoKeys"] = ["uploads/tok22222/bbbbbbbbbbbbbbbbbbbb.jpg"]
        training_payload = self.training_payload()
        training_payload["photoKeys"] = ["uploads/tok33333/cccccccccccccccccccc.jpg"]
        training_payload["attendancePhotoKeys"] = ["uploads/tok44444/dddddddddddddddddddd.jpg"]

        with patch("app.B2_BUCKET", "test-bucket"), \
             patch.dict(os.environ, {"B2_KEY_ID": "k", "B2_APPLICATION_KEY": "s", "B2_ENDPOINT": "s3.test.backblazeb2.com"}):
            self.client.post("/api/near-miss", json=near_miss_payload)
            self.client.post("/api/violations", json=violation_payload)
            self.client.post("/api/training", json=training_payload)

            fake_client = MagicMock()
            fake_client.get_object.side_effect = lambda **kwargs: {"Body": io.BytesIO(b"fake-photo-bytes")}
            with patch("app.b2_client", return_value=fake_client):
                near_miss_response = self.client.get("/admin/backup/near-miss.zip?token=test-export-token")
                violation_response = self.client.get("/admin/backup/violations.zip?token=test-export-token")
                training_response = self.client.get("/admin/backup/training.zip?token=test-export-token")

        for response in (near_miss_response, violation_response, training_response):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "application/zip")

        near_miss_names = zipfile.ZipFile(io.BytesIO(near_miss_response.data)).namelist()
        self.assertTrue(any(name.endswith(".pdf") for name in near_miss_names))
        self.assertTrue(any(name.endswith(".jpg") for name in near_miss_names))

        violation_names = zipfile.ZipFile(io.BytesIO(violation_response.data)).namelist()
        self.assertTrue(any(name.endswith(".pdf") for name in violation_names))
        self.assertTrue(any(name.endswith(".jpg") for name in violation_names))

        training_names = zipfile.ZipFile(io.BytesIO(training_response.data)).namelist()
        self.assertTrue(any(name.endswith(".pdf") for name in training_names))
        self.assertTrue(any("photos/" in name for name in training_names))
        self.assertTrue(any("attendance/" in name for name in training_names))

    def test_per_type_backup_fetches_each_photo_from_storage_only_once(self):
        # Every photo appears twice in the bundle (embedded in its record's PDF, and as
        # a raw file) — without a per-request cache this fetches the same key from B2
        # twice, which was slow enough to blow a platform request timeout on Render.
        payload = self.near_miss_payload()
        payload["photoKeys"] = ["uploads/tok11111/aaaaaaaaaaaaaaaaaaaa.jpg"]
        with patch("app.B2_BUCKET", "test-bucket"), \
             patch.dict(os.environ, {"B2_KEY_ID": "k", "B2_APPLICATION_KEY": "s", "B2_ENDPOINT": "s3.test.backblazeb2.com"}):
            self.client.post("/api/near-miss", json=payload)

            fake_client = MagicMock()
            fake_client.get_object.side_effect = lambda **kwargs: {"Body": io.BytesIO(b"fake-photo-bytes")}
            with patch("app.b2_client", return_value=fake_client):
                response = self.client.get("/admin/backup/near-miss.zip?token=test-export-token")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake_client.get_object.call_count, 1)

    def test_per_type_backup_endpoint_works_without_photo_storage_configured(self):
        payload = self.near_miss_payload()
        payload["photoKeys"] = ["uploads/tok11111/aaaaaaaaaaaaaaaaaaaa.jpg"]
        self.client.post("/api/near-miss", json=payload)

        response = self.client.get("/admin/backup/near-miss.zip?token=test-export-token")
        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.data))
        names = archive.namelist()
        self.assertFalse(any(name.endswith(".jpg") for name in names))
        self.assertTrue(any(name.endswith(".pdf") for name in names))


if __name__ == "__main__":
    unittest.main()
