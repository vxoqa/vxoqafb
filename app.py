import os
import re
import json
import requests
import gspread
from flask import Flask, jsonify
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# Load credentials from Render Environment Variables
FB_PAGE_ID = os.environ.get("FB_PAGE_ID")
FB_ACCESS_TOKEN = os.environ.get("FB_ACCESS_TOKEN")
SHEET_URL = os.environ.get("SHEET_URL")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON")
CRON_SECRET = os.environ.get("CRON_SECRET", "my-secret-trigger")

def get_drive_direct_bytes(drive_url, creds):
    """Extracts the Drive file ID from Column K and downloads the image bytes via Drive API."""
    match = re.search(r'/d/([a-zA-Z0-9_-]+)', drive_url)
    if not match:
        raise ValueError(f"Invalid Google Drive URL: {drive_url}")
    file_id = match.group(1)
    
    # Use Drive API v3 to download binary image directly (bypasses virus-scan/redirect issues)
    from google.auth.transport.requests import AuthorizedSession
    authed_session = AuthorizedSession(creds)
    download_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    response = authed_session.get(download_url)
    response.raise_for_status()
    return response.content

@app.route("/run-facebook-post/<secret>", methods=["GET", "POST"])
def post_next_row(secret):
    if secret != CRON_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly"
    ]
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)

    sheet = client.open_by_url(SHEET_URL).worksheet("Content Archive")
    rows = sheet.get_all_values()

    # Iterate through rows (skipping header row 1)
    for idx, row in enumerate(rows[1:], start=2):
        if len(row) < 15:
            continue
            
        record_id = row[0]        # Column A: Record ID
        drive_img_url = row[10]   # Column K: Final Facebook Image
        caption = row[11]         # Column L: Facebook Caption
        hashtags = row[12]        # Column M: Hashtags
        fb_status = row[14]       # Column O: Facebook Publishing Status

        # Check if row is waiting to be published
        if "Ready for Review" in fb_status or fb_status.strip().lower() == "approved":
            full_message = f"{caption}\n\n{hashtags}".strip()
            
            # Download image bytes from Drive
            img_bytes = get_drive_direct_bytes(drive_img_url, creds)

            # Publish Photo + Caption to Facebook Graph API
            fb_url = f"https://graph.facebook.com/v25.0/{FB_PAGE_ID}/photos"
            files = {"source": ("post_image.jpg", img_bytes, "image/jpeg")}
            payload = {
                "caption": full_message,
                "access_token": FB_ACCESS_TOKEN,
                # Optional: Feed targeting to prioritize US users on Page posts
                "feed_targeting": json.dumps({"geo_locations": {"countries": ["US"]}})
            }

            fb_res = requests.post(fb_url, data=payload, files=files)
            fb_data = fb_res.json()

            if fb_res.status_code == 200 and "id" in fb_data:
                # Update Column O (col 15) in Google Sheet
                sheet.update_cell(idx, 15, f"Published (FB ID: {fb_data['id']})")
                return jsonify({"status": "success", "record_id": record_id, "fb_post_id": fb_data["id"]})
            else:
                return jsonify({"status": "facebook_error", "details": fb_data}), 400

    return jsonify({"status": "no_pending_posts"})

@app.route("/")
def health():
    return "VXOQA Auto-Poster Running!"
