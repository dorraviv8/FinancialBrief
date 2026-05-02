"""
Flask web app — subscriber signup and unsubscribe.
Run with: python3 app.py (dev) or gunicorn (production)
"""

import re
import os
from flask import Flask, render_template, request, redirect, url_for
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix
import database

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", os.urandom(32))
app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Initialise DB on startup
database.init_db()
database.snapshot_subscribers_once_daily()


@app.route("/", methods=["GET"])
def signup():
    return render_template("signup.html")


@app.route("/signup", methods=["POST"])
def do_signup():
    name  = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    # Honeypot — bots fill this hidden field, humans don't
    if request.form.get("website", ""):
        return redirect(url_for("signup"))

    errors = []
    if not name:
        errors.append("נא להזין שם.")
    if not email or not EMAIL_RE.match(email):
        errors.append("נא להזין כתובת מייל תקינה.")

    if errors:
        return render_template("signup.html", errors=errors, name=name, email=email)

    result = database.add_subscriber(name, email)
    if "error" in result:
        return render_template("signup.html", errors=[result["error"]], name=name, email=email)

    return render_template("success.html", name=name)


@app.route("/unsubscribe/<token>", methods=["GET"])
def unsubscribe(token):
    found = database.unsubscribe(token)
    return render_template("unsubscribe.html", found=found)


if __name__ == "__main__":
    app_port = int(os.getenv("APP_PORT", "5000"))
    app.run(host="0.0.0.0", port=app_port, debug=False)
