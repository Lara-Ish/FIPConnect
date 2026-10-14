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
            id_file_path        TEXT,
            id_type             TEXT,
            verification_status TEXT    NOT NULL DEFAULT 'Unverified',
            created_at          TEXT    NOT NULL
        );
    """)

    # Migration check: Ensure 'role', 'id_file_path', 'id_type', and 'verification_status' columns exist
    cursor = db.execute("PRAGMA table_info(fip_team)")
    columns = [col[1] for col in cursor.fetchall()]
    if "role" not in columns:
        db.execute("ALTER TABLE fip_team ADD COLUMN role TEXT NOT NULL DEFAULT 'staff'")
    if "id_file_path" not in columns:
        db.execute("ALTER TABLE fip_team ADD COLUMN id_file_path TEXT")
    if "id_type" not in columns:
        db.execute("ALTER TABLE fip_team ADD COLUMN id_type TEXT")
    if "verification_status" not in columns:
        db.execute("ALTER TABLE fip_team ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'Unverified'")
    if "selfie_file_path" not in columns:
        db.execute("ALTER TABLE fip_team ADD COLUMN selfie_file_path TEXT")

    # Seed default admin account
    existing_admin = db.execute(
        "SELECT id FROM fip_team WHERE username = ?", ("admin",)
    ).fetchone()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not existing_admin:
        db.execute(
            """INSERT INTO fip_team (username, password_hash, full_name, role, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            ("admin", hash_password("admin123"),
             "FIP Administrator", "admin", now),
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
            """INSERT INTO fip_team (username, password_hash, full_name, role, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            ("staff", hash_password("staff123"),
             "FIP Operations Staff", "staff", now),
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
        full_name = request.form.get("full_name", "").strip()

        if not full_name:
            flash("Staff member full name is required.", "error")
            return redirect(url_for("manage_staff"))

        # Generate automated credentials
        login_id = generate_next_staff_id(db)
        temp_password = generate_temp_password(8)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            """INSERT INTO fip_team (username, password_hash, full_name, role, id_file_path, verification_status, created_at)
               VALUES (?, ?, ?, 'staff', NULL, 'Unverified', ?)""",
            (login_id, hash_password(temp_password), full_name, now),
        )
        db.commit()

        # Save generated credentials in session so admin view can render a dedicated copyable alert box
        session["latest_created_staff"] = {
            "full_name": full_name,
            "login_id": login_id,
            "temp_password": temp_password
        }

        flash(f"Staff account created! Login ID: {login_id} | Temporary Password: {temp_password}", "success")
        return redirect(url_for("manage_staff"))

    # Pop newly created staff info for high-visibility UI feedback
    new_staff_info = session.pop("latest_created_staff", None)

    # Fetch existing staff members
    staff_members = db.execute(
        "SELECT id, username, full_name, role, id_file_path, verification_status, created_at FROM fip_team WHERE role = 'staff' ORDER BY created_at DESC"
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

    return render_template("staff_dashboard.html",
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


@app.route("/staff/profile", methods=["GET", "POST"])
@login_required
@staff_required
def staff_profile():
    """Staff profile and automated ID + Face verification page."""
    db = get_db()
    user_id = session.get("user_id") or session.get("team_user_id")
    user = db.execute("SELECT * FROM fip_team WHERE id = ?", (user_id,)).fetchone()

    if request.method == "POST":
        id_type = request.form.get("id_type", "").strip()
        id_image = request.files.get("id_image") or request.files.get("id_file")
        live_face_base64 = request.form.get("live_face_base64", "").strip()

        if not id_type:
            flash("Please select a valid ID type from the dropdown.", "error")
            return redirect(url_for("staff_profile") + "#verification")

        if not id_image or not id_image.filename:
            flash("Please upload an image of the front of your valid ID.", "error")
            return redirect(url_for("staff_profile") + "#verification")

        if not live_face_base64:
            flash("Please capture your live face photo using the webcam before submitting.", "error")
            return redirect(url_for("staff_profile") + "#verification")

        # -------------------------------------------------------------------
        # 1. FIX FILE EXHAUSTION: Read bytes into memory EXACTLY ONCE
        # -------------------------------------------------------------------
        id_file_bytes = id_image.read()
        if not id_file_bytes:
            flash("The uploaded ID file appears to be empty. Please select a valid image.", "error")
            return redirect(url_for("staff_profile") + "#verification")

        # Encode ID bytes to Base64 for Face++ API (No re-reading required)
        id_base64 = base64.b64encode(id_file_bytes).decode("utf-8")

        # Clean Base64 live face image
        clean_live_b64 = live_face_base64.split(",", 1)[1] if "," in live_face_base64 else live_face_base64
        try:
            live_face_bytes = base64.b64decode(clean_live_b64)
            if not live_face_bytes:
                raise ValueError("Empty live face data")
        except Exception:
            flash("Invalid webcam capture image data. Please retake your face scan.", "error")
            return redirect(url_for("staff_profile") + "#verification")

        # Save both files to disk once for record keeping and UI display
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
        # 2. RUN OCR EXTRACTION & AGGRESSIVE TEXT NORMALIZATION
        # -------------------------------------------------------------------
        is_mock = os.getenv("ENABLE_MOCK_VERIFICATION", "false").lower() == "true"
        if is_mock or os.getenv("FACEPLUSPLUS_API_KEY", "").startswith("mock_"):
            ocr_text = f"REPUBLIC OF THE PHILIPPINES DRIVER LICENSE {user['full_name']} SERIAL 123456"
        else:
            ocr_text = extract_text_from_bytes(id_file_bytes)

        print(f"RAW OCR TEXT: {ocr_text}")

        # Aggressive Text Normalization: lowercase, strip punctuation/symbols/numbers, collapse whitespace
        clean_registered_name = re.sub(r'[^a-z\s]', '', user["full_name"].lower())
        clean_registered_name = re.sub(r'\s+', ' ', clean_registered_name).strip()

        clean_ocr_text = re.sub(r'[^a-z\s]', '', ocr_text.lower())
        clean_ocr_text = re.sub(r'\s+', ' ', clean_ocr_text).strip()

        # Advanced Fuzzy Matching using token_set_ratio
        fuzzy_score = fuzz.token_set_ratio(clean_registered_name, clean_ocr_text)

        print("[VERIFICATION STEP 1: OCR MATCH]")
        print(f"-> Registered Full Name (Cleaned) : '{clean_registered_name}'")
        print(f"-> OCR Extracted Text (Cleaned)   : '{clean_ocr_text}'")
        print(f"-> Token Set Ratio Score          : {fuzzy_score}% (Required: 75%)")
        print("-" * 65)

        # -------------------------------------------------------------------
        # 3. ISOLATED ERROR HANDLING: STEP 1 - OCR NAME VERIFICATION
        # -------------------------------------------------------------------
        if fuzzy_score < 75:
            db.execute(
                """UPDATE fip_team 
                   SET verification_status = 'Unverified', id_type = ?, id_file_path = ?, selfie_file_path = ?
                   WHERE id = ?""",
                (id_type, id_filename, selfie_filename, user_id),
            )
            db.commit()
            flash(f"OCR Verification Failed: Name not found on ID. (Score: {fuzzy_score}%)", "error")
            return redirect(url_for("staff_profile") + "#verification")

        # -------------------------------------------------------------------
        # 4. RUN FACE++ API CALL (Using pure Base64 strings)
        # -------------------------------------------------------------------
        face_confidence, face_error = call_faceplusplus_compare_base64(id_base64, clean_live_b64)

        print("[VERIFICATION STEP 2: FACE++ BIOMETRIC]")
        print(f"-> Match Confidence               : {face_confidence:.2f}% (Required: 75%)")
        if face_error:
            print(f"-> Face Error Detail              : {face_error}")
        print("=" * 65 + "\n")

        # -------------------------------------------------------------------
        # 5. ISOLATED ERROR HANDLING: STEP 2 - FACE BIOMETRIC VERIFICATION
        # -------------------------------------------------------------------
        if face_confidence < 75:
            db.execute(
                """UPDATE fip_team 
                   SET verification_status = 'Unverified', id_type = ?, id_file_path = ?, selfie_file_path = ?
                   WHERE id = ?""",
                (id_type, id_filename, selfie_filename, user_id),
            )
            db.commit()
            flash(f"Face Verification Failed: Selfie does not match ID photo. (Confidence: {int(face_confidence)}%)", "error")
            return redirect(url_for("staff_profile") + "#verification")

        # -------------------------------------------------------------------
        # 6. BOTH PASSED: UPDATE DATABASE TO 'VERIFIED'
        # -------------------------------------------------------------------
        db.execute(
            """UPDATE fip_team 
               SET verification_status = 'Verified', id_type = ?, id_file_path = ?, selfie_file_path = ?
               WHERE id = ?""",
            (id_type, id_filename, selfie_filename, user_id),
        )
        db.commit()
        flash("Account successfully verified!", "success")
        return redirect(url_for("staff_profile") + "#account")

    return render_template("staff_profile.html", user=user, active_tab="staff_profile")


@app.route("/staff/change-password", methods=["POST"])
@login_required
@staff_required
def staff_change_password():
    """Handle password change for authenticated staff members."""
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not current_password or not new_password or not confirm_password:
        flash("All password fields are required.", "error")
        return redirect(url_for("staff_profile"))

    if len(new_password) < 6:
        flash("New password must be at least 6 characters long.", "error")
        return redirect(url_for("staff_profile"))

    if new_password != confirm_password:
        flash("New password and confirmation password do not match.", "error")
        return redirect(url_for("staff_profile"))

    user_id = session.get("user_id") or session.get("team_user_id")
    db = get_db()
    user = db.execute("SELECT * FROM fip_team WHERE id = ?", (user_id,)).fetchone()

    if not user or not verify_password(user["password_hash"], current_password):
        flash("Current password entered is incorrect.", "error")
        return redirect(url_for("staff_profile"))

    # Update to new password hash
    new_hash = hash_password(new_password)
    db.execute("UPDATE fip_team SET password_hash = ? WHERE id = ?", (new_hash, user_id))
    db.commit()

    flash("Your password has been updated successfully! Please use your new password next time you log in.", "success")
    return redirect(url_for("staff_profile"))


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
