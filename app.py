import os
import sqlite3
import smtplib
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import Flask, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError:
    BackgroundScheduler = None

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        env_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), ".env")
        if not os.path.exists(env_path):
            return False

        with open(env_path, "r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())
        return True

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    import razorpay
except ImportError:
    razorpay = None


load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PRIMARY_DATABASE_PATH = os.path.join(BASE_DIR, "database.db")
FALLBACK_DATABASE_PATH = os.path.join(BASE_DIR, "database_fallback.db")
DATABASE_PATH = PRIMARY_DATABASE_PATH
DATABASE_URI_MODE = False
MEMORY_DB_KEEPALIVE = None
template_dir = os.path.join(BASE_DIR, "templates")
static_dir = os.path.join(BASE_DIR, "static")

app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "smart-schedule-dev-secret")

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
DEFAULT_SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
DEFAULT_SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "")
FREE_TASK_LIMIT = 5
LAST_REMINDER_CHECK_TS = 0.0
REMINDER_CHECK_LOCK = threading.Lock()


# Database helpers
def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH, uri=DATABASE_URI_MODE)
    conn.row_factory = None
    return conn


def resolve_database_path():
    global DATABASE_PATH, DATABASE_URI_MODE, MEMORY_DB_KEEPALIVE

    for candidate in (PRIMARY_DATABASE_PATH, FALLBACK_DATABASE_PATH):
        try:
            conn = sqlite3.connect(candidate)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master LIMIT 1")
            cur.fetchone()
            cur.execute("CREATE TABLE IF NOT EXISTS __healthcheck (id INTEGER)")
            cur.execute("DROP TABLE IF EXISTS __healthcheck")
            conn.commit()
            conn.close()
            DATABASE_PATH = candidate
            DATABASE_URI_MODE = False
            return
        except sqlite3.Error as exc:
            print(f"Database unavailable at {candidate}: {exc}")

    DATABASE_PATH = "file:smart_schedule?mode=memory&cache=shared"
    DATABASE_URI_MODE = True
    MEMORY_DB_KEEPALIVE = sqlite3.connect(DATABASE_PATH, uri=True)


def column_exists(cur, table_name, column_name):
    cur.execute(f"PRAGMA table_info({table_name})")
    return any(row[1] == column_name for row in cur.fetchall())


def ensure_column(cur, table_name, column_name, definition):
    if not column_exists(cur, table_name, column_name):
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


# Database setup
def init_db():
    with closing(get_db_connection()) as conn:
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                is_pro INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                task TEXT NOT NULL,
                description TEXT,
                priority INTEGER DEFAULT 2,
                deadline TEXT NOT NULL,
                deadline_time TEXT DEFAULT '09:00',
                category TEXT DEFAULT 'General',
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reminder_time TEXT NOT NULL,
                reminder_type TEXT DEFAULT 'email',
                email_sent INTEGER DEFAULT 0,
                dashboard_dismissed INTEGER DEFAULT 0,
                is_sent INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(task_id) REFERENCES tasks(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                email TEXT,
                email_password TEXT,
                notifications_enabled INTEGER DEFAULT 1,
                dashboard_alerts INTEGER DEFAULT 1,
                email_alerts INTEGER DEFAULT 1,
                browser_alerts INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )

        ensure_column(cur, "users", "is_pro", "INTEGER DEFAULT 0")
        ensure_column(cur, "users", "created_at", "TIMESTAMP")

        ensure_column(cur, "tasks", "description", "TEXT")
        ensure_column(cur, "tasks", "deadline_time", "TEXT DEFAULT '09:00'")
        ensure_column(cur, "tasks", "category", "TEXT DEFAULT 'General'")
        ensure_column(cur, "tasks", "status", "TEXT DEFAULT 'pending'")
        ensure_column(cur, "tasks", "created_at", "TIMESTAMP")
        ensure_column(cur, "tasks", "updated_at", "TIMESTAMP")

        ensure_column(cur, "reminders", "reminder_type", "TEXT DEFAULT 'email'")
        ensure_column(cur, "reminders", "email_sent", "INTEGER DEFAULT 0")
        ensure_column(cur, "reminders", "dashboard_dismissed", "INTEGER DEFAULT 0")
        ensure_column(cur, "reminders", "is_sent", "INTEGER DEFAULT 0")
        ensure_column(cur, "reminders", "created_at", "TIMESTAMP")

        ensure_column(cur, "user_settings", "notifications_enabled", "INTEGER DEFAULT 1")
        ensure_column(cur, "user_settings", "dashboard_alerts", "INTEGER DEFAULT 1")
        ensure_column(cur, "user_settings", "email_alerts", "INTEGER DEFAULT 1")
        ensure_column(cur, "user_settings", "browser_alerts", "INTEGER DEFAULT 1")
        ensure_column(cur, "user_settings", "created_at", "TIMESTAMP")

        conn.commit()

