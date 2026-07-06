---
name: verify
description: How to verify changes to this repo's launch-your-agent skill and its run-viewer template by driving them end-to-end.
---

# Verifying changes in this repo

The runtime surface is the **run viewer** (`references/run-viewer-template.py`) plus the skill's docs (docs-only edits have no runtime surface — skip those).

## Drive the viewer

1. A live test kit usually exists under `test-runs/run-NN/my-agent/` (gitignored) with `IDS.env` pointing at real CMA objects. Copy the template over its `viewer.py`, re-apply the kit's CONFIG block (only the CONFIG dict changes per agent), then:
   ```bash
   pkill -f "python3 viewer.py"; cd test-runs/run-01/my-agent && nohup python3 viewer.py &
   ```
   Auth rides the ladder automatically (shell env key → ./.env → ant CLI token from `~/.config/anthropic/credentials/default.json`; run `ant auth login` if none).
2. Happy path over HTTP: `GET /` (page must contain `const CFG={...}` JSON), `GET /api/sessions`, `GET /api/trace/$SESSION_ID` twice (second call should be measurably faster — the trace cache), then open `http://127.0.0.1:<port>` in a browser and click a run: Flow animates, grader node lists the rubric criteria, Report tab renders the deliverable.
3. Standard probes: foreign `Host:` header → 403; `Origin: https://evil…` → 403; `POST /api/run` without `X-Viewer-Csrf: 1` → 403; bogus session/file ids → 502 JSON (not a crash); hostile CONFIG values (apostrophes, `</script>`) must not break the served page (exec the module with a mutated CONFIG and check `const CFG=` still parses as JSON).

## Gotchas

- api.anthropic.com is NOT on the local Bash sandbox's network allowlist — run launch/viewer commands with sandbox disabled.
- macOS `open` fails inside the sandbox too (procNotFound).
- `POST /api/run` costs real money (creates a session in the account) — probe the guard with the 403 paths; only fire a legit run when a fresh run is actually wanted.
- The skill is mirrored at `~/.claude/skills/launch-your-agent/` — re-sync after edits (repo copy is the source of truth).
