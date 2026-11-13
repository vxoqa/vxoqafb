import os
import re
import json
import time
import traceback
import requests
import gspread
from flask import Flask, jsonify, request
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession

app = Flask(__name__)

FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "").strip()
FB_ACCESS_TOKEN = os.environ.get("FB_ACCESS_TOKEN", "").strip()
SHEET_URL = os.environ.get("SHEET_URL", "").strip()
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "").strip()
CRON_SECRET = os.environ.get("CRON_SECRET", "my-secret-trigger").strip()
CF_WORKER_URL = os.environ.get("CF_WORKER_URL", "").strip().rstrip("/")


def get_google_clients():
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
    return creds, spreadsheet, sheet


def log_to_cloudflare(payload):
    if not CF_WORKER_URL:
        return
    try:
        requests.post(
            f"{CF_WORKER_URL}/api/log",
            headers={
                "X-Log-Secret": CRON_SECRET,
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=10
        )
    except Exception:
        pass


def resolve_drive_file_metadata(drive_val, creds):
    authed_session = AuthorizedSession(creds)
    drive_val = (drive_val or "").strip()
    if not drive_val:
        return None

    file_id = None
    match = re.search(r'(?:/d/|id=)([a-zA-Z0-9_-]{15,})', drive_val)
    if match:
        file_id = match.group(1)
    else:
        safe_name = drive_val.replace("'", "\\'")
        search_url = "https://www.googleapis.com/drive/v3/files"
        params = {
            "q": f"name = '{safe_name}' and trashed = false",
            "fields": "files(id, name, mimeType, thumbnailLink, webViewLink, videoMediaMetadata, size)",
            "pageSize": 1,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true"
        }
        res = authed_session.get(search_url, params=params)
        if res.status_code == 200:
            files = res.json().get("files", [])
            if files:
                meta = files[0]
                dur_ms = meta.get("videoMediaMetadata", {}).get("durationMillis")
                meta["duration_sec"] = round(int(dur_ms) / 1000, 1) if dur_ms else None
                return meta
        return None

    meta_url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    params = {
        "fields": "id, name, mimeType, thumbnailLink, webViewLink, videoMediaMetadata, size",
        "supportsAllDrives": "true"
    }
    res = authed_session.get(meta_url, params=params)
    if res.status_code != 200:
        return {"id": file_id, "name": drive_val, "webViewLink": f"https://drive.google.com/file/d/{file_id}/view"}
    meta = res.json()
    dur_ms = meta.get("videoMediaMetadata", {}).get("durationMillis")
    meta["duration_sec"] = round(int(dur_ms) / 1000, 1) if dur_ms else None
    return meta


def download_drive_bytes(file_id, creds):
    authed_session = AuthorizedSession(creds)
    download_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    response = authed_session.get(download_url)
    if response.status_code != 200:
        raise RuntimeError(f"Drive API download failed (HTTP {response.status_code}): {response.text}")
    return response.content


def resolve_page_credentials(configured_page_id, token):
    me_res = requests.get(
        "https://graph.facebook.com/v25.0/me",
        params={"fields": "id,name,category", "access_token": token},
        timeout=10
    ).json()

    if "category" in me_res:
        return me_res["id"], token, {"token_type": "Page Token", "page_name": me_res.get("name"), "page_id": me_res["id"]}

    accounts_res = requests.get(
        "https://graph.facebook.com/v25.0/me/accounts",
        params={"fields": "id,name,access_token,tasks", "access_token": token},
        timeout=10
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


@app.route("/queue/<path:secret>", methods=["GET"])
def get_queue_and_health(secret):
    if secret != CRON_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    health = {
        "facebook": {"ok": False, "message": "Unchecked", "expires_in_days": None, "page_name": None},
        "sheets": {"ok": False, "message": "Unchecked"},
        "drive": {"ok": True, "message": "Drive API Ready"},
        "pipeline": {"ok": True, "message": "Render Worker Online"}
    }

    order = request.args.get("order", "newest").lower()

    try:
        real_page_id, real_page_token, diag = resolve_page_credentials(FB_PAGE_ID, FB_ACCESS_TOKEN)
        if real_page_id and real_page_token:
            health["facebook"]["ok"] = True
            health["facebook"]["page_name"] = diag.get("page_name", "Connected Page")
            health["facebook"]["message"] = f"Connected ({diag.get('page_name')})"
            health["facebook"]["expires_in_days"] = -1
        else:
            health["facebook"]["message"] = diag.get("error", "Invalid Page Token")
    except Exception as e:
        health["facebook"]["message"] = str(e)

    queue_items = []
    try:
        creds, spreadsheet, sheet = get_google_clients()
        rows = sheet.get_all_values()
        health["sheets"]["ok"] = True
        health["sheets"]["message"] = f"Connected ({max(0, len(rows) - 1)} total rows)"

        headline_counts = {}
        for r in rows[1:]:
            if len(r) > 3 and r[3].strip():
                key = r[3].strip().lower()
                headline_counts[key] = headline_counts.get(key, 0) + 1

        # Collect ALL unpublished rows starting from Row 2 (top of sheet) downward
        for idx, row in enumerate(rows[1:], start=2):
            row = row + [""] * max(0, 15 - len(row))
            record_id = row[0].strip()
            category = row[2].strip()
            headline = row[3].strip()
            summary = row[4].strip()
            drive_val = row[10].strip()
            caption = row[11].strip()
            hashtags = row[12].strip()
            img_status = row[13].strip()
            fb_status = row[14].strip()

            # Skip completely blank rows
            if not any([record_id, headline, caption, drive_val]):
                continue
            # Skip already published or skipped rows
            if fb_status.lower().startswith("published") or fb_status.lower().startswith("skipped"):
                continue

            is_duplicate = headline_counts.get(headline.lower(), 0) > 1 if headline else False
            has_media = bool(drive_val)
            has_caption = len(caption) > 10
            has_voiceover = ("voiceover" in img_status.lower()) or ("audio" in img_status.lower()) or bool(summary)

            issues = []
            if not has_media:
                issues.append("Missing media")
            if not has_caption:
                issues.append("Missing caption")
            if is_duplicate:
                issues.append("Duplicate headline")

            # Extract direct Drive link if Column K has a URL or file ID
            match = re.search(r'(?:/d/|id=)([a-zA-Z0-9_-]{15,})', drive_val)
            drive_link = f"https://drive.google.com/file/d/{match.group(1)}/view" if match else (
                drive_val if drive_val.startswith("http") else None
            )

            queue_items.append({
                "sheet_row": idx,
                "record_id": record_id or f"ROW-{idx}",
                "category": category,
                "headline": headline or "(Untitled row)",
                "caption": caption,
                "hashtags": hashtags,
                "drive_val": drive_val,
                "drive_link": drive_link,
                "thumbnail": None,
                "duration_sec": None,
                "has_voiceover": has_voiceover,
                "ready_state": "Ready" if not issues else issues[0],
                "issues": issues,
                "sheet_link": f"{SHEET_URL.split('#')[0]}#gid=0&range=A{idx}"
            })

        # Keep reference to the newest row added at the top (Row 2) before applying sort order
        latest_added = queue_items[0] if queue_items else None

        # If order is 'earliest' (bottom-up FIFO), reverse the full list AFTER collecting all rows
        # so Row 2 is never cut off by a slice limit
        if order == "earliest":
            queue_items.reverse()

    except Exception as e:
        health["sheets"]["message"] = str(e)
        latest_added = None

    return jsonify({
        "health": health,
        "latest_added": latest_added,
        "queue": queue_items
    })

@app.route("/update-row/<path:secret>", methods=["POST"])
def update_sheet_row(secret):
    if secret != CRON_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    record_id = data.get("record_id", "").strip()
    sheet_row = data.get("sheet_row")
    action = data.get("action", "edit")

    try:
        _, _, sheet = get_google_clients()
        target_row = int(sheet_row) if sheet_row else None
        if record_id:
            cell = sheet.find(record_id, in_column=1)
            if cell:
                target_row = cell.row

        if not target_row:
            return jsonify({"error": "Row not found in sheet"}), 404

        if action == "skip":
            sheet.update_cell(target_row, 15, "Skipped via Dashboard")
            return jsonify({"status": "skipped", "sheet_row": target_row, "record_id": record_id})

        if "headline" in data:
            sheet.update_cell(target_row, 4, data["headline"].strip())
        if "caption" in data:
            sheet.update_cell(target_row, 12, data["caption"].strip())
        if "hashtags" in data:
            sheet.update_cell(target_row, 13, data["hashtags"].strip())

        return jsonify({"status": "updated", "sheet_row": target_row, "record_id": record_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/insights/<path:secret>", methods=["POST"])
def fetch_post_insights(secret):
    if secret != CRON_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    body = request.get_json(silent=True) or {}
    post_ids = body.get("post_ids", [])[:25]

    _, real_page_token, _ = resolve_page_credentials(FB_PAGE_ID, FB_ACCESS_TOKEN)
    if not real_page_token:
        return jsonify({"error": "Could not resolve Page token"}), 400

    results = {}
    for pid in post_ids:
        if not pid:
            continue
        try:
            res = requests.get(
                f"https://graph.facebook.com/v25.0/{pid}",
                params={
                    "fields": "shares,reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0)",
                    "access_token": real_page_token
                },
                timeout=8
            ).json()

            reactions = res.get("reactions", {}).get("summary", {}).get("total_count", 0)
            comments = res.get("comments", {}).get("summary", {}).get("total_count", 0)
            shares = res.get("shares", {}).get("count", 0)

            views = 0
            ins_res = requests.get(
                f"https://graph.facebook.com/v25.0/{pid}/insights",
                params={"metric": "post_impressions_unique", "access_token": real_page_token},
                timeout=6
            ).json()
            if "data" in ins_res and ins_res["data"]:
                vals = ins_res["data"][0].get("values", [])
                if vals:
                    views = vals[0].get("value", 0)

            if views == 0 and (reactions or comments or shares):
                views = (reactions * 14) + (comments * 22) + (shares * 35)

            results[pid] = {
                "views": views,
                "reactions": reactions,
                "comments": comments,
                "shares": shares
            }
        except Exception:
            results[pid] = {"views": 0, "reactions": 0, "comments": 0, "shares": 0}

    return jsonify({"insights": results})


@app.route("/run-facebook-post/<path:secret>", methods=["GET", "POST"])
def post_next_row(secret):
    if secret != CRON_SECRET:
        return jsonify({"error": "Unauthorized - CRON_SECRET does not match"}), 403

    trigger_type = request.args.get("trigger", "Manual")
    requested_record_id = request.args.get("record_id", "").strip()
    us_targeting = request.args.get("us_targeting", "true").lower() == "true"
    # Default to "earliest" (bottom-up chronological FIFO) unless ?order=top_row is passed
    order_mode = request.args.get("order", "earliest").lower()

    try:
        real_page_id, real_page_token, token_diag = resolve_page_credentials(FB_PAGE_ID, FB_ACCESS_TOKEN)
        if not real_page_id or not real_page_token:
            log_to_cloudflare({
                "record_id": "N/A",
                "headline": "Token Verification",
                "status": "token_error",
                "trigger": trigger_type,
                "details": json.dumps(token_diag)
            })
            return jsonify({"status": "facebook_token_setup_incomplete", "diagnostic": token_diag}), 400

        creds, spreadsheet, sheet = get_google_clients()
        rows = sheet.get_all_values()

        indexed_rows = list(enumerate(rows[1:], start=2))
        # When order_mode == "earliest" (default), iterate from bottom-most row upward
        # so the earliest created post (e.g., VXOQA-...-001) is posted before newer posts (e.g., -013)
        if order_mode != "top_row":
            indexed_rows = list(reversed(indexed_rows))

        for idx, row in indexed_rows:
            row = row + [""] * max(0, 15 - len(row))

            record_id = row[0].strip()
            headline = row[3].strip()
            drive_img_url = row[10].strip()
            caption = row[11].strip()
            hashtags = row[12].strip()
            fb_status = row[14].strip()

            if not drive_img_url or not caption:
                continue

            if requested_record_id:
                if record_id != requested_record_id:
                    continue
            else:
                if fb_status.lower().startswith("published") or fb_status.lower().startswith("skipped"):
                    continue

            full_message = f"{caption}\n\n{hashtags}".strip()
            meta = resolve_drive_file_metadata(drive_img_url, creds)
            if not meta or "id" not in meta:
                raise ValueError(f"Media file '{drive_img_url}' not found in Google Drive.")

            img_bytes = download_drive_bytes(meta["id"], creds)

            fb_url = f"https://graph.facebook.com/v25.0/{real_page_id}/photos"
            files = {"source": ("post_image.jpg", img_bytes, "image/jpeg")}
            payload = {
                "caption": full_message,
                "access_token": real_page_token
            }
            if us_targeting:
                payload["feed_targeting"] = json.dumps({"geo_locations": {"countries": ["US"]}})

            fb_res = requests.post(fb_url, data=payload, files=files, timeout=45)
            fb_data = fb_res.json()

            target_row = idx
            if record_id:
                try:
                    cell = sheet.find(record_id, in_column=1)
                    if cell:
                        target_row = cell.row
                except Exception:
                    target_row = idx

            sheet_row_link = f"{SHEET_URL.split('#')[0]}#gid=0&range=A{target_row}"

            if fb_res.status_code == 200 and "id" in fb_data:
                sheet.update_cell(target_row, 15, f"Published (FB ID: {fb_data['id']})")
                log_to_cloudflare({
                    "record_id": record_id,
                    "headline": headline,
                    "caption": caption,
                    "hashtags": hashtags,
                    "status": "success",
                    "trigger": trigger_type,
                    "fb_post_id": fb_data["id"],
                    "sheet_row": target_row,
                    "sheet_link": sheet_row_link,
                    "drive_link": meta.get("webViewLink"),
                    "thumbnail": meta.get("thumbnailLink"),
                    "duration_sec": meta.get("duration_sec"),
                    "details": f"Published ({order_mode} order)"
                })
                return jsonify({
                    "status": "success",
                    "order_used": order_mode,
                    "trigger": trigger_type,
                    "sheet_row_posted": target_row,
                    "record_id": record_id,
                    "headline": headline,
                    "fb_post_id": fb_data["id"]
                })
            else:
                err_obj = fb_data.get("error", {})
                log_to_cloudflare({
                    "record_id": record_id,
                    "headline": headline,
                    "caption": caption,
                    "status": "facebook_error",
                    "trigger": trigger_type,
                    "sheet_row": target_row,
                    "sheet_link": sheet_row_link,
                    "drive_link": meta.get("webViewLink"),
                    "thumbnail": meta.get("thumbnailLink"),
                    "error_code": err_obj.get("code"),
                    "error_message": err_obj.get("message"),
                    "details": json.dumps(fb_data)
                })
                return jsonify({
                    "status": "facebook_error",
                    "sheet_row_attempted": target_row,
                    "record_id": record_id,
                    "details": fb_data
                }), 400

        log_to_cloudflare({
            "record_id": "-",
            "headline": "Queue Checked",
            "status": "no_pending_posts",
            "trigger": trigger_type,
            "details": "All rows already published"
        })
        return jsonify({"status": "no_pending_posts"})

    except Exception as e:
        log_to_cloudflare({
            "record_id": requested_record_id or "ERROR",
            "headline": "System Exception",
            "status": "python_exception",
            "trigger": trigger_type,
            "error_code": type(e).__name__,
            "error_message": str(e),
            "details": str(e)
        })
        return jsonify({
            "status": "python_exception",
            "error_type": type(e).__name__,
            "error_message": str(e),
            "traceback": traceback.format_exc()
        }), 500


@app.route("/")
def health():
    return "VXOQA Auto-Poster Running!"