resolve_database_path()
init_db()


# Optional integrations
def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return None
    return OpenAI(api_key=api_key)


def get_razorpay_client():
    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if razorpay is None or not key_id or not key_secret:
        return None
    return razorpay.Client(auth=(key_id, key_secret))


# Email and reminder helpers
def send_email(to_email, subject, body, sender_email, sender_password):
    sender_email = sender_email or DEFAULT_SENDER_EMAIL
    sender_password = sender_password or DEFAULT_SENDER_PASSWORD
    if not to_email or not sender_email or not sender_password:
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = sender_email
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.send_message(msg)
        return True
    except Exception as exc:
        print(f"Email send error: {exc}")
        return False


def calculate_reminder_datetime(deadline, deadline_time, reminder_time):
    deadline_dt = datetime.strptime(
        f"{deadline} {deadline_time or '09:00'}", "%Y-%m-%d %H:%M"
    )

    offsets = {
        "30_min_before": timedelta(minutes=30),
        "1_hour_before": timedelta(hours=1),
        "2_hours_before": timedelta(hours=2),
        "1_day_before": timedelta(days=1),
        "2_days_before": timedelta(days=2),
        "1_week_before": timedelta(weeks=1),
    }

    delta = offsets.get(reminder_time)
    if delta is None:
        return None
    return deadline_dt - delta


