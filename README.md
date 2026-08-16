# 1 Hotel Diriyah Daily OHS Checklist — Render edition

This folder contains the Render-ready edition of the checklist application.

## Included workflow

- Public mobile-friendly inspection form with all 102 requirements from the source checklist
- Required observations for every non-compliant response
- Automatic compliance score and digital sign-off
- Protected records dashboard with search and date filters
- Summary and item-level CSV downloads
- Individual printable records (use the browser's **Save as PDF** option)
- PostgreSQL storage on Render, with SQLite fallback for local testing

## Deploy on Render

1. Put the project in a GitHub, GitLab, or Bitbucket repository.
2. In Render, choose **New > Blueprint** and connect the repository.
3. Use `render.yaml` for long-term project records. It provisions the smallest paid web service and paid PostgreSQL database in Frankfurt.
4. When prompted, set a strong `ADMIN_PASSWORD`. Render generates `SECRET_KEY` and connects `DATABASE_URL` automatically.
5. Review the estimated monthly charges, then deploy the Blueprint.

For evaluation only, select `render.free.yaml` as the Blueprint path. A free Render PostgreSQL database expires after 30 days, so do not rely on it for lasting OHS records.

## Local test

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export ADMIN_PASSWORD='replace-this-for-local-testing'
export SECRET_KEY='replace-this-with-a-long-random-value'
python app.py
```

Without `DATABASE_URL`, records are written to `data/ohs.db`.
