import base64
import hmac
import io
import logging
import re
import secrets
import time
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import uuid4

from authlib.common.errors import AuthlibBaseError
from authlib.integrations.flask_client import OAuth
from flask import Flask, abort, g, jsonify, redirect, render_template, request, send_file, session
from joserfc.errors import JoseError
from requests import RequestException
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from survey.settings import Settings
from survey.storage import JobBusy, JobCapacityExceeded, QuotaExceeded, RedisStore, StorageError
from survey.translation import BRANDS, OpenAITranslator, TranslationError
from survey.workbooks import InvalidWorkbook, MAX_UPLOAD, extract_cells, make_batches, render_workbook

JOB_ID = re.compile(r"[a-f0-9]{32}")


def oidc_claims_options(settings):
    return {
        "iss": {"essential": True, "value": f"https://login.microsoftonline.com/{settings.tenant_id}/v2.0"},
        "aud": {"essential": True, "value": settings.client_id},
        "tid": {"essential": True, "value": settings.tenant_id},
    }


def create_app(settings=None, store=None, translator=None):
    settings = settings or Settings.from_env()
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.update(
        SECRET_KEY=settings.session_secret,
        SESSION_COOKIE_NAME="__Host-survey",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_PATH="/",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=1),
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=MAX_UPLOAD + 65536,
        MAX_FORM_MEMORY_SIZE=128 * 1024,
        MAX_FORM_PARTS=12,
        TRUSTED_HOSTS=[urlsplit(settings.origin).hostname],
    )
    store = store or RedisStore(settings)
    translator = translator or OpenAITranslator(settings)
    oauth = OAuth(app)
    oauth.register(
        name="microsoft",
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        server_metadata_url=(
            f"https://login.microsoftonline.com/{settings.tenant_id}"
            "/v2.0/.well-known/openid-configuration"
        ),
        client_kwargs={
            "scope": "openid profile", "code_challenge_method": "S256",
            "default_timeout": (5, 15),
        },
    )
    app.extensions["survey_oauth"] = oauth

    def current_user():
        user = session.get("user")
        if (
            not isinstance(user, dict)
            or user.get("oid") not in settings.allowed_users
            or user.get("tid") != settings.tenant_id
            or time.time() - session.get("signed_in_at", 0) >= 3600
        ):
            return None
        return user

    @app.before_request
    def protect_request():
        g.user = current_user()
        if request.path.startswith("/api/") and g.user is None:
            abort(401, description="Sign in with an approved Microsoft work account.")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            expected = session.get("csrf", "")
            supplied = request.headers.get("X-CSRF-Token", "") or request.form.get("csrf_token", "")
            if (
                request.headers.get("Origin") != settings.origin or not expected
                or not supplied or not hmac.compare_digest(expected, supplied)
            ):
                abort(403, description="The security token expired. Reload the page and try again.")

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; font-src 'self'; connect-src 'self'; "
            "form-action 'self' https://login.microsoftonline.com; "
            "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
        )
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error=error.description), error.code
        return render_template("error.html", message=error.description), error.code

    @app.errorhandler(InvalidWorkbook)
    def invalid_workbook(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(TranslationError)
    def translation_error(error):
        logging.warning("Survey translation request failed (%s).", type(error).__name__)
        return jsonify(error=str(error)), 502

    @app.errorhandler(StorageError)
    def storage_error(error):
        logging.error("Survey storage operation failed (%s).", type(error).__name__)
        return jsonify(error="Temporary storage is unavailable. Try again shortly."), 503

    @app.errorhandler(JobBusy)
    def busy_error(error):
        return jsonify(error=str(error)), 409

    @app.errorhandler(JobCapacityExceeded)
    def job_capacity_error(error):
        return jsonify(error=str(error)), 413

    @app.errorhandler(QuotaExceeded)
    def quota_error(error):
        return jsonify(error=str(error)), 429

    @app.errorhandler(500)
    def internal_error(error):
        # Never return tracebacks, tokens, document text or upstream response bodies.
        return jsonify(error="The request failed. Contact the app administrator."), 500

    @app.get("/")
    def home():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return render_template("index.html", user=g.user, brands=BRANDS, csrf=session["csrf"])

    @app.post("/auth/login")
    def login():
        return oauth.microsoft.authorize_redirect(
            settings.origin + "/auth/callback", response_mode="query",
        )

    @app.get("/auth/callback")
    def callback():
        try:
            token = oauth.microsoft.authorize_access_token(claims_options=oidc_claims_options(settings))
        except (AuthlibBaseError, JoseError, RequestException, ValueError):
            session.clear()
            abort(403, description="Microsoft sign-in could not be verified. Please sign in again.")
        claims = token.get("userinfo", {})
        if (
            claims.get("tid") != settings.tenant_id
            or claims.get("oid") not in settings.allowed_users
        ):
            session.clear()
            abort(403, description="This Microsoft account is not approved for the survey app.")
        session.clear()
        session.permanent = True
        session["signed_in_at"] = time.time()
        session["csrf"] = secrets.token_urlsafe(32)
        session["user"] = {
            "oid": claims["oid"], "tid": claims["tid"],
            "name": str(claims.get("name", "Team member"))[:100],
        }
        return redirect("/")

    @app.post("/auth/logout")
    def logout():
        session.clear()
        return redirect("/")

    def get_job(job_id):
        if not JOB_ID.fullmatch(job_id):
            abort(404)
        job = store.load(g.user["oid"], job_id)
        if job is None or time.time() >= job["expires_at"]:
            abort(404, description="This job is unavailable or has expired.")
        return job

    def summary(job_id, job):
        ready = [
            language for language in job["targets"]
            if len(job["translations"].get(language, {})) == len(job["cells"])
        ]
        return {
            "id": job_id, "filename": job["filename"], "brand": BRANDS[job["brand"]]["name"],
            "targets": job["targets"], "texts": len(job["cells"]),
            "completed": job["cursor"], "total": len(job["steps"]),
            "done": job["cursor"] == len(job["steps"]),
            "expires_at": job["expires_at"],
            "downloads": [
                {"language": target, "url": f"/api/jobs/{job_id}/download/{target}"}
                for target in ready
            ],
        }

    @app.post("/api/jobs")
    def upload():
        files = request.files.getlist("file")
        if len(files) != 1:
            abort(400, description="Upload one survey XLSX workbook at a time.")
        uploaded = files[0]
        original = uploaded.filename or ""
        filename = secure_filename(original)
        if not filename or not filename.lower().endswith(".xlsx") or len(filename) > 180:
            abort(400, description="Choose an XLSX file with a filename under 180 characters.")
        brand = request.form.get("brand", "")
        targets = request.form.getlist("targets")
        if (
            brand not in BRANDS or not targets or len(targets) != len(set(targets))
            or any(target not in BRANDS[brand]["targets"] for target in targets)
        ):
            abort(400, description="Choose a valid brand and its enabled target languages.")
        store.quota(g.user["oid"], "jobs", settings.user_jobs, settings.global_jobs)
        data = uploaded.read(MAX_UPLOAD + 1)
        cells, columns = extract_cells(data)
        batches = make_batches(cells)
        job = {
            "filename": filename, "brand": brand, "targets": targets,
            "source": base64.b64encode(data).decode(),
            "cells": cells, "columns": columns,
            "steps": [
                {"target": target, "ids": [cell["id"] for cell in batch]}
                for target in targets for batch in batches
            ],
            "translations": {}, "cursor": 0, "expires_at": time.time() + 3600,
        }
        job_id = uuid4().hex
        store.create(g.user["oid"], job_id, job)
        return jsonify(summary(job_id, job)), 201

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id):
        return jsonify(summary(job_id, get_job(job_id)))

    @app.post("/api/jobs/<job_id>/step")
    def step(job_id):
        get_job(job_id)
        body = request.get_json()
        if not isinstance(body, dict) or type(body.get("cursor")) is not int:
            abort(400, description="A valid job cursor is required.")
        owner = g.user["oid"]
        lock = store.lock(owner, job_id)
        try:
            job = get_job(job_id)
            if body["cursor"] < job["cursor"] or job["cursor"] == len(job["steps"]):
                return jsonify(summary(job_id, job))
            if body["cursor"] != job["cursor"]:
                abort(409, description="Refresh this job before resuming.")
            store.quota(owner, "batches", settings.user_batches, settings.global_batches)
            current = job["steps"][job["cursor"]]
            ids = set(current["ids"])
            cells = [cell for cell in job["cells"] if cell["id"] in ids]
            translations = translator.translate(cells, job["brand"], current["target"])
            job["translations"].setdefault(current["target"], {}).update(translations)
            job["cursor"] += 1
            store.save_locked(owner, job_id, job, lock)
            return jsonify(summary(job_id, job))
        finally:
            store.unlock(owner, job_id, lock)

    @app.get("/api/jobs/<job_id>/download/<target>")
    def download(job_id, target):
        job = get_job(job_id)
        if target not in job["targets"]:
            abort(404)
        translations = job["translations"].get(target, {})
        if len(translations) != len(job["cells"]):
            abort(409, description="This language is not finished yet.")
        data = render_workbook(
            base64.b64decode(job["source"]), job["cells"], job["columns"], translations,
        )
        return send_file(
            io.BytesIO(data), as_attachment=True,
            download_name=f"{job['filename'][:-5]}-{target}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.delete("/api/jobs/<job_id>")
    def delete(job_id):
        get_job(job_id)
        store.delete(g.user["oid"], job_id)
        return "", 204

    return app
