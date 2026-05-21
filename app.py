import os
import json
import base64
import io
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

# ── CONFIG ──────────────────────────────────────────────────────────────────
SHEET_ID      = "1MiIJ5B5u3cZF09mGDHlEy8NXuY-hzdArR37feOsbZ90"
SHEET_TAB     = "Sheet1"
DRIVE_FOLDER  = "1WJtafA1OxP3BvV2Clc3Hg8L_GrrF3F-Y"
SCOPES        = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Columns in the sheet (in order)
COLUMNS = ["id", "date", "amount", "category", "particulars", "notes",
           "submittedBy", "role", "receiptUrl", "createdAt"]


# ── GOOGLE AUTH ──────────────────────────────────────────────────────────────
def _creds():
    """Build credentials from SERVICE_ACCOUNT_JSON env var or local file."""
    sa_env = os.environ.get("SERVICE_ACCOUNT_JSON")
    if sa_env:
        info = json.loads(sa_env)
    else:
        sa_file = os.path.join(os.path.dirname(__file__), "service_account.json")
        with open(sa_file) as f:
            info = json.load(f)
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def sheets():
    return build("sheets", "v4", credentials=_creds(), cache_discovery=False)


def drive():
    return build("drive", "v3", credentials=_creds(), cache_discovery=False)


# ── SHEET HELPERS ────────────────────────────────────────────────────────────
def _ensure_header():
    """Write header row if Sheet1 is empty."""
    svc = sheets()
    result = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A1:Z1"
    ).execute()
    if not result.get("values"):
        svc.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A1",
            valueInputOption="RAW",
            body={"values": [COLUMNS]},
        ).execute()


def _append_row(row: list):
    sheets().spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_TAB}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def _get_all_rows():
    result = sheets().spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A:Z"
    ).execute()
    return result.get("values", [])


def _delete_row_by_id(entry_id: str):
    """Find the row with matching id and clear it (mark deleted)."""
    rows = _get_all_rows()
    for i, row in enumerate(rows):
        if row and row[0] == entry_id:
            # Row index in Sheets is 1-based; row 0 is header
            svc = sheets()
            svc.spreadsheets().values().clear(
                spreadsheetId=SHEET_ID,
                range=f"{SHEET_TAB}!A{i+1}:Z{i+1}",
            ).execute()
            return True
    return False


# ── DRIVE HELPERS ────────────────────────────────────────────────────────────
def _upload_to_drive(filename: str, mimetype: str, data: bytes) -> str:
    """Upload bytes to Drive folder, return public shareable URL."""
    svc = drive()
    meta = {"name": filename, "parents": [DRIVE_FOLDER]}
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype)
    file = svc.files().create(body=meta, media_body=media, fields="id").execute()
    file_id = file["id"]
    # Make it publicly readable so the img src works in the browser
    svc.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()
    return f"https://drive.google.com/uc?id={file_id}"


# ── ROUTES ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/expenses", methods=["GET"])
def get_expenses():
    try:
        rows = _get_all_rows()
        if not rows:
            return jsonify([])
        header = rows[0]
        expenses = []
        for row in rows[1:]:
            if not any(row):   # skip empty/deleted rows
                continue
            # Pad short rows
            padded = row + [""] * (len(header) - len(row))
            expenses.append(dict(zip(header, padded)))
        return jsonify(expenses)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/expenses", methods=["POST"])
def add_expense():
    try:
        _ensure_header()
        data = request.get_json()

        receipt_url = ""
        if data.get("receiptB64"):
            # Decode base64 image and upload to Drive
            header, b64data = data["receiptB64"].split(",", 1)
            mimetype = header.split(":")[1].split(";")[0]
            ext = mimetype.split("/")[1]
            img_bytes = base64.b64decode(b64data)
            filename = f"receipt_{data.get('id', datetime.now().timestamp())}.{ext}"
            receipt_url = _upload_to_drive(filename, mimetype, img_bytes)

        row = [
            str(data.get("id", "")),
            data.get("date", ""),
            str(data.get("amount", "")),
            data.get("category", ""),
            data.get("particulars", ""),
            data.get("notes", ""),
            data.get("submittedBy", ""),
            data.get("role", ""),
            receipt_url,
            datetime.now().isoformat(),
        ]
        _append_row(row)
        return jsonify({"ok": True, "receiptUrl": receipt_url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/expenses/<entry_id>", methods=["DELETE"])
def delete_expense(entry_id):
    try:
        found = _delete_row_by_id(entry_id)
        return jsonify({"ok": found})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/upload-csv", methods=["POST"])
def upload_csv():
    """Save uploaded CSV to Drive and return the Drive URL."""
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file"}), 400
        f = request.files["file"]
        filename = f.filename or f"upload_{datetime.now().timestamp()}.csv"
        csv_bytes = f.read()
        url = _upload_to_drive(filename, "text/csv", csv_bytes)
        return jsonify({"ok": True, "url": url, "filename": filename})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
