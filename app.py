# app.py — Render-ready Invoice Maker (Flask + SQLite + PDF + public invoice link + logo footer)
import os
import json
import secrets
import sqlite3
import re
from datetime import datetime, date, timedelta
from io import BytesIO
from functools import wraps

from flask import Flask, request, redirect, session, url_for, send_file, Response
from werkzeug.security import generate_password_hash, check_password_hash

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

# ---------------- CONFIG (Render-friendly) ----------------
APP_SECRET = os.getenv("APP_SECRET", "CHANGE_THIS_TO_A_LONG_RANDOM_SECRET")

# Render note:
# - If you add a Persistent Disk, set DB_FILE to its mount path, e.g. /var/data/app.db
# - Without a disk, Render redeploys can wipe the DB.
DB_FILE = os.getenv("DB_FILE", "app.db")

OWNER_SETUP_KEY = os.getenv("OWNER_SETUP_KEY", "CHANGE-ME-SETUP-KEY")
COMPANY_WEBSITE = "https://cargomonterrey.com/"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_FILE = os.path.join(BASE_DIR, "cargo_logo.png")  # must exist in repo

# ---------------- APP ----------------
app = Flask(__name__)
app.secret_key = APP_SECRET

# ---------------- DATABASE ----------------
def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'owner',
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_number TEXT NOT NULL,
        client_name TEXT NOT NULL,
        client_email TEXT,
        client_address TEXT,
        issue_date TEXT NOT NULL,
        due_date TEXT NOT NULL,
        currency TEXT NOT NULL DEFAULT 'USD',
        items_json TEXT NOT NULL DEFAULT '[]',
        payment_methods TEXT,
        notes TEXT,
        total_amount REAL NOT NULL DEFAULT 0,
        created_by_user_id INTEGER,
        view_token TEXT UNIQUE,
        created_at TEXT NOT NULL,
        FOREIGN KEY(created_by_user_id) REFERENCES users(id)
    )
    """)

    conn.commit()
    conn.close()

def migrate_db():
    """Adds missing columns safely (won't break existing DB)."""
    conn = db()
    cur = conn.cursor()

    cur.execute("PRAGMA table_info(invoices)")
    cols = [r[1] for r in cur.fetchall()]

    def add_col(name, ddl):
        if name not in cols:
            cur.execute(f"ALTER TABLE invoices ADD COLUMN {ddl}")

    add_col("client_email", "client_email TEXT")
    add_col("client_address", "client_address TEXT")
    add_col("currency", "currency TEXT NOT NULL DEFAULT 'USD'")
    add_col("items_json", "items_json TEXT NOT NULL DEFAULT '[]'")
    add_col("payment_methods", "payment_methods TEXT")
    add_col("notes", "notes TEXT")
    add_col("total_amount", "total_amount REAL NOT NULL DEFAULT 0")
    add_col("created_by_user_id", "created_by_user_id INTEGER")
    add_col("view_token", "view_token TEXT")
    add_col("created_at", "created_at TEXT NOT NULL DEFAULT ''")

    cur.execute("PRAGMA table_info(users)")
    ucols = [r[1] for r in cur.fetchall()]
    if "role" not in ucols:
        cur.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'owner'")
    if "created_at" not in ucols:
        cur.execute("ALTER TABLE users ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")

    conn.commit()
    conn.close()

def any_owner_exists():
    conn = db()
    row = conn.execute("SELECT 1 FROM users WHERE role='owner' LIMIT 1").fetchone()
    conn.close()
    return bool(row)

def invoices_table_has_column(col_name: str) -> bool:
    conn = db()
    rows = conn.execute("PRAGMA table_info(invoices)").fetchall()
    conn.close()
    return any(r["name"] == col_name for r in rows)

init_db()
migrate_db()

# If you previously had an old schema with invoices.user_id NOT NULL, this keeps it compatible.
HAS_USER_ID_COL = invoices_table_has_column("user_id")

# ---------------- SAFE ROW GET ----------------
def row_get(row, key, default=""):
    try:
        val = row[key]
        return default if val is None else val
    except Exception:
        return default

# ---------------- AUTH ----------------
def staff_required(fn):
    @wraps(fn)
    def w(*a, **k):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") not in ("owner", "employee"):
            return "No access", 403
        return fn(*a, **k)
    return w

# ---------------- ITEMS PARSER (type anything) ----------------
def parse_items(raw: str):
    """
    One item per line, any format.
    LAST number on the line becomes the price.
    Examples:
      Shipping - $120
      Customs fee 45
      Handling 9.99
      Service: $1,250.50
    Qty assumed 1. If no number -> 0.00
    """
    items = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue

        nums = re.findall(r"[-+]?\$?\s*\d[\d,]*\.?\d*", line)
        unit_price = 0.0

        if nums:
            last = nums[-1]
            cleaned = last.replace("$", "").replace(",", "").strip()
            try:
                unit_price = float(cleaned)
            except ValueError:
                unit_price = 0.0

            idx = line.rfind(last)
            desc = (line[:idx] + line[idx + len(last):]).strip(" -:|")
            if not desc:
                desc = line
        else:
            desc = line

        items.append({"desc": desc, "qty": 1, "unit_price": unit_price})

    if not items:
        raise ValueError("Add at least 1 item line.")
    return items

# ---------------- PDF ----------------
def invoice_pdf_bytes(inv, items):
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    page_w, page_h = letter

    currency = row_get(inv, "currency", "USD").upper()

    def money(x):
        sym = "$" if currency == "USD" else ""
        return f"{sym}{x:,.2f} {currency}".strip()

    y = page_h - 60
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, y, "INVOICE")

    y -= 26
    c.setFont("Helvetica", 10)
    c.drawString(50, y, f"Invoice #: {row_get(inv,'invoice_number')}")
    y -= 14
    c.drawString(50, y, f"Issue: {row_get(inv,'issue_date')}    Due: {row_get(inv,'due_date')}    Currency: {currency}")

    y -= 22
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, y, "Bill To:")
    y -= 14
    c.setFont("Helvetica", 10)
    c.drawString(50, y, row_get(inv, "client_name"))

    client_email = row_get(inv, "client_email")
    if client_email:
        y -= 14
        c.drawString(50, y, client_email)

    client_address = row_get(inv, "client_address")
    if client_address:
        for line in client_address.splitlines():
            y -= 14
            c.drawString(50, y, line[:110])

    y -= 24
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "Item")
    c.drawRightString(560, y, "Amount")
    y -= 10
    c.line(50, y, 560, y)
    y -= 16

    c.setFont("Helvetica", 10)
    total = 0.0
    FOOTER_SPACE = 160

    for it in items:
        line_total = float(it.get("qty", 1)) * float(it.get("unit_price", 0))
        total += line_total

        if y < FOOTER_SPACE:
            c.showPage()
            y = page_h - 60
            c.setFont("Helvetica", 10)

        c.drawString(50, y, str(it.get("desc", ""))[:80])
        c.drawRightString(560, y, money(line_total))
        y -= 14

    y -= 8
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(560, y, f"TOTAL: {money(total)}")

    # -------- Footer: centered logo + centered clickable website --------
    footer_y = 80

    c.setStrokeColorRGB(0.75, 0.75, 0.75)
    c.line(50, footer_y + 75, page_w - 50, footer_y + 75)

    try:
        logo = ImageReader(LOGO_FILE)
        logo_w, logo_h = 220, 65
        logo_x = (page_w - logo_w) / 2
        c.drawImage(logo, logo_x, footer_y, width=logo_w, height=logo_h, mask="auto")
    except Exception:
        pass

    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0, 0, 0)
    c.drawCentredString(page_w / 2, footer_y - 10, "Visit our website:")

    c.setFillColorRGB(0, 0, 1)
    c.drawCentredString(page_w / 2, footer_y - 25, COMPANY_WEBSITE)

    c.linkURL(COMPANY_WEBSITE, (page_w / 2 - 150, footer_y - 30, page_w / 2 + 150, footer_y - 5), relative=0)
    c.setFillColorRGB(0, 0, 0)

    c.save()
    buf.seek(0)
    return buf

