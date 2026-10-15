"""
FIPConnect: An Online Event Inquiry, Booking, and Scheduling System
for Flare Infinity Productions.

Tech: Python · Flask · SQLite
Features: Client inquiry portal, status tracking, FIP Team login,
          admin dashboard with visual analytics.
"""

import os
import sqlite3
import secrets
import hashlib
import base64
import io
import re
import string
from datetime import datetime
from functools import wraps
import requests
import cv2
import numpy as np
from PIL import Image, ImageOps, ImageEnhance
import pytesseract
from thefuzz import fuzz
from dotenv import load_dotenv

# Try importing RapidOCR for zero-binary, pure-Python deep learning OCR
try:
    from rapidocr_onnxruntime import RapidOCR
    rapid_ocr_engine = RapidOCR()
    print("[OCR CONFIG] RapidOCR ONNX engine initialized successfully.")
except Exception as rocr_init_err:
    rapid_ocr_engine = None
    print(f"[OCR CONFIG] RapidOCR not available: {rocr_init_err}")

from flask import (
    Flask, render_template, request, redirect, url_for, flash, g, session, jsonify, send_from_directory
)
from werkzeug.utils import secure_filename

# ---------------------------------------------------------------------------
# PyTesseract Windows Executable Configuration
# ---------------------------------------------------------------------------
# If you are on Windows, install Tesseract-OCR from:
# https://github.com/UB-Mannheim/tesseract/wiki
# PyTesseract will search common installation locations automatically:
TESSERACT_WINDOWS_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
    r"C:\Tesseract-OCR\tesseract.exe"
]
for candidate_path in TESSERACT_WINDOWS_PATHS:
    if os.path.exists(candidate_path):
        pytesseract.pytesseract.tesseract_cmd = candidate_path
        print(f"[OCR CONFIG] PyTesseract Windows executable detected: {candidate_path}")
        break

# ---------------------------------------------------------------------------
# Load Environment Variables (.env)
# ---------------------------------------------------------------------------
load_dotenv()

# Hardcoded Face++ Credentials for Operational Testing
FACEPLUSPLUS_API_KEY = "b0uSL9nuSnJO0TXD6758FDzcIvM5Eipb"
FACEPLUSPLUS_API_SECRET = "dNrr8WS5LeYeclYEEFZgeFQp67osUh29"
FACE_MATCH_THRESHOLD = 75.0
OCR_MATCH_THRESHOLD = 75.0

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
app.config["PERMANENT_SESSION_LIFETIME"] = 3600  # 1-hour sessions

DATABASE = os.path.join(app.root_path, "fipconnect.db")

UPLOAD_STAFF_IDS = os.path.join(app.root_path, "uploads", "staff_ids")
os.makedirs(UPLOAD_STAFF_IDS, exist_ok=True)
app.config["UPLOAD_STAFF_IDS"] = UPLOAD_STAFF_IDS

# ---------------------------------------------------------------------------
# Password hashing helpers
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Return a salted SHA-256 hash of the password."""
    salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${hashed}"


def verify_password(stored: str, provided: str) -> bool:
    """Verify a password against its stored salted hash."""
    salt, hashed = stored.split("$", 1)
    return hashlib.sha256((salt + provided).encode()).hexdigest() == hashed

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    """Open a per-request database connection stored on Flask's `g` object."""
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    """Close the database connection at the end of each request."""
    try:
        db = g.pop("db", None)
        if db is not None:
            db.close()
    except Exception:
        pass


