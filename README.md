# Survey Studio

A separate Vercel-ready survey translation application. This repository has a
new Git history and copies only the five brands' translation configuration,
prompts and glossaries from gparse. It does not import or modify the original
application and contains no old credentials, uploaded workbooks or OAuth tokens.

## What it does

- Sign in through a single Microsoft Entra tenant, restricted to approved user
  object IDs.
- Upload one XLSX workbook and choose a brand and enabled target languages.
- Translate both Content and Messages columns, across their respective sheets.
- Download one workbook per language, preserving source cells, sheets and cell
  formatting supported by openpyxl.
- Pause between batches and resume in the same browser tab within one hour.
- Delete a job immediately, or let its encrypted data expire automatically.

The app includes Gaminator, GameTwist, Slotpark, DCS and BBC. Filename tokens are
no longer needed: brand selection is explicit. There are no Jira, Google Sheets,
DOCX, Windows Explorer or network-share operations.

## Architecture

The UI and Python API run as a Flask Vercel Function. The browser requests one
bounded translation batch at a time. Every batch is authenticated, authorized,
CSRF checked and charged against distributed quotas before calling OpenAI.
Completed batches are saved to Redis, so requests do not depend on process-local
state or Vercel's temporary filesystem. The browser must remain open for progress;
this is not an unattended background worker.

A dedicated Upstash Redis database stores each uploaded workbook, extracted text
and translated text as a Fernet-encrypted job with a one-hour TTL. Only the app
has the encryption key. Redis key names contain user object IDs and opaque job
IDs, not document filenames. Browser session storage contains only the job ID.
Downloads are generated in memory behind owner authorization. No public blob URLs
are created. Redis backups and provider retention policies require separate
administrative review; expiration is not a promise about third-party backups.

This intentionally bounded first version uses encrypted Redis storage instead of
adding a separate object-storage service. For substantially larger workbooks,
move file payloads to private object storage and retain job metadata in Redis.

## Deployment

1. Create a **private** GitHub repository and push this repository's new history.
   Brand prompts and glossaries are business content; do not publish them in a
   public repository. Never fork or copy the original repository's Git history.
2. Import the repository into a new Vercel project. The Flask framework and
   `app.py` entrypoint are configured in `vercel.json`; Python 3.12 is selected in
   `.python-version`. Choose an approved deployment region and enable the plan's
   available deployment protection. Do not expose production secrets to untrusted
   pull-request previews.
3. Create a dedicated private Upstash Redis database in an approved region.
   Use its HTTPS REST URL and read/write REST token. Ensure the plan supports
   commands/payloads up to **8 MiB**. Job encryption and base64 increase the size
   of a 2 MiB workbook. Do not use a read-only token.
4. Register a **single-tenant Web application** in Microsoft Entra ID. Set the
   redirect URI to `https://YOUR-APP/auth/callback`. Use the authorization-code
   flow; do not enable implicit access-token or ID-token issuance. The app
   requests only `openid profile`, with PKCE and nonce/state validation.
5. In Entra Enterprise applications, enable **Assignment required**, assign the
   approved users, and apply your organization's MFA/Conditional Access policy.
   Put those users' tenant object IDs in `ALLOWED_USER_IDS`. Email suffixes are
   not used as an authorization boundary.
6. Add the variables listed in `.env.example` to Vercel's protected environment
   settings. `APP_ORIGIN` must exactly match the canonical HTTPS origin. Requests
   to other deployment hostnames are intentionally rejected. Use separate
   credentials, Redis databases and origins for preview/staging deployments.