# ---------------- HTML ----------------
def page(title, body):
    return f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8"/>
      <meta name="viewport" content="width=device-width, initial-scale=1"/>
      <title>{title}</title>
      <style>
        body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;margin:0;background:#0b1020;color:#eaf0ff}}
        a{{color:#7dd3fc;text-decoration:none}}
        .wrap{{max-width:980px;margin:0 auto;padding:22px}}
        .card{{background:#101a33;border:1px solid rgba(255,255,255,.08);border-radius:16px;padding:18px;margin:12px 0}}
        input,textarea,select{{width:100%;padding:12px;border-radius:12px;border:1px solid rgba(255,255,255,.12);background:#0b142b;color:#eaf0ff}}
        textarea{{min-height:110px;resize:vertical}}
        .row{{display:flex;gap:10px;flex-wrap:wrap}}
        .row > div{{flex:1;min-width:220px}}
        .btn{{display:inline-block;background:linear-gradient(135deg,#6d5efc,#00d4ff);color:white;border:none;padding:12px 14px;border-radius:12px;font-weight:800;cursor:pointer}}
        .btn2{{display:inline-block;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.12);padding:12px 14px;border-radius:12px;font-weight:800;color:#eaf0ff}}
        .muted{{color:rgba(234,240,255,.7)}}
        table{{width:100%;border-collapse:collapse}}
        th,td{{padding:10px;border-bottom:1px solid rgba(255,255,255,.08);text-align:left;font-size:14px}}
      </style>
    </head>
    <body><div class="wrap">{body}</div></body></html>
    """

# ---------------- ROUTES ----------------
@app.get("/")
def home():
    return redirect(url_for("dashboard") if session.get("user_id") else url_for("login"))

@app.route("/owner-setup", methods=["GET", "POST"])
def owner_setup():
    if any_owner_exists():
        return page("Owner Setup", """
            <div class="card">
              <h2>Owner already exists</h2>
              <p class="muted">Login normally.</p>
              <a class="btn2" href="/login">Go to login</a>
            </div>
        """)

    if request.method == "POST":
        key = request.form.get("setup_key", "")
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")

        if key != OWNER_SETUP_KEY:
            return page("Owner Setup", "<div class='card'><h3>Wrong setup key</h3></div>")
        if len(username) < 3 or len(password) < 6 or password != password2:
            return page("Owner Setup", "<div class='card'><h3>Bad input</h3><p class='muted'>Username ≥ 3, Password ≥ 6, passwords match.</p></div>")

        conn = db()
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'owner', ?)",
            (username, generate_password_hash(password), datetime.utcnow().isoformat())
        )
        conn.commit()
        conn.close()
        return redirect(url_for("login"))

    return page("Owner Setup", """
      <div class="card">
        <h2>Owner Setup</h2>
        <p class="muted">One-time setup. Use an env var on Render: OWNER_SETUP_KEY</p>
        <form method="POST">
          <div class="row"><div><input name="setup_key" placeholder="Setup Key" required></div></div>
          <div class="row"><div><input name="username" placeholder="Owner Username" required></div></div>
          <div class="row">
            <div><input type="password" name="password" placeholder="Password" required></div>
            <div><input type="password" name="password2" placeholder="Confirm Password" required></div>
          </div>
          <button class="btn" type="submit">Create Owner</button>
        </form>
      </div>
    """)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")

        conn = db()
        user = conn.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
        conn.close()

        if not user or not check_password_hash(user["password_hash"], p):
            return page("Login", "<div class='card'><h3>Invalid login</h3><a class='btn2' href='/login'>Try again</a></div>")

        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["role"] = user["role"]
        return redirect(url_for("dashboard"))

    return page("Login", """
      <div class="card">
        <h2>Sign In</h2>
        <p class="muted">Private invoice maker (staff only)</p>
        <form method="POST">
          <div class="row"><div><input name="username" placeholder="Username" required></div></div>
          <div class="row"><div><input type="password" name="password" placeholder="Password" required></div></div>
          <button class="btn" type="submit">Sign In</button>
        </form>
        <p class="muted" style="margin-top:12px">First time? <a href="/owner-setup">/owner-setup</a></p>
      </div>
    """)

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.get("/dashboard")
@staff_required
def dashboard():
    conn = db()
    invs = conn.execute("""
        SELECT id, invoice_number, client_name, client_email, total_amount, currency, created_at, view_token
        FROM invoices
        ORDER BY id DESC
        LIMIT 250
    """).fetchall()
    conn.close()

    rows = ""
    for r in invs:
        rows += f"""
          <tr>
            <td>{r['invoice_number']}</td>
            <td>{r['client_name']}</td>
            <td>{row_get(r,'client_email')}</td>
            <td>{row_get(r,'currency','USD')} {float(row_get(r,'total_amount',0) or 0):.2f}</td>
            <td><a href="/invoice/{r['id']}/pdf">PDF</a></td>
            <td><a href="/view/{r['view_token']}">Link</a></td>
          </tr>
        """

    return page("Dashboard", f"""
      <div class="card">
        <div class="row" style="justify-content:space-between;align-items:center">
          <div>
            <h2>Invoice Maker</h2>
            <p class="muted">Logged in as <b>{session["username"]}</b> ({session["role"]})</p>
          </div>
          <div class="row" style="justify-content:flex-end">
            <a class="btn2" href="/logout">Logout</a>
            <a class="btn" href="/new">Create Invoice</a>
          </div>
        </div>
      </div>

      <div class="card">
        <h3>Recent Invoices</h3>
        <table>
          <thead>
            <tr><th>Invoice #</th><th>Client</th><th>Email</th><th>Total</th><th>PDF</th><th>Client Link</th></tr>
          </thead>
          <tbody>
            {rows or "<tr><td colspan='6' class='muted'>No invoices yet.</td></tr>"}
          </tbody>
        </table>
      </div>
    """)

@app.route("/new", methods=["GET", "POST"])
@staff_required
def new_invoice():
    if request.method == "POST":
        invoice_number = request.form.get("invoice_number", "").strip()
        client_name = request.form.get("client_name", "").strip()
        client_email = request.form.get("client_email", "").strip()
        client_address = request.form.get("client_address", "").strip()
        issue_date = request.form.get("issue_date", "").strip()
        due_date = request.form.get("due_date", "").strip()
        currency = (request.form.get("currency", "USD") or "USD").upper()
        items_raw = request.form.get("items_raw", "")
        payment_methods = request.form.get("payment_methods", "").strip()
        notes = request.form.get("notes", "").strip()

        if not invoice_number or not client_name or not issue_date or not due_date:
            return page("Create Invoice", "<div class='card'><h3>Missing required fields</h3></div>")

        try:
            items = parse_items(items_raw)
        except ValueError as e:
            return page("Create Invoice", f"<div class='card'><h3>Items error</h3><p class='muted'>{str(e)}</p></div>")

        total = sum(float(i["qty"]) * float(i["unit_price"]) for i in items)
        token = secrets.token_urlsafe(18)
        created_at = datetime.utcnow().isoformat()

        conn = db()
        cur = conn.cursor()

        if HAS_USER_ID_COL:
            cur.execute("""
                INSERT INTO invoices (
                  user_id,
                  invoice_number, client_name, client_email, client_address,
                  issue_date, due_date, currency, items_json,
                  payment_methods, notes, total_amount,
                  created_by_user_id, view_token, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session["user_id"],
                invoice_number, client_name, client_email, client_address,
                issue_date, due_date, currency, json.dumps(items),
                payment_methods, notes, total,
                session["user_id"], token, created_at
            ))
        else:
            cur.execute("""
                INSERT INTO invoices (
                  invoice_number, client_name, client_email, client_address,
                  issue_date, due_date, currency, items_json,
                  payment_methods, notes, total_amount,
                  created_by_user_id, view_token, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                invoice_number, client_name, client_email, client_address,
                issue_date, due_date, currency, json.dumps(items),
                payment_methods, notes, total,
                session["user_id"], token, created_at
            ))

        invoice_id = cur.lastrowid
        conn.commit()
        conn.close()
        return redirect(url_for("created", invoice_id=invoice_id))

    default_inv = f"INV-{date.today().strftime('%Y%m%d')}-{str(int(datetime.utcnow().timestamp()))[-5:]}"
    return page("Create Invoice", f"""
      <div class="card">
        <h2>Create Invoice</h2>
        <form method="POST">
          <div class="row">
            <div><input name="invoice_number" value="{default_inv}" required></div>
            <div>
              <select name="currency">
                <option>USD</option><option>CAD</option><option>EUR</option><option>GBP</option>
              </select>
            </div>
          </div>

          <div class="row">
            <div><input name="client_name" placeholder="Client name" required></div>
            <div><input name="client_email" placeholder="Client email (optional)"></div>
          </div>

          <div class="row">
            <div><input name="issue_date" value="{date.today().isoformat()}" required></div>
            <div><input name="due_date" value="{(date.today()+timedelta(days=14)).isoformat()}" required></div>
          </div>

          <div class="row">
            <div><textarea name="client_address" placeholder="Client address (optional)"></textarea></div>
          </div>

          <div class="row">
            <div>
              <textarea name="items_raw" required
                placeholder="Items (one per line) — type anything (last number becomes price):&#10;Shipping - $120&#10;Customs fee 45&#10;Handling 9.99"></textarea>
            </div>
          </div>

          <div class="row">
            <div>
              <textarea name="payment_methods"
                placeholder="Payment Methods (shown on invoice):&#10;CashApp: $YourTag&#10;Zelle: you@email.com&#10;PayPal: paypal.me/you"></textarea>
            </div>
          </div>

          <div class="row">
            <div><textarea name="notes" placeholder="Notes (optional)"></textarea></div>
          </div>

          <button class="btn" type="submit">Create Invoice</button>
          <a class="btn2" href="/dashboard" style="margin-left:8px">Cancel</a>
        </form>
      </div>
    """)

@app.get("/created/<int:invoice_id>")
@staff_required
def created(invoice_id):
    conn = db()
    inv = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    conn.close()
    if not inv:
        return page("Not Found", "<div class='card'><h3>Invoice not found</h3></div>")

    # Render-safe base URL (no hardcoded localhost)
    base = request.url_root.rstrip("/")
    client_link = f"{base}{url_for('public_view', token=inv['view_token'])}"

    mailto = ""
    if row_get(inv, "client_email"):
        subject = f"Invoice {row_get(inv,'invoice_number')}"
        body = f"Here is your invoice link: {client_link}"
        mailto = f"mailto:{row_get(inv,'client_email')}?subject={subject.replace(' ','%20')}&body={body.replace(' ','%20')}"

    return page("Created", f"""
      <div class="card">
        <h2>Invoice Created ✅</h2>
        <p class="muted"><b>{row_get(inv,'invoice_number')}</b> for {row_get(inv,'client_name')} • {row_get(inv,'currency','USD')} {float(row_get(inv,'total_amount',0) or 0):.2f}</p>

        <div class="row">
          <div><a class="btn" href="/invoice/{inv['id']}/pdf">Download PDF</a></div>
          <div><a class="btn2" href="/view/{inv['view_token']}">Client Link</a></div>
          <div><a class="btn2" href="/dashboard">Dashboard</a></div>
        </div>

        <div class="card" style="margin-top:14px">
          <h3>Client Link</h3>
          <p class="muted">{client_link}</p>
          {f"<p><a class='btn2' href='{mailto}'>Email Link</a></p>" if mailto else "<p class='muted'>No client email added (optional).</p>"}
        </div>
      </div>
    """)

@app.get("/invoice/<int:invoice_id>/pdf")
@staff_required
def invoice_pdf(invoice_id):
    conn = db()
    inv = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    conn.close()
    if not inv:
        return "Not found", 404

    items = json.loads(row_get(inv, "items_json", "[]") or "[]")
    pdf_buf = invoice_pdf_bytes(inv, items)
    return send_file(pdf_buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"{row_get(inv,'invoice_number','invoice')}.pdf")

# Public view (no login)
@app.get("/view/<token>")
def public_view(token):
    conn = db()
    inv = conn.execute("SELECT * FROM invoices WHERE view_token=?", (token,)).fetchone()
    conn.close()
    if not inv:
        return "Not found", 404

    pm = row_get(inv, "payment_methods", "").strip()
    pm_html = "<br>".join([line for line in pm.splitlines() if line.strip()]) if pm else "No payment methods listed."

    return page("Invoice", f"""
      <div class="card">
        <h2>Invoice {row_get(inv,'invoice_number')}</h2>
        <p class="muted"><b>Client:</b> {row_get(inv,'client_name')}</p>
        <p class="muted"><b>Total:</b> {row_get(inv,'currency','USD')} {float(row_get(inv,'total_amount',0) or 0):.2f}</p>
        <p class="muted"><b>Issue:</b> {row_get(inv,'issue_date')} • <b>Due:</b> {row_get(inv,'due_date')}</p>
        <div class="card">
          <h3>Payment Methods</h3>
          <p class="muted">{pm_html}</p>
        </div>
        <a class="btn" href="/view/{token}/pdf">Download PDF</a>
      </div>
    """)

@app.get("/view/<token>/pdf")
def public_pdf(token):
    conn = db()
    inv = conn.execute("SELECT * FROM invoices WHERE view_token=?", (token,)).fetchone()
    conn.close()
    if not inv:
        return "Not found", 404

    items = json.loads(row_get(inv, "items_json", "[]") or "[]")
    pdf_buf = invoice_pdf_bytes(inv, items)
    return send_file(pdf_buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"{row_get(inv,'invoice_number','invoice')}.pdf")

@app.get("/health")
def health():
    return Response("ok", mimetype="text/plain")

# Render/Gunicorn will import `app` from this file.
# For local dev: python app.py
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
