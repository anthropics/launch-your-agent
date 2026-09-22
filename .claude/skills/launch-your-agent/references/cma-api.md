<!-- Copyright 2026 Anthropic PBC -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# CMA API — verified command shapes

> Source: platform.claude.com/docs/en/managed-agents/* (read 2026-06-14; refreshed against the live docs 2026-09-21 — shapes added in that refresh come from the docs and have not been run yet). If a call fails on a field name, the **live docs win** — check, adapt, retry once.
> Two equivalent surfaces: `ant` CLI (`brew install anthropics/tap/ant`) and curl. The Python/TS SDKs mirror these exactly (`client.beta.<resource>.<verb>`).

## Setup (every terminal)

Make getting the key in as low-friction as possible. **First check whether `ANTHROPIC_API_KEY` is already set in the shell env** — many founders will already have it exported, or will happily set it that way (`export ANTHROPIC_API_KEY=…` in their own terminal); if it's there, skip the ask entirely and copy it into `.env` without printing it. Otherwise pre-create `./my-agent/.env` with the Write tool (content: a single `ANTHROPIC_API_KEY=` placeholder line — a shell `> .env` redirect is typically permission-blocked), `chmod 600` it, add it to `.gitignore`, and have the founder paste the key into the file via its **absolute path** (their terminal's cwd isn't yours). The only hard rule is that the key never lands in the chat or an exported transcript — if it does, tell them to rotate it.

```bash
set -a; source .env; set +a      # ANTHROPIC_API_KEY=sk-ant-...  (founder-created at platform.claude.com → API keys)
BASE=https://api.anthropic.com/v1
H=(-H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" \
   -H "anthropic-beta: managed-agents-2026-04-01" -H "content-type: application/json")
```

When the founder creates the key, have them note **which workspace it belongs to** — every object created with it lands in that workspace, and the Console only shows the workspace currently selected (this is the answer to "I can't see it in the Console").

Save every returned id into `IDS.env` immediately (`echo "AGENT_ID=..." >> IDS.env`); load it with `set -a; source IDS.env; set +a` so child processes (python parsers, scripts) see the variables, and make every launch step read `IDS.env` first and skip objects that already exist, so a failed step can be re-run without creating duplicates. Pattern for readable errors: `-o /tmp/resp.json -w '%{http_code}\n'`. **Parse responses with python, not jq** — session payloads embed the agent's `system` prompt with literal newlines/control characters that jq rejects:

```bash
python3 -c "import json,sys; d=json.JSONDecoder(strict=False).decode(open('/tmp/resp.json').read()); print(d['id'])"
```

## Pick a model

```bash
curl -sS "$BASE/models" "${H[@]:0:4}" | jq -r '.data[].id'
```
Default to the newest Opus-class. Reach for Sonnet-class when speed or run cost matters more for the use case — e.g. high-frequency runs, latency-sensitive paths, or the founder asks for cheaper/faster. Fast mode: pass model as `{"id":"claude-opus-5-5","speed":"fast"}` (Opus 5.5, Opus 5 and Opus 4.8 only). The same object form also takes `effort` (`low`…`max`) and `inference_geo` (`"us"` | `"global"`, Claude 4.6 and later models).

## 1. Agent (versioned; create once)

```bash
# curl
curl -sS --fail-with-body "$BASE/agents" "${H[@]}" -d @agent.json
# ant
ant beta:agents create --name "..." --model '{id: claude-opus-5-5}' \
  --system "..." --tool '{type: agent_toolset_20260401}'
```
`agent.json` fields: `name`*, `model`*, `system`, `tools`, `mcp_servers`, `skills`, `multiagent`, `description`, `metadata`.
Response: save `id` AND `version` (starts at 1).

**Update** (new version; `version` is optional — pass the current one as a concurrency guard, 409 on mismatch, or omit it for last-write-wins; arrays are FULL replacement, omitted fields preserved):
```bash
curl -sS --fail-with-body "$BASE/agents/$AGENT_ID" "${H[@]}" \
  -d '{"version": '$AGENT_VERSION', "system": "...new instructions..."}'
ant beta:agents update --agent-id "$AGENT_ID" --version "$AGENT_VERSION" --system "..."
```
Then bump `AGENT_VERSION` in IDS.env to the returned version.
Versions list: `GET $BASE/agents/$AGENT_ID/versions` · Archive: `POST $BASE/agents/$AGENT_ID/archive`.

### Tool config patterns

```jsonc
"tools": [
  {"type": "agent_toolset_20260401"},                                  // all 8 tools, always_allow
  {"type": "agent_toolset_20260401",                                   // gate bash behind approval
   "default_config": {"permission_policy": {"type": "always_allow"}},
   "configs": [{"name": "bash", "permission_policy": {"type": "always_ask"}}]},
  {"type": "agent_toolset_20260401",                                   // limit what the web tools can reach
   "configs": [{"type": "web_search", "name": "web_search", "allowed_domains": ["example.com"]},   // allowed_domains OR blocked_domains per entry,
               {"type": "web_fetch",  "name": "web_fetch",  "allowed_domains": ["example.com"]}]}, // plain hostnames; covers subdomains
  {"type": "mcp_toolset", "mcp_server_name": "linear",                 // MCP tools (default always_ask)
   "default_config": {"permission_policy": {"type": "always_allow"}}},
  {"type": "custom", "name": "get_weather", "description": "...",      // client-executed
   "input_schema": {"type": "object", "properties": {...}, "required": [...]}}
]
"mcp_servers": [{"type": "url", "name": "linear", "url": "https://mcp.linear.app/mcp"}]
"skills": [{"type": "anthropic", "skill_id": "xlsx"},                  // xlsx | docx | pptx | pdf
           {"type": "custom", "skill_id": "skill_abc123", "version": "latest"}]
"multiagent": {"type": "coordinator", "agents": [{"type": "agent", "id": "agent_..."}]}
```
Built-in tool names: `bash read write edit glob grep web_fetch web_search`. Disable one: `configs:[{"name":"web_fetch","enabled":false}]`.
Permission policy types: `always_allow` · `always_ask` · `auto` (the server evaluates each call and runs it, denies it, or pauses for approval — not a human checkpoint; keep `always_ask` on anything a person must review).

## 2. Environment (reusable; not versioned)

```bash
curl -sS --fail-with-body "$BASE/environments" "${H[@]}" -d '{
  "name": "<name>-env",
  "config": {"type": "cloud", "networking": {"type": "unrestricted"},
             "packages": {"pip": ["pandas"], "npm": []}}}'
ant beta:environments create --name "<name>-env" --config '{type: cloud, networking: {type: unrestricted}}'
```
Limited networking: `{"type":"limited","allowed_hosts":["api.example.com","*.example.com"],"allow_mcp_servers":true,"allow_package_managers":true}` (booleans default false; hostnames only, no scheme). If the environment sets `packages`, `limited` networking needs `"allow_package_managers":true` or the create is a 400.
Package managers: `apt cargo gem go npm pip` (cached across sessions). Names unique per workspace.
List/retrieve/archive/delete: standard verbs on `$BASE/environments[/:id][/archive]` (delete only if no sessions reference it).

## 3. Session (one run; sandbox provisioned at create, work starts on first event)

```bash
curl -sS --fail-with-body "$BASE/sessions" "${H[@]}" -d '{
  "agent": "'$AGENT_ID'",                         // string = latest version
  "environment_id": "'$ENV_ID'",
  "title": "first run",
  "vault_ids": [],                                 // optional
  "resources": []                                  // optional: memory_store / file / github_repository
}'
ant beta:sessions create --agent "$AGENT_ID" --environment-id "$ENV_ID" --title "first run"
```
Pin a version: `"agent": {"type":"agent","id":"...","version":1}`.
Attach memory: `"resources":[{"type":"memory_store","memory_store_id":"memstore_...","access":"read_write","instructions":"..."}]` (creation-time only).

Optional on the same body: `initial_events` (up to 50 `user.message` / `user.define_outcome` events — the session is created already `running`, no separate send), and `agent` as `{"type":"agent_with_overrides","id":"...","model":{...}}` to swap `model`/`system`/`tools`/`mcp_servers`/`skills` for one session without versioning the agent.

Statuses: `idle` (waiting — sessions created without `initial_events` start here; a run that finishes returns here) · `running` · `rescheduling` · `terminated` (unrecoverable error, or archived).
Retrieve: `GET $BASE/sessions/$SESSION_ID` (read `.status`, `.usage`, `.outcome_evaluations[]`).
List: `GET $BASE/sessions?agent_id=$AGENT_ID` · Archive: `POST .../archive` (not while running — interrupt first) · Delete: `DELETE $BASE/sessions/$SESSION_ID`.
Mid-session you can update a session's `agent.tools`/`agent.mcp_servers` (session-local, full replacement, must be idle): `POST $BASE/sessions/$SESSION_ID` with `{"agent":{"tools":[...],"mcp_servers":[...]}}`.

## 4. Kickoff events

**Outcome kickoff (default)** — task + definition of done + retry budget in one move; agent starts immediately:
```bash
EVT=$(jq -n --rawfile task first_prompt.txt --rawfile rubric outcome.md \
  '{type:"user.define_outcome", description:$task,
    rubric:{type:"text", content:$rubric}, max_iterations:3}')      # default 3, max 20
curl -sS --fail-with-body "$BASE/sessions/$SESSION_ID/events" "${H[@]}" -d "{\"events\":[$EVT]}"
ant beta:sessions:events send --session-id "$SESSION_ID" <<YAML
events:
  - type: user.define_outcome
    description: <task>
    rubric: {type: text, content: "<rubric markdown>"}
    max_iterations: 3
YAML
```
File rubric instead: `"rubric":{"type":"file","file_id":"file_..."}` (upload it through the Files API first, `POST $BASE/files` — out of beta, no extra beta header — and pass the file id).

**Plain message kickoff / steering / resume** (steering mid-run is allowed; agent picks it up at the next turn boundary):
```bash
curl -sS --fail-with-body "$BASE/sessions/$SESSION_ID/events" "${H[@]}" \
  -d '{"events":[{"type":"user.message","content":[{"type":"text","text":"..."}]}]}'
```

**Interrupt + redirect:** send `{"type":"user.interrupt"}` then a `user.message` in the same `events` array.
**Tool confirmation** (when status_idle has `stop_reason.type == "requires_action"`):
`{"type":"user.tool_confirmation","tool_use_id":"<event id>","result":"allow"|"deny","deny_message":"..."}`.
**Custom tool result:** `{"type":"user.custom_tool_result","custom_tool_use_id":"<id>","content":[{"type":"text","text":"..."}]}`.

## 5. Watch the run

```bash
# SSE stream — open BEFORE sending events; only post-open events are delivered. -N = don't buffer.
curl -sS -N --fail-with-body "$BASE/sessions/$SESSION_ID/events/stream" "${H[@]}" -H "accept: text/event-stream"
# Poll instead (python, NOT jq — the embedded system prompt breaks jq):
curl -sS "$BASE/sessions/$SESSION_ID" "${H[@]}" -o /tmp/sess.json
python3 -c "import json; d=json.JSONDecoder(strict=False).decode(open('/tmp/sess.json').read()); print(d['status'], [e.get('result') for e in d.get('outcome_evaluations',[])])"
# Full history (filterable): GET $BASE/sessions/$SESSION_ID/events?types[]=agent.tool_use
```
Run the first poll in the foreground and confirm it parses before putting the loop in a background task — a poller that errors silently every iteration looks like "still running" for ten minutes.
Narrate: `agent.message` (text), `agent.tool_use` (name), `span.outcome_evaluation_end` (`result` + `explanation`), `session.status_idle` (`stop_reason`: `end_turn` = done, `requires_action` = answer it, `budget_reached` = the session hit its budget — raise or remove the budget to resume), `session.error`.
Outcome results: `satisfied` · `needs_revision` · `max_iterations_reached` · `failed` · `interrupted`.
Runs of 8–10+ minutes are normal — say so. Also hand the founder the Console sessions view for the key's workspace so they can watch it there.
Optional, founder-run: `ant beta:sessions connect $SESSION_ID` (`ant` 1.32.0 or later) follows the run live in **their own terminal**, where they can also send messages and answer tool confirmations; `--web` opens the Console session viewer locally instead. It needs an interactive terminal, so it is not a command to run from here — scripts keep using the stream/poll calls above. Their terminal needs the same key exported, or an `ant auth login` into the key's workspace.

## 6. Outputs

Agent writes to `/mnt/session/outputs/`. **List files only after the session's `status` is `idle`** — output files are re-finalized under a new `file_id` when the run settles, so an ID captured mid-run can 404 on `/content`. If a download 404s, re-list; the file is still there under a fresh ID.
```bash
curl -sS "$BASE/files?scope_id=$SESSION_ID" "${H[@]}" | jq -r '.data[] | "\(.id) \(.filename)"'
curl -sS "$BASE/files/$FILE_ID/content" "${H[@]}" -o output.md
ant beta:files list --scope-id "$SESSION_ID" --beta managed-agents-2026-04-01 ; ant files download --file-id "$FILE_ID" --output output.md
```

## 7. Memory store (optional)

```bash
curl -sS "$BASE/memory_stores" "${H[@]}" -d '{"name":"<name>-memory","description":"<what it holds — shown to the agent>"}'
# seed: POST $BASE/memory_stores/$MEMSTORE_ID/memories  {"path":"/context.md","content":"..."}
# attach: session/deployment "resources" array (see §3). Mounted under /mnt/memory/<store-name-slug>/ (exact path: mount_path on the resource).
```
Beta header: the docs' memory-store examples now send `anthropic-beta: agent-memory-2026-07-22` on `/v1/memory_stores…` calls **in place of** `managed-agents-2026-04-01` (the SDKs and `ant` do this for you; sending both on one call is a 400). If a raw memory-store call here is rejected on its beta header, swap that one value for that call — replace, never append. Attaching a store is a session call and keeps `managed-agents-2026-04-01`.
Limits: 100kB/memory, 10,000 memories/store, 8 stores/session. Access is `read_write` by default — the agent can update the store across runs (the usual choice for state that accumulates); use `read_only` only for reference material the agent shouldn't modify.

## 8. Vault + credential (optional; only when a credential is in hand)

```bash
VAULT_ID=$(curl -sS "$BASE/vaults" "${H[@]}" -d '{"display_name":"<founder name>"}' | jq -r .id)
# MCP bearer:   {"display_name":"Linear key","auth":{"type":"static_bearer","mcp_server_url":"https://mcp.linear.app/mcp","token":"..."}}
# MCP OAuth:    {"auth":{"type":"mcp_oauth","mcp_server_url":"...","access_token":"...","expires_at":"...","refresh":{...}}}
# Env var:      {"auth":{"type":"environment_variable","secret_name":"NOTION_API_KEY","secret_value":"...",
#                "networking":{"type":"limited","allowed_hosts":["api.notion.com"]}}}
curl -sS "$BASE/vaults/$VAULT_ID/credentials" "${H[@]}" -d @cred.json
# use: pass "vault_ids": ["'$VAULT_ID'"] at session/deployment creation
```
Secrets are write-only. Credential `mcp_server_url` must point at the same server as the agent's `mcp_servers[].url` — the two are normalized before matching, so host case, a default port or a trailing slash don't matter, but a different path, subdomain or non-default port does (e.g. `…/sse` vs `…/mcp` will not match).

## 9. Scheduled deployment (when the use case is recurring)

Only the right finale when the agent's job actually repeats on a clock — a weekly digest, a nightly sync, a market-hours poll. If the use case is on-demand or event-driven, skip this and the launch script / webhook trigger is the close instead.

```bash
curl -sS --fail-with-body "$BASE/deployments?beta=true" "${H[@]}" -d '{
  "name": "<human name>",
  "agent": "'$AGENT_ID'",
  "environment_id": "'$ENV_ID'",
  "initial_events": [ <the same kickoff event(s) as §4> ],
  "schedule": {"type": "cron", "expression": "0 7 * * 1", "timezone": "America/Los_Angeles"}
}'
ant beta:deployments create <<YAML ... YAML
```
**`initial_events` are replayed verbatim every run** — the kickoff text must use relative dates ("today", "the last N days as of this run"), never a literal date; re-read it for hard-coded dates before sending.
Response includes `schedule.upcoming_runs_at` — read it back to the founder to confirm.
Optional on the same body: `vault_ids`, memory/file/GitHub `resources`, `budget` (same shape as a session budget — see Cost & cleanup; applied to each run separately).
**Test now:** `curl -X POST -d '{}' "$BASE/deployments/$DEPLOYMENT_ID/run?beta=true" "${H[@]}"` (manual run, creates a session immediately — the explicit `-X POST` matters; a bodyless curl defaults to GET and 405s).
Runs history: `GET $BASE/deployment_runs?deployment_id=...&has_error=true`.
Pause / unpause / archive: `POST $BASE/deployments/$DEPLOYMENT_ID/{pause|unpause|archive}?beta=true`.
Cron = 5-field POSIX, minute granularity, IANA timezone, wall-clock DST. Limit 1,000/org.

## Cost & cleanup

- Session `usage` (input/output/cache tokens, plus `list_cost` in whole cents and `active_seconds`) on `GET /sessions/:id` — read it after each run.
- Optional hard cap per session: add `"budget":{"type":"limit","max_list_cost":{"amount":"200","currency":"USD"}}` to the session-create body (`amount` = whole US cents as a string, so `"200"` = $2.00; creation-time only). At the cap the session idles with `stop_reason: budget_reached`; resume by raising it — `POST $BASE/sessions/$SESSION_ID` with a higher `max_list_cost` — or removing it with `{"budget": null}` (one-way). Priced at public list rates; can overshoot by the request in flight.
- Rate limits: creates 300/min, reads 1,200/min per org.
- End of session hygiene: archive sessions you're done with; never leave one `running`.

## Console links (paste these proactively whenever something is ready to review)

Deep links work — link the specific object that just became reviewable, not only the list page:

- Agent: `https://platform.claude.com/workspaces/<workspace>/agents/<AGENT_ID>` (confirmed in use)
- Deployments list (+ cron builder): `https://platform.claude.com/workspaces/<workspace>/deployments` (documented); a created deployment: `…/deployments/<DEPLOYMENT_ID>` (same pattern — confirm on first click)
- Session: `…/workspaces/<workspace>/sessions/<SESSION_ID>` (same pattern — confirm on first click; fall back to the sessions list if it 404s)

The `<workspace>` segment: use `default` until known — it resolves when the key lives in the default workspace; otherwise add a one-time "switch workspaces with the picker (Settings → API Keys shows which workspace the key belongs to)". Once the founder pastes any Console URL, lift the workspace ID from it and use it in every link after.

## Troubleshooting quick hits

- **401/403** → key not in `.env` / typo'd / billing issue. Fix `.env`, re-`source`, retry once.
- **400 on agent create ("tools")** → built-in tools were listed individually. Use the toolset wrapper: `"tools": [{"type": "agent_toolset_20260401"}]` (with `configs` overrides if needed) — never `{"type":"bash"}` etc.
- **404 on a path** → check the live docs for the exact resource path before assuming the feature doesn't exist (e.g. deployments live at `/v1/deployments`, docs page is `scheduled-deployments`).
- **405 on deployment manual run** → the curl defaulted to GET; use an explicit `-X POST` with a `{}` body.
- **jq: "control characters … not allowed"** → the response embeds the agent `system` prompt with literal newlines. Parse with `python3` + `json.JSONDecoder(strict=False)` instead of jq.
- **Stream looks hung** → you forgot `-N`, or the run is just long (8–10 min is normal). Poll `.status` instead.
- **`requires_action`** → the agent is waiting on YOU: a tool confirmation or a custom tool result. Read the last events and answer.
- **"I can't see it in the Console"** → the Console only shows the currently selected workspace; the objects live in the workspace the API key belongs to. Settings → API Keys → find the key → switch to that workspace → Managed Agents.
- **Vault MCP auth fails** → credential `mcp_server_url` doesn't point at the same server as the agent's `mcp_servers[].url` (different path, subdomain or non-default port; case and trailing slash are ignored).
- **Update rejected (version conflict)** → re-fetch the agent, retry with the current `version`.