7. Generate a fresh 64-character-or-longer session secret and a Fernet data key:

   ```powershell
   python -c "import secrets; print(secrets.token_hex(32))"
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Enter these directly into the environment settings. Do not commit them. Use a
   new project-scoped OpenAI API key with appropriate project access and usage
   controls. Do not reuse the old repository's exposed OAuth credentials.
8. Deploy and perform the live acceptance checks below before inviting users.

Missing configuration causes startup to fail. There is no anonymous mode, local
authentication bypass, in-memory production store or fallback credential.

## Limits and retention

| Limit | Default |
| --- | --- |
| Upload | 2 MiB, one XLSX |
| Download | 4 MiB per language |
| Archive | 200 entries, 20 MiB uncompressed, compression ratio <= 200 |
| Worksheet dimensions | 20 sheets, 5,000 rows, 100 columns, 80,000 bounding cells total |
| Translated input | 2,000 cells, 100,000 characters, 2,000 characters per cell |
| Translation batch | 12 cells / 6,000 input characters |
| Model output | 12,000 completion tokens per batch |
| Session / job lifetime | One hour, fixed expiration |
| Encrypted job storage | Below 8 MiB, including all target languages |
| Jobs per UTC day | 10 per user, 100 total |
| API batches per UTC day | 200 per user, 1,000 total |

The four daily quotas are configurable. Both successful and failed attempts
consume quota. A maximum-sized survey across many languages can exceed the daily
batch quota or one-hour retention; split such surveys before uploading. Quotas
are request ceilings, not exact monetary budgets. Also configure OpenAI and
hosting-provider cost controls.

Ordinary duplicate completed-step requests return the stored result without a
second API call. Locks prevent concurrent processing of a job. If a provider
request succeeds but storage fails, resuming can repeat that API charge; exactly
once billing cannot be guaranteed across those services. Reload after an
ambiguous response to retrieve the saved cursor. Closing the browser pauses
progress; it does not delete the job. Deleting during an in-flight batch cannot
restore the deleted job, although the already-started API call may still finish.

## Workbook and security behavior

Source formulas, external links, embedded objects, macros and data connections
are rejected. Remove them before uploading. ZIP paths, XML entities, compression
ratios and actual cell coordinates are validated before workbook loading.
Model-produced strings beginning with `=` are stored as text, never formulas.
Specialist Excel features not supported by openpyxl are not guaranteed to survive
a round trip. Keep the original workbook and review the output.

All job operations check ownership. Session cookies are Secure, HttpOnly and
SameSite=Lax, with an absolute one-hour application session. Logout clears the
browser cookie; a previously stolen copy remains usable until expiry unless
the user is removed from the allowlist or SESSION_SECRET is rotated. Microsoft
tokens are not persisted. Authentication uses validated tenant/object IDs, not
request-supplied identities. Apply infrastructure rate limits to sign-in routes.

Mutations require a session-bound CSRF token and exact Origin. Host validation,
CSP, framing protection and no-store responses are applied. Secrets stay server
side. Uploaded filenames are sanitized metadata, never filesystem paths. Survey
text is sent only to the fixed OpenAI API endpoint; the request sets `store=false`.
That flag does not override OpenAI's account-level data-retention policies. Obtain
organizational approval for this data flow and the hosting/storage regions.

The application does not provide an antivirus guarantee or a security
certification. Keep dependencies patched and perform a deployment-specific
security review before production use.

## Local verification

Use a current security-patched Python. The deployment targets Python 3.12; the
application is also testable on Python 3.11. Tests use synthetic in-memory
workbooks and fake external services, without credentials or paid API calls.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
python tools/audit_dependencies.py
node --check static/app.js
```

The hash lock requires wheels and pins the complete runtime dependency graph.
CI repeats tests and the live OSV audit on pushes, pull requests and weekly.
An empty advisory result is a database snapshot, not proof of absence of bugs.

Verification recorded on 2026-10-21: all 51 automated tests passed locally on
Python 3.11, dependency consistency and JavaScript syntax checks passed, and OSV
reported no known advisories for the 20 locked runtime dependencies. Linux
Python 3.12 wheel/hash resolution also passed; execution on that runtime remains
part of CI and deployment verification. Desktop and mobile browser checks passed
for upload, translation, download, reload/resume and deletion using synthetic
workbooks and fake external services. Live Microsoft, OpenAI and Redis integration
has not yet been verified.

For actual local sign-in, configure the environment variables in your shell, use
`APP_ORIGIN=https://localhost:5000`, register that callback in a separate Entra
development application, and run:

```powershell
.\.venv\Scripts\python.exe -m flask --app app run --host 127.0.0.1 --cert adhoc
```

Keep debug mode disabled. Environment files are ignored, but the app deliberately
does not load `.env` automatically. The browser needs to accept the local
development certificate; production must use valid HTTPS.

## Dependency updates

Update `requirements.in`, resolve to a temporary report, then regenerate the hash
lock. The lock generator rejects packages with OSV findings and includes only
non-yanked wheel hashes from PyPI. Review dependency changes and rerun tests.

```powershell
.\.venv\Scripts\python.exe -m pip install --dry-run --ignore-installed --only-binary=:all: --report "$env:TEMP\survey-resolve.json" -r requirements.in
python tools/lock_dependencies.py "$env:TEMP\survey-resolve.json"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Do not manually remove advisory checks to force an upgrade. Dependabot proposals
may need lock regeneration before they can be installed with hashes.

## Live acceptance checks still required

- Microsoft login succeeds for an assigned, allowlisted user and fails for a
  different tenant or unapproved user; MFA follows the tenant policy.
- A small approved survey translates through the configured OpenAI model/key.
- An approved second user cannot read, download, advance or delete the first
  user's job.
- Redis quotas and locks work across simultaneous Vercel instances; verify the
  provider command-size limit and TTL using non-sensitive synthetic files.
- Pausing/resuming, downloads, immediate deletion and one-hour expiry work on the
  deployed origin. Review provider logs for accidental sensitive data.

## References

- [Flask on Vercel](https://vercel.com/docs/frameworks/backend/flask)
- [Vercel function limits](https://vercel.com/docs/functions/limitations)
- [Microsoft claim validation](https://learn.microsoft.com/en-us/entra/identity-platform/claims-validation)
- [Upstash Redis REST API](https://upstash.com/docs/redis/features/restapi)
- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat)
