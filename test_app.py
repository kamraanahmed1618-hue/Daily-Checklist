import csv
import io
import os
import tempfile
import unittest
import zipfile
from unittest.mock import patch


TEST_FILES = tempfile.TemporaryDirectory()
os.environ["OHS_DB_PATH"] = os.path.join(TEST_FILES.name, "test.db")
os.environ["ADMIN_PASSWORD"] = "test-admin-password"
os.environ["SECRET_KEY"] = "test-secret-key-that-is-only-used-by-the-automated-suite"
os.environ["EXPORT_TOKEN"] = "test-export-token"

from app import CHECKLIST_ITEMS, app, database  # noqa: E402


class ChecklistApplicationTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        self.client = app.test_client()
        with database() as connection:
            connection.execute("DELETE FROM inspections")
            connection.execute("DELETE FROM near_miss_reports")
            connection.execute("DELETE FROM violation_notices")

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
        return {
            "ptwNumber": "BAJV-829",
            "ptwType": "Hot work",
            "issuer": "Faisal",
            "receiver": "Sayed",
            "company": "BAJV",
            "location": "Basement",
            "workDescription": "Drilling, grinding & pipe installation",
            "startDate": "2026-08-18",
            "startTime": "08:00",
            "endDate": "2026-08-18",
            "endTime": "17:00",
        }

    def test_homepage_links_to_all_systems(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        for path in ("/inspection", "/near-miss", "/violation", "/ptw", "/admin"):
            self.assertIn(path.encode(), response.data)

    def test_inspection_form_moved_from_root(self):
        response = self.client.get("/inspection")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"OHS Team Inspection Checklist", response.data)

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
        })
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(f"/admin/ptw/{record_id}")
        self.assertIn(b'value="Faisal"', detail.data)
        export = self.client.get("/admin/export/ptw")
        self.assertIn("closed", export.get_data(as_text=True))

    def test_ptw_edit_rejects_invalid_update_and_keeps_entered_values(self):
        record_id = self.client.post("/api/ptw", json=self.ptw_payload()).json["id"]
        self.login()
        response = self.client.post(f"/admin/ptw/{record_id}", data={
            "ptwNumber": "", "issuer": "Faisal", "receiver": "Sayed", "ptwType": "Hot work",
            "workDescription": "Drilling", "areaHsePersonnel": "", "location": "Basement", "shift": "",
            "startDate": "2026-08-18", "startTime": "08:00", "endDate": "2026-08-18", "endTime": "17:00",
            "company": "BAJV", "status": "open", "workersCount": "", "reviewedBy": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"PTW number is required", response.data)

    def test_delete_ptw(self):
        record_id = self.client.post("/api/ptw", json=self.ptw_payload()).json["id"]
        self.login()
        response = self.client.post(f"/admin/ptw/{record_id}/delete")
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(f"/admin/ptw/{record_id}")
        self.assertEqual(detail.status_code, 404)

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
        response = self.client.post(f"/admin/records/{record_id}/delete")
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(f"/admin/records/{record_id}")
        self.assertEqual(detail.status_code, 404)

    def test_delete_near_miss(self):
        record_id = self.client.post("/api/near-miss", json=self.near_miss_payload()).json["id"]
        self.login()
        response = self.client.post(f"/admin/near-miss/{record_id}/delete")
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(f"/admin/near-miss/{record_id}")
        self.assertEqual(detail.status_code, 404)

    def test_delete_violation(self):
        record_id = self.client.post("/api/violations", json=self.violation_payload()).json["id"]
        self.login()
        response = self.client.post(f"/admin/violations/{record_id}/delete")
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(f"/admin/violations/{record_id}")
        self.assertEqual(detail.status_code, 404)

    def test_near_miss_and_violation_detail_pages_render(self):
        near_miss_id = self.client.post("/api/near-miss", json=self.near_miss_payload()).json["id"]
        violation_id = self.client.post("/api/violations", json=self.violation_payload()).json["id"]
        self.login()
        near_miss_detail = self.client.get(f"/admin/near-miss/{near_miss_id}")
        self.assertEqual(near_miss_detail.status_code, 200)
        violation_detail = self.client.get(f"/admin/violations/{violation_id}")
        self.assertEqual(violation_detail.status_code, 200)

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

        response = self.client.get("/admin/backup?token=test-export-token")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")

        archive = zipfile.ZipFile(io.BytesIO(response.data))
        names = archive.namelist()
        self.assertEqual(len(names), 5)
        self.assertTrue(any(name.startswith("inspections-summary-") for name in names))
        self.assertTrue(any(name.startswith("inspections-detailed-") for name in names))
        self.assertTrue(any(name.startswith("near-miss-") for name in names))
        self.assertTrue(any(name.startswith("violations-") for name in names))
        self.assertTrue(any(name.startswith("ptw-log-") for name in names))


if __name__ == "__main__":
    unittest.main()
