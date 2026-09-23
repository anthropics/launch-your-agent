<!-- Copyright 2026 Anthropic PBC -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Claude Managed Agents — Features & Primitives Inventory

> Source: full read of https://platform.claude.com/docs/en/managed-agents/* on 2026-06-14; refreshed against the live docs and the public release notes on 2026-09-21.
> Truth order if anything here drifts: **live public docs win** over this file.
> This is the design substrate for the "launch your agent in ~60 min" skill.

## What CMA is

A **hosted agent harness**. You define an agent (model, prompt, tools); Anthropic runs the agent loop, tool execution, and a **sandboxed Linux container** server-side. The alternative is the Messages API (you build your own loop). CMA is for **long-running, asynchronous, stateful** work.

- **Beta:** every request needs header `anthropic-beta: managed-agents-2026-04-01` (SDK sets it automatically). Enabled by default for all API accounts. On the `/v1/memory_stores…` endpoints the docs now send `agent-memory-2026-07-22` in its place — never both on one call, that is a 400 (see Memory stores).
- **API base:** `https://api.anthropic.com/v1` + `-H "anthropic-version: 2023-06-01"`. Many examples also append `?beta=true` on the URL.
- **Two surfaces:** `ant` CLI (`brew install anthropics/tap/ant`, or release binary, or `go install`) and curl/SDK (Python, TS, Java, Go, C#, Ruby, PHP). Plus a **Console** UI (platform.claude.com → Managed Agents) with the session list and a session viewer: timeline minimap, transcript grouped by model request, and an Inspector panel (Session · Events · Tools · Resources · Threads tabs, incl. cost and spend against a budget). The session viewer is for Developer and Admin roles only.
- **CLI extras:** `ant beta:sessions connect <session_id>` (`ant` ≥ 1.32.0) attaches an interactive terminal to a session — follow it live, send messages, interrupt, allow/deny waiting tool calls; `--web` opens the Console session viewer from a local server instead. Needs a real terminal, so scripts use `ant beta:sessions:events stream` / `send`. `ant apply` (`ant` ≥ 1.30.0) creates and updates agents, environments, skills, memory stores and deployments from files in a repo and records their IDs in a `claude-lock.json` lockfile you commit.
- **Not eligible** for Zero Data Retention or HIPAA BAA (stateful by design). You can delete sessions/files anytime.
- **In-Claude-Code helper:** `/claude-api managed-agents-onboard` gives a guided setup.

---

## The 4 core primitives

| Primitive | What it is | ID prefix |
|---|---|---|
| **Agent** | Reusable, **versioned** config: model + system prompt + tools + MCP servers + skills (+ multiagent roster) | `agent_…` |
| **Environment** | Where sessions run: Anthropic cloud sandbox, or self-hosted. **Not versioned.** | (env id) |
| **Session** | One running agent instance in an environment; holds conversation history + sandbox state | `sesn_…` |
| **Events** | Bi-directional messages: you send `user.*`/`system.*`; you receive `agent.*`/`session.*`/`span.*` | (event ids) |

Minimal launch = create agent → create environment → create session → send a user event (or define an outcome). The agent provisions a sandbox, runs the loop, executes tools, streams events, and emits `session.status_idle` when done.

---

## Agent

**Config fields:** `name`* , `model`* , `system`, `tools`, `mcp_servers`, `skills`, `multiagent`, `description`, `metadata`. (*required)

- **model:** any Claude 4.5-family or later. String form `"claude-opus-5-5"`, or object form `{"id":"claude-opus-5-5", …}` with optional `speed` (`"fast"` for fast mode — Opus 5.5, Opus 5 and Opus 4.8 only), `effort` (`low` | `medium` | `high` | `xhigh` | `max`) and `inference_geo` (`"us"` | `"global"`). Response stores the object with defaults filled in (`speed: standard`, the model's default `effort`).
- **`inference_geo`** pins where **model inference** runs for sessions on this agent (Claude 4.6 and later models only; 400 on older ones; `"us"` is priced at 1.1×; unset = the workspace's default geo). It does not move where data is stored or where the sandbox runs. Must be allowed by the workspace's `allowed_inference_geos`; in a multiagent roster every pin must match or all be unset. Can also be set per session through an `agent_with_overrides` `model` override.
- **Versioning:** create once, reference by ID forever. Each config-changing update mints a **new version** (`version` starts at 1, increments). No-op updates return the existing version.
- **Update semantics:** `version` is optional — pass the current one as a concurrency guard (mismatch → 409), or omit it to apply unconditionally (last write wins). Omitted fields preserved. Scalars replaced (`system`/`description` clearable with `null`; `model`/`name` mandatory). **Array fields `tools`/`mcp_servers`/`skills` are FULL replacement** — omit to preserve, `null`/`[]` to clear. `metadata` merges per-key (set a key to `null` to delete it).
- **Lifecycle:** Update (new version) · List versions (full history) · Archive (read-only; existing sessions keep running, new sessions can't reference). 
- **Sessions can pin a version**: pass `agent` as string = latest; as `{"type":"agent","id":…,"version":N}` = pinned; as `{"type":"agent_with_overrides","id":…}` plus any of `model`/`system`/`tools`/`mcp_servers`/`skills` = per-session overrides (each field you pass replaces the agent's in full; the agent itself and its version are unchanged).

---

## Environment

Created once, referenced by many sessions; **each session gets its own fresh isolated container** (no shared FS). Not versioned (log your own changes if you mutate them).

- **`config.type`:** `cloud` (Anthropic-managed) or `self_hosted` (your infra, via `ant beta:worker`).
- **`config.packages`:** pre-install + cache across sessions. Managers: `apt`, `cargo`, `gem`, `go`, `npm`, `pip` (run alphabetically). Version pinning supported (`pandas==2.2.0`, `express@4.18.0`, etc.). With `limited` networking, `packages` requires `allow_package_managers: true` — otherwise the request is a 400.
- **`config.networking`:**
  - `unrestricted` (default) — full outbound minus a safety blocklist.
  - `limited` — only `allowed_hosts` (bare hostnames / `*.wildcard`, no scheme) + booleans `allow_mcp_servers` (default false) and `allow_package_managers` (default false). Recommended for production (least privilege).
  - Networking does NOT affect the `web_search`/`web_fetch` tools — they run on Anthropic's servers, not in the sandbox. Restrict those with per-tool `allowed_domains` / `blocked_domains` (see Tools).
- **Pre-installed runtimes** out of the box (langs, DBs, utilities) — see Sandbox reference page.
- **Lifecycle:** list · retrieve · archive (read-only; running sessions continue) · delete (only if no sessions reference it). `name` must be unique per org+workspace.

---

## Session

Two-step: **create** (provisions sandbox, starts `idle`) → **send event** to start work. Or one call: pass `initial_events` on create (up to 50 `user.message` / `user.define_outcome` events) and the session is created already `running`. Acts as a state machine; events drive execution.

- **Create fields:** `agent` (id string, pinned object, or overrides object), `environment_id`, `title`, `vault_ids[]`, `resources[]`, `initial_events[]`, `budget`.
- **`resources[]`** can attach: **`memory_store`**, **`file`**, and **`github_repository`** resources. (Memory stores and repositories attach only at creation — a repository's token can be rotated later, but to mount a different repo you start a new session; file resources can be added/removed on a running session.)
- **Statuses:** `idle` (waiting for input/tool confirmation; sessions created without `initial_events` start here) · `running` · `rescheduling` (transient retry) · `terminated` (unrecoverable error, or the session was archived — a session that simply finishes its work goes `idle`, not `terminated`).
- **Mid-session agent update:** can update a session's `agent.tools` and `agent.mcp_servers` (incl. permission policies and the web tools' domain lists) **without a new agent version** — session-local, full-replacement, session must be `idle` (interrupt first if running).
- **Checkpointing & resume:** on idle, sandbox is checkpointed (FS, installed packages, files). Resume by sending a `user.message`. **Sandbox state is kept 30 days from when the sandbox was created** — activity does not extend the window (history is kept until deleted). After that a resumed session starts from a fresh sandbox, so have the agent write anything that matters to outputs.
- **`usage` field:** cumulative `input_tokens`, `output_tokens`, `cache_read_input_tokens`, a `cache_creation` breakdown (5-min / 1-hour entries), `list_cost` (whole US cents, priced at public list rates), `active_seconds`, `server_tool_use` counts. 5-min cache TTL by default. The same snapshot is emitted as a `session.usage` event right before every idle. Use for cost tracking; to enforce a cap use a session budget (below).
- **Budget (per-session spend cap):** optional `budget: {"type":"limit","max_list_cost":{"amount":"2500","currency":"USD"}}` at session create only (`amount` = whole US cents as a string → $25.00; `USD` only). Priced at public **list cost** (tokens + web searches + running time, not your contracted price), checked between model requests — can overshoot by the request in flight. At the cap → `idle`, `stop_reason: budget_reached` (history + sandbox kept); raise it, or remove it with `null` (one-way), to resume. One cap shared by all multiagent threads; deployments copy theirs onto each run.
- **Ops:** retrieve · list (filter `agent_id`) · update · archive (preserves history, blocks new events; can't archive `running`) · delete (removes record+events+sandbox; can't delete `running`). Memory stores/vaults/skills/environments/agents and files you uploaded survive session deletion; files the session itself produced are scoped to it and are deleted with it — download what you need first.
- **Deliverables convention:** agent writes outputs to `/mnt/session/outputs/`; fetch via Files API scoped `?scope_id=<session_id>`.

---

## Events

`{domain}.{action}` naming. Every event has `processed_at` (null = queued).

**User events (you send):** `user.message` · `user.interrupt` (stop mid-execution; can pair with a follow-up message) · `user.custom_tool_result` · `user.tool_confirmation` (allow/deny gated tools) · `user.define_outcome` · `user.tool_result` (self-hosted only).

**System event:** `system.message` — append system-level guidance mid-session (it adds to the session's system context; the agent's `system` field itself is fixed for the session). **Only on models that support mid-conversation system messages** — per the docs today: Claude Fable 5.1, Mythos 5.1, Fable 5, Mythos 5, Opus 5.5, Opus 5 and Opus 4.8; other primary models reject it. 1–1000 text items; while the session is waiting on `requires_action` it is only accepted trailing a tool result in the same request.

**Agent events (you receive):** `agent.message` · `agent.thinking` · `agent.tool_use` / `agent.tool_result` · `agent.mcp_tool_use` / `agent.mcp_tool_result` · `agent.custom_tool_use` · `agent.thread_context_compacted` (auto context compaction) · `agent.thread_message_received` / `agent.thread_message_sent` (multiagent).

**Session events:** `session.status_running` · `session.status_idle` (carries `stop_reason`: `end_turn`, `requires_action`, `budget_reached`, etc.) · `session.status_rescheduled` · `session.status_terminated` · `session.deleted` · `session.updated` · `session.error` (typed `error` + `retry_status`) · `session.usage` (cumulative usage + list cost snapshot, emitted right before every idle) · plus `session.thread_*` (multiagent).

**Span events (observability):** `span.model_request_start`/`_end` (with `model_usage` token counts) · `span.outcome_evaluation_start`/`_ongoing`/`_end`.

**Streaming:** SSE at `/v1/sessions/:id/events/stream?beta=true`. **Open stream BEFORE sending events** (only post-open events delivered). Reconnect pattern: open stream → list history to seed seen-IDs → tail live, skipping seen. List past events at `/v1/sessions/:id/events` (filter with `types[]=`). **Live text previews (opt-in):** add `event_deltas[]=agent.message` to the stream URL to get stream-only `event_start` / `event_delta` events while the model is still writing; best-effort and never persisted — the buffered `agent.message` that follows is the record.

**Steering:** send a `user.message` mid-run (agent may pick it up at next turn boundary). **Interrupt + redirect:** send `user.interrupt` then `user.message`.

---

## Tools

Enable the prebuilt toolset with `{"type":"agent_toolset_20260401"}`. Tools (all on by default): `bash`, `read`, `write`, `edit`, `glob`, `grep`, `web_fetch`, `web_search`. (Tool output over 100,000 characters — about 25k tokens — auto-spills to a sandbox file with a truncated preview.)

- **Disable specific:** `configs:[{"name":"web_fetch","enabled":false}]`.
- **Allow-list only:** `default_config:{"enabled":false}` + per-tool `configs[].enabled:true`.
- **Restrict the web tools' reach:** `allowed_domains` **or** `blocked_domains` (one or the other; 1–64 plain hostnames, subdomains covered) on the `web_search` / `web_fetch` entries in `configs`; each tool has its own list. Blocked `web_fetch` → error result `url_not_allowed`; `web_search` omits disallowed results. Same entries take `max_content_tokens` (`web_fetch`) and `user_location` (`web_search`). Environment networking and the Console's org-level web settings don't apply here.
- **Custom tools** (`{"type":"custom", name, description, input_schema}`): client-executed. Flow = `agent.custom_tool_use` → session idles `requires_action` → you run it → `user.custom_tool_result` (with `custom_tool_use_id`). Best practices: very detailed descriptions, consolidate related ops under one tool with an `action` param, namespace names, return high-signal output only.
- **MCP toolset:** `{"type":"mcp_toolset","mcp_server_name":"…"}` referencing an entry in the agent's `mcp_servers`.

---

## Outcomes (the "definition of done" loop)

Elevates a session from conversation → graded work. Send `user.define_outcome` after creating the session (no separate user.message needed — agent starts immediately), or pass it as the one `user.define_outcome` in the session's `initial_events` to create and start in a single call.

- **Fields:** `description` (the task), `rubric` (**required**), `max_iterations` (optional, **default 3, max 20**).
- **Rubric:** markdown with explicit per-criterion checks. Either inline `{"type":"text","content":"…"}` or `{"type":"file","file_id":"…"}` (upload it once through the Files API and reuse it across sessions).
- **Grader:** auto-provisioned, **separate context window** (isolated from the agent's choices); returns pass/fail explanation fed back for the next iteration.
- **Results** (`span.outcome_evaluation_end.result`): `satisfied` (→idle) · `needs_revision` (new cycle) · `max_iterations_reached` (one final acknowledgment turn, no further evaluation →idle) · `failed` (rubric fundamentally mismatched →idle) · `interrupted`.
- **Only one outcome at a time**, but chainable (send a new `user.define_outcome` after the prior terminal event). Outcome sessions still accept `user.message` steering. `user.interrupt` marks current eval `interrupted`.
- **Check status:** stream `span.outcome_evaluation_end`, or poll `GET /v1/sessions/:id` → `outcome_evaluations[].result`.

---

## Permission policies (safety rails)

Govern server-executed tools (agent toolset + MCP toolset). NOT custom tools (you gate those yourself).

- **Types:** `always_allow` (runs with no confirmation) · `always_ask` (pause → wait for your `user.tool_confirmation`) · `auto` (`{"type":"auto"}` — the server evaluates each call, looking at the tool, its input and the session so far, and either runs it, denies it — the agent gets an error tool result and your client cannot override it — or pauses for your approval exactly like `always_ask`). **`auto` is not a human checkpoint**: a call the server judges safe runs before anyone sees it; put `always_ask` on any tool a person must review.
- **Defaults:** agent toolset → `always_allow`; **MCP toolset → `always_ask`** (so new MCP tools can't run unapproved). No toolset uses `auto` unless you set it.
- **Set:** `default_config.permission_policy` on the toolset; override per-tool in `configs[].permission_policy` (e.g. allow everything but `always_ask` on `bash`).
- **Confirmation flow:** `agent.tool_use`/`agent.mcp_tool_use` → `session.status_idle` `requires_action` (ids in `stop_reason.event_ids`) → send `user.tool_confirmation` per id with `result:"allow"|"deny"` (+ optional `deny_message`). Same flow when an `auto` call comes back undetermined.
- **See what happened:** every `agent.tool_use` / `agent.mcp_tool_use` event carries `evaluated_permission` (`allow` | `ask` | `deny`) and usually an `evaluation` object naming the policy that produced it (under `auto`, also a `reason_code` such as `indeterminate` or `high_risk`). Only `ask` events accept a `user.tool_confirmation` — confirming anything else is a 400.

---

## Skills

Filesystem-based, on-demand expertise. Two routes in: the agent's `skills[]` array, or a mounted GitHub repo. `skills[]` entries: `type` (`anthropic` | `custom`), `skill_id`, `version` (optional for both types; pin or `latest`, the default).
- **Prebuilt Anthropic skills:** document tasks — `xlsx`, `docx`, `pptx`, `pdf` (use short name as `skill_id`).
- **Custom skills:** author + upload to workspace through the Skills API (`POST /v1/skills`, zip or files → `skill_*` id). The Skills API is out of beta — no beta header on `/v1/skills` itself; attaching skills to an agent is still a Managed Agents (beta) call.
- **Skills from a repo:** when a session mounts a repo as a `github_repository` resource, skills at exactly `.claude/skills/<name>/SKILL.md` in the repo root are discovered automatically at session start — no upload, no `skills[]` entry. Cloud sandboxes only; needs the `read` tool (on by default); scanned once at session start from the checked-out branch/commit. Repository skills are agent instructions, so **mount only repos you trust**.
- **Limit: 500 skills per session** (deduplicated across all agents in a multiagent session); more skills = slower sandbox start, so attach only what the agent needs.

---

## Memory stores (cross-session persistence — the "managed" payoff)

Workspace-scoped collections of text docs that survive across sessions. Without one, every session starts fresh.

- **Header:** the docs' calls to `/v1/memory_stores` and its sub-resources send `anthropic-beta: agent-memory-2026-07-22` **in place of** `managed-agents-2026-04-01` (both together → 400). The SDKs and the `ant` CLI switch automatically; in raw curl it is one header value to swap for that call — replace, never append. Attaching a store to a session is a session call, so that keeps `managed-agents-2026-04-01`.
- **Create:** `name` + `description` (description shown to agent). ID `memstore_…`. Seed via `memories.create` (`path` + `content`).
- **Attach:** in session `resources[]` as `{type:"memory_store", memory_store_id, access, instructions}`. **Only at session creation.** `access`: `read_write` (default) | `read_only`. `instructions` ≤ 4096 chars.
- **Runtime:** each store is mounted as its own directory under `/mnt/memory/` (a slug of the store name, e.g. "Demo Memory" → `/mnt/memory/demo-memory/`; the exact path is `mount_path` on the session's resource); agent reads/writes with normal file tools (agent toolset required); a mount note is auto-added to the system prompt. Writes sync back across sessions sharing the store.
- **Limits:** memory ≤ 100 kB (~25k tokens); **≤ 10,000 memories/store**; **≤ 8 stores/session**. Use many small focused files + multiple scoped stores (per-user, shared read-only reference, etc.).
- **Audit:** every write creates an immutable **memory version** (`memver_…`); list/retrieve/redact; retained 30 days (recent always kept). No restore endpoint — re-write old `content`. Optimistic concurrency via `content_sha256` precondition.
- **Security warning:** `read_write` + untrusted input = prompt-injection can poison memory for future sessions. Use `read_only` for reference material.
- **Manage:** retrieve/update/list/archive (one-way, read-only)/delete.
- **Self-hosted sandboxes:** sessions on a self-hosted environment can attach memory stores too (the only resource type they accept). The Python / TypeScript / Go **SDK worker** downloads each store to its `mount_path` and syncs changes back on an interval; the `ant beta:worker` CLI worker does not mount them. Memory stores on self-hosted environments are not available on Claude Platform on AWS.

---

## Vaults & credentials (auth without your own secret store)

Register third-party creds once, reference by `vault_ids` at session creation. Anthropic handles OAuth refresh. **Workspace-scoped** (anyone with a workspace key can reference; revoke by delete/archive).

- **Vault:** `display_name` + optional `metadata`. ID `vlt_…`.
- **Credential categories** (all secret values are **write-only**, never returned):
  - **`mcp_oauth`** — keyed by `mcp_server_url`; `access_token` + `expires_at` + optional `refresh` block (Anthropic auto-refreshes; `token_endpoint_auth.type` ∈ `none`/`client_secret_basic`/`client_secret_post`).
  - **`static_bearer`** — keyed by `mcp_server_url`; fixed `token`.
  - **`environment_variable`** — keyed by `secret_name`; stored as opaque placeholder, **substituted at egress only** (agent never sees value). Optional `injection_location` `{header, body}` scopes where in the outbound request it is substituted (API default: both; credentials created in the Console: header only). Has its own `networking.allowed_hosts` (controls which hosts the secret is used for — separate from env-level networking; both must allow the host). Not yet supported on self-hosted. Breaks SigV4/signature-based clients.
- **Runtime:** unmatched MCP cred → unauthenticated connection. First matching vault wins. Constraints: unique key/vault, keys immutable (archive+recreate to change), ≤ 20 creds/vault.
- **Rotation:** update secret value + display_name, and `injection_location` on environment-variable credentials (structural fields locked). Re-resolved periodically (propagates rotation/archival to running sessions). Diagnose OAuth refresh failures via `…/mcp_oauth_validate` (`valid`/`invalid`/`unknown`). Webhooks: `vault.archived/deleted`, `vault_credential.archived/deleted/refresh_failed`.

---

## Multi-agent sessions

One **coordinator** delegates to a roster of agents, each in its own **session thread** (context-isolated). All threads share the sandbox, filesystem, and vault credentials; tools/MCP/context are NOT shared.

- **Declare:** `multiagent:{type:"coordinator", agents:[…]}` on the coordinator agent. Roster entries: `{type:"agent",id}` (latest at create time), `{type:"agent",id,version}` (pinned), `{type:"self"}` (spawn copies), or `{type:"advisor", model:"<model id>"}` (an advisor, below). Roster snapshotted at coordinator create/update — update the coordinator to pick up newer sub-agent versions.
- **Advisor:** at most one `{type:"advisor", model}` roster entry (can be the only entry) — a model the **primary thread** consults mid-turn for strategic guidance (plan, get unstuck, review before finishing). Must be at least as capable as the agent's own model (else 400). Not a roster agent: the coordinator can't message it, roster agents can't consult it. Each consultation = a short-lived `anthropic.advisor` thread, billed at the advisor model's rates; a failed one never fails the turn.
- **Depth 1 only.** ≤ 20 unique agents in roster; ≤ **25 concurrent threads** (coordinator may call multiple copies of one agent; advisor consultation threads don't count).
- **Threads:** primary thread = session-level stream (condensed view: start/end of subagents + blocking events). Drill into a thread via `/v1/sessions/:id/threads/:tid/stream`. List threads; interrupt a thread (`user.interrupt` + `session_thread_id`); archive an idle thread to free the 25-cap.
- **Patterns:** parallelization, specialization, escalation. MCP servers are agent-scoped; vault creds are session-scoped (apply to all threads — supply a cred for every MCP server used across agents).

---

## MCP connectivity

- **Remote MCP servers** over HTTP (streamable HTTP transport; servers that only speak the deprecated SSE transport still work through an automatic fallback). Declare on agent: `mcp_servers:[{type:"url", name, url}]` + a `mcp_toolset` tool entry referencing `mcp_server_name` — every server needs a toolset entry and vice versa, ≤ 20 servers per agent.
- **Private servers** via **MCP tunnels** (limited research preview — request access).
- Auth via vaults: the credential's `mcp_server_url` must point at the same server as the agent's `mcp_servers[].url`. Both URLs are normalized before matching (host case, a default port and a trailing slash don't matter); a different path, subdomain or non-default port does.

---

## Files API

- Upload (`POST /v1/files`) — used for rubric files, inputs. The Files API is out of beta: no `files-api-2025-04-14` header needed any more (requests that still send it keep working, with the older response shapes).
- Mount an uploaded file into a session: `resources[]` entry `{type:"file", file_id, mount_path}` → lands read-only under `/mnt/session/uploads/`. ≤ 500 files/session.
- List session outputs: `/v1/files?scope_id=<session_id>` — the `scope_id` filter is the one part that still needs the `managed-agents-2026-04-01` header; download `/v1/files/:id/content`.
- Files you upload are independent resources; deletable anytime; survive session deletion. Files a session produced are scoped to that session and are deleted with it.

---

## Scheduled deployments (native cron — "runs without you")

Launched ~June 2026. A **deployment** kicks off sessions autonomously on a recurring cron schedule — **no external scheduler needed**. ID `depl_…`.

- **Create** `POST /v1/deployments`: `name`, `agent` (id/pinned), `environment_id`, **`initial_events`** (at least one `user.message` or `user.define_outcome` to start each run's work; unlike a session's `initial_events`, a deployment's also accept `system.message`), `schedule:{type:"cron", expression, timezone}`. Optionally attach **files**, **GitHub**, **memory stores**, **vaults** (same session config surface), and a **`budget`** (same shape as a session budget; caps every run separately, not the deployment's cumulative spend; unlike a session's it can be cleared with `null` and set again).
- **Schedule:** standard 5-field POSIX cron (`min hour dom month dow`), **minute granularity**; `timezone` = IANA id; **wall-clock DST** (literal local time; nonexistent spring-forward times skipped, fall-back times fire twice — use 1–3 AM-avoidance or UTC if that matters). Actual firing is jittered to spread load: up to 15% of the interval between runs, minimum 5 s, maximum 9 min. Response returns `schedule.upcoming_runs_at` (next fire times) to confirm. Console has a cron builder/validator at `/workspaces/default/deployments`.
- **Each firing → a session.** Tracked as a **deployment run** (`drun_…`) with `trigger_context:{type:"schedule"|"manual", scheduled_at}`. Success → `session_id`; failure → `error.type` (e.g. `environment_archived_error`, `agent_archived_error`, `session_rate_limited_error`). List `GET /v1/deployment_runs?deployment_id=…` (filter `has_error=true`). Rate-limited triggers are recorded (no retry) and retried next occurrence.
- **Lifecycle:** **pause** (suppress future triggers; running sessions continue; manual `run` still allowed; sets `paused_reason`) · **unpause** (resume from next occurrence, no backfill) · **archive** (terminal). **Manual run:** `POST /v1/deployments/:id/run` — fires immediately (test before committing to the schedule).
- **Auto-behavior:** if the agent is archived/deleted the deployment auto-archives; if a subagent is archived — or the environment or a vault it needs is archived — the next run is recorded as failed and the deployment auto-**pauses** so you can fix + resume.
- **Limit: 1,000 deployments/org** (contact support for more).

This is the real path to a "managed agent that works while you sleep" — a single API call, fully hosted.

---

## Dreams (research preview — needs `dreaming-2026-04-21` header + access)

Async job that reads a memory store + 1–100 past session transcripts and produces a **new, reorganized output memory store** (dedup, replace stale, surface insights). By default the input is never modified — review/discard the output. Models (research preview): `claude-opus-5`, `claude-fable-5`, `claude-opus-4-8`, `claude-opus-4-7`, `claude-sonnet-5`, `claude-sonnet-4-6`. Statuses: pending/running/completed/failed/canceled. Billed at standard token rates (`usage` reports totals). Use to keep memory stores under their 10,000-memory cap.

---

## Rate limits & key limits

- API: create endpoints **300 req/min**; read endpoints **1,200 req/min** (per org). Plus org spend limits + tier rate limits.
- Outcome `max_iterations`: default 3, max 20. Skills: 500/session. Files: 500/session. Memory: 100kB/memory, 10,000/store, 8 stores/session. Multiagent: 20 roster, 25 threads, depth 1. MCP servers: 20/agent. Vault: 20 creds. Sandbox state: 30 days from sandbox creation.

---

## ⚠️ Notable constraints

- **Scheduling IS native** — see Scheduled deployments above (`POST /v1/deployments`, cron + timezone). Doc lives at `/managed-agents/scheduled-deployments` (the `/deployments` path 404s, which is what misled my first pass — corrected).
- **Spend caps are per session, not per agent** — a session `budget` (see Session) is a hard cap on one session's list cost, and a deployment applies its `budget` to each run separately; there is no cumulative per-agent or per-deployment cap. Organization/workspace spend limits in the Console still apply on top.
- Not ZDR / not HIPAA-eligible (stateful by design).
- `system.message` mid-session only works on a short list of newer models — see Events.
- Webhooks exist for session, vault, agent, deployment, deployment-run, environment and memory-store lifecycle events (`/managed-agents/webhooks`; some names differ from the stream's, e.g. `session.status_idled`, and there is a `session.budget_reached`) — an alternative to polling/streaming for unattended deployments.

---

## Design implications for the 60-minute skill (technical founder)

1. **Happy path is short:** agent → environment → session → `user.define_outcome`. A technical founder can run curl/`ant`/SDK directly — expose it, don't hide it.
2. **The "managed agent" payoff = Outcomes + Memory.** Outcomes give a self-grading stop condition (the safety rail + quality loop). A memory store is what makes run #10 smarter than run #1 — strong candidate for the headline differentiator vs. a chat.
3. **Safety rails are first-class & native:** `always_ask` permission policies, `limited` networking allowlist, `read_only` memory, `max_iterations` bound, a per-session `budget`, workspace spend limit. Use these instead of hand-rolled guardrails.
4. **"Runs without you" is native and one call** — a **scheduled deployment** (cron + timezone + initial events) is the headline "this is a *worker*, not a chat" moment. The 60-min arc can credibly end with the founder's agent live on a recurring schedule. Use a **manual `run`** to test it instantly before trusting the cron.
5. **Iteration is cheap & version-safe:** sharper rubric = edit + re-kickoff (no agent version bump); prompt/tool change = agent update (new version). Sessions resume from their checkpoint until the sandbox is 30 days old.
