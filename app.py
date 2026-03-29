import os
from flask import Flask, render_template, request, redirect, session, jsonify
import sqlite3
from datetime import datetime, timedelta
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

# Explicitly set template and static folders
template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'templates'))
static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'static'))
app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
app.secret_key = "secretkey"

# -------- EMAIL CONFIGURATION --------
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "your-email@gmail.com"  # User will set this in settings
SENDER_PASSWORD = "your-app-password"  # User will set this in settings

# -------- DATABASE --------
def init_db():
    conn = sqlite3.connect("database.db")
    cur = conn.cursor()

    cur.execute('''CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    cur.execute('''CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        task TEXT,
        description TEXT,
        priority INTEGER,
        deadline TEXT,
        deadline_time TEXT,
        category TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id))''')

    cur.execute('''CREATE TABLE IF NOT EXISTS reminders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
        user_id INTEGER,
        reminder_time TEXT,
        reminder_type TEXT,
        is_sent INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(task_id) REFERENCES tasks(id),
        FOREIGN KEY(user_id) REFERENCES users(id))''')

    cur.execute('''CREATE TABLE IF NOT EXISTS user_settings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE,
        email TEXT,
        email_password TEXT,
        notifications_enabled INTEGER DEFAULT 1,
        dashboard_alerts INTEGER DEFAULT 1,
        email_alerts INTEGER DEFAULT 1,
        browser_alerts INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id))''')

    conn.commit()
    conn.close()

init_db()

# -------- EMAIL FUNCTIONS --------
def send_email(to_email, subject, body, sender_email, sender_password):
    """Send email notification"""
    try:
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = to_email
        msg['Subject'] = subject

        msg.attach(MIMEText(body, 'html'))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"Email send error: {e}")
        return False

def get_pending_reminders():
    """Get all reminders that need to be sent"""
    try:
        conn = sqlite3.connect("database.db")
        cur = conn.cursor()
        
        now = datetime.now()
        
        # Get all unsent reminders with task and user info
        cur.execute("""
            SELECT r.id, r.task_id, r.user_id, r.reminder_time, t.task, t.deadline, 
                   t.deadline_time, u.username, s.email
            FROM reminders r
            JOIN tasks t ON r.task_id = t.id
            JOIN users u ON r.user_id = u.id
            LEFT JOIN user_settings s ON u.id = s.user_id
            WHERE r.is_sent = 0 AND t.status != 'completed'
            ORDER BY r.created_at
        """)
        
        reminders = cur.fetchall()
        conn.close()
        
        reminders_to_send = []
        
        for reminder in reminders:
            reminder_id, task_id, user_id, reminder_time, task_name, deadline, deadline_time, username, email = reminder
            
            # Calculate when reminder should be sent
            try:
                deadline_dt = datetime.strptime(f"{deadline} {deadline_time or '09:00'}", "%Y-%m-%d %H:%M")
                
                # Calculate reminder time
                if reminder_time == "30_min_before":
                    should_remind_at = deadline_dt - timedelta(minutes=30)
                elif reminder_time == "1_hour_before":
                    should_remind_at = deadline_dt - timedelta(hours=1)
                elif reminder_time == "1_day_before":
                    should_remind_at = deadline_dt - timedelta(days=1)
                elif reminder_time == "2_days_before":
                    should_remind_at = deadline_dt - timedelta(days=2)
                elif reminder_time == "1_week_before":
                    should_remind_at = deadline_dt - timedelta(weeks=1)
                else:
                    continue
                
                # If current time is past the reminder time, add to list
                if now >= should_remind_at:
                    reminders_to_send.append({
                        'id': reminder_id,
                        'task_id': task_id,
                        'user_id': user_id,
                        'task_name': task_name,
                        'deadline': deadline,
                        'deadline_time': deadline_time,
                        'username': username,
                        'email': email,
                        'reminder_time': reminder_time
                    })
            except Exception as e:
                print(f"Error calculating reminder: {e}")
        
        return reminders_to_send
    except Exception as e:
        print(f"Error getting reminders: {e}")
        return []

def mark_reminder_sent(reminder_id):
    """Mark reminder as sent"""
    try:
        conn = sqlite3.connect("database.db")
        cur = conn.cursor()
        cur.execute("UPDATE reminders SET is_sent = 1 WHERE id = ?", (reminder_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error marking reminder sent: {e}")

def check_and_send_reminders():
    """Background job to check and send reminders"""
    try:
        reminders = get_pending_reminders()
        
        for reminder in reminders:
            user_id = reminder['user_id']
            
            # Get user settings with credentials
            conn = sqlite3.connect("database.db")
            cur = conn.cursor()
            cur.execute("SELECT email_alerts, email, email_password FROM user_settings WHERE user_id = ?", (user_id,))
            settings = cur.fetchone()
            conn.close()
            
            # Send email if enabled and credentials are set
            if settings and settings[0] and reminder['email']:
                email_alerts_enabled = settings[0]
                sender_email = settings[1]  # User's Gmail address
                sender_password = settings[2]  # User's App Password
                
                # Only send if credentials are configured
                if sender_email and sender_password:
                    subject = f"📅 Reminder: {reminder['task_name']}"
                    body = f"""
                    <html>
                        <body style="font-family: Arial, sans-serif; background-color: #f5f5f5; padding: 20px;">
                            <div style="background-color: white; padding: 20px; border-radius: 10px; max-width: 600px; margin: 0 auto;">
                                <h2 style="color: #2563eb;">Task Reminder</h2>
                                <p>Hi <strong>{reminder['username']}</strong>,</p>
                                <p>Your task is coming up!</p>
                                <div style="background-color: #f0f9ff; padding: 15px; border-left: 4px solid #2563eb; margin: 20px 0;">
                                    <h3 style="color: #1e3a8a; margin: 0 0 10px 0;">📌 {reminder['task_name']}</h3>
                                    <p style="margin: 5px 0;"><strong>Due:</strong> {reminder['deadline']} at {reminder['deadline_time'] or '09:00'}</p>
                                </div>
                                <p>Log in to your Smart Schedule to view and manage your task.</p>
                                <p style="color: #666; font-size: 12px;">— Smart Schedule Team</p>
                            </div>
                        </body>
                    </html>
                    """
                    
                    try:
                        # Send email with user's credentials
                        send_email(reminder['email'], subject, body, sender_email, sender_password)
                        print(f"Email sent to {reminder['email']} for task: {reminder['task_name']}")
                    except Exception as e:
                        print(f"Error sending email: {e}")
            
            # Mark as sent
            mark_reminder_sent(reminder['id'])
    except Exception as e:
        print(f"Reminder check error: {e}")

# Start background scheduler
if not app.debug:
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=check_and_send_reminders, trigger="interval", minutes=1)
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())
@app.route("/")
def dashboard():
    # Force check for valid session
    if "user_id" not in session or session.get("user_id") is None:
        session.clear()  # Clear any stale session data
        return redirect("/login")

    conn = sqlite3.connect("database.db")
    cur = conn.cursor()

    # Get user info
    cur.execute("SELECT username FROM users WHERE id=?", (session["user_id"],))
    user = cur.fetchone()
    
    # Get tasks for current user with reminders
    cur.execute("""SELECT id, task, description, priority, deadline, deadline_time, category, status 
                   FROM tasks WHERE user_id=? ORDER BY deadline, deadline_time""", (session["user_id"],))
    tasks = cur.fetchall()

    # Get pending reminders for dashboard alert
    cur.execute("""
        SELECT r.id, t.task, t.deadline, t.deadline_time, r.reminder_time
        FROM reminders r
        JOIN tasks t ON r.task_id = t.id
        WHERE r.user_id = ? AND r.is_sent = 0 AND t.status != 'completed'
        ORDER BY t.deadline, t.deadline_time
    """, (session["user_id"],))
    pending_reminders = cur.fetchall()

    conn.close()
    
    # Get stats
    total_tasks = len(tasks)
    completed_tasks = sum(1 for t in tasks if t[7] == 'completed')
    pending_tasks = total_tasks - completed_tasks

    username = user[0] if user else "User"
    return render_template("dashboard.html", 
                         tasks=tasks, 
                         username=username, 
                         total_tasks=total_tasks,
                         completed_tasks=completed_tasks,
                         pending_tasks=pending_tasks,
                         pending_reminders=pending_reminders)

# -------- ADD TASK --------
@app.route("/add", methods=["GET", "POST"])
def add_task():
    if "user_id" not in session:
        return redirect("/login")

    if request.method == "POST":
        task = request.form.get("task", "").strip()
        description = request.form.get("description", "").strip()
        priority = request.form.get("priority", "2")
        deadline = request.form.get("deadline", "")
        deadline_time = request.form.get("deadline_time", "09:00")
        category = request.form.get("category", "General")
        reminder_time = request.form.get("reminder_time", "1_day_before")
        
        if not task or not deadline:
            return render_template("add_task.html", error="Task and deadline are required")

        try:
            priority = int(priority)
            conn = sqlite3.connect("database.db")
            cur = conn.cursor()

            cur.execute(
                """INSERT INTO tasks (user_id, task, description, priority, deadline, deadline_time, category, status) 
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (session["user_id"], task, description, priority, deadline, deadline_time, category, 'pending')
            )
            task_id = cur.lastrowid

            # Add reminder
            if reminder_time and reminder_time != 'none':
                cur.execute(
                    """INSERT INTO reminders (task_id, user_id, reminder_time, reminder_type) 
                       VALUES (?, ?, ?, ?)""",
                    (task_id, session["user_id"], reminder_time, 'email')
                )

            conn.commit()
            conn.close()
            return redirect("/")
        except Exception as e:
            return render_template("add_task.html", error=f"Error adding task: {str(e)}")

    return render_template("add_task.html")

# -------- EDIT TASK --------
@app.route("/edit/<int:id>", methods=["GET", "POST"])
def edit_task(id):
    if "user_id" not in session:
        return redirect("/login")

    conn = sqlite3.connect("database.db")
    cur = conn.cursor()

    # Verify task belongs to current user and get it in correct order
    cur.execute("SELECT id, task, description, priority, deadline, deadline_time, category, status FROM tasks WHERE id=? AND user_id=?", (id, session["user_id"]))
    task = cur.fetchone()

    if not task:
        conn.close()
        return redirect("/")

    if request.method == "POST":
        task_name = request.form.get("task", "").strip()
        description = request.form.get("description", "").strip()
        priority = request.form.get("priority", "2")
        deadline = request.form.get("deadline", "")
        deadline_time = request.form.get("deadline_time", "09:00")
        category = request.form.get("category", "General")
        status = request.form.get("status", "pending")
        reminder_time = request.form.get("reminder_time", "1_day_before")

        if not task_name or not deadline:
            return render_template("edit_task.html", task=task, error="Task and deadline are required")

        try:
            priority = int(priority)
            cur.execute(
                """UPDATE tasks SET task=?, description=?, priority=?, deadline=?, deadline_time=?, 
                   category=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (task_name, description, priority, deadline, deadline_time, category, status, id)
            )

            # Update reminders
            cur.execute("DELETE FROM reminders WHERE task_id=?", (id,))
            if reminder_time and reminder_time != 'none':
                cur.execute(
                    """INSERT INTO reminders (task_id, user_id, reminder_time, reminder_type) 
                       VALUES (?, ?, ?, ?)""",
                    (id, session["user_id"], reminder_time, 'email')
                )

            conn.commit()
            conn.close()
            return redirect("/")
        except Exception as e:
            return render_template("edit_task.html", task=task, error=f"Error updating task: {str(e)}")

    conn.close()
    return render_template("edit_task.html", task=task)

# -------- UPDATE TASK STATUS --------
@app.route("/api/task-status/<int:id>/<status>", methods=["POST"])
def update_task_status(id, status):
    if "user_id" not in session:
        return jsonify({'success': False}), 401

    try:
        conn = sqlite3.connect("database.db")
        cur = conn.cursor()

        cur.execute("SELECT user_id FROM tasks WHERE id=?", (id,))
        task = cur.fetchone()

        if not task or task[0] != session["user_id"]:
            conn.close()
            return jsonify({'success': False}), 403

        cur.execute("UPDATE tasks SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, id))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# -------- DELETE --------
@app.route("/delete/<int:id>", methods=["GET", "POST"])
def delete_task(id):
    if "user_id" not in session:
        return redirect("/login")
    
    try:
        conn = sqlite3.connect("database.db")
        cur = conn.cursor()
        
        # Verify task belongs to current user
        cur.execute("SELECT user_id FROM tasks WHERE id=?", (id,))
        task = cur.fetchone()
        
        if not task or task[0] != session["user_id"]:
            conn.close()
            return redirect("/")
        
        # Delete reminders first
        cur.execute("DELETE FROM reminders WHERE task_id=?", (id,))
        # Then delete task
        cur.execute("DELETE FROM tasks WHERE id=?", (id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error deleting task: {e}")
    
    return redirect("/")

# -------- DELETE (AJAX) --------
@app.route("/api/delete/<int:id>", methods=["POST"])
def api_delete_task(id):
    if "user_id" not in session:
        return jsonify({'success': False}), 401
    
    try:
        conn = sqlite3.connect("database.db")
        cur = conn.cursor()
        
        # Verify task belongs to current user
        cur.execute("SELECT user_id FROM tasks WHERE id=?", (id,))
        task = cur.fetchone()
        
        if not task or task[0] != session["user_id"]:
            conn.close()
            return jsonify({'success': False}), 403
        
        cur.execute("DELETE FROM tasks WHERE id=?", (id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        print(f"Error deleting task: {e}")
        return jsonify({'success': False}), 500

# -------- LOGIN --------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        
        if not username or not password:
            return render_template("login.html", error="Username and password are required")

        conn = sqlite3.connect("database.db")
        cur = conn.cursor()

        cur.execute("SELECT * FROM users WHERE username=? AND password=?", (username, password))
        user = cur.fetchone()

        conn.close()

        if user:
            session["user_id"] = user[0]
            return redirect("/")
        else:
            return render_template("login.html", error="Invalid username or password")

    return render_template("login.html")

# -------- REGISTER --------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()
        
        if not username or not password:
            return render_template("register.html", error="Username and password are required")
        
        if password != confirm_password:
            return render_template("register.html", error="Passwords do not match")
        
        if len(password) < 4:
            return render_template("register.html", error="Password must be at least 4 characters")
        
        try:
            conn = sqlite3.connect("database.db")
            cur = conn.cursor()

            cur.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
            conn.commit()
            conn.close()
            return redirect("/login")
        except sqlite3.IntegrityError:
            return render_template("register.html", error="Username already exists")
        except Exception as e:
            return render_template("register.html", error=f"Registration error: {str(e)}")

    return render_template("register.html")

# -------- LOGOUT --------
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# -------- SETTINGS --------
@app.route("/settings", methods=["GET", "POST"])
def settings():
    if "user_id" not in session:
        return redirect("/login")

    # Always get current user and settings first
    conn = sqlite3.connect("database.db")
    cur = conn.cursor()
    
    cur.execute("SELECT username FROM users WHERE id=?", (session["user_id"],))
    user = cur.fetchone()
    
    cur.execute("SELECT email, email_password, email_alerts, dashboard_alerts, browser_alerts FROM user_settings WHERE user_id=?", 
                (session["user_id"],))
    user_settings = cur.fetchone()

    settings_data = {
        'email': user_settings[0] if user_settings else '',
        'email_alerts': user_settings[2] if user_settings else True,
        'dashboard_alerts': user_settings[3] if user_settings else True,
        'browser_alerts': user_settings[4] if user_settings else True
    }

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        email_password = request.form.get("email_password", "").strip()
        email_alerts = request.form.get("email_alerts") == "on"
        dashboard_alerts = request.form.get("dashboard_alerts") == "on"
        browser_alerts = request.form.get("browser_alerts") == "on"

        try:
            # Check if settings exist
            cur.execute("SELECT id FROM user_settings WHERE user_id=?", (session["user_id"],))
            existing = cur.fetchone()

            if existing:
                cur.execute("""UPDATE user_settings 
                             SET email=?, email_password=?, email_alerts=?, 
                                 dashboard_alerts=?, browser_alerts=?
                             WHERE user_id=?""",
                           (email, email_password, email_alerts, dashboard_alerts, browser_alerts, session["user_id"]))
            else:
                cur.execute("""INSERT INTO user_settings 
                             (user_id, email, email_password, email_alerts, dashboard_alerts, browser_alerts)
                             VALUES (?, ?, ?, ?, ?, ?)""",
                           (session["user_id"], email, email_password, email_alerts, dashboard_alerts, browser_alerts))

            conn.commit()
            conn.close()
            
            # Update settings_data for display after save
            settings_data = {
                'email': email,
                'email_alerts': email_alerts,
                'dashboard_alerts': dashboard_alerts,
                'browser_alerts': browser_alerts
            }
            
            return render_template("settings.html", 
                                 username=user[0],
                                 settings=settings_data,
                                 message="Settings updated successfully!")
        except Exception as e:
            conn.close()
            return render_template("settings.html", 
                                 username=user[0],
                                 settings=settings_data,
                                 error=f"Error saving settings: {str(e)}")

    conn.close()
    return render_template("settings.html", username=user[0], settings=settings_data)

# -------- API: GET PENDING REMINDERS --------
@app.route("/api/pending-reminders")
def api_pending_reminders():
    if "user_id" not in session:
        return jsonify({'success': False}), 401

    try:
        conn = sqlite3.connect("database.db")
        cur = conn.cursor()
        
        now = datetime.now()
        
        # Get all unsent reminders with task info
        cur.execute("""
            SELECT r.id, t.task, t.deadline, t.deadline_time, r.reminder_time
            FROM reminders r
            JOIN tasks t ON r.task_id = t.id
            WHERE r.user_id = ? AND r.is_sent = 0 AND t.status != 'completed'
            ORDER BY t.deadline, t.deadline_time
        """, (session["user_id"],))
        
        reminders = cur.fetchall()
        conn.close()
        
        reminders_list = []
        for r in reminders:
            # Only include reminders that are due
            try:
                deadline_dt = datetime.strptime(f"{r[2]} {r[3] or '09:00'}", "%Y-%m-%d %H:%M")
                
                # Calculate when reminder should be sent
                if r[4] == "30_min_before":
                    should_remind_at = deadline_dt - timedelta(minutes=30)
                elif r[4] == "1_hour_before":
                    should_remind_at = deadline_dt - timedelta(hours=1)
                elif r[4] == "1_day_before":
                    should_remind_at = deadline_dt - timedelta(days=1)
                elif r[4] == "2_days_before":
                    should_remind_at = deadline_dt - timedelta(days=2)
                elif r[4] == "1_week_before":
                    should_remind_at = deadline_dt - timedelta(weeks=1)
                else:
                    continue
                
                # Only include if current time is past reminder time
                if now >= should_remind_at:
                    reminders_list.append({
                        'id': r[0],
                        'task': r[1],
                        'deadline': r[2],
                        'deadline_time': r[3],
                        'reminder_time': r[4]
                    })
            except Exception as e:
                print(f"Error calculating reminder time: {e}")
        
        return jsonify({'success': True, 'reminders': reminders_list})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# -------- API: MARK REMINDER AS NOTIFIED --------
@app.route("/api/mark-reminder/<int:id>", methods=["POST"])
def api_mark_reminder(id):
    if "user_id" not in session:
        return jsonify({'success': False}), 401

    try:
        conn = sqlite3.connect("database.db")
        cur = conn.cursor()
        
        cur.execute("UPDATE reminders SET is_sent = 1 WHERE id = ? AND user_id = ?", (id, session["user_id"]))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# -------- RUN --------
if __name__ == "__main__":
    # Run on all network interfaces (0.0.0.0) so it's accessible from other devices
    # Port 5000 - accessible as http://YOUR_IP:5000 from other laptops
    app.run(debug=True, host='0.0.0.0', port=5000)
#---------ai task---------
@app.route("/ai-task", methods=["POST"])
def ai_task():
    data = request.json
    task = data.get("task", "").lower()

    # Simple AI logic
    if "urgent" in task or "asap" in task:
        return {"priority": 1, "days": 1, "text": "High priority task"}
    elif "meeting" in task:
        return {"priority": 2, "days": 2, "text": "Meeting task"}
    elif "study" in task:
        return {"priority": 3, "days": 5, "text": "Learning task"}
    else:
        return {"priority": 2, "days": 3, "text": "Normal task"}
