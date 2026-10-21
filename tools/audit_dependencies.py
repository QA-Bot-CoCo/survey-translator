"""Audit every pinned runtime dependency without importing it."""
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

queries = [
    {"package": {"name": name, "ecosystem": "PyPI"}, "version": version}
    for name, version in re.findall(
        r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", Path("requirements.txt").read_text(), re.MULTILINE
    )
]
if not queries:
    raise SystemExit("No locked packages found.")
request = Request(
    "https://api.osv.dev/v1/querybatch",
    data=json.dumps({"queries": queries}).encode(),
    headers={"Content-Type": "application/json"},
)
with urlopen(request, timeout=30) as response:
    results = json.load(response)["results"]
findings = [
    {"package": query["package"]["name"], "version": query["version"],
     "advisories": [v["id"] for v in result.get("vulns", [])]}
    for query, result in zip(queries, results) if result.get("vulns")
]
print(json.dumps({"packages_checked": len(queries), "findings": findings}, indent=2))
raise SystemExit(1 if findings else 0)
