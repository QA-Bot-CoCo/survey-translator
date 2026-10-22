import io
import json
import re
import zipfile

from flask import Flask, abort, jsonify, request, send_file
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from survey.glossary import InvalidGlossary, parse_glossary
from survey.settings import Settings
from survey.translation import BRANDS, OpenAITranslator, TranslationError
from survey.workbooks import (
    InvalidWorkbook, MAX_BATCH_TEXT, MAX_CELL_TEXT, MAX_OUTPUT, MAX_UPLOAD,
    extract_cells, make_batches, render_workbook,
)

MAX_REQUEST = 4 * 1024 * 1024
MAX_TRANSLATIONS = 2 * 1024 * 1024
ITEM_ID = re.compile(r"item[0-9]{1,4}")


def valid_brand(value):
    if not isinstance(value, str) or value not in BRANDS:
        abort(400, description="Choose a valid brand.")
    return value


def valid_targets(brand, targets):
    if (
        not isinstance(targets, list) or not targets
        or any(not isinstance(target, str) or target not in BRANDS[brand]["targets"] for target in targets)
        or len(targets) != len(set(targets))
    ):
        abort(400, description="Choose the brand's enabled target languages without duplicates.")
    return targets


def json_body():
    body = request.get_json()
    if not isinstance(body, dict):
        abort(400, description="Send a JSON object.")
    return body


def uploaded_workbook():
    files = request.files.getlist("file")
    if len(files) != 1:
        abort(400, description="Choose one XLSX workbook.")
    filename = secure_filename(files[0].filename or "")
    if not filename.lower().endswith(".xlsx") or len(filename) > 180:
        abort(400, description="Choose an XLSX file with a filename under 180 characters.")
    data = files[0].read(MAX_UPLOAD + 1)
    cells, columns = extract_cells(data)
    return filename, data, cells, columns


def valid_translation(value):
    return (
        isinstance(value, str) and bool(value.strip()) and len(value) <= 8000
        and ILLEGAL_CHARACTERS_RE.search(value) is None
    )


def create_app(endpoint=None, settings=None, translator=None, serve_static=False):
    settings = settings or Settings.from_env()
    app = Flask(__name__, static_folder="../public" if serve_static else None, static_url_path="")
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_REQUEST,
        MAX_FORM_MEMORY_SIZE=MAX_TRANSLATIONS + 65536,
        MAX_FORM_PARTS=16,
    )
    provider = translator or OpenAITranslator(settings)

    @app.before_request
    def same_origin():
        if request.method == "POST":
            origin = settings.origin or request.host_url.rstrip("/")
            if request.headers.get("Origin") != origin or request.headers.get("X-Survey-Request") != "1":
                abort(403, description="Send requests from this site's page.")

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.errorhandler(InvalidWorkbook)
    @app.errorhandler(InvalidGlossary)
    def invalid_input(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(TranslationError)
    def provider_error(error):
        return jsonify(error=str(error)), 502

    def glossary():
        body = json_body()
        brand = BRANDS[valid_brand(body.get("brand"))]
        headers, rows = parse_glossary(body.get("csv"), brand["source"], brand["targets"])
        return jsonify(rows=len(rows), languages=[target for target in brand["targets"] if target in headers])

    def prepare():
        brand = valid_brand(request.form.get("brand"))
        targets = valid_targets(brand, request.form.getlist("targets"))
        filename, _, cells, _ = uploaded_workbook()
        return jsonify(filename=filename, targets=targets, batches=make_batches([
            {"id": cell["id"], "text": cell["text"]} for cell in cells
        ]), count=len(cells))

    def translate():
        body = json_body()
        brand = valid_brand(body.get("brand"))
        target = valid_targets(brand, [body.get("target")])[0]
        cells = body.get("cells")
        if not isinstance(cells, list) or not 1 <= len(cells) <= 12:
            abort(400, description="A translation batch must contain 1 to 12 texts.")
        ids = set()
        length = 0
        for cell in cells:
            if (
                not isinstance(cell, dict) or not isinstance(cell.get("id"), str)
                or not ITEM_ID.fullmatch(cell["id"]) or cell["id"] in ids
                or not isinstance(cell.get("text"), str) or not cell["text"].strip()
                or len(cell["text"]) > MAX_CELL_TEXT
            ):
                abort(400, description="Invalid text or duplicate ID in translation batch.")
            ids.add(cell["id"])
            length += len(cell["text"])
        if length > MAX_BATCH_TEXT:
            abort(400, description="A batch must contain at most 6,000 characters.")
        glossary_csv = body.get("glossary")
        if glossary_csv is not None:
            parse_glossary(glossary_csv, BRANDS[brand]["source"], BRANDS[brand]["targets"])
        if translator is None and not settings.api_key:
            abort(503, description="Set OPENAI_API_KEY on the server to enable translations.")
        result = provider.translate(cells, brand, target, glossary_csv)
        if not isinstance(result, dict) or set(result) != ids or not all(map(valid_translation, result.values())):
            raise TranslationError("The provider returned invalid translations. Retry this batch.")
        return jsonify(translations=result)

    def download_zip():
        brand = valid_brand(request.form.get("brand"))
        targets = valid_targets(brand, request.form.getlist("targets"))
        raw = request.form.get("translations", "")
        if len(raw.encode("utf-8")) > MAX_TRANSLATIONS:
            abort(413, description="Translations are too large. Translate fewer languages at a time.")
        try:
            translations = json.loads(raw)
        except ValueError:
            abort(400, description="Send valid translation JSON.")
        if not isinstance(translations, dict) or set(translations) != set(targets):
            abort(400, description="Include translations for every selected language.")
        filename, data, cells, columns = uploaded_workbook()
        ids = {cell["id"] for cell in cells}
        for target in targets:
            values = translations[target]
            if (
                not isinstance(values, dict) or set(values) != ids
                or not all(map(valid_translation, values.values()))
            ):
                abort(400, description=f"Translations for {target} are invalid or incomplete.")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for target in targets:
                archive.writestr(
                    f"{filename[:-5]}-{target}.xlsx",
                    render_workbook(data, cells, columns, translations[target]),
                )
                if output.tell() > MAX_OUTPUT:
                    abort(413, description="ZIP exceeds 4 MiB. Translate fewer languages at a time.")
        if output.tell() > MAX_OUTPUT:
            abort(413, description="ZIP exceeds 4 MiB. Translate fewer languages at a time.")
        output.seek(0)
        return send_file(output, mimetype="application/zip", as_attachment=True,
                         download_name=f"{filename[:-5]}-translations.zip")

    routes = {"prepare": prepare, "glossary": glossary, "translate": translate, "zip": download_zip}
    for name, view in routes.items():
        if endpoint is None or endpoint == name:
            app.add_url_rule(f"/api/{name}", name, view, methods=["POST"])
    if serve_static:
        app.add_url_rule("/", "home", lambda: app.send_static_file("index.html"))
    return app
