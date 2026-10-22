# Survey translations

A plain static page with four small, stateless Python serverless functions.
Choose a brand, optionally replace its glossary CSV, upload an XLSX survey,
select languages and download all translated workbooks in one ZIP.

## How it works

- `public/` contains HTML, CSS and vanilla JavaScript. There is no frontend build.
- Each brand's uploaded glossary CSV is validated, then saved in browser
  `localStorage`. Uploading another CSV replaces that brand's override.
  **Use bundled glossary** removes the override.
- The original workbook and translation progress stay in the open page's memory.
  Pause/resume works in that tab. Reloading or closing the page clears the job,
  while glossary updates remain.
- Functions process requests without Redis, a database, accounts or server-side
  job storage. The OpenAI API key is held only on the server.
- One XLSX is generated per language, then all files are returned in a ZIP.
  Content and Messages are translated across their respective sheets; source
  cells and openpyxl-supported formatting are preserved.

The included brands are Gaminator, GameTwist, Slotpark, DCS and BBC. Enabled
languages are defined in `public/brands.json`; prompts and bundled glossaries
remain in `assets/<brand>/`.

## Glossary CSV

Use UTF-8 CSV with semicolon, comma or tab separators. Headers are language
codes matching the brand, for example:

```csv
enUS;deDE
Free spins;Freispiele
Coins;Coins
```

The source header is `enUS` for Gaminator/DCS and `enGB` for the other brands.
Include at least one enabled target language. A custom CSV replaces the entire
bundled glossary for that brand. Targets absent from the CSV translate with the
brand prompt and no glossary terms. Quoted fields, line breaks and a UTF-8 BOM
are supported. Limits: 256 KiB, 5,000 rows, 2,000 characters per term.

Glossaries are local to this browser and site origin; they are not synced to
other users or devices. Clearing site data removes them. CSV validation and
translation send the selected CSV to the server; translation includes its
relevant terms in the OpenAI request. No glossary override is persisted by the
server.

## Serverless endpoints

| POST endpoint | Input | Output |
| --- | --- | --- |
| `/api/glossary` | Brand and CSV as JSON | Validated row count and languages |
| `/api/prepare` | Workbook, brand and targets as multipart | Bounded text batches |
| `/api/translate` | One batch, brand, target and optional CSV as JSON | Translations by cell ID |
| `/api/zip` | Original workbook, brand, targets and translations as multipart | ZIP of translated XLSX files |

Each file in `api/` is a separate Vercel Function. Shared request validation and
workbook code live in `survey/`. Translation runs sequentially in batches of up
to 12 cells / 6,000 characters. A failed batch can be retried without discarding
completed batches; an ambiguous network failure can repeat a provider charge.
ZIP download retries do not translate again.

## Deploy on Vercel

1. Import this repository into a Vercel project. Use the **Other** framework
   preset, no build command, and `public` as the output directory.
   `vercel.json` configures this and the four functions.
2. Set `OPENAI_API_KEY` in the project's server environment. Optionally set
   `OPENAI_MODEL` (default: `gpt-6-luna`). These are the only translation settings.
3. Use Vercel deployment protection for team access and configure provider
   spending limits. This version has no application login or distributed quotas;
   anyone with deployment access can call its translation function.
4. Deploy and verify one real translation and ZIP download.

`APP_ORIGIN` is optional. Set it only to restrict requests to one canonical
origin; leaving it unset allows each preview deployment to use its own origin.
Requests must carry the page's Origin and custom request header. This check
prevents cross-origin browser requests; it does not authenticate callers.

The static page loads without an API key. Translation reports a configuration
error until the server key is set. API data is sent with no-store response
headers. Source workbooks are limited to 2 MiB, translation JSON to 2 MiB,
the complete request to 4 MiB and the ZIP response to 4 MiB. For larger output,
translate fewer languages or split the input workbook.

Source formulas, external links, embedded objects, macros and data connections
are rejected. Translated strings starting with `=` are stored as text.
Specialist Excel features unsupported by openpyxl may not survive a round trip.

## Local development

Use Python 3.12 (Python 3.11 also works for local tests) and Node 22+ for the
JavaScript tests.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# Set OPENAI_API_KEY in your shell when testing real translations.
.\.venv\Scripts\python.exe -m flask --app dev run --host 127.0.0.1
```

Open http://127.0.0.1:5000. The development server serves the same static files
and API routes. Environment files are not loaded automatically.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
node --check public/app.js
node --test tests/browser-state.test.mjs
python tools/audit_dependencies.py
```

Tests use synthetic workbooks and a fake translator, with no paid API calls.
CI runs the Python and JavaScript checks plus a dependency advisory audit.
The runtime dependencies retain pinned wheel hashes. To update them, resolve
`requirements.in` to a pip report and run `tools/lock_dependencies.py`.

Verified locally: 38 Python tests, four JavaScript tests, desktop/mobile browser
flows (CSV persistence, reset, failure recovery and ZIP download), and an advisory
audit of all 15 runtime dependencies. Live OpenAI calls and Vercel deployment
have not been verified.

Deployment routing follows Vercel's
[static project configuration](https://vercel.com/docs/project-configuration/vercel-json)
and [Python file-based functions](https://vercel.com/docs/functions/runtimes/python/api-directory).
