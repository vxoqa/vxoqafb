import os
import re
import json
import traceback
import requests
import gspread
from flask import Flask, jsonify
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession

app = Flask(__name__)

FB_PAGE_ID = os.environ.get("FB_PAGE_ID")
FB_ACCESS_TOKEN = os.environ.get("FB_ACCESS_TOKEN")
SHEET_URL = os.environ.get("SHEET_URL")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON")
CRON_SECRET = os.environ.get("CRON_SECRET", "my-secret-trigger")

def get_drive_direct_bytes(drive_url, creds):
    match = re.search(r'/d/([a-zA-Z0-9_-]+)', drive_url)
    if not match:
        raise ValueError(f"Invalid Google Drive URL in Column K: {drive_url}")
    file_id = match.group(1)
    
    authed_session = AuthorizedSession(creds)
    download_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    response = authed_session.get(download_url)
    if response.status_code != 200:
        raise RuntimeError(f"Drive API download failed (HTTP {response.status_code}): {response.text}")
    return response.content

@app.route("/run-facebook-post/<path:secret>", methods=["GET", "POST"])
def post_next_row(secret):
    if secret != CRON_SECRET:
        return jsonify({"error": "Unauthorized - CRON_SECRET does not match"}), 403

    try:
        # 1. Verify Environment Variables exist
        missing_vars = [
            name for name, val in [
                ("FB_PAGE_ID", FB_PAGE_ID),
                ("FB_ACCESS_TOKEN", FB_ACCESS_TOKEN),
                ("SHEET_URL", SHEET_URL),
                ("GOOGLE_CREDS_JSON", GOOGLE_CREDS_JSON)
            ] if not val
        ]
        if missing_vars:
            return jsonify({"error": "Missing Render Environment Variables", "missing": missing_vars}), 400

        # 2. Parse Google Service Account JSON
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly"
        ]
        creds_dict = json.loads(GOOGLE_CREDS_JSON.strip())
        # Fix private_key escaped newlines if Render altered them
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)

        # 3. Open Sheet & Worksheet
        spreadsheet = client.open_by_url(SHEET_URL.strip())
        try:
            sheet = spreadsheet.worksheet("Content Archive")
        except Exception:
            sheet = spreadsheet.get_worksheet(0)  # Fallback to first tab

        rows = sheet.get_all_values()

        # 4. Find next pending row
        for idx, row in enumerate(rows[1:], start=2):
            if len(row) < 15:
                continue
                
            record_id = row[0]        # Column A: Record ID
            drive_img_url = row[10]   # Column K: Final Facebook Image
            caption = row[11]         # Column L: Facebook Caption
            hashtags = row[12]        # Column M: Hashtags
            fb_status = row[14]       # Column O: Facebook Publishing Status

            if "Ready for Review" in fb_status or fb_status.strip().lower() == "approved":
                full_message = f"{caption}\n\n{hashtags}".strip()
                img_bytes = get_drive_direct_bytes(drive_img_url, creds)

                fb_url = f"https://graph.facebook.com/v25.0/{FB_PAGE_ID.strip()}/photos"
                files = {"source": ("post_image.jpg", img_bytes, "image/jpeg")}
                payload = {
                    "caption": full_message,
                    "access_token": FB_ACCESS_TOKEN.strip()
                }

                fb_res = requests.post(fb_url, data=payload, files=files)
                fb_data = fb_res.json()

                if fb_res.status_code == 200 and "id" in fb_data:
                    sheet.update_cell(idx, 15, f"Published (FB ID: {fb_data['id']})")
                    return jsonify({"status": "success", "record_id": record_id, "fb_post_id": fb_data["id"]})
                else:
                    return jsonify({"status": "facebook_error", "details": fb_data}), 400

        return jsonify({"status": "no_pending_posts"})

    except Exception as e:
        return jsonify({
            "status": "python_exception",
            "error_type": type(e).__name__,
            "error_message": str(e),
            "traceback": traceback.format_exc()
        }), 500

@app.route("/")
def health():
    return "VXOQA Auto-Poster Running!"
