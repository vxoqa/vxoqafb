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

FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "").strip()
FB_ACCESS_TOKEN = os.environ.get("FB_ACCESS_TOKEN", "").strip()
SHEET_URL = os.environ.get("SHEET_URL", "").strip()
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "").strip()
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

def resolve_page_credentials(configured_page_id, token):
    """
    Checks whether the provided token is a Page Token or a User Token.
    If it is a User Token, automatically fetches the real Page ID and Page Access Token from /me/accounts.
    """
    me_res = requests.get(
        "https://graph.facebook.com/v25.0/me",
        params={"fields": "id,name,category", "access_token": token}
    ).json()

    # If "category" exists on /me, the token is ALREADY a Page Access Token
    if "category" in me_res:
        return me_res["id"], token, {"token_type": "Page Token", "page_name": me_res.get("name")}

    # Otherwise, it is a User Access Token -> look up the user's Pages via /me/accounts
    accounts_res = requests.get(
        "https://graph.facebook.com/v25.0/me/accounts",
        params={"fields": "id,name,access_token,tasks", "access_token": token}
    ).json()

    perms_res = requests.get(
        "https://graph.facebook.com/v25.0/me/permissions",
        params={"access_token": token}
    ).json()

    pages = accounts_res.get("data", [])
    if pages:
        # Match configured_page_id if possible; otherwise auto-select the first Page
        selected_page = next((p for p in pages if p["id"] == configured_page_id), pages[0])
        if "access_token" in selected_page:
            return selected_page["id"], selected_page["access_token"], {
                "token_type": "Auto-converted User Token to Page Token",
                "page_name": selected_page.get("name"),
                "page_id": selected_page.get("id"),
                "tasks": selected_page.get("tasks", [])
            }

    # If no Pages were found on the User Token, return a clear diagnostic
    return None, None, {
        "error": "Your FB_ACCESS_TOKEN is a User Token, not a Page Token, and no Facebook Pages are linked to it.",
        "token_owner": me_res,
        "granted_permissions": perms_res.get("data", []),
        "pages_returned_by_me_accounts": accounts_res
    }

@app.route("/run-facebook-post/<path:secret>", methods=["GET", "POST"])
def post_next_row(secret):
    if secret != CRON_SECRET:
        return jsonify({"error": "Unauthorized - CRON_SECRET does not match"}), 403

    try:
        # 1. Verify Facebook Token & Auto-Resolve Page Token + Page ID
        real_page_id, real_page_token, token_diag = resolve_page_credentials(FB_PAGE_ID, FB_ACCESS_TOKEN)
        if not real_page_id or not real_page_token:
            return jsonify({"status": "facebook_token_setup_incomplete", "diagnostic": token_diag}), 400

        # 2. Authenticate Google Sheets & Drive
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

        # 3. Find next pending row and post
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

                fb_url = f"https://graph.facebook.com/v25.0/{real_page_id}/photos"
                files = {"source": ("post_image.jpg", img_bytes, "image/jpeg")}
                payload = {
                    "caption": full_message,
                    "access_token": real_page_token
                }

                fb_res = requests.post(fb_url, data=payload, files=files)
                fb_data = fb_res.json()

                if fb_res.status_code == 200 and "id" in fb_data:
                    sheet.update_cell(idx, 15, f"Published (FB ID: {fb_data['id']})")
                    return jsonify({
                        "status": "success",
                        "record_id": record_id,
                        "fb_post_id": fb_data["id"],
                        "page_used": token_diag
                    })
                else:
                    return jsonify({
                        "status": "facebook_error",
                        "page_used": token_diag,
                        "details": fb_data
                    }), 400

        return jsonify({"status": "no_pending_posts", "page_used": token_diag})

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
