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

# Environment Variables configured in Render
FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "").strip()
FB_ACCESS_TOKEN = os.environ.get("FB_ACCESS_TOKEN", "").strip()
SHEET_URL = os.environ.get("SHEET_URL", "").strip()
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "").strip()
CRON_SECRET = os.environ.get("CRON_SECRET", "my-secret-trigger").strip()
CF_WORKER_URL = os.environ.get("CF_WORKER_URL", "").strip().rstrip("/")


def log_to_cloudflare(record_id, headline, status, fb_post_id=None, details="", sheet_row=None):
    """Sends post execution logs to the Cloudflare Worker KV Dashboard."""
    if not CF_WORKER_URL:
        return
    try:
        requests.post(
            f"{CF_WORKER_URL}/api/log",
            headers={
                "X-Log-Secret": CRON_SECRET,
                "Content-Type": "application/json"
            },
            json={
                "record_id": record_id,
                "headline": headline,
                "status": status,
                "fb_post_id": fb_post_id,
                "sheet_row": sheet_row,
                "details": str(details)[:300]
            },
            timeout=10
        )
    except Exception:
        pass


def get_drive_direct_bytes(drive_url, creds):
    """Extracts the Google Drive file ID from Column K and downloads the image bytes."""
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


def resolve_page_credentials(configured_page_id, token):
    """
    Checks if the token is already a Page Token or a User Token.
    If it is a User Token, automatically retrieves the Page ID and Page Access Token from /me/accounts.
    """
    me_res = requests.get(
        "https://graph.facebook.com/v25.0/me",
        params={"fields": "id,name,category", "access_token": token}
    ).json()

    # If 'category' is returned, the token is already a Page Access Token
    if "category" in me_res:
        return me_res["id"], token, {"token_type": "Page Token", "page_name": me_res.get("name")}

    # Otherwise, query /me/accounts to extract the Page Access Token
    accounts_res = requests.get(
        "https://graph.facebook.com/v25.0/me/accounts",
        params={"fields": "id,name,access_token,tasks", "access_token": token}
    ).json()

    pages = accounts_res.get("data", [])
    if pages:
        selected_page = next((p for p in pages if p["id"] == configured_page_id), pages[0])
        if "access_token" in selected_page:
            return selected_page["id"], selected_page["access_token"], {
                "token_type": "Auto-converted Page Token",
                "page_name": selected_page.get("name"),
                "page_id": selected_page.get("id")
            }

    return None, None, {"error": "No Page Token found", "details": accounts_res}


@app.route("/run-facebook-post/<path:secret>", methods=["GET", "POST"])
def post_next_row(secret):
    if secret != CRON_SECRET:
        return jsonify({"error": "Unauthorized - CRON_SECRET does not match"}), 403

    try:
        # 1. Verify Environment Variables
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

        # 2. Resolve Facebook Page ID and Page Token
        real_page_id, real_page_token, token_diag = resolve_page_credentials(FB_PAGE_ID, FB_ACCESS_TOKEN)
        if not real_page_id or not real_page_token:
            log_to_cloudflare("N/A", "Token Check", "token_error", details=json.dumps(token_diag))
            return jsonify({"status": "facebook_token_setup_incomplete", "diagnostic": token_diag}), 400

        # 3. Authenticate Google Sheets & Drive API
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly"
        ]
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)

        spreadsheet = client.open_by_url(SHEET_URL)
        try:
            sheet = spreadsheet.worksheet("Content Archive")
        except Exception:
            sheet = spreadsheet.get_worksheet(0)

        rows = sheet.get_all_values()

        # 4. Iterate strictly from Row 2 (newest top row) downward
        for idx, row in enumerate(rows[1:], start=2):
            # Pad row so trailing blank cells never skip a valid row
            row = row + [""] * max(0, 15 - len(row))

            record_id = row[0].strip()      # Column A: Record ID
            headline = row[3].strip()       # Column D: News Headline
            drive_img_url = row[10].strip() # Column K: Final Facebook Image
            caption = row[11].strip()       # Column L: Facebook Caption
            hashtags = row[12].strip()      # Column M: Hashtags
            fb_status = row[14].strip()     # Column O: Facebook Publishing Status

            # Skip if row has no image/caption or is already marked Published
            if not drive_img_url or not caption:
                continue
            if fb_status.lower().startswith("published"):
                continue

            # 5. Download image from Google Drive and post to Facebook Page
            full_message = f"{caption}\n\n{hashtags}".strip()
            img_bytes = get_drive_direct_bytes(drive_img_url, creds)

            fb_url = f"https://graph.facebook.com/v25.0/{real_page_id}/photos"
            files = {"source": ("post_image.jpg", img_bytes, "image/jpeg")}
            payload = {
                "caption": full_message,
                "access_token": real_page_token
            }

            fb_res = requests.post(fb_url, data=payload, files=files)
            fb_data = fb_res.json()

            if fb_res.status_code == 200 and "id" in fb_data:
                # Re-verify the exact row number by Record ID (Column A) in case a new row was inserted at Row 2 mid-run
                target_row = idx
                if record_id:
                    try:
                        cell = sheet.find(record_id, in_column=1)
                        if cell:
                            target_row = cell.row
                    except Exception:
                        target_row = idx

                sheet.update_cell(target_row, 15, f"Published (FB ID: {fb_data['id']})")
                log_to_cloudflare(
                    record_id=record_id,
                    headline=headline,
                    status="success",
                    fb_post_id=fb_data["id"],
                    sheet_row=target_row
                )
                return jsonify({
                    "status": "success",
                    "sheet_row_posted": target_row,
                    "record_id": record_id,
                    "headline": headline,
                    "fb_post_id": fb_data["id"]
                })
            else:
                log_to_cloudflare(
                    record_id=record_id,
                    headline=headline,
                    status="facebook_error",
                    details=json.dumps(fb_data),
                    sheet_row=idx
                )
                return jsonify({
                    "status": "facebook_error",
                    "sheet_row_attempted": idx,
                    "record_id": record_id,
                    "details": fb_data
                }), 400

        log_to_cloudflare("-", "Queue Checked", "no_pending_posts", details="All rows already published")
        return jsonify({"status": "no_pending_posts"})

    except Exception as e:
        log_to_cloudflare("ERROR", "System Exception", "python_exception", details=str(e))
        return jsonify({
            "status": "python_exception",
            "error_type": type(e).__name__,
            "error_message": str(e),
            "traceback": traceback.format_exc()
        }), 500


@app.route("/")
def health():
    return "VXOQA Auto-Poster Running!"
