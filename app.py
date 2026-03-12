# app.py (modified version)

import os
import uuid
import sqlite3
import json
import random
from datetime import datetime, timedelta
from io import BytesIO

from flask import (
    Flask, render_template, request, redirect,
    url_for, send_from_directory, abort, send_file, session
)
from flask_sock import Sock
from werkzeug.utils import secure_filename
import qrcode
import redis

# -------------------------------------------------
# CONFIG
# -------------------------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this")
app.config["UPLOAD_FOLDER"] = os.path.join(BASE_DIR, "uploads")
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB

# Initialize Sock for WebSocket support
sock = Sock(app)

# Redis connection
r = redis.Redis(host="localhost", port=6379, decode_responses=True)

DB_PATH = os.path.join(BASE_DIR, "app.db")

# In-memory room storage (consider using Redis for production)
rooms = {}

# -------------------------------------------------
# DATABASE (sqlite3)
# -------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            filename TEXT,
            original_name TEXT,
            content TEXT,
            created_at TEXT,
            expires_at TEXT
        )
        """)

init_db()

# -------------------------------------------------
# HELPERS
# -------------------------------------------------
def generate_id():
    return f"{uuid.uuid4().int % 1000000:06d}"

EXPIRY_OPTIONS = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

def get_expiry(key):
    if key == "never":
        return None
    return datetime.utcnow() + EXPIRY_OPTIONS.get(key, timedelta(days=1))

@app.template_filter("datetimeformat")
def datetimeformat(value):
    return value[:16] if value else ""

# -------------------------------------------------
# ROUTES (SHARE)
# -------------------------------------------------
@app.route("/home")
def index():
    return render_template("index.html")

@app.route("/", methods=["GET", "POST"])
def share():
    if request.method == "POST":
        text = (request.form.get("text") or "").strip()
        expiry = request.form.get("expiry", "1d")
        file = request.files.get("file")

        if not text and not file:
            return render_template("share.html", error="Provide text or file")

        item_id = generate_id()
        expires_at = get_expiry(expiry)

        stored_name = None
        safe_name = None

        if file and file.filename:
            safe_name = secure_filename(file.filename)
            stored_name = f"{item_id}_{safe_name}"
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], stored_name))

        with get_db() as db:
            db.execute("""
                INSERT INTO items (
                    id, kind, filename, original_name,
                    content, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                item_id,
                "file" if file else "text",
                stored_name,
                safe_name,
                text if text else None,
                datetime.utcnow().isoformat(),
                expires_at.isoformat() if expires_at else None
            ))

        return redirect(url_for("view_item", item_id=item_id))

    return render_template("share.html")

@app.route("/<item_id>")
def view_item(item_id):
    with get_db() as db:
        item = db.execute(
            "SELECT * FROM items WHERE id = ?",
            (item_id,)
        ).fetchone()

    if not item:
        abort(404)

    share_url = url_for("view_item", item_id=item_id, _external=True)
    return render_template("view_item.html", item=item, share_url=share_url)

@app.route("/download/<item_id>")
def download_file(item_id):
    with get_db() as db:
        item = db.execute(
            "SELECT * FROM items WHERE id = ? AND kind = 'file'",
            (item_id,)
        ).fetchone()

    if not item:
        abort(404)

    return send_from_directory(
        app.config["UPLOAD_FOLDER"],
        item["filename"],
        as_attachment=True,
        download_name=item["original_name"]
    )

@app.route("/qrcode/<item_id>")
def qrcode_image(item_id):
    share_url = url_for("view_item", item_id=item_id, _external=True)
    img = qrcode.make(share_url)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")

@app.route("/about")
def about():
    return render_template("about.html")

# -------------------------------------------------
# RECEIVER
# -------------------------------------------------
@app.route("/receiver")
def receiver_home():
    code = request.args.get("code")
    if code:
        return redirect(url_for("receiver_item", item_id=code))
    return render_template("receiver_home.html")

@app.route("/receiver/<item_id>")
def receiver_item(item_id):
    with get_db() as db:
        item = db.execute(
            "SELECT * FROM items WHERE id = ?",
            (item_id,)
        ).fetchone()

    if not item:
        return render_template("404.html", message=f"Item with ID {item_id} not found"), 404

    return render_template("receiver.html", item=item)

# -------------------------------------------------
# CHAT ROUTES
# -------------------------------------------------
@app.route("/chat")
def chat_home():
    return render_template("chat.html")

@app.route("/anonymous")
def omebox():
    return render_template("landing-anonymous.html")

@app.route('/create-room')
def create_room():
    code = str(random.randint(100000, 999999))
    rooms[code] = {}
    return {"room": code}

# -------------------------------------------------
# WEBSOCKET ENDPOINTS
# -------------------------------------------------
@sock.route('/ws/<room>/<username>')
def websocket_endpoint(ws, room, username):
    # Accept connection (Flask-Sock does this automatically)
    
    if room not in rooms:
        rooms[room] = {}
    
    rooms[room][username] = ws
    
    # Broadcast join message
    broadcast(room, {
        "type": "join",
        "user": username,
        "users": list(rooms[room].keys())
    })
    
    try:
        while True:
            # Receive message
            message = ws.receive()
            if message is None:
                break
                
            data = json.loads(message)
            
            if data["type"] == "message":
                broadcast(room, {
                    "type": "message",
                    "user": username,
                    "text": data["text"]
                })
            
            elif data["type"] == "typing":
                broadcast(room, {
                    "type": "typing",
                    "user": username
                })
            
            elif data["type"] == "reaction":
                broadcast(room, {
                    "type": "reaction",
                    "msg": data["msg"],
                    "emoji": data["emoji"]
                })
    
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        # Clean up on disconnect
        if room in rooms and username in rooms[room]:
            del rooms[room][username]
        
        # Broadcast leave message if room still exists
        if room in rooms:
            broadcast(room, {
                "type": "leave",
                "user": username,
                "users": list(rooms[room].keys())
            })

def broadcast(room, data):
    """Broadcast message to all users in a room"""
    if room not in rooms:
        return
    
    disconnected = []
    for username, ws in rooms[room].items():
        try:
            ws.send(json.dumps(data))
        except Exception:
            disconnected.append(username)
    
    # Clean up disconnected clients
    for username in disconnected:
        if room in rooms and username in rooms[room]:
            del rooms[room][username]

# -------------------------------------------------
# CLEANUP
# -------------------------------------------------
def cleanup_expired():
    now = datetime.utcnow().isoformat()

    with get_db() as db:
        expired = db.execute("""
            SELECT * FROM items
            WHERE expires_at IS NOT NULL AND expires_at < ?
        """, (now,)).fetchall()

        for item in expired:
            if item["kind"] == "file" and item["filename"]:
                try:
                    os.remove(os.path.join(app.config["UPLOAD_FOLDER"], item["filename"]))
                except:
                    pass

            db.execute("DELETE FROM items WHERE id = ?", (item["id"],))

# -------------------------------------------------
# START
# -------------------------------------------------
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        debug=False
    )