def get_user_settings(user_id):
    with closing(get_db_connection()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT email, email_password, email_alerts, dashboard_alerts, browser_alerts
            FROM user_settings
            WHERE user_id=?
            """,
            (user_id,),
        )
        return cur.fetchone()


def get_notification_preferences(user_id):
    settings = get_user_settings(user_id)
    if not settings:
        return {
            "email": "",
            "email_alerts": True,
            "dashboard_alerts": True,
            "browser_alerts": True,
        }
    return {
        "email": settings[0] or "",
        "email_alerts": bool(settings[2]),
        "dashboard_alerts": bool(settings[3]),
        "browser_alerts": bool(settings[4]),
    }


def get_pending_reminders_for_user(user_id=None, for_email=False, for_dashboard=False):
    query = """
        SELECT r.id, r.task_id, r.user_id, r.reminder_time, t.task, t.deadline,
               t.deadline_time, u.username, s.email, s.email_password, s.email_alerts,
               r.email_sent, r.dashboard_dismissed
        FROM reminders r
        JOIN tasks t ON r.task_id = t.id
        JOIN users u ON r.user_id = u.id
        LEFT JOIN user_settings s ON r.user_id = s.user_id
        WHERE t.status != 'completed'
    """
    params = []
    if user_id is not None:
        query += " AND r.user_id = ?"
        params.append(user_id)
    if for_email:
        query += " AND r.email_sent = 0"
    if for_dashboard:
        query += " AND r.dashboard_dismissed = 0"
    query += " ORDER BY t.deadline, t.deadline_time"

    now = datetime.now()
    reminders_to_send = []

    with closing(get_db_connection()) as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        reminders = cur.fetchall()

    for reminder in reminders:
        try:
            should_remind_at = calculate_reminder_datetime(reminder[5], reminder[6], reminder[3])
            if should_remind_at and now >= should_remind_at:
                reminders_to_send.append(reminder)
        except Exception as exc:
            print(f"Reminder parse error: {exc}")

    return reminders_to_send


def mark_reminder_sent(reminder_id):
    with closing(get_db_connection()) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE reminders SET email_sent = 1, is_sent = 1 WHERE id = ?",
            (reminder_id,),
        )
        conn.commit()


def dismiss_dashboard_reminder(reminder_id, user_id):
    with closing(get_db_connection()) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE reminders SET dashboard_dismissed = 1 WHERE id = ? AND user_id = ?",
            (reminder_id, user_id),
        )
        conn.commit()


def check_and_send_reminders():
    for reminder in get_pending_reminders_for_user(for_email=True):
        reminder_id, _, _, _, task_name, deadline, deadline_time, username, email, email_password, email_alerts, _, _ = reminder

        sent_ok = False
        if email and email_alerts:
            subject = f"Reminder: {task_name}"
            body = f"""
            <html>
                <body style="font-family: Arial, sans-serif; padding: 20px;">
                    <h2>Task Reminder</h2>
                    <p>Hi <strong>{username}</strong>,</p>
                    <p>Your task <strong>{task_name}</strong> is due on {deadline} at {deadline_time or '09:00'}.</p>
                </body>
            </html>
            """
            sent_ok = send_email(email, subject, body, DEFAULT_SENDER_EMAIL, DEFAULT_SENDER_PASSWORD)

        if sent_ok:
            mark_reminder_sent(reminder_id)


def maybe_check_reminders():
    """Fallback trigger for platforms where background schedulers are unreliable."""
    global LAST_REMINDER_CHECK_TS

    now_ts = time.time()
    if now_ts - LAST_REMINDER_CHECK_TS < 60:
        return

    if not REMINDER_CHECK_LOCK.acquire(blocking=False):
        return

    try:
        LAST_REMINDER_CHECK_TS = now_ts
        check_and_send_reminders()
    finally:
        REMINDER_CHECK_LOCK.release()


# Session and access helpers
def ensure_logged_in():
    if "user_id" not in session:
        return redirect("/login")
    return None


def current_user_is_pro(user_id):
    with closing(get_db_connection()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT is_pro FROM users WHERE id=?", (user_id,))
        row = cur.fetchone()
        return bool(row and row[0])


def user_task_count(user_id):
    with closing(get_db_connection()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM tasks WHERE user_id=?", (user_id,))
        return cur.fetchone()[0]


@app.before_request
def reminder_fallback_runner():
    # Render and similar platforms may not keep in-process schedulers alive reliably.
    maybe_check_reminders()

# Dashboard
@app.route("/")
def dashboard():
    login_redirect = ensure_logged_in()
    if login_redirect:
        session.clear()
        return login_redirect

    user_id = session["user_id"]
    with closing(get_db_connection()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT username FROM users WHERE id=?", (user_id,))
        user = cur.fetchone()

        cur.execute(
            """
            SELECT id, task, description, priority, deadline, deadline_time, category, status
            FROM tasks
            WHERE user_id=?
            ORDER BY deadline, deadline_time
            """,
            (user_id,),
        )
        tasks = cur.fetchall()

    preferences = get_notification_preferences(user_id)
    pending_reminders = []
    if preferences["dashboard_alerts"]:
        pending_reminders = [
            (row[0], row[4], row[5], row[6], row[3])
            for row in get_pending_reminders_for_user(user_id, for_dashboard=True)
        ]
    username = user[0] if user else "User"
    completed_tasks = sum(1 for task in tasks if task[7] == "completed")

    return render_template(
        "dashboard.html",
        tasks=tasks,
        username=username,
        total_tasks=len(tasks),
        completed_tasks=completed_tasks,
        pending_tasks=len(tasks) - completed_tasks,
        pending_reminders=pending_reminders,
    )

# Add task
@app.route("/add", methods=["GET", "POST"])
def add_task():
    login_redirect = ensure_logged_in()
    if login_redirect:
        return login_redirect

    if request.method == "POST":
        user_id = session["user_id"]
        if not current_user_is_pro(user_id) and user_task_count(user_id) >= FREE_TASK_LIMIT:
            return render_template(
                "add_task.html",
                error=f"Free limit reached ({FREE_TASK_LIMIT} tasks). Upgrade to Pro.",
            )

        task = request.form.get("task", "").strip()
        description = request.form.get("description", "").strip()
        priority = request.form.get("priority", "2")
        deadline = request.form.get("deadline", "").strip()
        deadline_time = request.form.get("deadline_time", "09:00").strip() or "09:00"
        category = request.form.get("category", "General").strip() or "General"
        reminder_time = request.form.get("reminder_time", "none").strip()

        if not task or not deadline:
            return render_template("add_task.html", error="Task and deadline are required")

        try:
            priority_value = int(priority)
        except ValueError:
            return render_template("add_task.html", error="Priority must be a number")

        with closing(get_db_connection()) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO tasks (user_id, task, description, priority, deadline, deadline_time, category, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (user_id, task, description, priority_value, deadline, deadline_time, category),
            )
            task_id = cur.lastrowid

            if reminder_time and reminder_time != "none":
                cur.execute(
                    """
                    INSERT INTO reminders (task_id, user_id, reminder_time, reminder_type, is_sent)
                    VALUES (?, ?, ?, 'email', 0)
                    """,
                    (task_id, user_id, reminder_time),
                )

            conn.commit()

        return redirect("/")

    return render_template("add_task.html")

# Edit task
@app.route("/edit/<int:task_id>", methods=["GET", "POST"])
def edit_task(task_id):
    login_redirect = ensure_logged_in()
    if login_redirect:
        return login_redirect

    user_id = session["user_id"]
    with closing(get_db_connection()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, task, description, priority, deadline, deadline_time, category, status
            FROM tasks
            WHERE id=? AND user_id=?
            """,
            (task_id, user_id),
        )
        task = cur.fetchone()

        if not task:
            return redirect("/")

        if request.method == "POST":
            task_name = request.form.get("task", "").strip()
            description = request.form.get("description", "").strip()
            deadline = request.form.get("deadline", "").strip()
            deadline_time = request.form.get("deadline_time", "09:00").strip() or "09:00"
            category = request.form.get("category", "General").strip() or "General"
            status = request.form.get("status", "pending").strip() or "pending"
            reminder_time = request.form.get("reminder_time", "none").strip()

            try:
                priority = int(request.form.get("priority", "2"))
            except ValueError:
                return render_template("edit_task.html", task=task, error="Priority must be a number")

            if not task_name or not deadline:
                return render_template("edit_task.html", task=task, error="Task and deadline are required")

            cur.execute(
                """
                UPDATE tasks
                SET task=?, description=?, priority=?, deadline=?, deadline_time=?, category=?, status=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND user_id=?
                """,
                (task_name, description, priority, deadline, deadline_time, category, status, task_id, user_id),
            )

            cur.execute("DELETE FROM reminders WHERE task_id=?", (task_id,))
            if reminder_time and reminder_time != "none":
                cur.execute(
                    """
                    INSERT INTO reminders (task_id, user_id, reminder_time, reminder_type, is_sent)
                    VALUES (?, ?, ?, 'email', 0)
                    """,
                    (task_id, user_id, reminder_time),
                )

            conn.commit()
            return redirect("/")

    return render_template("edit_task.html", task=task)

