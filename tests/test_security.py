import base64
import copy
import io
import json
import time
import unittest
import zipfile
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import openpyxl
from cryptography.fernet import Fernet
from joserfc.errors import InvalidClaimError, JoseError
from joserfc import jwt
from joserfc.jwk import RSAKey
from openpyxl.styles import Font

from survey.settings import Settings
from survey.storage import JobBusy, JobCapacityExceeded, QuotaExceeded, RedisStore, StorageError
from survey.translation import BRANDS, OpenAITranslator, TranslationError, system_prompt
from survey.web import create_app, oidc_claims_options
from survey.workbooks import InvalidWorkbook, extract_cells, make_batches, render_workbook, validate_archive

USER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
TENANT = "33333333-3333-4333-8333-333333333333"
ORIGIN = "https://survey.test"


def settings():
    return Settings(
        origin=ORIGIN, session_secret="a" * 64,
        encryption_key=Fernet.generate_key().decode(), tenant_id=TENANT,
        client_id="44444444-4444-4444-8444-444444444444",
        client_secret="test-only", allowed_users=frozenset({USER, OTHER}),
        redis_url="https://test.upstash.io", redis_token="test-only",
        api_key="test-only", model="gpt-6-luna",
    )


def workbook(content="Hello", messages="Welcome"):
    wb = openpyxl.Workbook()
    first = wb.active
    first.title = "Survey Content"
    first.append(["ID", "Content", "Translated Content"])
    first.append(["intro", content, None])
    first["B2"].font = Font(bold=True)
    second = wb.create_sheet("Survey Messages")
    second.append(["ID", "Messages", "Translated Messages"])
    second.append(["success", messages, None])
    stream = io.BytesIO()
    wb.save(stream)
    wb.close()
    return stream.getvalue()


def amend_archive(data, name, value):
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as old, zipfile.ZipFile(out, "w") as new:
        for info in old.infolist():
            if info.filename != name:
                new.writestr(info.filename, old.read(info.filename))
        new.writestr(name, value)
    return out.getvalue()


class MemoryStore:
    """Test double only: production always uses authenticated Redis."""
    def __init__(self):
        self.jobs = {}
        self.locks = {}
        self.quotas = []
        self.block_quota = False

    def load(self, owner, job):
        return copy.deepcopy(self.jobs.get((owner, job)))

    def create(self, owner, job, value):
        self.jobs[owner, job] = copy.deepcopy(value)

    def save_locked(self, owner, job, value, token):
        if self.locks.get((owner, job)) != token or (owner, job) not in self.jobs:
            raise JobBusy("Lock lost.")
        self.jobs[owner, job] = copy.deepcopy(value)

    def lock(self, owner, job):
        if (owner, job) in self.locks:
            raise JobBusy("Busy.")
        self.locks[owner, job] = "test-lock"
        return "test-lock"

    def unlock(self, owner, job, token):
        self.locks.pop((owner, job), None)

    def delete(self, owner, job):
        self.jobs.pop((owner, job), None)

    def quota(self, owner, category, user_limit, global_limit):
        if self.block_quota:
            raise QuotaExceeded("Quota reached.")
        self.quotas.append(category)


