import importlib
import io
import json
import unittest
import zipfile
from unittest.mock import Mock, patch

import openpyxl

from survey.glossary import InvalidGlossary, parse_glossary
from survey.settings import Settings
from survey.translation import BRANDS, TranslationError, system_prompt
from survey.web import create_app
from test_security import ORIGIN, settings, workbook


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.provider = Mock()
        self.provider.translate.side_effect = lambda cells, brand, target, glossary: {
            cell["id"]: f"{target}: {cell['text']}" for cell in cells
        }
        self.app = create_app(settings=settings(), translator=self.provider, serve_static=True)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def post(self, path, **kwargs):
        headers = kwargs.pop("headers", {"Origin": ORIGIN, "X-Survey-Request": "1"})
        return self.client.post(path, base_url=ORIGIN, headers=headers, **kwargs)

    def upload(self, **overrides):
        return {
            "file": (io.BytesIO(workbook()), "../../survey.xlsx"),
            "brand": "gametwist", "targets": ["deDE", "frFR"], **overrides,
        }

    def batch(self, **overrides):
        return {"brand": "gaminator", "target": "deDE",
                "cells": [{"id": "item0", "text": "Hello"}], **overrides}

    def test_static_page_and_assets_need_no_secrets_or_sessions(self):
        with patch.dict("os.environ", {}, clear=True):
            client = create_app(serve_static=True).test_client()
            for path in ("/", "/app.js", "/state.mjs", "/style.css", "/brands.json"):
                response = client.get(path)
                self.assertEqual(response.status_code, 200, path)
                self.assertNotIn("Set-Cookie", response.headers)
                response.close()
            with client.get("/") as response:
                self.assertNotIn(b"{{", response.data)

    def test_end_to_end_two_languages_in_zip_without_shared_state(self):
        response = self.post("/api/prepare", data=self.upload())
        self.assertEqual(response.status_code, 200, response.json)
        prepared = response.json
        self.assertEqual(prepared["filename"], "survey.xlsx")
        self.assertEqual(prepared["count"], 2)
        csv = "enGB;deDE;frFR\nHello;Hallo;Bonjour\n"
        translations = {}
        for target in prepared["targets"]:
            translations[target] = {}
            for batch in prepared["batches"]:
                # A new function instance handles every request.
                self.client = create_app(settings=settings(), translator=self.provider).test_client()
                result = self.post("/api/translate", json={
                    "brand": "gametwist", "target": target, "cells": batch, "glossary": csv,
                })
                self.assertEqual(result.status_code, 200, result.json)
                translations[target].update(result.json["translations"])
        self.assertEqual(self.provider.translate.call_args.args[3], csv)
        response = self.post("/api/zip", data=self.upload(translations=json.dumps(translations)))
        self.assertEqual(response.status_code, 200, response.data[:100])
        self.assertEqual(response.mimetype, "application/zip")
        self.assertIn("survey-translations.zip", response.headers["Content-Disposition"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(archive.namelist(), ["survey-deDE.xlsx", "survey-frFR.xlsx"])
            for target in prepared["targets"]:
                book = openpyxl.load_workbook(io.BytesIO(archive.read(f"survey-{target}.xlsx")))
                self.assertEqual(book["Survey Content"]["B2"].value, "Hello")
                self.assertEqual(book["Survey Content"]["C2"].value, f"{target}: Hello")
                self.assertEqual(book["Survey Messages"]["C2"].value, f"{target}: Welcome")
                self.assertTrue(book["Survey Content"]["C2"].font.bold)
                book.close()
        self.assertEqual(self.provider.translate.call_count, 2)

    def test_each_deployed_entrypoint_registers_only_its_endpoint(self):
        for name in ("prepare", "glossary", "translate", "zip"):
            module = importlib.import_module(f"api.{name}")
            self.assertEqual([rule.rule for rule in module.app.url_map.iter_rules()], [f"/api/{name}"])

    def test_csv_validation_does_not_require_provider_key(self):
        self.client = create_app(settings=Settings()).test_client()
        result = self.post("/api/glossary", json={"brand": "gaminator", "csv": "enUS;deDE\nHello;Hallo"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json, {"rows": 1, "languages": ["deDE"]})

    def test_missing_key_is_reported_when_translating(self):
        self.client = create_app(settings=Settings()).test_client()
        response = self.post("/api/translate", json=self.batch())
        self.assertEqual(response.status_code, 503)
        self.assertIn("OPENAI_API_KEY", response.json["error"])

    def test_rejects_cross_origin_and_missing_request_header(self):
        for headers in ({}, {"Origin": ORIGIN}, {"Origin": "https://other.test", "X-Survey-Request": "1"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.post("/api/translate", headers=headers, json=self.batch()).status_code, 403)
        self.provider.translate.assert_not_called()

    def test_rejects_bad_json_shapes_and_unknown_targets(self):
        for body in ([], None, self.batch(brand=[]), self.batch(target="../../secret"), self.batch(cells=[])):
            with self.subTest(body=body):
                response = self.post("/api/translate", data=json.dumps(body), content_type="application/json")
                self.assertEqual(response.status_code, 400)
        self.provider.translate.assert_not_called()

    def test_bounds_batches_before_provider_call(self):
        bad_cells = [
            [{"id": "item0", "text": "Hello"}] * 2,
            [{"id": f"item{i}", "text": "x"} for i in range(13)],
            [{"id": f"item{i}", "text": "x" * 2000} for i in range(4)],
            [{"id": "item0", "text": "x" * 2001}],
            [{"id": "item0", "text": " "}],
        ]
        for cells in bad_cells:
            self.assertEqual(self.post("/api/translate", json=self.batch(cells=cells)).status_code, 400)
        self.provider.translate.assert_not_called()

    def test_invalid_custom_glossary_does_not_call_provider(self):
        self.assertEqual(self.post("/api/translate", json=self.batch(glossary="wrong;headers\na;b")).status_code, 400)
        self.provider.translate.assert_not_called()

    def test_provider_failure_and_invalid_output_are_explicit(self):
        self.provider.translate.side_effect = TranslationError("Provider unavailable.")
        self.assertEqual(self.post("/api/translate", json=self.batch()).status_code, 502)
        self.provider.translate.side_effect = None
        for output in ({}, {"item0": "\x01"}, {"item0": ""}):
            self.provider.translate.return_value = output
            self.assertEqual(self.post("/api/translate", json=self.batch()).status_code, 502)

    def test_zip_requires_all_texts_for_all_selected_languages(self):
        for translations in ({}, {"deDE": {}}, {"deDE": {}, "frFR": {}}):
            result = self.post("/api/zip", data=self.upload(translations=json.dumps(translations)))
            self.assertEqual(result.status_code, 400)
        self.provider.translate.assert_not_called()

    def test_zip_rejects_illegal_excel_characters(self):
        values = {"item0": "\x01", "item1": "Bonjour"}
        response = self.post("/api/zip", data=self.upload(translations=json.dumps({"deDE": values, "frFR": values})))
        self.assertEqual(response.status_code, 400)

    def test_zip_preserves_formula_like_output_as_text(self):
        values = {"item0": "=1+1", "item1": "Welcome"}
        response = self.post("/api/zip", data=self.upload(translations=json.dumps({"deDE": values, "frFR": values})))
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            book = openpyxl.load_workbook(io.BytesIO(archive.read("survey-deDE.xlsx")))
            self.assertEqual(book["Survey Content"]["C2"].data_type, "s")
            book.close()

    def test_zip_size_limit_is_reported(self):
        values = {"item0": "Hallo", "item1": "Welcome"}
        with patch("survey.web.MAX_OUTPUT", 100):
            response = self.post("/api/zip", data=self.upload(translations=json.dumps({"deDE": values, "frFR": values})))
        self.assertEqual(response.status_code, 413)

    def test_upload_rejects_duplicates_and_large_files(self):
        self.assertEqual(self.post("/api/prepare", data=self.upload(targets=["deDE", "deDE"])).status_code, 400)
        self.assertEqual(self.post("/api/prepare", data=self.upload(file=(io.BytesIO(b"x" * (2 * 1024 * 1024 + 1)), "big.xlsx"))).status_code, 400)


class GlossaryTests(unittest.TestCase):
    def test_csv_supports_bom_quotes_delimiters_and_multiline_terms(self):
        for delimiter in (";", ",", "\t"):
            text = f'\ufeffenUS{delimiter}deDE\r\n"Hello\nfriend"{delimiter}"Hallo, Freund"\r\n'
            headers, rows = parse_glossary(text, "enUS", ["deDE"])
            self.assertEqual(headers, ["enUS", "deDE"])
            self.assertEqual(rows, [{"enUS": "Hello\nfriend", "deDE": "Hallo, Freund"}])

    def test_rejects_malformed_empty_or_wrong_language_csv(self):
        for text in ("", "enUS;deDE", "enUS;deDE\n;", "enGB;deDE\nhello;hallo",
                     "enUS;deDE;deDE\na;b;c", 'enUS;deDE\n"unterminated;b',
                     "enUS;deDE\na;b;c", "enUS;deDE\n" + "x" * 2001 + ";b"):
            with self.subTest(text=text[:50]), self.assertRaises(InvalidGlossary):
                parse_glossary(text, "enUS", ["deDE"])

    def test_custom_glossary_replaces_default_without_cross_request_leaks(self):
        first = system_prompt("gaminator", "deDE", "enUS;deDE\nHello;CustomOne")
        second = system_prompt("gaminator", "deDE", "enUS;deDE\nHello;CustomTwo")
        default = system_prompt("gaminator", "deDE")
        self.assertIn('"Hello": "CustomOne"', first)
        self.assertNotIn("CustomOne", second)
        self.assertNotIn("CustomTwo", default)
        self.assertNotIn("Apprentice I", first)

    def test_all_existing_brand_prompts_render(self):
        for brand, config in BRANDS.items():
            for target in config["targets"]:
                self.assertIn("untrusted data", system_prompt(brand, target))

    def test_configuration_accepts_empty_environment(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Settings.from_env(), Settings())