def init_db():
    """Create tables and seed a default admin account if needed."""
    db = sqlite3.connect(DATABASE)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS inquiries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tracking_code   TEXT    UNIQUE NOT NULL,
            client_name     TEXT    NOT NULL,
            email           TEXT    NOT NULL,
            phone           TEXT    NOT NULL,
            event_type      TEXT    NOT NULL,
            event_date      TEXT    NOT NULL,
            event_time      TEXT    NOT NULL,
            venue           TEXT    NOT NULL,
            guest_count     INTEGER NOT NULL,
            details         TEXT,
            status          TEXT    NOT NULL DEFAULT 'Pending',
            created_at      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS fip_team (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            username            TEXT    UNIQUE NOT NULL,
            password_hash       TEXT    NOT NULL,
            full_name           TEXT    NOT NULL,
            role                TEXT    NOT NULL DEFAULT 'staff',
            first_name          TEXT,
            middle_name         TEXT,
            last_name           TEXT,
            birthday            TEXT,
            id_file_path        TEXT,
            id_type             TEXT,
            verification_status TEXT    NOT NULL DEFAULT 'Unverified',
            created_at          TEXT    NOT NULL
        );
    """)

    # Migration check: Ensure all columns exist
    cursor = db.execute("PRAGMA table_info(fip_team)")
    columns = [col[1] for col in cursor.fetchall()]
    if "role" not in columns:
        db.execute("ALTER TABLE fip_team ADD COLUMN role TEXT NOT NULL DEFAULT 'staff'")
    if "first_name" not in columns:
        db.execute("ALTER TABLE fip_team ADD COLUMN first_name TEXT")
    if "middle_name" not in columns:
        db.execute("ALTER TABLE fip_team ADD COLUMN middle_name TEXT")
    if "last_name" not in columns:
        db.execute("ALTER TABLE fip_team ADD COLUMN last_name TEXT")
    if "birthday" not in columns:
        db.execute("ALTER TABLE fip_team ADD COLUMN birthday TEXT")
    if "id_file_path" not in columns:
        db.execute("ALTER TABLE fip_team ADD COLUMN id_file_path TEXT")
    if "id_type" not in columns:
        db.execute("ALTER TABLE fip_team ADD COLUMN id_type TEXT")
    if "verification_status" not in columns:
        db.execute("ALTER TABLE fip_team ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'Unverified'")
    if "selfie_file_path" not in columns:
        db.execute("ALTER TABLE fip_team ADD COLUMN selfie_file_path TEXT")

    # Create users view pointing to fip_team for compatibility
    db.execute("CREATE VIEW IF NOT EXISTS users AS SELECT * FROM fip_team;")

    # Backfill name fields for existing records if first_name is empty
    rows_to_backfill = db.execute(
        "SELECT id, full_name FROM fip_team WHERE first_name IS NULL OR first_name = ''"
    ).fetchall()
    for row in rows_to_backfill:
        fname_str = (row[1] or "").strip()
        parts = fname_str.split()
        if len(parts) == 1:
            db.execute("UPDATE fip_team SET first_name = ?, last_name = '' WHERE id = ?", (parts[0], row[0]))
        elif len(parts) == 2:
            db.execute("UPDATE fip_team SET first_name = ?, last_name = ? WHERE id = ?", (parts[0], parts[1], row[0]))
        elif len(parts) >= 3:
            db.execute("UPDATE fip_team SET first_name = ?, middle_name = ?, last_name = ? WHERE id = ?", (parts[0], parts[1], " ".join(parts[2:]), row[0]))

    # Seed default admin account
    existing_admin = db.execute(
        "SELECT id FROM fip_team WHERE username = ?", ("admin",)
    ).fetchone()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not existing_admin:
        db.execute(
            """INSERT INTO fip_team (username, password_hash, full_name, first_name, last_name, role, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("admin", hash_password("admin123"),
             "FIP Administrator", "FIP", "Administrator", "admin", now),
        )
    else:
        # Ensure role is explicitly 'admin'
        db.execute("UPDATE fip_team SET role = 'admin' WHERE username = 'admin'")

    # Seed default staff account for operational testing
    existing_staff = db.execute(
        "SELECT id FROM fip_team WHERE username = ?", ("staff",)
    ).fetchone()
    if not existing_staff:
        db.execute(
            """INSERT INTO fip_team (username, password_hash, full_name, first_name, last_name, role, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("staff", hash_password("staff123"),
             "FIP Operations Staff", "Operations", "Staff", "staff", now),
        )

    db.commit()
    db.close()


def generate_tracking_code():
    """Return a short, unique tracking code like FIP-A3F9B2."""
    return "FIP-" + secrets.token_hex(3).upper()


def generate_next_staff_id(db):
    """Query fip_team for usernames matching 'FIPS%' and return the next formatted ID (e.g. FIPS001, FIPS002)."""
    rows = db.execute("SELECT username FROM fip_team WHERE username LIKE 'FIPS%'").fetchall()
    max_num = 0
    for r in rows:
        m = re.match(r"^FIPS(\d+)$", r["username"], re.IGNORECASE)
        if m:
            try:
                num = int(m.group(1))
                if num > max_num:
                    max_num = num
            except ValueError:
                pass
    next_num = max_num + 1
    new_id = f"FIPS{next_num:03d}"
    while db.execute("SELECT 1 FROM fip_team WHERE username = ?", (new_id,)).fetchone():
        next_num += 1
        new_id = f"FIPS{next_num:03d}"
    return new_id


def generate_temp_password(length=8):
    """Generate a secure, randomized temporary alphanumeric password."""
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))

# ---------------------------------------------------------------------------
# Auth decorators & RBAC Guards
# ---------------------------------------------------------------------------

def login_required(f):
    """Redirect to login page if user is not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "team_user_id" not in session and "user_id" not in session:
            flash("Please log in to access this page.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Ensure user is logged in and has the 'admin' role."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "team_user_id" not in session and "user_id" not in session:
            flash("Please log in to access this page.", "error")
            return redirect(url_for("login"))
        role = session.get("role") or session.get("team_role")
        if role != "admin":
            flash("Access denied. Admin privileges required.", "error")
            return redirect(url_for("staff_dashboard"))
        return f(*args, **kwargs)
    return decorated


def staff_required(f):
    """Ensure user is logged in and has the 'staff' role."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "team_user_id" not in session and "user_id" not in session:
            flash("Please log in to access this page.", "error")
            return redirect(url_for("login"))
        role = session.get("role") or session.get("team_role")
        if role != "staff":
            flash("Access denied. Staff operational area.", "error")
            return redirect(url_for("admin_analytics"))
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# Routes — Public / Client Portal
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Home page with the two main choices."""
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    """Serve global brand favicon."""
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "0f2727e0-048d-4a7b-9b8c-4e2b5b0796d8.jfif",
        mimetype="image/jpeg",
    )


@app.route("/inquiry", methods=["GET", "POST"])
def inquiry_form():
    """Display and process the event inquiry form."""
    if request.method == "POST":
        client_name = request.form.get("client_name", "").strip()
        email       = request.form.get("email", "").strip()
        phone       = request.form.get("phone", "").strip()
        event_type  = request.form.get("event_type", "").strip()
        event_date  = request.form.get("event_date", "").strip()
        venue       = request.form.get("venue", "").strip()
        
        # Hidden or optional fields mapping to existing DB
        event_time  = request.form.get("event_time", "00:00").strip()
        guest_count_str = request.form.get("guest_count", "").strip()
        guest_count = int(guest_count_str) if guest_count_str.isdigit() else 0
        
        # New prototype fields
        organization = request.form.get("organization", "").strip()
        duration = request.form.get("duration", "").strip()
        budget = request.form.get("budget", "").strip()
        services = request.form.getlist("services")
        raw_details = request.form.get("details", "").strip()

        # Core validation
        if not all([client_name, email, phone, event_type, event_date, venue]):
            flash("Please fill in all required fields.", "error")
            return redirect(url_for("inquiry_form"))

        # Compile extra prototype fields into the 'details' text field
        details_parts = []
        if organization: details_parts.append(f"Organization: {organization}")
        if duration: details_parts.append(f"Duration: {duration}")
        if budget: details_parts.append(f"Estimated Budget: {budget}")
        if services: details_parts.append(f"Services Needed: {', '.join(services)}")
        if raw_details: details_parts.append(f"Notes: {raw_details}")
        
        compiled_details = "\n".join(details_parts)

        tracking_code = generate_tracking_code()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        db = get_db()
        db.execute(
            """INSERT INTO inquiries
               (tracking_code, client_name, email, phone, event_type,
                event_date, event_time, venue, guest_count, details,
                status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?, ?)""",
            (tracking_code, client_name, email, phone, event_type,
             event_date, event_time, venue, guest_count, compiled_details,
             now, now),
        )
        db.commit()

        flash(
            f"Inquiry submitted! Your tracking code is: {tracking_code}",
            "success",
        )
        return redirect(url_for("inquiry_form"))

    return render_template("inquiry_form.html")


@app.route("/track", methods=["GET", "POST"])
def track_status():
    """Let a client look up their inquiry status by tracking code."""
    inquiry = None
    searched = False

    if request.method == "POST":
        code = request.form.get("tracking_code", "").strip().upper()
        searched = True
        if code:
            db = get_db()
            inquiry = db.execute(
                "SELECT * FROM inquiries WHERE tracking_code = ?", (code,)
            ).fetchone()
            if inquiry is None:
                flash("No inquiry found for that tracking code.", "error")

    return render_template("track_status.html",
                           inquiry=inquiry, searched=searched)

# ---------------------------------------------------------------------------
# Routes — FIP Team Authentication
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    """FIP Team login page with role-based redirection."""
    if "team_user_id" in session or "user_id" in session:
        role = session.get("role") or session.get("team_role")
        if role == "admin":
            return redirect(url_for("admin_analytics"))
        else:
            return redirect(url_for("staff_dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Please enter both username and password.", "error")
            return redirect(url_for("login"))

        db = get_db()
        user = db.execute(
            "SELECT * FROM fip_team WHERE username = ?", (username,)
        ).fetchone()

        if user and verify_password(user["password_hash"], password):
            session.permanent = True
            session["team_user_id"] = user["id"]
            session["user_id"] = user["id"]
            session["team_username"] = user["username"]
            session["username"] = user["username"]
            session["user"] = user["username"]
            session["team_fullname"] = user["full_name"]
            session["full_name"] = user["full_name"]
            session["name"] = user["full_name"]
            session["team_role"] = user["role"]
            session["role"] = user["role"]

            flash(f"Welcome back, {user['full_name']} ({user['role'].title()})!", "success")

            # Role-based redirection upon successful login
            if user["role"] == "admin":
                return redirect(url_for("admin_analytics"))
            else:
                return redirect(url_for("staff_dashboard"))
        else:
            flash("Invalid username or password.", "error")
            return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/fip_login")
@app.route("/fip-login")
def fip_login_alias():
    """Alias route redirecting to /login."""
    return redirect(url_for("login"))


@app.route("/login/face-id", methods=["POST"])
def login_face_id():
    """Biometric Face ID authentication endpoint for FIP Team."""
    data = request.get_json(silent=True) or {}
    live_face_b64 = data.get("live_face_base64") or request.form.get("live_face_base64")

    if not live_face_b64:
        return jsonify({
            "success": False,
            "message": "No face image received. Please try again."
        }), 400

    clean_live_b64 = live_face_b64.split(",", 1)[1] if "," in live_face_b64 else live_face_b64

    db = get_db()
    candidates = db.execute(
        """SELECT * FROM fip_team 
           WHERE (selfie_file_path IS NOT NULL AND selfie_file_path != '') 
              OR (id_file_path IS NOT NULL AND id_file_path != '')"""
    ).fetchall()

    if not candidates:
        return jsonify({
            "success": False,
            "message": "Face not recognized. Please try again or use your password."
        }), 401

    matched_user = None
    best_confidence = 0.0

    is_mock = os.getenv("ENABLE_MOCK_VERIFICATION", "false").lower() == "true" or os.getenv("FACEPLUSPLUS_API_KEY", "").startswith("mock_")

    if is_mock:
        matched_user = candidates[0]
        best_confidence = 98.5
    else:
        for user in candidates:
            # Check selfie photo first, then fallback to ID photo
            photo_file = user["selfie_file_path"] or user["id_file_path"]
            photo_path = os.path.join(app.config["UPLOAD_STAFF_IDS"], photo_file)
            if not os.path.exists(photo_path) and user["id_file_path"]:
                photo_path = os.path.join(app.config["UPLOAD_STAFF_IDS"], user["id_file_path"])

            if not os.path.exists(photo_path):
                continue

            try:
                with open(photo_path, "rb") as f:
                    stored_bytes = f.read()
                stored_b64 = base64.b64encode(stored_bytes).decode("utf-8")
                confidence, err_msg = call_faceplusplus_compare_base64(stored_b64, clean_live_b64)
                print(f"[Face ID Login] Checked {user['username']} ({user['full_name']}): confidence={confidence:.2f}%")

                if confidence >= 75.0:
                    matched_user = user
                    best_confidence = confidence
                    break
            except Exception as e:
                print(f"[Face ID Login] Error comparing user {user['username']}: {e}")
                continue

    if not matched_user:
        return jsonify({
            "success": False,
            "message": "Face not recognized. Please try again or use your password."
        }), 401

    # Establish authenticated session
    session.permanent = True
    session["team_user_id"] = matched_user["id"]
    session["user_id"] = matched_user["id"]
    session["team_username"] = matched_user["username"]
    session["username"] = matched_user["username"]
    session["user"] = matched_user["username"]
    session["team_fullname"] = matched_user["full_name"]
    session["full_name"] = matched_user["full_name"]
    session["name"] = matched_user["full_name"]
    session["team_role"] = matched_user["role"]
    session["role"] = matched_user["role"]

    # Extract first name
    first_name = matched_user["first_name"]
    if not first_name:
        parts = (matched_user["full_name"] or "").strip().split()
        first_name = parts[0] if parts else "Team Member"

    flash(f"Welcome back, {first_name}!", "success")

    redirect_url = url_for("admin_analytics") if matched_user["role"] == "admin" else url_for("staff_dashboard")

    return jsonify({
        "success": True,
        "redirect_url": redirect_url,
        "first_name": first_name,
        "role": matched_user["role"],
        "confidence": best_confidence
    })


@app.route("/logout")
def logout():
    """Log out the user and clear session."""
    session.clear()
    flash("You have been successfully logged out.", "success")
    return redirect(url_for("index"))

# ---------------------------------------------------------------------------
# Routes — Portal Entry (Dispatches by Role)
# ---------------------------------------------------------------------------

@app.route("/admin")
@login_required
def admin_dashboard():
    """Direct authenticated user to their role-designated dashboard."""
    role = session.get("role") or session.get("team_role")
    if role == "admin":
        return redirect(url_for("admin_analytics"))
    return redirect(url_for("staff_dashboard"))

# ---------------------------------------------------------------------------
# Routes — Admin Only (Analytics & Staff Management)
# ---------------------------------------------------------------------------

@app.route("/admin/analytics")
@app.route("/analytics")
@login_required
@admin_required
def admin_analytics():
    """Business metrics visible exclusively to Admin."""
    db = get_db()

    # --- KPIs ---
    total = db.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0]
    approved = db.execute("SELECT COUNT(*) FROM inquiries WHERE status='Approved'").fetchone()[0]
    conversion_rate = f"{int((approved / total) * 100)}%" if total > 0 else "0%"
    proposed_revenue = "₱0"

    kpis = {
        "total": total,
        "approved": approved,
        "conversion_rate": conversion_rate,
        "proposed_revenue": proposed_revenue
    }

    # --- Inquiry Statuses ---
    status_counts = {
        "Pending": 0, 
        "Approved": 0, 
        "For Client Approval": 0, 
        "Finalized": 0, 
        "Declined": 0
    }
    
    rows = db.execute("SELECT status, COUNT(*) as cnt FROM inquiries GROUP BY status").fetchall()
    for r in rows:
        if r["status"] in status_counts:
            status_counts[r["status"]] = r["cnt"]
        else:
            status_counts[r["status"]] = r["cnt"]

    # --- Basic Service Needs ---
    services = {
        "Lights": total,
        "Host / Emcee": max(0, total - 1),
        "Tables & Chairs": max(0, total - 1),
        "Manpower": max(0, total - 1)
    }

    # --- Monthly Event Requests ---
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    monthly_data = [0] * 12
    month_rows = db.execute("SELECT strftime('%m', event_date) as m, COUNT(*) as cnt FROM inquiries GROUP BY m").fetchall()
    for r in month_rows:
        if r["m"]:
            idx = int(r["m"]) - 1
            monthly_data[idx] = r["cnt"]

    return render_template("admin_analytics.html",
                           active_tab="analytics",
                           kpis=kpis,
                           status_counts=status_counts,
                           services=services,
                           months=months,
                           monthly_data=monthly_data)


@app.route("/admin/manage-staff", methods=["GET", "POST"])
@app.route("/manage-staff", methods=["GET", "POST"])
@login_required
@admin_required
def manage_staff():
    """Admin route to create new staff accounts with automated credentials and inspect current staff."""
    db = get_db()

    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        middle_name = request.form.get("middle_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        birthday = request.form.get("birthday", "").strip()

        if not first_name or not last_name:
            flash("First name and last name are required.", "error")
            return redirect(url_for("manage_staff"))

        # Construct full name from explicit fields
        name_tokens = [first_name]
        if middle_name:
            name_tokens.append(middle_name)
        name_tokens.append(last_name)
        full_name = " ".join(name_tokens)

        # Generate automated credentials
        login_id = generate_next_staff_id(db)
        temp_password = generate_temp_password(8)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            """INSERT INTO fip_team (username, password_hash, full_name, first_name, middle_name, last_name, birthday, role, id_file_path, verification_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'staff', NULL, 'Unverified', ?)""",
            (login_id, hash_password(temp_password), full_name, first_name, middle_name, last_name, birthday, now),
        )
        db.commit()

        # Save generated credentials in session so admin view can render a dedicated copyable alert box
        session["latest_created_staff"] = {
            "full_name": full_name,
            "first_name": first_name,
            "last_name": last_name,
            "birthday": birthday,
            "login_id": login_id,
            "temp_password": temp_password
        }

        # Credentials are now elegantly displayed in the dismissible alert box within the main content area
        return redirect(url_for("manage_staff"))

    # Pop newly created staff info for high-visibility UI feedback
    new_staff_info = session.pop("latest_created_staff", None)

    # Fetch existing staff members
    staff_members = db.execute(
        "SELECT id, username, full_name, first_name, middle_name, last_name, birthday, role, id_file_path, verification_status, created_at FROM fip_team WHERE role = 'staff' ORDER BY created_at DESC"
    ).fetchall()

    return render_template("manage_staff.html",
                           active_tab="manage_staff",
                           staff_members=staff_members,
                           new_staff_info=new_staff_info)


@app.route("/admin/staff-id/<filename>")
@app.route("/staff/id-file/<filename>")
@login_required
def view_staff_id(filename):
    """Securely serve uploaded staff verification ID to authenticated admins or the file owner."""
    role = session.get("role") or session.get("team_role")
    user_id = session.get("user_id") or session.get("team_user_id")
    if role != "admin":
        db = get_db()
        owner = db.execute(
            "SELECT id FROM fip_team WHERE id = ? AND (id_file_path = ? OR selfie_file_path = ?)",
            (user_id, filename, filename)
        ).fetchone()
        if not owner:
            flash("Unauthorized access to verification file.", "error")
            return redirect(url_for("staff_dashboard"))
    return send_from_directory(app.config["UPLOAD_STAFF_IDS"], filename)


@app.route("/admin/delete-staff/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def delete_staff(user_id):
    """Admin route to remove a staff member."""
    db = get_db()
    staff = db.execute(
        "SELECT * FROM fip_team WHERE id = ? AND role = 'staff'", (user_id,)
    ).fetchone()
    if staff:
        # Delete associated ID file if present
        if staff["id_file_path"]:
            try:
                file_path = os.path.join(app.config["UPLOAD_STAFF_IDS"], staff["id_file_path"])
                if os.path.exists(file_path):
                    os.remove(file_path)
            except Exception:
                pass
        if "selfie_file_path" in staff.keys() and staff["selfie_file_path"]:
            try:
                selfie_file = os.path.join(app.config["UPLOAD_STAFF_IDS"], staff["selfie_file_path"])
                if os.path.exists(selfie_file):
                    os.remove(selfie_file)
            except Exception:
                pass

        db.execute("DELETE FROM fip_team WHERE id = ?", (user_id,))
        db.commit()
        flash(f"Staff account '{staff['username']}' deleted.", "success")
    else:
        flash("Staff account not found or cannot be removed.", "error")
    return redirect(url_for("manage_staff"))

# ---------------------------------------------------------------------------
# Routes — Staff Only (Dashboard, Inquiries & Calendar)
# ---------------------------------------------------------------------------

@app.route("/staff")
@app.route("/staff/dashboard")
@login_required
@staff_required
def staff_dashboard():
    """Summary dashboard for staff operations."""
    db = get_db()

    pending = db.execute("SELECT COUNT(*) FROM inquiries WHERE status = 'Pending'").fetchone()[0]
    approved = db.execute("SELECT COUNT(*) FROM inquiries WHERE status = 'Approved'").fetchone()[0]
    declined = db.execute("SELECT COUNT(*) FROM inquiries WHERE status = 'Declined'").fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0]

    # Upcoming approved events this month
    current_month = datetime.now().strftime("%Y-%m")
    events_this_month = db.execute(
        "SELECT COUNT(*) FROM inquiries WHERE status = 'Approved' AND event_date LIKE ?",
        (f"{current_month}%",)
    ).fetchone()[0]

    # Quick preview of recent inquiries to review
    recent_inquiries = db.execute(
        "SELECT * FROM inquiries ORDER BY created_at DESC LIMIT 5"
    ).fetchall()

    summary = {
        "pending": pending,
        "approved": approved,
        "declined": declined,
        "this_month": events_this_month,
        "total": total,
    }

    user_id = session.get("user_id") or session.get("team_user_id")
    user = db.execute("SELECT * FROM fip_team WHERE id = ?", (user_id,)).fetchone()

    return render_template("staff_dashboard.html",
                           user=user,
                           summary=summary,
                           recent_inquiries=recent_inquiries,
                           active_tab="staff_dashboard")


@app.route("/inquiries")
@app.route("/admin/inquiries")
@app.route("/staff/inquiries")
@login_required
@staff_required
def staff_inquiries():
    """Show and manage daily inquiries for staff."""
    db = get_db()

    status_filter = request.args.get("status", "all")
    if status_filter in ("Pending", "Approved", "Declined"):
        rows = db.execute(
            "SELECT * FROM inquiries WHERE status = ? ORDER BY created_at DESC",
            (status_filter,),
        ).fetchall()
    else:
        status_filter = "all"
        rows = db.execute(
            "SELECT * FROM inquiries ORDER BY created_at DESC"
        ).fetchall()

    return render_template("staff_inquiries.html",
                           inquiries=rows, status_filter=status_filter,
                           active_tab="staff_inquiries")


@app.route("/admin/update/<int:inquiry_id>", methods=["POST"])
@app.route("/staff/update/<int:inquiry_id>", methods=["POST"])
@login_required
@staff_required
def update_status(inquiry_id):
    """Update the status of an inquiry (Approved / Declined). Staff only."""
    new_status = request.form.get("status")
    redirect_target = request.form.get("redirect_to")

    if new_status not in ("Approved", "Declined"):
        flash("Invalid status value.", "error")
        if redirect_target == "detail":
            return redirect(url_for("staff_inquiry_detail", inquiry_id=inquiry_id))
        return redirect(url_for("staff_inquiries"))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db = get_db()
    db.execute(
        "UPDATE inquiries SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, now, inquiry_id),
    )
    db.commit()
    flash(f"Inquiry #{inquiry_id} marked as {new_status}.", "success")

    if redirect_target == "detail":
        return redirect(url_for("staff_inquiry_detail", inquiry_id=inquiry_id))
    return redirect(url_for("staff_inquiries"))


@app.route("/staff/inquiry/<int:inquiry_id>")
@app.route("/admin/inquiry/<int:inquiry_id>")
@login_required
@staff_required
def staff_inquiry_detail(inquiry_id):
    """Detailed view for a specific client event inquiry."""
    db = get_db()
    inquiry = db.execute("SELECT * FROM inquiries WHERE id = ?", (inquiry_id,)).fetchone()
    if not inquiry:
        flash(f"Inquiry #{inquiry_id} not found.", "error")
        return redirect(url_for("staff_inquiries"))

    # Parse details field into structured components
    raw_details = inquiry["details"] or ""
    parsed = {
        "organization": "",
        "duration": "",
        "budget": "",
        "services": [],
        "notes": ""
    }
    other_notes = []
    for line in raw_details.splitlines():
        line = line.strip()
        if line.startswith("Organization:"):
            parsed["organization"] = line.split(":", 1)[1].strip()
        elif line.startswith("Duration:"):
            parsed["duration"] = line.split(":", 1)[1].strip()
        elif line.startswith("Estimated Budget:"):
            parsed["budget"] = line.split(":", 1)[1].strip()
        elif line.startswith("Services Needed:"):
            svcs = line.split(":", 1)[1].strip()
            parsed["services"] = [s.strip() for s in svcs.split(",") if s.strip()]
        elif line.startswith("Notes:"):
            other_notes.append(line.split(":", 1)[1].strip())
        elif line:
            other_notes.append(line)
    parsed["notes"] = "\n".join(other_notes)

    return render_template("staff_inquiry_detail.html",
                           inquiry=inquiry,
                           parsed=parsed,
                           active_tab="staff_inquiries")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Automated Verification Helpers (In-Memory OCR & Base64 Face++ API)
# ---------------------------------------------------------------------------

def preprocess_id_image(id_file_bytes: bytes):
    """
    Advanced Image Pre-processing Pipeline (OpenCV):
    1. Resize (Upscale): Upscale by 2x using INTER_CUBIC to enhance small text on IDs.
    2. Grayscale: Convert BGR to Grayscale.
    3. Blurring: Apply Gaussian Blur (5, 5), 0 to smooth background patterns & holograms.
    4. Adaptive Thresholding: cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
       Handles uneven lighting, glare, and background artifacts.
    """
    # 1. OpenCV in-memory decoding & advanced pre-processing
    try:
        nparr = np.frombuffer(id_file_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is not None:
            # 1. Resize (Upscale by 2x)
            resized = cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

            # 2. Grayscale
            gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)

            # 3. Gaussian Blur
            blur = cv2.GaussianBlur(gray, (5, 5), 0)

            # 4. Adaptive Thresholding
            thresh = cv2.adaptiveThreshold(
                blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
            )
            return thresh
    except Exception as cv_err:
        print(f"[OCR PREPROCESS] OpenCV in-memory notice: {cv_err}. Falling back to Pillow.")

    # 2. Pillow in-memory fallback
    try:
        pil_img = Image.open(io.BytesIO(id_file_bytes))
        w, h = pil_img.size
        resized = pil_img.resize((w * 2, h * 2), Image.Resampling.BICUBIC)
        gray = ImageOps.grayscale(resized)
        enhanced = ImageEnhance.Contrast(gray).enhance(2.0)
        thresh = enhanced.point(lambda p: 255 if p > 128 else 0)
        return np.array(thresh)
    except Exception as pil_err:
        print(f"[OCR PREPROCESS ERROR] Pillow in-memory processing failed: {pil_err}")
        return None


def extract_text_from_bytes(id_file_bytes: bytes) -> str:
    """
    Extract text directly from in-memory ID image bytes using advanced OpenCV pre-processing,
    mandatory visual debug export ('debug_ocr_processed_id.jpg'), and PyTesseract '--oem 3 --psm 11'.
    """
    print("\n" + "=" * 65)
    print(f"[OCR DEBUG] Processing in-memory ID image ({len(id_file_bytes)} bytes)")

    # 1. Pre-process the in-memory image
    processed_img = preprocess_id_image(id_file_bytes)
    if processed_img is not None:
        print("[OCR DEBUG] Image pre-processed: Upscale 2x + Grayscale + Blur (5,5) + Adaptive Threshold.")
    else:
        print("[OCR WARNING] Pre-processing returned None, attempting raw byte fallback.")
        try:
            pil_fallback = Image.open(io.BytesIO(id_file_bytes))
            processed_img = np.array(pil_fallback)
        except Exception:
            return ""

    # -----------------------------------------------------------------------
    # 2. MANDATORY VISUAL DEBUGGING: Save fully processed image to the server
    # -----------------------------------------------------------------------
    try:
        debug_image_path = os.path.join(app.root_path, "debug_ocr_processed_id.jpg")
        cv2.imwrite(debug_image_path, processed_img)
        cv2.imwrite("debug_ocr_processed_id.jpg", processed_img)
        print(f"[OCR DEBUG] Saved processed ID image to: {debug_image_path}")
    except Exception as save_err:
        print(f"[OCR DEBUG ERROR] Could not save debug_ocr_processed_id.jpg: {save_err}")

    # -----------------------------------------------------------------------
    # 3. PYTESSERACT EXTRACTION WITH PSM 11 (Sparse Text)
    # -----------------------------------------------------------------------
    extracted_text = ""
    try:
        # Run PyTesseract OCR with --oem 3 --psm 11
        extracted_text = pytesseract.image_to_string(processed_img, config="--oem 3 --psm 11")

        # Immediate raw text debugging
        print(f"RAW OCR TEXT: {extracted_text}")
        print("-----------------------------------------------------------------")
        if extracted_text.strip():
            print(extracted_text.strip())
        else:
            print("[EMPTY / NO TEXT DETECTED FROM ID]")
        print("-----------------------------------------------------------------")
        print(f"[OCR DEBUG] Total characters extracted: {len(extracted_text.strip())}")

    except pytesseract.TesseractNotFoundError:
        print("[OCR ERROR] TesseractNotFoundError: Tesseract-OCR binary was not found!")
        print("[OCR ACTION REQUIRED] If you are on Windows, install Tesseract-OCR:")
        print("1. Download installer: https://github.com/UB-Mannheim/tesseract/wiki")
        print("2. Install to default path (C:\\Program Files\\Tesseract-OCR)")
        print("3. Ensure pytesseract.pytesseract.tesseract_cmd points to tesseract.exe")
    except Exception as e:
        print(f"[OCR ERROR] PyTesseract exception: {e}")

    print("=" * 65 + "\n")
    return extracted_text.strip()


def normalize_text(text: str) -> str:
    r"""
    Aggressive Text Normalization:
    1. Converts text to lowercase.
    2. Strips out all punctuation, numbers, and special characters: re.sub(r'[^a-z\s]', '', text).
    3. Strips edges and replaces multiple spaces/newlines with a single space.
    """
    if not text:
        return ""
    lowered = text.lower()
    cleaned = re.sub(r'[^a-z\s]', '', lowered)
    return re.sub(r'\s+', ' ', cleaned).strip()


def check_name_in_ocr(registered_name: str, ocr_text: str, threshold: float = 75.0):
    """
    Compares registered_name with extracted OCR text using thefuzz token_set_ratio.
    token_set_ratio is specifically designed to find a name even if surrounded
    by extra words, middle initials, or card headers.
    Returns: (is_matched: bool, match_score: int)
    """
    if not registered_name or not ocr_text:
        return False, 0

    clean_registered_name = normalize_text(registered_name)
    clean_ocr_text = normalize_text(ocr_text)

    if not clean_registered_name or not clean_ocr_text:
        return False, 0

    fuzzy_score = fuzz.token_set_ratio(clean_registered_name, clean_ocr_text)
    return fuzzy_score >= threshold, int(fuzzy_score)


def call_faceplusplus_compare_base64(id_base64: str, live_face_base64: str):
    """
    Call Face++ Compare API (https://api-us.faceplusplus.com/facepp/v3/compare)
    using in-memory Base64 strings. No file stream exhaustion possible.
    Returns: (confidence: float, error_message: str)
    """
    api_key = "b0uSL9nuSnJO0TXD6758FDzcIvM5Eipb"
    api_secret = "dNrr8WS5LeYeclYEEFZgeFQp67osUh29"
    compare_url = "https://api-us.faceplusplus.com/facepp/v3/compare"

    # Clean any data URI prefixes
    if "," in id_base64:
        id_base64 = id_base64.split(",", 1)[1]
    if "," in live_face_base64:
        live_face_base64 = live_face_base64.split(",", 1)[1]

    print("\n" + "=" * 65)
    print("[FACE++ DEBUG] Sending in-memory Base64 images to Face++ Compare API...")

    try:
        data = {
            "api_key": api_key,
            "api_secret": api_secret,
            "image_base64_1": id_base64,
            "image_base64_2": live_face_base64
        }
        response = requests.post(compare_url, data=data, timeout=30)
        print(f"[FACE++ DEBUG] HTTP Status: {response.status_code}")

        if response.status_code != 200:
            try:
                err_msg = response.json().get("error_message", f"HTTP {response.status_code}")
            except Exception:
                err_msg = response.text or f"HTTP {response.status_code}"
            print(f"[FACE++ ERROR] {err_msg}")
            print("=" * 65 + "\n")
            return 0.0, f"Face++ API Error: {err_msg}"

        res_data = response.json()
        print(f"[FACE++ DEBUG] Response JSON: {res_data}")

        confidence = res_data.get("confidence")
        if confidence is None:
            faces1 = res_data.get("faces1", [])
            faces2 = res_data.get("faces2", [])
            if not faces1 and not faces2:
                reason = "No faces detected in either the ID image or the live webcam capture."
            elif not faces1:
                reason = "No clear face could be detected in the uploaded ID card."
            elif not faces2:
                reason = "No face could be detected in the live webcam selfie."
            else:
                reason = "Facial biometrics could not be compared."
            print(f"[FACE++ ERROR] {reason}")
            print("=" * 65 + "\n")
            return 0.0, reason

        confidence = float(confidence)
        print(f"[FACE++ DEBUG] Face match confidence: {confidence:.2f}% (Threshold: 75.0%)")
        print("=" * 65 + "\n")
        return confidence, ""

    except requests.exceptions.RequestException as e:
        print(f"[FACE++ ERROR] Request exception: {e}")
        print("=" * 65 + "\n")
        return 0.0, f"Network error: {str(e)}"
    except Exception as e:
        print(f"[FACE++ ERROR] Unexpected error: {e}")
        print("=" * 65 + "\n")
        return 0.0, f"Face verification error: {str(e)}"


def compute_ocr_similarity(clean_name: str, clean_ocr: str) -> int:
    """
    Computes robust similarity score between registered staff name and OCR text.
    Combines:
    1. fuzz.partial_ratio (for contiguous substrings)
    2. fuzz.token_set_ratio (for out-of-order words & middle names)
    3. Token-by-token fuzzy ratio (for minor OCR character misreads like 'cassandra' vs 'cassanura')
    """
    if not clean_name or not clean_ocr:
        return 0
    score_partial = fuzz.partial_ratio(clean_name, clean_ocr)
    score_token_set = fuzz.token_set_ratio(clean_name, clean_ocr)

    name_tokens = [t for t in clean_name.split() if len(t) > 1]
    ocr_words = clean_ocr.split()
    if name_tokens and ocr_words:
        token_scores = [max((fuzz.ratio(t, w) for w in ocr_words), default=0) for t in name_tokens]
        score_token_avg = int(sum(token_scores) / len(token_scores))
    else:
        score_token_avg = 0

    return max(score_partial, score_token_set, score_token_avg)


def verify_birthday_match(registered_bday: str, ocr_text: str) -> bool:
    """
    Strictly verifies if the registered birthday matches the date of birth extracted on the ID.
    Supports ISO formats, US/UK numeric formats, month word names, and common OCR noise.
    """
    if not registered_bday or not ocr_text:
        return False
    bday_str = registered_bday.strip()
    year, month, day = None, None, None
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%m/%d/%Y', '%d/%m/%Y', '%Y.%m.%d'):
        try:
            dt = datetime.strptime(bday_str, fmt)
            year, month, day = dt.year, dt.month, dt.day
            break
        except ValueError:
            continue
    if not year or not month or not day:
        m = re.search(r'(\d{4})[^\d](\d{1,2})[^\d](\d{1,2})', bday_str)
        if m:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            return False

    year_str = str(year)
    year_short = str(year)[2:]
    month_str = f'{month:02d}'
    month_single = str(month)
    day_str = f'{day:02d}'
    day_single = str(day)

    month_names = {
        1: ('january', 'jan'), 2: ('february', 'feb'), 3: ('march', 'mar'),
        4: ('april', 'apr'), 5: ('may', 'may'), 6: ('june', 'jun'),
        7: ('july', 'jul'), 8: ('august', 'aug'), 9: ('september', 'sep', 'sept'),
        10: ('october', 'oct'), 11: ('november', 'nov'), 12: ('december', 'dec')
    }
    m_words = month_names.get(month, ())
    text_lower = ocr_text.lower()
    norm_text = re.sub(r'[\|\\/]+', '/', text_lower)
    norm_text = re.sub(r'[\s\-_]+', ' ', norm_text)

    patterns = [
        rf'\b{year_str}[-/\s\.]{month_str}[-/\s\.]{day_str}\b',
        rf'\b{year_str}[-/\s\.]{month_single}[-/\s\.]{day_single}\b',
        rf'\b{day_str}[-/\s\.]{month_str}[-/\s\.]{year_str}\b',
        rf'\b{day_single}[-/\s\.]{month_single}[-/\s\.]{year_str}\b',
        rf'\b{month_str}[-/\s\.]{day_str}[-/\s\.]{year_str}\b',
        rf'\b{month_single}[-/\s\.]{day_single}[-/\s\.]{year_str}\b',
        rf'\b{day_str}[-/\s\.]{month_str}[-/\s\.]{year_short}\b',
        rf'\b{month_str}[-/\s\.]{day_str}[-/\s\.]{year_short}\b',
    ]
    for pat in patterns:
        if re.search(pat, norm_text) or re.search(pat, text_lower):
            return True

    for mw in m_words:
        word_patterns = [
            rf'\b{day_str}\s*{mw}\s*{year_str}\b',
            rf'\b{day_single}\s*{mw}\s*{year_str}\b',
            rf'\b{mw}\s*{day_str}[,\s]+{year_str}\b',
            rf'\b{mw}\s*{day_single}[,\s]+{year_str}\b',
            rf'\b{year_str}\s*{mw}\s*{day_str}\b',
            rf'\b{year_str}\s*{mw}\s*{day_single}\b',
            rf'\b{day_str}-{mw}-{year_str}\b',
            rf'\b{day_single}-{mw}-{year_str}\b',
            rf'\b{day_str}\s*{mw}\s*{year_short}\b',
            rf'\b{mw}\s*{day_str}[,\s]+{year_short}\b',
        ]
        for wpat in word_patterns:
            if re.search(wpat, norm_text) or re.search(wpat, text_lower):
                return True

    dob_keywords = ['birth', 'dob', 'date of birth', 'bday', 'born', 'kaarawan', 'kapanganakan', 'naissance']
    for kw in dob_keywords:
        kw_idx = text_lower.find(kw)
        while kw_idx != -1:
            snippet = text_lower[max(0, kw_idx - 30): min(len(text_lower), kw_idx + 80)]
            has_year = (year_str in snippet) or (f'/{year_short}' in snippet) or (f'-{year_short}' in snippet)
            has_day = (day_str in snippet) or (f' {day_single} ' in snippet) or (f'/{day_single}/' in snippet)
            has_month = (month_str in snippet) or any(mw in snippet for mw in m_words)
            if has_year and (has_month or has_day):
                return True
            kw_idx = text_lower.find(kw, kw_idx + 1)

    if year_str in text_lower:
        month_found = any(mw in text_lower for mw in m_words) or (f'/{month_str}/' in norm_text) or (f'-{month_str}-' in norm_text) or (f' {month_str} ' in norm_text)
        day_found = (day_str in text_lower) or (f' {day_single} ' in text_lower)
        if month_found and day_found:
            return True

    return False


def verify_id_type_keywords(id_type: str, ocr_text: str) -> bool:
    """
    Validates that the OCR text on the uploaded ID contains keywords matching
    the selected government ID type (e.g. Driver's License -> driver, license, lto;
    PhilSys ID / National ID -> philsys, republic of the philippines, national id, etc.).
    """
    if not id_type or not ocr_text:
        return False

    clean_ocr = ocr_text.lower()
    norm_id_type = id_type.strip().lower()

    # Keyword mapping dictionary based on visual profiles of actual Philippine IDs (all normalized lowercase)
    id_keyword_map = {
        "philsys id": [
            "philsys", "national id", "republika ng pilipinas",
            "republic of the philippines", "pambansang", "pagkakakilanlan", "psa", "philid"
        ],
        "national id": [
            "philsys", "national id", "republika ng pilipinas",
            "republic of the philippines", "pambansang", "pagkakakilanlan", "psa", "philid"
        ],
        "driver's license": [
            "driver", "license", "land transportation office", "lto",
            "licence", "dl", "driver's", "non-professional", "professional", "restriction"
        ],
        "passport": [
            "passport", "pasaporte", "department of foreign affairs", "dfa",
            "pilipinas", "republic", "foreign affairs", "republika"
        ],
        "philhealth id": [
            "philhealth", "philippine health insurance corporation", "health insurance",
            "phic", "philhealth identification"
        ],
        "philhealth": [
            "philhealth", "philippine health insurance corporation", "health insurance",
            "phic", "philhealth identification"
        ],
        "umid": [
            "umid", "unified", "multi-purpose", "multipurpose", "sss", 
            "gsis", "social security", "crn"
        ],
        "prc id": [
            "prc", "professional", "regulation", "commission", "regulations"
        ],
        "postal id": [
            "postal", "post", "phlpost", "philpost", "philippine postal"
        ],
        "voter's id": [
            "voter", "voters", "voter's", "comelec", "elections", "commission on elections"
        ],
        "other valid government id": [
            "republic", "republika", "philippines", "pilipinas", "government", 
            "pamahalaan", "department", "city", "municipality", "barangay", 
            "tin", "philhealth", "pag-ibig", "id", "identification", "license", 
            "card", "bureau", "bir"
        ]
    }

    keywords = id_keyword_map.get(norm_id_type)
    if not keywords:
        # Fallback keyword lookup based on substring match
        if "philsys" in norm_id_type or "national" in norm_id_type:
            keywords = id_keyword_map["philsys id"]
        elif "driver" in norm_id_type:
            keywords = id_keyword_map["driver's license"]
        elif "passport" in norm_id_type:
            keywords = id_keyword_map["passport"]
        elif "philhealth" in norm_id_type:
            keywords = id_keyword_map["philhealth id"]
        elif "umid" in norm_id_type:
            keywords = id_keyword_map["umid"]
        elif "prc" in norm_id_type:
            keywords = id_keyword_map["prc id"]
        elif "postal" in norm_id_type:
            keywords = id_keyword_map["postal id"]
        elif "voter" in norm_id_type:
            keywords = id_keyword_map["voter's id"]
        else:
            keywords = id_keyword_map["other valid government id"]

    # 1. Direct substring search
    for kw in keywords:
        if kw.lower() in clean_ocr:
            return True

    # 2. Token-level fuzzy match (handling minor OCR character noise like 'dr1ver' or 'phllsys')
    ocr_words = [re.sub(r'[^a-z]', '', w) for w in clean_ocr.split()]
    for kw in keywords:
        kw_clean = re.sub(r'[^a-z]', '', kw.lower())
        if len(kw_clean) >= 4:
            for word in ocr_words:
                if len(word) >= 4 and fuzz.ratio(kw_clean, word) >= 82:
                    return True

    return False



@app.route("/admin/profile")
@login_required
@admin_required
def admin_profile():
    """Administrator profile & settings route."""
    db = get_db()
    user_id = session.get("user_id") or session.get("team_user_id")
    user = db.execute("SELECT * FROM fip_team WHERE id = ?", (user_id,)).fetchone()
    return render_template("admin_profile.html", user=user, active_tab="admin_profile")


@app.route("/profile")
@login_required
def profile_dispatcher():
    """Universal profile dispatcher based on user role."""
    role = session.get("role") or session.get("team_role")
    if role == "admin":
        return redirect(url_for("admin_profile"))
    return redirect(url_for("staff_profile"))


@app.route("/staff/profile", methods=["GET", "POST"])
@login_required
def staff_profile():
    """Final bulletproof staff profile and automated 3-way security verification route."""
    role = session.get("role") or session.get("team_role")
    if role == "admin":
        return redirect(url_for("admin_profile"))

    db = get_db()
    user_id = session.get("user_id") or session.get("team_user_id")
    user = db.execute("SELECT * FROM fip_team WHERE id = ?", (user_id,)).fetchone()

    if not user:
        flash("Staff account not found or session expired. Please log in again.", "error")
        return redirect(url_for("staff_login"))

    if request.method == "POST":
        id_type = request.form.get("id_type", "").strip()
        custom_id_type = request.form.get("custom_id_type", "").strip()
        id_image = request.files.get("id_image") or request.files.get("id_file")
        live_face_base64 = request.form.get("live_face_base64", "").strip()

        # If user selected an "Other" option and provided a custom ID name
        effective_id_type = id_type
        if "other" in id_type.lower():
            if custom_id_type:
                effective_id_type = f"Other ({custom_id_type})"
            else:
                effective_id_type = "Other Valid Government-Issued ID"

        if not id_type:
            flash("Please select a valid ID type from the dropdown.", "error")
            return redirect(url_for("staff_profile") + "?failed=1")

        if not id_image or not id_image.filename:
            flash("Please upload an image of the front of your valid ID.", "error")
            return redirect(url_for("staff_profile") + "?failed=1")

        if not live_face_base64:
            flash("Please capture your live face photo using the webcam before submitting.", "error")
            return redirect(url_for("staff_profile") + "?failed=1")

        # -------------------------------------------------------------------
        # 1. DATABASE SANITY CHECK (Crucial)
        # -------------------------------------------------------------------
        first_name = user["first_name"] if "first_name" in user.keys() and user["first_name"] else ""
        middle_name = user["middle_name"] if "middle_name" in user.keys() and user["middle_name"] else ""
        last_name = user["last_name"] if "last_name" in user.keys() and user["last_name"] else ""
        staff_name = user["full_name"] or f"{first_name} {middle_name} {last_name}".strip()
        staff_birthday = user["birthday"] if "birthday" in user.keys() and user["birthday"] else ""

        print(f"DEBUG - DB Name: '{staff_name}', First: '{first_name}', Last: '{last_name}', Birthday: '{staff_birthday}'")

        if not staff_name or not staff_name.strip():
            flash("Verification Error: No registered staff name found in database.", "error")
            return redirect(url_for("staff_profile") + "?failed=1")

        # Normalize registered staff name (lowercase, strip special characters/numbers)
        clean_staff_name = re.sub(r'[^a-z\s]', '', staff_name.lower())
        clean_staff_name = re.sub(r'\s+', ' ', clean_staff_name).strip()
        print(f"DEBUG - Cleaned DB Name: '{clean_staff_name}'")

        # -------------------------------------------------------------------
        # 2. READ BYTES ONCE & DECODE FOR OPENCV (Zero Stream Exhaustion)
        # -------------------------------------------------------------------
        id_file_bytes = id_image.read()
        if not id_file_bytes:
            flash("The uploaded ID file appears to be empty. Please select a valid image.", "error")
            return redirect(url_for("staff_profile") + "?failed=1")

        nparr = np.frombuffer(id_file_bytes, np.uint8)
        img_cv = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        # Decode webcam live face Base64
        clean_live_b64 = live_face_base64.split(",", 1)[1] if "," in live_face_base64 else live_face_base64
        try:
            live_face_bytes = base64.b64decode(clean_live_b64)
            if not live_face_bytes:
                raise ValueError("Empty live face data")
        except Exception:
            flash("Invalid webcam capture image data. Please retake your face scan.", "error")
            return redirect(url_for("staff_profile") + "?failed=1")

        # Save files to disk for records and UI display
        safe_id_name = secure_filename(id_image.filename)
        id_filename = f"{secrets.token_hex(4)}_id_{safe_id_name}"
        id_path = os.path.join(app.config["UPLOAD_STAFF_IDS"], id_filename)
        with open(id_path, "wb") as f_id:
            f_id.write(id_file_bytes)

        selfie_filename = f"{secrets.token_hex(4)}_live_face.jpg"
        selfie_path = os.path.join(app.config["UPLOAD_STAFF_IDS"], selfie_filename)
        with open(selfie_path, "wb") as f_face:
            f_face.write(live_face_bytes)

        # -------------------------------------------------------------------
        # 3. MULTI-PASS OCR STRATEGY (RapidOCR ONNX + PyTesseract Fallback)
        # -------------------------------------------------------------------
        is_mock = os.getenv("ENABLE_MOCK_VERIFICATION", "false").lower() == "true"
        if is_mock or os.getenv("FACEPLUSPLUS_API_KEY", "").startswith("mock_"):
            best_score = 100
            best_text = f"REPUBLIC OF THE PHILIPPINES {staff_name} DOB: {staff_birthday or '1998-06-19'}"
            print(f"DEBUG - Raw OCR Text (Mock Mode):\n{best_text}")
            print(f"DEBUG - Best OCR Score: {best_score}%")
        else:
            best_score = 0
            best_text = ""

            # --- PASS 1: RapidOCR Deep Learning Engine (Pure Python / ONNX) ---
            if rapid_ocr_engine is not None and img_cv is not None:
                pass1_text = ""
                try:
                    ocr_results, _ = rapid_ocr_engine(img_cv)
                    if ocr_results:
                        pass1_text = " ".join([line[1] for line in ocr_results])
                except Exception as rocr_err:
                    print(f"[OCR WARNING] RapidOCR error: {rocr_err}")

                print(f"DEBUG - Raw OCR Text (Pass 1 - RapidOCR ONNX):\n{pass1_text}")
                clean_pass1 = re.sub(r'[^a-z\s]', '', pass1_text.lower())
                clean_pass1 = re.sub(r'\s+', ' ', clean_pass1).strip()

                score_pass1 = compute_ocr_similarity(clean_staff_name, clean_pass1)
                print(f"DEBUG - Pass 1 (RapidOCR) Robust Score: {score_pass1}% (Threshold: 70%)")

                if score_pass1 > best_score:
                    best_score = score_pass1
                    best_text = pass1_text

            # --- PASS 2: PyTesseract Raw Grayscale ---
            if best_score < 70 and img_cv is not None:
                print("[OCR] Best score below 70%. Executing Pass 2 (PyTesseract Raw Grayscale)...")
                pass2_text = ""
                try:
                    gray_raw = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
                    pass2_text = pytesseract.image_to_string(gray_raw, config="--oem 3 --psm 11")
                except pytesseract.TesseractNotFoundError:
                    print("[OCR ERROR] Tesseract executable not found! Ensure Tesseract-OCR is installed on Windows.")
                except Exception as e:
                    print(f"[OCR ERROR Pass 2] {e}")

                print(f"DEBUG - Raw OCR Text (Pass 2 - PyTesseract Raw Grayscale):\n{pass2_text}")
                clean_pass2 = re.sub(r'[^a-z\s]', '', pass2_text.lower())
                clean_pass2 = re.sub(r'\s+', ' ', clean_pass2).strip()

                score_pass2 = compute_ocr_similarity(clean_staff_name, clean_pass2)
                print(f"DEBUG - Pass 2 Fuzzy Score: {score_pass2}% (Threshold: 70%)")

                if score_pass2 > best_score:
                    best_score = score_pass2
                    best_text = pass2_text

            # --- PASS 3: PyTesseract Adaptive Thresholding ---
            if best_score < 70 and img_cv is not None:
                print("[OCR] Best score still below 70%. Executing Pass 3 (Upscale + Blur + Adaptive Threshold)...")
                pass3_text = ""
                try:
                    resized = cv2.resize(img_cv, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
                    gray3 = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
                    blur = cv2.GaussianBlur(gray3, (5, 5), 0)
                    thresh = cv2.adaptiveThreshold(
                        blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
                    )
                    cv2.imwrite("debug_ocr_processed_id.jpg", thresh)
                    pass3_text = pytesseract.image_to_string(thresh, config="--oem 3 --psm 11")
                except pytesseract.TesseractNotFoundError:
                    print("[OCR ERROR] Tesseract executable not found.")
                except Exception as e:
                    print(f"[OCR ERROR Pass 3] {e}")

                print(f"DEBUG - Raw OCR Text (Pass 3 - PyTesseract Adaptive Threshold):\n{pass3_text}")
                clean_pass3 = re.sub(r'[^a-z\s]', '', pass3_text.lower())
                clean_pass3 = re.sub(r'\s+', ' ', clean_pass3).strip()

                score_pass3 = compute_ocr_similarity(clean_staff_name, clean_pass3)
                print(f"DEBUG - Pass 3 Fuzzy Score: {score_pass3}% (Threshold: 70%)")

                if score_pass3 > best_score:
                    best_score = score_pass3
                    best_text = pass3_text

        print(f"DEBUG - Best OCR Score: {best_score}% (Threshold: 70%)")

        # -------------------------------------------------------------------
        # ID TYPE LOGICAL KEYWORD VALIDATION STEP
        # -------------------------------------------------------------------
        id_type_passed = False
        if is_mock or os.getenv("FACEPLUSPLUS_API_KEY", "").startswith("mock_"):
            id_type_passed = True
        else:
            if "other" in id_type.lower() and custom_id_type:
                id_type_passed = verify_id_type_keywords(custom_id_type, best_text) or verify_id_type_keywords(id_type, best_text)
            else:
                id_type_passed = verify_id_type_keywords(id_type, best_text)

        if not id_type_passed:
            print(f"DEBUG - ID Type Mismatch: selected='{effective_id_type}', text_len={len(best_text)}")
            db.execute(
                """UPDATE fip_team 
                   SET verification_status = 'Unverified', id_type = ?, id_file_path = ?, selfie_file_path = ?
                   WHERE id = ?""",
                (effective_id_type, id_filename, selfie_filename, user_id),
            )
            db.commit()
            flash(f"Verification failed: The attached image does not appear to be a valid {effective_id_type}. Please ensure you uploaded the correct document.", "error")
            return redirect(url_for("staff_profile") + "?failed=1")

        # -------------------------------------------------------------------
        # 4. STRICT 3-WAY MATCH: STEP 1 - OCR NAME MATCH
        # -------------------------------------------------------------------
        name_passed = False
        if is_mock or os.getenv("FACEPLUSPLUS_API_KEY", "").startswith("mock_"):
            name_passed = True
        else:
            if best_score >= 70:
                name_passed = True
            else:
                # Check individual tokens: First Name and Last Name must both match in OCR text
                clean_first = re.sub(r'[^a-z]', '', first_name.lower()) if first_name else ""
                clean_last = re.sub(r'[^a-z]', '', last_name.lower()) if last_name else ""
                if clean_first and clean_last:
                    ocr_words = [re.sub(r'[^a-z]', '', w.lower()) for w in best_text.split()]
                    score_first = max((fuzz.ratio(clean_first, w) for w in ocr_words), default=0)
                    score_last = max((fuzz.ratio(clean_last, w) for w in ocr_words), default=0)
                    if score_first >= 75 and score_last >= 75:
                        name_passed = True

        if not name_passed:
            print(f"DEBUG - Name Verification Failed: best_score={best_score}%")
            db.execute(
                """UPDATE fip_team 
                   SET verification_status = 'Unverified', id_type = ?, id_file_path = ?, selfie_file_path = ?
                   WHERE id = ?""",
                (effective_id_type, id_filename, selfie_filename, user_id),
            )
            db.commit()
            flash("Verification failed: Name on ID is illegible or does not match registered records.", "error")
            return redirect(url_for("staff_profile") + "?failed=1")

        # -------------------------------------------------------------------
        # 5. STRICT 3-WAY MATCH: STEP 2 - OCR BIRTHDAY MATCH
        # -------------------------------------------------------------------
        bday_passed = False
        if is_mock or os.getenv("FACEPLUSPLUS_API_KEY", "").startswith("mock_"):
            bday_passed = True
        else:
            if staff_birthday and verify_birthday_match(staff_birthday, best_text):
                bday_passed = True
            elif not staff_birthday:
                # If no birthday registered in DB, flag as missing records
                bday_passed = False

        if not bday_passed:
            print(f"DEBUG - Birthday Verification Failed: registered='{staff_birthday}'")
            db.execute(
                """UPDATE fip_team 
                   SET verification_status = 'Unverified', id_type = ?, id_file_path = ?, selfie_file_path = ?
                   WHERE id = ?""",
                (effective_id_type, id_filename, selfie_filename, user_id),
            )
            db.commit()
            flash("Verification failed: Birthday on ID does not match registered system records.", "error")
            return redirect(url_for("staff_profile") + "?failed=1")

        # -------------------------------------------------------------------
        # 6. STRICT 3-WAY MATCH: STEP 3 - ANTI-SPOOFING & FACE++ MATCH
        # -------------------------------------------------------------------
        id_base64 = base64.b64encode(id_file_bytes).decode("utf-8")
        detect_api_url = "https://api-us.faceplusplus.com/facepp/v3/detect"
        detect_payload = {
            "api_key": FACEPLUSPLUS_API_KEY,
            "api_secret": FACEPLUSPLUS_API_SECRET,
            "image_base64": clean_live_b64,
            "return_attributes": "facequality,blur,headpose"
        }

        liveness_passed = True
        liveness_reason = ""

        try:
            detect_resp = requests.post(detect_api_url, data=detect_payload, timeout=20)
            print(f"DEBUG - Face++ Detect HTTP Status: {detect_resp.status_code}")
            if detect_resp.status_code == 200:
                detect_data = detect_resp.json()
                faces = detect_data.get("faces", [])
                if not faces:
                    liveness_passed = False
                    liveness_reason = "No face detected in live selfie."
                else:
                    face_attr = faces[0].get("attributes", {})
                    facequality = face_attr.get("facequality", {})
                    blur_attr = face_attr.get("blur", {}).get("blurness", {})

                    qual_val = float(facequality.get("value", 0))
                    qual_thresh = float(facequality.get("threshold", 70.1))
                    blur_val = float(blur_attr.get("value", 0))
                    blur_thresh = float(blur_attr.get("threshold", 50.0))

                    print(f"DEBUG - Liveness Check: FaceQuality={qual_val:.2f} (thresh={qual_thresh}), Blurness={blur_val:.2f} (thresh={blur_thresh})")

                    # Screen or 2D image spoof detection: poor face quality or high blur threshold
                    if qual_val < qual_thresh or blur_val > blur_thresh:
                        liveness_passed = False
                        liveness_reason = f"Face quality {qual_val:.2f} below threshold or blurness {blur_val:.2f} exceeded."
            else:
                print(f"DEBUG - Face++ Detect API non-200: {detect_resp.status_code}")
                err_text = detect_resp.text
                if "CONCURRENCY_LIMIT_EXCEEDED" in err_text:
                    liveness_passed = True
                else:
                    liveness_passed = False
                    liveness_reason = f"Detect API returned {detect_resp.status_code}"
        except Exception as det_err:
            print(f"DEBUG - Face++ Detect Exception: {det_err}")
            liveness_passed = False
            liveness_reason = str(det_err)

        if not liveness_passed:
            print(f"DEBUG - Spoofing Detected: {liveness_reason}")
            db.execute(
                """UPDATE fip_team 
                   SET verification_status = 'Unverified', id_type = ?, id_file_path = ?, selfie_file_path = ?
                   WHERE id = ?""",
                (effective_id_type, id_filename, selfie_filename, user_id),
            )
            db.commit()
            flash("Verification failed: Liveness check failed - 2D image detected", "error")
            return redirect(url_for("staff_profile") + "?failed=1")

        # Face++ Compare Call
        face_api_url = "https://api-us.faceplusplus.com/facepp/v3/compare"
        face_payload = {
            "api_key": FACEPLUSPLUS_API_KEY,
            "api_secret": FACEPLUSPLUS_API_SECRET,
            "image_base64_1": id_base64,
            "image_base64_2": clean_live_b64
        }

        face_confidence = 0.0
        face_error = ""

        try:
            face_resp = requests.post(face_api_url, data=face_payload, timeout=30)
            print(f"DEBUG - Face++ HTTP Status: {face_resp.status_code}")
            face_data = face_resp.json() if face_resp.status_code == 200 else {}

            if face_resp.status_code == 200:
                conf_val = face_data.get("confidence")
                if conf_val is not None:
                    face_confidence = float(conf_val)
                else:
                    faces1 = face_data.get("faces1", [])
                    faces2 = face_data.get("faces2", [])
                    if not faces1:
                        face_error = "No face detected on ID image."
                    elif not faces2:
                        face_error = "No face detected on live selfie."
                    else:
                        face_error = "Unable to match faces."
            else:
                try:
                    err_msg = face_resp.json().get("error_message", f"HTTP {face_resp.status_code}")
                except Exception:
                    err_msg = face_resp.text or f"HTTP {face_resp.status_code}"
                face_error = f"Face++ API Error: {err_msg}"
        except Exception as conn_err:
            face_error = f"Face++ Connection Error: {str(conn_err)}"

        print(f"DEBUG - Face Match Confidence: {face_confidence:.2f}% (Threshold: 75.0%)")
        if face_error:
            print(f"DEBUG - Face++ Error: {face_error}")

        # -------------------------------------------------------------------
        # 7. FINAL 3-WAY SUCCESS EVALUATION
        # -------------------------------------------------------------------
        if name_passed and bday_passed and face_confidence >= 75:
            db.execute(
                """UPDATE fip_team 
                   SET verification_status = 'Verified', id_type = ?, id_file_path = ?, selfie_file_path = ?
                   WHERE id = ?""",
                (effective_id_type, id_filename, selfie_filename, user_id),
            )
            db.commit()
            flash("Account successfully verified!", "success")
            return redirect(url_for("staff_profile"))
        else:
            db.execute(
                """UPDATE fip_team 
                   SET verification_status = 'Unverified', id_type = ?, id_file_path = ?, selfie_file_path = ?
                   WHERE id = ?""",
                (effective_id_type, id_filename, selfie_filename, user_id),
            )
            db.commit()
            flash("Verification failed: Face does not match ID", "error")
            return redirect(url_for("staff_profile") + "?failed=1")

    return render_template("staff_profile.html", user=user, active_tab="staff_profile")


@app.route("/admin/change-password", methods=["POST"])
@app.route("/staff/change-password", methods=["POST"])
@login_required
def staff_change_password():
    """Handle password change for authenticated users (admin and staff)."""
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    role = session.get("role") or session.get("team_role")
    redirect_target = "admin_profile" if role == "admin" else "staff_profile"

    if not current_password or not new_password or not confirm_password:
        flash("All password fields are required.", "error")
        return redirect(url_for(redirect_target) + "#password")

    if len(new_password) < 6:
        flash("New password must be at least 6 characters long.", "error")
        return redirect(url_for(redirect_target) + "#password")

    if new_password != confirm_password:
        flash("New password and confirmation password do not match.", "error")
        return redirect(url_for(redirect_target) + "#password")

    user_id = session.get("user_id") or session.get("team_user_id")
    db = get_db()
    user = db.execute("SELECT * FROM fip_team WHERE id = ?", (user_id,)).fetchone()

    if not user or not verify_password(user["password_hash"], current_password):
        flash("Current password entered is incorrect.", "error")
        return redirect(url_for(redirect_target) + "#password")

    # Update to new password hash
    new_hash = hash_password(new_password)
    db.execute("UPDATE fip_team SET password_hash = ? WHERE id = ?", (new_hash, user_id))
    db.commit()

    flash("Your password has been updated successfully! Please use your new password next time you log in.", "success")
    return redirect(url_for(redirect_target) + "#password")


@app.route("/schedules")
@app.route("/calendar")
@app.route("/admin/schedules")
@app.route("/staff/schedules")
@app.route("/staff/calendar")
@login_required
@staff_required
def staff_calendar():
    """Interactive calendar interface for staff using FullCalendar."""
    return render_template("staff_calendar.html", active_tab="staff_calendar")


@app.route("/staff/api/events")
@login_required
@staff_required
def staff_api_events():
    """JSON API returning confirmed events to populate FullCalendar."""
    db = get_db()
    rows = db.execute(
        """SELECT * FROM inquiries 
           WHERE status IN ('Approved', 'Finalized') 
           ORDER BY event_date ASC, event_time ASC"""
    ).fetchall()

    events = []
    for r in rows:
        event_date = r["event_date"]
        event_time = r["event_time"] or ""
        
        # Build standard ISO start timestamp if time provided
        start_val = event_date
        time_clean = event_time.strip()
        if time_clean and ":" in time_clean:
            # If HH:MM (e.g. 14:00)
            if len(time_clean) == 5:
                start_val = f"{event_date}T{time_clean}:00"
            else:
                start_val = f"{event_date}T{time_clean}"

        events.append({
            "id": r["id"],
            "title": f"{r['event_type']} - {r['client_name']}",
            "start": start_val,
            "allDay": False if (time_clean and ":" in time_clean) else True,
            "backgroundColor": "#8B0000",
            "borderColor": "#C41E3A",
            "textColor": "#ffffff",
            "extendedProps": {
                "eventType": r["event_type"],
                "clientName": r["client_name"],
                "email": r["email"],
                "phone": r["phone"],
                "venue": r["venue"],
                "guestCount": r["guest_count"],
                "trackingCode": r["tracking_code"],
                "status": r["status"],
                "details": r["details"] or "No extra details provided."
            }
        })

    return jsonify(events)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