class WorkbookTests(unittest.TestCase):
    def test_both_sheets_are_translated_and_styles_preserved(self):
        data = workbook()
        cells, columns = extract_cells(data)
        self.assertEqual([cell["text"] for cell in cells], ["Hello", "Welcome"])
        result = render_workbook(data, cells, columns, {"item0": "Hallo", "item1": "Willkommen"})
        wb = openpyxl.load_workbook(io.BytesIO(result))
        self.assertEqual(wb["Survey Content"]["B2"].value, "Hello")
        self.assertEqual(wb["Survey Content"]["C2"].value, "Hallo")
        self.assertEqual(wb["Survey Messages"]["C2"].value, "Willkommen")
        self.assertTrue(wb["Survey Content"]["C2"].font.bold)
        wb.close()

    def test_model_formulas_are_literal_strings(self):
        data = workbook()
        cells, columns = extract_cells(data)
        result = render_workbook(data, cells, columns, {"item0": "=1+1", "item1": "@SUM(A1)"})
        wb = openpyxl.load_workbook(io.BytesIO(result))
        self.assertEqual(wb["Survey Content"]["C2"].data_type, "s")
        self.assertEqual(wb["Survey Content"]["C2"].value, "=1+1")
        wb.close()

    def test_rejects_source_formulas(self):
        with self.assertRaises(InvalidWorkbook):
            extract_cells(workbook("=1+1"))

    def test_rejects_zip_traversal(self):
        with self.assertRaises(InvalidWorkbook):
            validate_archive(amend_archive(workbook(), "../outside.xml", b"<x/>"))

    def test_rejects_macros(self):
        with self.assertRaises(InvalidWorkbook):
            validate_archive(amend_archive(workbook(), "xl/vbaProject.bin", b"macro"))

    def test_rejects_external_relationship(self):
        xml = b'<Relationships><Relationship TargetMode="External" Target="file:///secret"/></Relationships>'
        with self.assertRaises(InvalidWorkbook):
            validate_archive(amend_archive(workbook(), "xl/_rels/evil.rels", xml))

    def test_rejects_dtd_and_entities(self):
        xml = b'<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///secret">]><x>&secret;</x>'
        with self.assertRaises(InvalidWorkbook):
            validate_archive(amend_archive(workbook(), "custom.xml", xml))

    def test_rejects_expansion_bomb(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr("xl/workbook.xml", "<x>" + "x" * 100000 + "</x>")
        with self.assertRaises(InvalidWorkbook):
            validate_archive(out.getvalue())

    def test_rejects_non_zip(self):
        with self.assertRaises(InvalidWorkbook):
            extract_cells(b"not a workbook")

    def test_rejects_missing_messages(self):
        wb = openpyxl.Workbook()
        wb.active.append(["Content"])
        wb.active.append(["Hello"])
        out = io.BytesIO()
        wb.save(out)
        wb.close()
        with self.assertRaises(InvalidWorkbook):
            extract_cells(out.getvalue())

    def test_rejects_oversized_cell(self):
        with self.assertRaises(InvalidWorkbook):
            extract_cells(workbook("a" * 2001))

    def test_rejects_cell_outside_forged_dimensions(self):
        xml = b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1"/><sheetData><row r="2"><c r="XFD2"><v>1</v></c></row></sheetData></worksheet>'
        with self.assertRaises(InvalidWorkbook):
            extract_cells(amend_archive(workbook(), "xl/worksheets/sheet1.xml", xml))

    def test_rejects_xlm_macro_sheet(self):
        with self.assertRaises(InvalidWorkbook):
            validate_archive(amend_archive(workbook(), "xl/macrosheets/sheet1.xml", b"<sheet/>"))

    def test_rejects_incomplete_translation(self):
        data = workbook()
        cells, columns = extract_cells(data)
        with self.assertRaises(InvalidWorkbook):
            render_workbook(data, cells, columns, {"item0": "Hallo"})

    def test_batches_bound_text_and_cell_counts(self):
        cells = [{"id": f"item{i}", "text": "x" * 1500} for i in range(17)]
        batches = make_batches(cells)
        self.assertEqual(sum(len(batch) for batch in batches), 17)
        self.assertTrue(all(sum(len(cell["text"]) for cell in batch) <= 6000 for batch in batches))


class WebTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.translator = Mock()
        self.translator.translate.side_effect = lambda cells, brand, target: {
            cell["id"]: f"Translated {cell['text']}" for cell in cells
        }
        self.app = create_app(settings(), self.store, self.translator)
        self.app.config.update(TESTING=True, SERVER_NAME="survey.test")
        self.client = self.app.test_client()
        self.authenticate()

    def authenticate(self, user=USER, tenant=TENANT):
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session["user"] = {"oid": user, "tid": tenant, "name": "Tester"}
            session["signed_in_at"] = time.time()
            session["csrf"] = "test-csrf"

    def call(self, method, path, **kwargs):
        headers = kwargs.pop("headers", {"Origin": ORIGIN, "X-CSRF-Token": "test-csrf"})
        return self.client.open(path, method=method, base_url=ORIGIN, headers=headers, **kwargs)

    def upload(self, filename="Example_GM_Survey.xlsx", **kwargs):
        return self.call("POST", "/api/jobs", data={
            "brand": "gaminator", "targets": ["deDE"],
            "file": (io.BytesIO(workbook()), filename),
        }, **kwargs)

    def test_requires_sign_in(self):
        self.authenticate("unapproved")
        self.assertEqual(self.upload().status_code, 401)
        self.assertFalse(self.store.jobs)

    def test_requires_correct_tenant_on_every_request(self):
        self.authenticate(tenant=OTHER)
        self.assertEqual(self.upload().status_code, 401)

    def test_session_has_absolute_expiry(self):
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session["signed_in_at"] = time.time() - 3601
        self.assertEqual(self.upload().status_code, 401)

    def test_rejects_cross_origin_post(self):
        response = self.upload(headers={"Origin": "https://attacker.test", "X-CSRF-Token": "test-csrf"})
        self.assertEqual(response.status_code, 403)

    def test_rejects_missing_csrf(self):
        self.assertEqual(self.upload(headers={"Origin": ORIGIN}).status_code, 403)

    def test_rejects_missing_origin(self):
        self.assertEqual(self.upload(headers={"X-CSRF-Token": "test-csrf"}).status_code, 403)

    def test_rejects_untrusted_host(self):
        response = self.client.get("/", base_url="https://attacker.test")
        self.assertEqual(response.status_code, 400)

    def test_owner_isolation(self):
        job = self.upload().json
        self.authenticate(OTHER)
        for method, suffix in [("GET", ""), ("POST", "/step"), ("GET", "/download/deDE"), ("DELETE", "")]:
            response = self.call(method, f"/api/jobs/{job['id']}{suffix}", json={"cursor": 0})
            self.assertEqual(response.status_code, 404)

    def test_duplicate_step_does_not_bill_twice(self):
        job = self.upload().json
        path = f"/api/jobs/{job['id']}/step"
        first = self.call("POST", path, json={"cursor": 0})
        second = self.call("POST", path, json={"cursor": 0})
        self.assertEqual(first.status_code, 200)
        self.assertTrue(second.json["done"])
        self.assertEqual(self.translator.translate.call_count, 1)
        self.assertEqual(self.store.quotas.count("batches"), 1)

    def test_future_cursor_rejected(self):
        job = self.upload().json
        self.assertEqual(self.call("POST", f"/api/jobs/{job['id']}/step", json={"cursor": 99}).status_code, 409)
        self.translator.translate.assert_not_called()

    def test_concurrent_step_is_blocked(self):
        job = self.upload().json
        self.store.lock(USER, job["id"])
        self.assertEqual(self.call("POST", f"/api/jobs/{job['id']}/step", json={"cursor": 0}).status_code, 409)
        self.translator.translate.assert_not_called()

    def test_failed_provider_call_can_resume_without_skipping(self):
        job = self.upload().json
        path = f"/api/jobs/{job['id']}/step"
        self.translator.translate.side_effect = TranslationError("Provider unavailable.")
        self.assertEqual(self.call("POST", path, json={"cursor": 0}).status_code, 502)
        self.assertEqual(self.store.load(USER, job["id"])["cursor"], 0)
        self.assertFalse(self.store.locks)

    def test_usage_limit_blocks_provider(self):
        job = self.upload().json
        self.store.block_quota = True
        self.assertEqual(self.call("POST", f"/api/jobs/{job['id']}/step", json={"cursor": 0}).status_code, 429)
        self.translator.translate.assert_not_called()

    def test_download_only_after_completion(self):
        job = self.upload().json
        url = f"/api/jobs/{job['id']}/download/deDE"
        self.assertEqual(self.call("GET", url).status_code, 409)
        self.call("POST", f"/api/jobs/{job['id']}/step", json={"cursor": 0})
        response = self.call("GET", url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("-deDE.xlsx", response.headers["Content-Disposition"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_delete_removes_job(self):
        job = self.upload().json
        path = f"/api/jobs/{job['id']}"
        self.assertEqual(self.call("DELETE", path).status_code, 204)
        self.assertEqual(self.call("GET", path).status_code, 404)

    def test_filename_is_only_sanitized_metadata(self):
        job = self.upload(filename="../../outside.xlsx").json
        self.assertEqual(job["filename"], "outside.xlsx")
        self.assertNotIn("source", job)
        self.assertNotIn("cells", job)

    def test_normal_multipart_file_larger_than_decoder_buffer_is_accepted(self):
        data = amend_archive(workbook(), "unused.dat", bytes(range(256)) * 600)
        self.assertGreater(len(data), 65536)
        response = self.call("POST", "/api/jobs", data={
            "brand": "gaminator", "targets": ["deDE"],
            "file": (io.BytesIO(data), "survey.xlsx"),
        })
        self.assertEqual(response.status_code, 201, response.json)

    def test_rejects_arbitrary_target(self):
        response = self.call("POST", "/api/jobs", data={
            "brand": "gaminator", "targets": ["../../secret"],
            "file": (io.BytesIO(workbook()), "survey.xlsx"),
        })
        self.assertEqual(response.status_code, 400)

    def test_home_renders_and_has_security_headers(self):
        response = self.call("GET", "/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"SurveyStudio", response.data.replace(b"<span>", b""))
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_unauthenticated_login_cookie_is_secure(self):
        self.client = self.app.test_client()
        response = self.call("GET", "/")
        cookie = response.headers["Set-Cookie"]
        self.assertIn("__Host-survey=", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn(b"Continue with Microsoft", response.data)

    def test_callback_denies_unapproved_identity(self):
        oauth = self.app.extensions["survey_oauth"]
        with patch.object(oauth.microsoft, "authorize_access_token", return_value={
            "userinfo": {"tid": TENANT, "oid": "unapproved"},
        }):
            self.assertEqual(self.call("GET", "/auth/callback").status_code, 403)

    def test_callback_denies_wrong_tenant(self):
        oauth = self.app.extensions["survey_oauth"]
        with patch.object(oauth.microsoft, "authorize_access_token", return_value={
            "userinfo": {"tid": OTHER, "oid": USER},
        }):
            self.assertEqual(self.call("GET", "/auth/callback").status_code, 403)

    def test_callback_handles_invalid_signed_claims(self):
        oauth = self.app.extensions["survey_oauth"]
        with patch.object(oauth.microsoft, "authorize_access_token", side_effect=InvalidClaimError("nonce")):
            response = self.call("GET", "/auth/callback")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.upload().status_code, 401)

    def test_login_uses_tenant_nonce_state_and_pkce(self):
        microsoft = self.app.extensions["survey_oauth"].microsoft
        endpoint = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize"
        with patch.object(microsoft, "load_server_metadata", return_value={"authorization_endpoint": endpoint}):
            response = self.call("POST", "/auth/login")
        self.assertEqual(response.status_code, 302)
        url = urlsplit(response.headers["Location"])
        query = parse_qs(url.query)
        self.assertEqual(url.scheme + "://" + url.netloc + url.path, endpoint)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["redirect_uri"], [ORIGIN + "/auth/callback"])
        self.assertTrue(query["state"][0])
        self.assertTrue(query["nonce"][0])
        self.assertIn("https://login.microsoftonline.com", response.headers["Content-Security-Policy"])

    def test_real_oidc_signature_audience_issuer_and_nonce_validation(self):
        microsoft = self.app.extensions["survey_oauth"].microsoft
        key = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
        issuer = f"https://login.microsoftonline.com/{TENANT}/v2.0"
        claims = {
            "iss": issuer, "aud": settings().client_id, "sub": "test-subject",
            "exp": int(time.time()) + 300, "iat": int(time.time()),
            "nonce": "expected-nonce", "tid": TENANT, "oid": USER,
        }
        metadata = {"issuer": issuer, "id_token_signing_alg_values_supported": ["RS256"]}
        def verify(value):
            token = jwt.encode({"alg": "RS256", "kid": "test-key"}, value, key)
            return microsoft.parse_id_token(
                {"id_token": token, "access_token": "test-access"}, nonce="expected-nonce",
                claims_options=oidc_claims_options(settings()),
            )
        with patch.object(microsoft, "load_server_metadata", return_value=metadata), patch.object(
            microsoft, "fetch_jwk_set", return_value={"keys": [key.as_dict(private=False)]}
        ):
            self.assertEqual(verify(claims)["oid"], USER)
            for field, value in [("aud", "other-client"), ("iss", "https://attacker.test"), ("nonce", "wrong-nonce"), ("tid", OTHER)]:
                with self.subTest(field=field), self.assertRaises(InvalidClaimError):
                    verify({**claims, field: value})
            with self.assertRaises(InvalidClaimError):
                verify({**claims, "aud": "other-client", "azp": settings().client_id})
            with self.assertRaises(JoseError):
                verify({**claims, "exp": int(time.time()) - 1000})
            other_key = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
            forged = jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, other_key)
            with self.assertRaises(JoseError):
                microsoft.parse_id_token(
                    {"id_token": forged, "access_token": "test-access"}, nonce="expected-nonce",
                    claims_options=oidc_claims_options(settings()),
                )

    def test_missing_oauth_state_is_rejected_without_network(self):
        self.assertEqual(self.call("GET", "/auth/callback?code=forged&state=unknown").status_code, 403)

    def test_deleting_during_provider_call_does_not_resurrect_job(self):
        job = self.upload().json
        def delete_during_translation(cells, brand, target):
            self.store.delete(USER, job["id"])
            return {cell["id"]: "translated" for cell in cells}
        self.translator.translate.side_effect = delete_during_translation
        response = self.call("POST", f"/api/jobs/{job['id']}/step", json={"cursor": 0})
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(self.store.load(USER, job["id"]))

    def test_callback_rotates_session_and_does_not_store_tokens(self):
        oauth = self.app.extensions["survey_oauth"]
        with patch.object(oauth.microsoft, "authorize_access_token", return_value={
            "access_token": "must-not-persist",
            "userinfo": {"tid": TENANT, "oid": USER, "name": "Tester"},
        }):
            self.assertEqual(self.call("GET", "/auth/callback").status_code, 302)
        with self.client.session_transaction(base_url=ORIGIN) as session:
            self.assertNotEqual(session["csrf"], "test-csrf")
            self.assertNotIn("must-not-persist", json.dumps(dict(session)))


class StorageAndProviderTests(unittest.TestCase):
    def test_stored_job_is_encrypted_with_expiry(self):
        store = RedisStore(settings())
        store.command = Mock(return_value="OK")
        store.create(USER, "job", {"text": "confidential survey text"})
        args = store.command.call_args.args
        self.assertNotIn("confidential survey text", args[2])
        self.assertEqual(args[-2:], ("EX", 3600))
        store.command.return_value = args[2]
        self.assertEqual(store.load(USER, "job"), {"text": "confidential survey text"})

    def test_tampered_ciphertext_fails_closed(self):
        store = RedisStore(settings())
        store.command = Mock(return_value="tampered")
        with self.assertRaises(StorageError):
            store.load(USER, "job")

    def test_oversized_encrypted_job_is_not_sent_to_storage(self):
        store = RedisStore(settings())
        store.command = Mock()
        with patch("survey.storage.MAX_STORED_JOB", 100), self.assertRaises(JobCapacityExceeded):
            store.create(USER, "job", {"text": "x" * 1000})
        store.command.assert_not_called()

    def test_missing_configuration_fails_closed(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_all_brand_prompts_render(self):
        for brand_id, brand in BRANDS.items():
            for target in brand["targets"]:
                self.assertIn("untrusted survey text", system_prompt(brand_id, target))

    def test_provider_uses_fixed_endpoint_and_strict_schema(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "choices": [{"finish_reason": "stop", "message": {"content": '{"item0":"Hallo"}'}}],
        }
        with patch("survey.translation.requests.post", return_value=response) as post:
            result = OpenAITranslator(settings()).translate(
                [{"id": "item0", "text": "Hello"}], "gaminator", "deDE",
            )
        self.assertEqual(result, {"item0": "Hallo"})
        self.assertEqual(post.call_args.args[0], "https://api.openai.com/v1/chat/completions")
        payload = post.call_args.kwargs["json"]
        self.assertFalse(payload["store"])
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_incomplete_model_output_is_rejected(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
        }
        with patch("survey.translation.requests.post", return_value=response):
            with self.assertRaises(TranslationError):
                OpenAITranslator(settings()).translate(
                    [{"id": "item0", "text": "Hello"}], "gaminator", "deDE",
                )

    def test_upstream_errors_do_not_disclose_response_body(self):
        response = Mock(status_code=401, text="secret-provider-response")
        with patch("survey.translation.requests.post", return_value=response):
            with self.assertRaises(TranslationError) as error:
                OpenAITranslator(settings()).translate(
                    [{"id": "item0", "text": "Hello"}], "gaminator", "deDE",
                )
        self.assertNotIn("secret-provider-response", str(error.exception))


if __name__ == "__main__":
    unittest.main()
