import csv
import io
import os
import tempfile
import unittest


TEST_FILES = tempfile.TemporaryDirectory()
os.environ["OHS_DB_PATH"] = os.path.join(TEST_FILES.name, "test.db")
os.environ["ADMIN_PASSWORD"] = "test-admin-password"
os.environ["SECRET_KEY"] = "test-secret-key-that-is-only-used-by-the-automated-suite"

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
            "reportNo": "QA-TEST-001",
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

    def test_checklist_has_all_source_requirements(self):
        response = self.client.get("/api/checklist")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["total"], 102)
        self.assertEqual(len(response.json["sections"]), 14)

    def test_submit_review_and_export_record(self):
        response = self.client.post("/api/inspections", json=self.payload())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["score"], 100.0)
        record_id = response.json["id"]

        self.assertEqual(self.login().status_code, 302)
        dashboard = self.client.get("/admin")
        self.assertIn(b"QA-TEST-001", dashboard.data)
        detail = self.client.get(f"/admin/records/{record_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"GENERAL BEST PRACTICE", detail.data)

        summary = self.client.get("/admin/export?kind=summary")
        self.assertEqual(summary.status_code, 200)
        self.assertIn("attachment", summary.headers["Content-Disposition"])
        self.assertIn("QA-TEST-001", summary.get_data(as_text=True))

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

    def test_near_miss_form_pages_load(self):
        self.assertEqual(self.client.get("/near-miss").status_code, 200)
        self.assertEqual(self.client.get("/violation").status_code, 200)

    def test_near_miss_requires_at_least_one_type(self):
        payload = self.near_miss_payload()
        payload["nearMissTypes"] = []
        response = self.client.post("/api/near-miss", json=payload)
        self.assertEqual(response.status_code, 400)

    def test_submit_review_and_export_near_miss(self):
        response = self.client.post("/api/near-miss", json=self.near_miss_payload())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["reportNo"], "NM-0001")
        record_id = response.json["id"]

        self.login()
        dashboard = self.client.get("/admin?view=near-miss")
        self.assertIn(b"NM-0001", dashboard.data)
        detail = self.client.get(f"/admin/near-miss/{record_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"NEAR MISS REPORTING FORM", detail.data)
        self.assertIn(b"Unsafe Condition", detail.data)

        export = self.client.get("/admin/export/near-miss")
        self.assertEqual(export.status_code, 200)
        self.assertIn("NM-0001", export.get_data(as_text=True))

    def test_violation_requires_employee_name(self):
        payload = self.violation_payload()
        payload["employeeName"] = ""
        response = self.client.post("/api/violations", json=payload)
        self.assertEqual(response.status_code, 400)

    def test_submit_review_and_export_violation(self):
        response = self.client.post("/api/violations", json=self.violation_payload())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["violationNo"], "VN-0001")
        record_id = response.json["id"]

        self.login()
        dashboard = self.client.get("/admin?view=violations")
        self.assertIn(b"VN-0001", dashboard.data)
        detail = self.client.get(f"/admin/violations/{record_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"VIOLATION NOTICE", detail.data.upper())
        self.assertIn(b"First Warning", detail.data)

        export = self.client.get("/admin/export/violations")
        self.assertEqual(export.status_code, 200)
        self.assertIn("VN-0001", export.get_data(as_text=True))

    def test_sequential_report_numbers(self):
        first = self.client.post("/api/inspections", json=self.payload())
        second_payload = self.payload()
        second_payload["reportNo"] = ""
        second = self.client.post("/api/inspections", json=second_payload)
        self.assertEqual(first.json["reportNo"], "QA-TEST-001")
        self.assertTrue(second.json["reportNo"].startswith("OHS-"))

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


if __name__ == "__main__":
    unittest.main()