# Update task status
@app.route("/api/task-status/<int:task_id>/<status>", methods=["POST"])
def update_task_status(task_id, status):
    login_redirect = ensure_logged_in()
    if login_redirect:
        return jsonify({"success": False}), 401

    if status not in {"pending", "in_progress", "completed"}:
        return jsonify({"success": False, "error": "Invalid status"}), 400

    user_id = session["user_id"]
    with closing(get_db_connection()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM tasks WHERE id=?", (task_id,))
        task = cur.fetchone()
        if not task or task[0] != user_id:
            return jsonify({"success": False}), 403

        cur.execute(
            "UPDATE tasks SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, task_id),
        )
        conn.commit()

    return jsonify({"success": True})

# Delete task from normal page flow
@app.route("/delete/<int:task_id>")
def delete_task(task_id):
    login_redirect = ensure_logged_in()
    if login_redirect:
        return login_redirect

    user_id = session["user_id"]
    with closing(get_db_connection()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM tasks WHERE id=?", (task_id,))
        task = cur.fetchone()
        if task and task[0] == user_id:
            cur.execute("DELETE FROM reminders WHERE task_id=?", (task_id,))
            cur.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            conn.commit()

    return redirect("/")

# Delete task from AJAX requests
@app.route("/api/delete/<int:task_id>", methods=["POST"])
def api_delete_task(task_id):
    login_redirect = ensure_logged_in()
    if login_redirect:
        return jsonify({"success": False}), 401

    user_id = session["user_id"]
    with closing(get_db_connection()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM tasks WHERE id=?", (task_id,))
        task = cur.fetchone()
        if not task or task[0] != user_id:
            return jsonify({"success": False}), 403

        cur.execute("DELETE FROM reminders WHERE task_id=?", (task_id,))
        cur.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.commit()

    return jsonify({"success": True})

# Login
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            return render_template("login.html", error="Username and password are required")

        with closing(get_db_connection()) as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, username, password FROM users WHERE username=?", (username,))
            user = cur.fetchone()

        if user and check_password_hash(user[2], password):
            session["user_id"] = user[0]
            return redirect("/")

        return render_template("login.html", error="Invalid username or password")

    return render_template("login.html")

# Register
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not username or not password:
            return render_template("register.html", error="Username and password are required")
        if password != confirm_password:
            return render_template("register.html", error="Passwords do not match")
        if len(password) < 4:
            return render_template("register.html", error="Password must be at least 4 characters")

        hashed_password = generate_password_hash(password)

        try:
            with closing(get_db_connection()) as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO users (username, password) VALUES (?, ?)",
                    (username, hashed_password),
                )
                conn.commit()
        except sqlite3.IntegrityError:
            return render_template("register.html", error="Username already exists")

        return redirect("/login")

    return render_template("register.html")

# Logout
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# Settings
@app.route("/settings", methods=["GET", "POST"])
def settings():
    login_redirect = ensure_logged_in()
    if login_redirect:
        return login_redirect

    user_id = session["user_id"]
    with closing(get_db_connection()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT username FROM users WHERE id=?", (user_id,))
        user = cur.fetchone()
        cur.execute(
            """
            SELECT email, email_password, email_alerts, dashboard_alerts, browser_alerts
            FROM user_settings
            WHERE user_id=?
            """,
            (user_id,),
        )
        user_settings = cur.fetchone()

        settings_data = get_notification_preferences(user_id)

        if request.method == "POST":
            email = request.form.get("email", "").strip()
            email_alerts = 1 if request.form.get("email_alerts") == "on" else 0
            dashboard_alerts = 1 if request.form.get("dashboard_alerts") == "on" else 0
            browser_alerts = 1 if request.form.get("browser_alerts") == "on" else 0

            if email_alerts and not email:
                return render_template(
                    "settings.html",
                    username=user[0] if user else "User",
                    settings=settings_data,
                    error="Enter an email address to receive reminders.",
                )

            cur.execute("SELECT id FROM user_settings WHERE user_id=?", (user_id,))
            existing = cur.fetchone()

            if existing:
                cur.execute(
                    """
                    UPDATE user_settings
                    SET email=?, email_alerts=?, dashboard_alerts=?, browser_alerts=?
                    WHERE user_id=?
                    """,
                    (email, email_alerts, dashboard_alerts, browser_alerts, user_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO user_settings (
                        user_id, email, email_alerts, dashboard_alerts, browser_alerts
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, email, email_alerts, dashboard_alerts, browser_alerts),
                )

            conn.commit()
            settings_data = {
                "email": email,
                "email_alerts": bool(email_alerts),
                "dashboard_alerts": bool(dashboard_alerts),
                "browser_alerts": bool(browser_alerts),
            }
            return render_template(
                "settings.html",
                username=user[0] if user else "User",
                settings=settings_data,
                message="Settings updated successfully!",
            )

    return render_template(
        "settings.html",
        username=user[0] if user else "User",
        settings=settings_data,
    )

# Get reminders for browser notifications
@app.route("/api/pending-reminders")
def api_pending_reminders():
    login_redirect = ensure_logged_in()
    if login_redirect:
        return jsonify({"success": False}), 401

    preferences = get_notification_preferences(session["user_id"])
    if not preferences["browser_alerts"]:
        return jsonify({"success": True, "reminders": []})

    reminders_list = []
    for reminder in get_pending_reminders_for_user(session["user_id"], for_dashboard=True):
        reminders_list.append(
            {
                "id": reminder[0],
                "task": reminder[4],
                "deadline": reminder[5],
                "deadline_time": reminder[6] or "09:00",
                "reminder_time": reminder[3],
            }
        )

    return jsonify({"success": True, "reminders": reminders_list})

# Mark a reminder as handled in the UI
@app.route("/api/mark-reminder/<int:reminder_id>", methods=["POST"])
def api_mark_reminder(reminder_id):
    login_redirect = ensure_logged_in()
    if login_redirect:
        return jsonify({"success": False}), 401

    dismiss_dashboard_reminder(reminder_id, session["user_id"])

    return jsonify({"success": True})

# AI task suggestion endpoint
@app.route("/ai-task", methods=["POST"])
def ai_task():
    login_redirect = ensure_logged_in()
    if login_redirect:
        return jsonify({"result": "Login required"}), 401

    user_id = session["user_id"]
    if not current_user_is_pro(user_id) and user_task_count(user_id) >= FREE_TASK_LIMIT:
        return jsonify({"result": "Upgrade to Pro for more AI suggestions."}), 403

    data = request.get_json(silent=True) or {}
    task_text = data.get("task", "").lower()

    if not task_text:
        return jsonify({"priority": 2, "days": 3, "text": "Enter a task to get a suggestion."})

    client = get_openai_client()
    if client is not None:
        try:
            response = client.responses.create(
                model="gpt-4.1-mini",
                input=(
                    "Classify this task and return a compact JSON object with keys "
                    "priority (1-3), days (1-7), and text (short explanation): "
                    f"{task_text}"
                ),
            )
            output_text = getattr(response, "output_text", "") or ""
            if output_text:
                import json

                parsed = json.loads(output_text)
                return jsonify(parsed)
        except Exception as exc:
            print(f"OpenAI suggestion fallback: {exc}")

    if "urgent" in task_text or "asap" in task_text:
        return jsonify({"priority": 1, "days": 1, "text": "High priority task"})
    if "meeting" in task_text:
        return jsonify({"priority": 2, "days": 2, "text": "Meeting task"})
    if "study" in task_text or "learn" in task_text:
        return jsonify({"priority": 3, "days": 5, "text": "Learning task"})
    return jsonify({"priority": 2, "days": 3, "text": "Normal task"})

# Test email delivery using saved settings
@app.route("/test-email")
def test_email():
    login_redirect = ensure_logged_in()
    if login_redirect:
        return login_redirect

    settings = get_user_settings(session["user_id"])
    if not settings or not settings[0]:
        return "Configure your email in Settings first.", 400
    if not DEFAULT_SENDER_EMAIL or not DEFAULT_SENDER_PASSWORD:
        return "App sender email is not configured on the server.", 500

    sent = send_email(
        to_email=settings[0],
        subject="Smart Schedule Test Email",
        body="<p>Your email settings are working.</p>",
        sender_email=DEFAULT_SENDER_EMAIL,
        sender_password=DEFAULT_SENDER_PASSWORD,
    )
    return "Email sent!" if sent else ("Email failed to send.", 500)

# Upgrade page
@app.route("/upgrade")
def upgrade():
    try:
        return render_template(
            "upgrade.html",
            razorpay_key_id=os.getenv("RAZORPAY_KEY_ID", ""),
        )
    except Exception:
        return "<h1>Upgrade to Pro</h1><p>Unlimited tasks and AI suggestions.</p>"

# Create a Razorpay order when payment is configured
@app.route("/create-order")
def create_order():
    client = get_razorpay_client()
    if client is None:
        return jsonify({"error": "Payment gateway is not configured."}), 503

    amount = int(request.args.get("amount", 49900))
    order = client.order.create({
        "amount": amount,
        "currency": "INR",
        "payment_capture": 1,
    })

    return {"id": order["id"], "amount": amount}


# Mark the current user as Pro after a successful payment
@app.route("/payment-success", methods=["POST"])
def payment_success():
    login_redirect = ensure_logged_in()
    if login_redirect:
        return login_redirect

    with closing(get_db_connection()) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET is_pro = 1 WHERE id=?", (session["user_id"],))
        conn.commit()

    return redirect("/")


# Background scheduler to check reminders every minute
if BackgroundScheduler is not None and not app.debug:
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=check_and_send_reminders, trigger="interval", minutes=1)
    scheduler.start()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
