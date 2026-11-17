"""Print a compact Morning Report from GET /api/report/latest (JSON on stdin)."""
import json
import sys

api = sys.argv[1]
r = json.load(sys.stdin)
run = r["run"]
print(f"run #{run['id']} {run['status']}  readiness={run['readiness']}")
for f in r["findings"]:
    print(f"  [{f['status']}] {f['title']}: {f['summary']}  (cites {', '.join(f['source_ids'])})")
for p in r["repairs"]:
    link = f"  {api}{p['artifact_url']}" if p["artifact_url"] else ""
    print(f"  repair [{p['status']}] {p['title']}{link}")
mem = (r.get("cognee") or {}).get("memory_snippets") or []
if mem:
    print("  cognee memory (hypothesis):", mem[0].replace("\n", " ")[:200])
