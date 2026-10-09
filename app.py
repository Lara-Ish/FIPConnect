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
from datetime import datetime
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for, flash, g, session
)

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
app.config["PERMANENT_SESSION_LIFETIME"] = 3600  # 1-hour sessions

DATABASE = os.path.join(app.root_path, "fipconnect.db")

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
    db = g.pop("db", None)
    if db is not None:
        db.close()


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
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    UNIQUE NOT NULL,
            password_hash TEXT    NOT NULL,
            full_name     TEXT    NOT NULL,
            role          TEXT    NOT NULL DEFAULT 'staff',
            created_at    TEXT    NOT NULL
        );
    """)

    # Seed default admin account
    existing = db.execute(
        "SELECT id FROM fip_team WHERE username = ?", ("admin",)
    ).fetchone()
    if not existing:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            """INSERT INTO fip_team (username, password_hash, full_name, role, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            ("admin", hash_password("admin123"),
             "FIP Administrator", "admin", now),
        )
    db.commit()
    db.close()


def generate_tracking_code():
    """Return a short, unique tracking code like FIP-A3F9B2."""
    return "FIP-" + secrets.token_hex(3).upper()

# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------

def login_required(f):
    """Redirect to login page if the FIP team member is not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "team_user_id" not in session:
            flash("Please log in to access the FIP Team Dashboard.", "error")
            return redirect(url_for("login"))
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
    """FIP Team login page."""
    if "team_user_id" in session:
        return redirect(url_for("admin_dashboard"))

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
            session["team_username"] = user["username"]
            session["team_fullname"] = user["full_name"]
            session["team_role"] = user["role"]
            flash(f"Welcome back, {user['full_name']}!", "success")
            return redirect(url_for("admin_dashboard"))
        else:
            flash("Invalid username or password.", "error")
            return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    """Log out the FIP team member with a one-time flash message."""
    session.pop("team_user_id", None)
    session.pop("team_username", None)
    session.pop("team_fullname", None)
    session.pop("team_role", None)
    flash("You have been successfully logged out.", "success")
    return redirect(url_for("index"))

# ---------------------------------------------------------------------------
# Routes — FIP Team Dashboard (Protected)
# ---------------------------------------------------------------------------

@app.route("/admin")
@login_required
def admin_dashboard():
    """Show high-level overview metrics."""
    db = get_db()

    # --- Status breakdown ---
    total     = db.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0]
    pending   = db.execute("SELECT COUNT(*) FROM inquiries WHERE status='Pending'").fetchone()[0]
    approved  = db.execute("SELECT COUNT(*) FROM inquiries WHERE status='Approved'").fetchone()[0]
    declined  = db.execute("SELECT COUNT(*) FROM inquiries WHERE status='Declined'").fetchone()[0]

    status_counts = {
        "total": total,
        "pending": pending,
        "approved": approved,
        "declined": declined,
        "approval_rate": round((approved / total) * 100, 1) if total else 0,
    }

    # --- Average guest count ---
    avg_guests = db.execute(
        "SELECT COALESCE(ROUND(AVG(guest_count)), 0) FROM inquiries"
    ).fetchone()[0]

    return render_template("admin_dashboard.html",
                           active_tab="dashboard",
                           status_counts=status_counts,
                           avg_guests=avg_guests)


@app.route("/admin/inquiries")
@login_required
def admin_inquiries():
    """Show and manage all inquiries for the production team."""
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

    return render_template("admin_dashboard.html",
                           inquiries=rows, status_filter=status_filter,
                           active_tab="inquiries")


@app.route("/admin/update/<int:inquiry_id>", methods=["POST"])
@login_required
def update_status(inquiry_id):
    """Update the status of an inquiry (Approved / Declined)."""
    new_status = request.form.get("status")
    if new_status not in ("Approved", "Declined"):
        flash("Invalid status value.", "error")
        return redirect(url_for("admin_inquiries"))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db = get_db()
    db.execute(
        "UPDATE inquiries SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, now, inquiry_id),
    )
    db.commit()
    flash(f"Inquiry #{inquiry_id} marked as {new_status}.", "success")
    return redirect(url_for("admin_inquiries"))

# ---------------------------------------------------------------------------
# Routes — Analytics Dashboard (Protected)
# ---------------------------------------------------------------------------

@app.route("/admin/analytics")
@login_required
def admin_analytics():
    """Business metrics using the new static layout but powered by real dynamic data."""
    db = get_db()

    # --- KPIs ---
    total = db.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0]
    approved = db.execute("SELECT COUNT(*) FROM inquiries WHERE status='Approved'").fetchone()[0]
    conversion_rate = f"{int((approved / total) * 100)}%" if total > 0 else "0%"
    proposed_revenue = "₱0" # Mocked placeholder as requested

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

    # --- Basic Service Needs (Mocked based on real total inquiries) ---
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

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
