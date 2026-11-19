# LotusBuild FastAPI backend handoff

## Milestone 1 · 19 November 2026 (development only)

The conversation mode uses the existing authorization transactions, scheduler and fenced
run records. A version-specific ready status excludes legacy workspace-only workers.
The default dispatcher has no admitted conversational responder: it records `blocked_budget`
rather than starting Daytona or spending money. Project creation accepts a scoped operation ID.
The workspace path, Stripe, and existing financial ownership remain in place. No staging or
provider calls; release remains blocked on budget admission, isolated staging and rollback.
`uv run --no-sync python -m scripts.check` passed locally on Linux: format, lint, Pyright,
111 unit/contract tests, installed dependencies and lockfile. Coverage includes a racing
create transaction, clarification replay, Stop/correction during model execution, claim
takeover, revocation, the Firestore query encoding and no workspace calls for conversations.
Provider results, identity, Redis and Firestore in these tests are local fixtures. The
existing file-backed recovery suite still passes. The separate opt-in frontend passed
typecheck, 361 contracts and its build. No new dependencies or credentials.

Next: test a same-origin `/api/v1/projects` route to this Python service on isolated staging
while existing endpoints stay on TypeScript. Verify both accounts, query indexes, revocation,
lease takeover, lost acknowledgements, private-data retention and mixed-version rollback.
Keep paid conversational execution disabled until the authoritative payer has atomic
reservation, settlement and reconciliation; a model key is not spend authorization.

## Current checkpoint · 17 November 2026

**Pending-write reconciliation.** Writes are reserved in receipt-only recovery
before submission. A confirmed result records history and review atomically
with settlement of the reservation, including on model context overflow.
Unknown outcomes keep the project slot. Three lease-spaced receipt attempts
are allowed per recovery batch. Existing interrupted reservations can use the
same owner run PUT or Stop. See README for coordinated worker rollout and
operator recovery. Legacy daemon errors remain ambiguous. New workers only
certify rejection when no write preparation was journaled.

Final local verification: `uv run python -m scripts.check` passed formatting,
lint, Pyright, dependency and lock checks. Of 103 tests, 102 passed and one
Linux-only daemon process test was skipped on Windows. Tests use simulated
Daytona, Firebase and transactional storage, including commit failures, lost
acknowledgements, revoked access, lease takeover, budget overflow and repeated
recovery. WSL is unavailable here. No live services or deployment were used.
Next verify the updated daemon on Linux and the dispatcher/API together in
staging before rollout. Preserve the bound sandbox and journal for any
unresolved receipt. A receipt or run larger than the hard storage limits
requires operator investigation even if the configured model budget is smaller.

**Mid-task steering and Stop.** The API accepts ordered, idempotent corrections
and Stop on the active run. The dispatcher checks them transactionally when
reserving actions and committing results. An uncertain write holds the project
until its receipt is reconciled. See README under Daytona workspace runs for
the frontend routes and the shared context limit. The final repository check
passed formatting, lint, types and dependency checks. Of 93 tests, 92 passed,
including corrections during model calls, Stop during a write, receipt recovery
and preserving the run baseline. One Linux daemon test was skipped on Windows.
Next connect the frontend controls and verify the updated API and dispatcher
together in staging with Claude and Daytona. No deployment or live calls made.

**Agent message and tool security.** Session intake, run submission and dispatch
validate message text and its UTF-8 byte limit. The dispatcher separates user
requests from workspace observations, scopes tools to the immutable run mode
and validates sandbox receipts. The daemon validates commands before touching
SQLite. Session and run JSON escape HTML-sensitive characters. See the Daytona
workspace runs section in README for the read-only mode and client contract.
`uv run python -m scripts.check` checked formatting, lint, types and dependencies.
Of 90 tests, 89 passed and one Linux-only daemon test was skipped on Windows.
No live Claude or Daytona run was made. Next verify the daemon on Linux, inspect
rendering in the frontend and exercise the provider loop in staging.

**Claude agent integration decision pending.** The user wants reusable agents
defined through Claude Console and invoked by API, with additional agents in
later phases. The supplied Agents/Create screen matches the workflow Anthropic
documents under Claude Managed Agents. The user explicitly excluded Managed
Agents. Clarify whether that exclusion refers to Anthropic hosting the sandbox
or to the entire Agents API before replacing the current Messages API loop in
`workspace_dispatch.py`. Keep Daytona as the project workspace. No provider
agent or session was created during this clarification.

**Resume here.** Each LotusBuild project needs its own persistent local Git
repository. The agent chooses each next action from the user's intent, the
current working tree, available tools and the results of earlier actions.
It can answer a question directly or create and revise project artifacts such
as documents, data, images, configuration and code. Avoid keyword-based task
routing or a mandatory scaffold, edit, test and commit sequence. Enforce project
isolation and permissions on each tool call regardless of the task.

For changes, capture the working state at the start of the run, including
uncommitted user work. Present the agent's actual additions, edits and deletions
relative to that state. Support reviewable diffs and patches for text and
format-appropriate previews or before/after versions for binary assets.
GitHub sync becomes available only after the user explicitly connects a GitHub
repository to that project. This applies to user-created projects; the backend
checkout is a separate workspace.

### Accepted architecture · Daytona sandbox worker

- Daytona is the first execution environment. A background worker (daemon) runs
  inside the project's isolated sandbox. The cloud desktop and worker open the
  same Git working tree. A desktop disconnect leaves an active run working.
- FastAPI authorizes and dispatches work. Anthropic credentials, billing decisions
  and durable run progress belong to the backend. The worker receives only the
  authority needed for its assigned project and run. One agent writer may hold
  the project at a time.
- Keep the sandbox awake while a run is active. After both the user and agent
  have been idle for five minutes, let it sleep. A waiting daemon is idle.
- Stopping the sandbox stops the process. A fresh worker reopens the existing
  repository and checks durable run progress before taking further action.
  Preserve Git history and uncommitted files in recoverable checkpoints before
  replacing a sandbox.

**Current implementation.** `workspace_runs.py` exposes PUT/GET on a session's
`/run` URL. Firestore stores the question, observations, run phase, tool receipts
returned to the backend, model usage and review patches. Submission binds the
verified owner, project epoch, configured action and individual plan. The
dispatcher and sandbox both enforce single-writer coordination. The dispatcher
rechecks access, leases a persisted step and keeps Anthropic and Daytona credentials
on the backend. The model can answer, list, read or choose a bounded UTF-8 edit.
The workspace daemon lives in `sandbox_worker.py`; `deploy/Daytona.Dockerfile`
installs it in a desktop snapshot. It journals operations by ID and checks the
starting file state and observed revision before writing. Run setup and known
recovery limits are in README.md, under Daytona workspace runs.

**Verification.** `uv run python -m scripts.check` passed formatting, lint,
Pyright, dependency and lock checks. Of 87 tests discovered, 86 passed and
the Linux daemon restart test was skipped on this Windows host. Real Git was
used locally to generate and apply document patches. Dispatch, duplicate claims,
owner isolation and model decisions used simulated providers. No Daytona sandbox
was provisioned and no Anthropic request was sent. Verify the actual snapshot,
stop/start, desktop activity, daemon restart, and safe checkpoint/restore before
enabling sandbox recycling. Automatic replacement and deletion are disabled.
Team spending, binary tools and remote desktop UI integration remain pending.

The existing session implementation
saves and reopens the question using a client-generated UUIDv4. See
`project_sessions.py` and the Private projects section of README.md for the
PUT/GET contract and three required session ENV settings. Ownership and the
appropriate project action are verified in the same Firestore transaction.
An identical retry is safe. Reuse of the ID with different text returns 409.

The worker and model-tool loop are implemented for the first private-project
milestone. Live execution and source citations remain unverified.
The user selected Anthropic and the existing shared rate limiter, and requested
Stripe subscriptions through APIs and webhooks. This supersedes the older billing
deferral below. The official Anthropic and Stripe SDKs are installed and locked.
Partial, unverified local changes include individual Checkout and subscription
webhook routes in `billing.py`, plan configuration in `config.py`, router wiring,
test fixtures, packaging, and a queued-run query in `firestore_access.py`.
The local repository checks now pass. Billing lifecycle behavior still needs its
own verification. No live provider calls or billing deployment were performed.

Confirmed product rules: Free uses lower-cost models, Pro uses higher-capability
models with extended limits. Teams costs a flat
$60/month, credited as prepaid shared usage. Desktop, cloud sessions, CLI and Review
all draw from the same team balance. Admins can top up and control member features.
Admins can configure up to three usage tiers. Each member is assigned one tier.
Its cap applies individually and blocks new work even when the team has credits.
Consume each member's included quota first, then shared credits. Flex seats have
zero included quota. Never multiply the $60 by the number of seats.

### Sales-led Business plan

The user described Business and Enterprise together. Treat them as one sales-led
offering pending final public naming. Show **Contact us** for larger companies.
LotusBuild provisions the features, ACU price and contracted volume agreed in
each customer's order form. Desktop, cloud sessions and Review consume one company
pool of Agent Compute Units (ACUs). Self-serve credit and quota rules apply only
to their respective self-serve plans.

One company account can contain multiple organizations. Company admins can group
organizations under shared budgets and set monthly per-user limits through tiers,
with overrides and a limit-increase request workflow. Organization-group budgets
are Enterprise-only. The three-tier maximum belongs to Teams. Enterprise controls
include SSO, SCIM user provisioning, admin dashboards and role-based access.

No training on company data is a product requirement. Before offering it as a
contractual guarantee, verify LotusBuild's use of that data and the applicable
terms and settings for every model provider receiving it. Retention and deletion
terms require their own decisions. Enterprise workflows and ACU enforcement are
requirements at this checkpoint. They have not been implemented or verified.

### Enterprise ACU metering and reporting

The order form sets the price per ACU and the purchased volume. A published
rate card converts measured usage into ACUs.

- Desktop and agent work accrue ACUs from the chosen model's token usage alone.
- Cloud sessions and code review also accrue ACUs for metered compute, VMs and
  other billable infrastructure. The rate card needs explicit units for each
  component so the same machine work is counted once.
- A session sleeps after five idle minutes. Time asleep accrues zero ACUs.
- Session Insights shows usage per session. Consumption shows totals by day,
  user, organization and product.

Proposed ledger design: retain the measured quantities, rate-card version,
session, timestamp, user, organization and product on each charge. Derive both
admin views from those charges so historical totals can be reconciled after a
rate change. Numerical rates, infrastructure units and rounding remain open.
Metering, sleep transitions and reporting remain unimplemented.

Next implementation unit: a durable usage ledger with atomic reservations against
the individual cap, included quota and team balance across all clients. Deduplicate
monthly funding and top-ups by the underlying confirmed Stripe payment, and settle
each run once. The Redis request limiter remains separate from monetary accounting.
Self-serve credit denomination, usage conversion, included allowances, cap/reset periods,
rollover and refund handling remain open. The project's local Git working tree
is the agreed source of files for the agent.
The last local
check command passed all 83 tests including extended project/session cases, Ruff,
Pyright, dependency and lock checks. Live services were not exercised.

The next release milestone remains a two-account staging check. Earlier versions
are recorded under Historical checkpoints.

The app now mounts the project create, reopen and paginated owner-list endpoints in
`projects.py`. This supersedes the older notes about resource routes being absent.
See the Private projects section in README.md for the request contract and four
new required ENV settings. All routes use the existing authenticated rate limit.
Ownership comes from the verified UID. The shared authorization service checks
the Firestore policy and ownership in the transaction that reads or creates data.

Local verification passed with 83 tests, Ruff, Pyright, dependency and lock checks.
Staging verification remains open for the configured policy, actual query indexes,
two real Firebase users and direct client denial under deployed Firestore rules.
Creation has no idempotency key. Clients should refresh after an uncertain response.

**How to verify the next milestone**

1. Use a designated staging Firebase project, Firestore database, self-hosted
   Redis, and two test accounts. Configuration keys are in `.env.example`.
   Confirm the selected Firestore policy contains the configured project kind
   and its create/read actions. Keep credential values outside this document.
2. Follow the real-service check in `README.md`. `scripts/check_services.py`
   uses an in-process client and one real identity to check the account endpoint,
   policy and Redis. The two-account project workflow needs its own staging check.
3. Through the staging API behind Caddy, have A create a project and reopen it
   after signing in again. Confirm it appears in A's list. Have B request the same
   ID and a missing ID and compare the 404 errors. Check every page of B's list
   for A's ID and verify direct Firestore client reads are denied. Record the
   environment label, date, and pass/fail outcomes here.

**Open decisions.** Before adding automatic retries for project creation, define
how repeated requests identify the same create operation. Team invitation and
acceptance are a later milestone. Billing remains deferred.

## Historical checkpoints

Date: 17 November 2026 (Europe/London).
Workspace: `C:\Users\Preet\Desktop\fastapipythonbackend`.

This document records this conversation and the source inspected when writing it. Reinspect the checkout before making changes. It does not authorize additional implementation, commits, publishing, deployment, or production changes.

## Earlier foundation work

### Latest: shared Firestore resource authorization

This update supersedes the paused owner-only proposal and earlier notes saying
authorization was not implemented. User chose Firestore, then expanded the model
to shared collections and resource grants for Teams sharing. Billing/subscriptions
remain explicitly deferred. Confirmed product rules: only owners manage sharing;
direct recipients must first be active team members; teammates receive explicit
actions; public/shared-link access is read-only. The earlier per-user-path model
was not implemented and must not be resumed.

Implemented `authorization.py` and strict schemas in `authorization_models.py`.
`Authorization` binds the principal from verified Firebase `CurrentUser`; service
methods do not accept a caller UID from a request. `RequireResource` is a reusable
read dependency. The service implements private root creation, checked reads and
bounded explicit-ID batches, payload-only updates, deletion, owner-managed direct
grants/team/public settings, secret link issue/revocation, and `make_private`.
Read/update/delete/create actions come from a server-owned Firestore policy with
distinct action categories; no role catalog, wildcard grants, plan-name check,
or permission default is invented. No resource, sharing, invitation, or anonymous
HTTP API routes were added. Future routes must use the dependencies and protected
mutation methods; authentication alone does not protect raw SDK operations.

Sharing requires a trusted team `sharing_enabled` capability and active owner
membership. Missing/disabled capability blocks every non-owner sharing mode.
Invited-user grants require active recipient membership and current member action
permissions. An explicit public/link read can reach an outside visitor, as agreed;
it never authorizes a mutation. No end-user API can enable the team capability.
The future entitlement publisher must set it only for eligible Teams accounts;
actual subscription eligibility is not implemented or verified here.

Ownership, team/parent associations, grants, capability, and policy are Firestore
records, not ENV user lists. Collection names, policy document ID, link token
entropy, parent-depth/batch bounds, database ID, and deadlines are required ENV
settings documented in `.env.example`. No actual `.env`, data, policy, team,
membership, grant, invitation, or provider setup was fabricated or provisioned.
`AUTHORIZATION.md` defines the new stored schema and integration boundaries.

`database.py` now reuses Firebase credentials to initialize one public Firestore
RPC client per worker. `firestore_access.py` performs single-attempt transactions:
begin/read/commit use explicit transaction identities, bounded deadlines, and
`retry=None`. Access checks and mutations commit together; conflicts return 409
without a blind retry. Failures attempt bounded rollback, whose failure is logged.
The codec reuses the locked SDK's internal encode/decode helpers; compatibility
must be reviewed on upgrades. `google-cloud-firestore` is declared directly.

Metadata stays outside editable `data`. Parent IDs/ancestors are checked; there
is no implicit permission inheritance. Resource IDs are server generated, and
access epochs bind grants and links so they cannot survive a generation change.
`make_private` clears public/team grants and rotates the epoch atomically,
invalidating direct grants and links without an unbounded scan. It affects the
selected resource, not an assumed subtree policy. Owners can revoke by link
digest ID without retaining the original token. Link secrets are cryptographically
generated, returned once as `SecretStr`, and stored only as SHA-256 IDs. Expiry
must be an explicit future aware timestamp or null, not a guessed lifetime.

The older pending ENV audit is also incorporated and verified: CORS method/header/
exposure/cache settings now come from ENV. The smoke command's target environment,
origin, simulated peer IP, token, and expected UID are required integration settings;
the fixed staging-label assumption was removed. `/health/live` returns `live` and
`/health/ready` returns `ready`. Request-context IDs also correlate lower-level
authorization failure logs. All original auth, IP/user rate, host, origin, size,
and timeout safeguards remain.

Final verification: `uv run python -m scripts.check` passed Ruff format/lint,
Pyright (zero errors/warnings), all 80 tests, dependency compatibility (92 packages),
and lock verification (95 packages). Tests include cross-user/team/kind/parent
tampering, HTTP identity binding, explicit permissions, active memberships,
capability suspension, revoked/expired grants and links, metadata protection,
read-only public access, atomic make-private, transaction IDs/deadlines/no retries,
and aborting a mutation when permission state changes before commit. Firestore is
simulated/mocked in the normal suite; no Firebase or Firestore emulator is used.
The initial expanded-suite timeout fixture was corrected to include the new
Firestore timeout, and a request-context type-narrowing issue was fixed before
the final passing run.

`scripts/check_services.py` now also reads/validates the real configured Firestore
policy in a transaction, but has not been run against real services. Actual Rules/
IAM, provider concurrency/revocation, service connectivity, browser/public-route
integration, Caddy/TLS, and the container build remain unverified. The checked-in
`deploy/firestore.rules` is a reviewable deny-client baseline for a server-only
database, not a deployed change. Admin/IAM clients bypass Rules, so never treat
that file as a substitute for backend authorization or assume direct database
access is safe before reviewing deployed Rules/IAM.

Read `AUTHORIZATION.md` before extending this feature. Invitation email/acceptance
flows, membership administration, workspace ownership/transfers, child creation,
query/index/pagination discovery, share-management listings/UI, subtree policy,
durable job authorization, idempotent HTTP creation, and orphan retention/cleanup
need their own concrete endpoint/data contracts. Do not pretend those product
flows or billing integration are complete merely because the shared engine exists.

Added/updated authorization-related files: `authorization.py`,
`authorization_models.py`, `firestore_access.py`, `database.py`, `config.py`,
`main.py`, `observability.py`, `health.py`, ENV/dependency/container allowlists,
shared tests and new authorization/RPC/route tests, smoke checks, README,
`AUTHORIZATION.md`, `deploy/firestore.rules`, and this handoff. No Git repository,
commit, push, deployment, provider data/rule/IAM change, billing change, or Caddy
reload was performed. No memory files outside this workspace were modified.

### Subsequent ENV-only policy refinement

User requested removing hardcoded deployment choices from `config.py` and using
environment configuration. `REQUIRE_HTTPS` is now required and explicitly controls
both request and CORS-origin HTTPS enforcement. Environment names no longer
select transport rules, and there is no hardcoded localhost exception.
`RATE_LIMIT_DEFAULT_POLICY` is required and selects a named configured policy;
`default_rate_limit` delegates to the same shared user-limiter function as named
policies. `API_ENVIRONMENT` accepts a nonempty deployment label. `LOG_LEVEL` is
validated against Python's supported logging levels instead of a fixed subset.
`.env.example` sets `REQUIRE_HTTPS=true` for the public API and documents both
required settings. README local-development examples explicitly set it false.
Protocol validity, positive counts, valid ports, bounded timeouts, exact origins,
proxy-trust checks, and Firebase emulator rejection remain validation invariants.
They are not guessed deployment values. No actual environment file or production
configuration was modified. The staging smoke script still deliberately requires
the `staging` label for an explicit real-service check.

After this refinement, `uv run python -m scripts.check` passed formatting, lint,
Pyright (zero errors/warnings), all 40 tests, dependency compatibility, and lock
verification. Tests verify that changing the default policy name actually changes
route enforcement and that environment labels no longer implicitly control HTTPS.

This section supersedes descriptions of the earlier file layout, environment
requirements, app entry point, and verification counts below. Earlier sections
remain as conversation history, not instructions to recreate old code.

User asked to implement the seven API foundations in order while keeping shared,
maintainable modules. Caddy is the chosen reverse proxy; Redis is self-hosted.
User approved these configurable initial values: 300 requests per minute per IP
before authentication, a 1 MiB request body, and a 30-second ordinary request
timeout. The existing approved aggregate user quota remains 60 per 60 seconds.
No real domains, frontend path, service credentials, or hosting layout were given.

### Implemented locally

1. `config.py`: one source of required API settings plus server settings. Firebase,
   Redis, policy, body/time limits, origins, hosts, environment, docs, and logging
   are validated. No runtime policy defaults or storage fallbacks were introduced.
   Emulator configuration is still rejected. Redis URL secrets and configuration
   values are excluded from startup validation error messages. Proxy trust rejects
   wildcards, unrestricted networks, and noncanonical CIDRs with host bits.
2. `observability.py`: generated request IDs, UTC JSON events, safe request/failure
   fields, non-cacheable responses, basic security headers, and a shared error
   envelope. Validation errors do not echo input; unexpected errors do not expose
   exception text. Raw URLs, query strings, headers, claims, and bodies are not
   included in application logs. `request_limits.py` owns host/origin checks,
   declared/streamed body limits, and async request deadlines.
3. `health.py`: a mounted plain Starlette operational router exposes private
   `/health/live` and `/health/ready`. These avoid API user dependencies and quotas,
   while retaining the outer request/host safeguards. Liveness has no remote call;
   readiness checks initialized state and Redis. It does not pretend to perform a
   live Firebase login or certify Firebase remote availability.
4. Backend Next.js compatibility: exact-origin CORS with bearer Authorization,
   readable request ID/retry headers, and no credentialed cookies/wildcards.
   `README.md` gives the frontend contract. No frontend files or login screens were
   changed; real browser email/password/Google login is still unverified.
5. `rate_limits.py`: pre-auth IP checks reuse the same Redis pool and atomic
   `consume_limit` implementation as user policies. They use only Uvicorn's resolved
   ASGI peer address, not raw forwarded headers. Trusted-Caddy interpretation is
   configured in `serve.py`. Missing peers/Redis failures do not bypass protection.
   Firebase receives a configurable bounded network timeout shorter than the
   request deadline. Redis uses bounded connection/read timeouts and no transport
   retries. Health probes are exempt from IP quotas; CORS preflights are handled
   before Firebase/IP work and do not consume those quotas.
6. Saved verification: Ruff, Pyright, unittest, dependency compatibility, and lock
   validation run through `uv run python -m scripts.check`. Development tool
   versions are locked. `scripts/check_services.py` is a separate, explicit
   real-staging smoke check requiring an existing test user's ID token/expected
   UID in environment variables. It uses separate expiring Redis keys and never
   clears existing counters. It has not been run against real services.
7. Deployment preparation: `serve.py` configures Uvicorn workers/concurrency,
   trusted proxy IPs, keep-alive, graceful shutdown, and disabled raw access logs.
   `deploy/Caddyfile` supplies environment-driven HTTPS proxy configuration, edge
   body/read limits, private health blocking, and an upstream timeout longer than
   the API deadline. `Dockerfile` uses required approved immutable Python/uv image
   references, locked production dependencies, non-root execution, and an allowlist
   build context. `README.md` documents secret injection, restart/probe/resource
   arrangements, safe release validation, and rollback. No deployment was performed.

### Entry point and configuration changes

- There is now an application factory, `main:create_app`, instead of `main.app`.
  It loads validated configuration before constructing middleware. Tests inject
  explicit settings. Normal startup is `uv run --env-file .env python serve.py`.
  A direct `uvicorn main:create_app --factory` development command is documented.
  The old `fastapi dev main.py` instruction is superseded.
- `.env.example` documents all required fields and approved initial request
  policies. Export variables or explicitly use uv's `--env-file`; no implicit
  dotenv loading occurs. Actual origins, allowed hosts, Redis URL, key prefix,
  Firebase project, credentials, and server/network settings must be supplied.
- Remove/unset `GOOGLE_APPLICATION_CREDENTIALS` when using an attached service
  identity rather than exporting it with a blank value. Keep local service-account
  files outside the repository.
- The Caddy upstream timeout is separately configurable and must exceed the API
  request timeout; the example uses 35 seconds versus the approved API 30 seconds.
  Supply matching values to the Caddy process. Network topology, resource capacity,
  domains, certificate status, and trusted Caddy peer IPs were not guessed.
- Application errors now use `{"error":{"code":"...","message":"..."},"request_id":"..."}`
  instead of FastAPI's default `detail` response. Proxy/protocol/CORS failures may
  use other bodies. The account response and `/api/v1/users/account` path remain.
- API docs/OpenAPI are explicitly enabled/disabled by configuration. If enabled,
  framework documentation is public with the IP guard, not user-authenticated.
- Python cannot forcibly cancel synchronous worker threads or undo side effects;
  SDK/network limits and worker concurrency are separate safeguards. Agent jobs
  still require a future durable execution design. Started streams are interrupted
  on failure rather than receiving an invalid second HTTP response.

### Verified and remaining

Final `uv run python -m scripts.check` passed: Ruff formatting/lint, Pyright with
zero errors/warnings, all 37 tests, `uv pip check` (92 compatible packages), and
`uv lock --check` (95 resolved packages). Tests cover configuration, credential
lifecycle, verified identity and rejected tokens, private health/readiness, CORS,
request/error/log redaction, malformed headers, streamed body size, deadlines,
IP spoof resistance/trusted proxies, shared quotas, concurrency, expiry, and
Redis outages. They use mocked Firebase and fake Redis with Lua, not a Firebase
emulator. Earlier executable-launch blocking did not prevent this run using the
tools' Python module entry points; no Windows security settings were changed.

Unverified: actual Firebase/Redis credentials and connectivity, real browser login,
real Caddy config parsing/routing/TLS, container build/run, multi-host performance,
monitoring/alert delivery, and real rollout/rollback. Caddy and Docker executables
were not available in this workspace. Do not call the deployed API production-ready
based solely on the passing local suite. Use the saved README and smoke command
once the real configuration is available. Existing Caddy configuration must be
reviewed/merged and validated before an explicitly authorized reload.

Files added/updated in this pass: `config.py`, `auth.py`, `rate_limits.py`, `main.py`,
`observability.py`, `request_limits.py`, `health.py`, `serve.py`, `.env.example`,
`.gitignore`, `pyproject.toml`, `uv.lock`, tests/helpers and test modules,
`scripts/check.py`, `scripts/check_services.py`, package marker files,
`deploy/Caddyfile`, `Dockerfile`, `.dockerignore`, `README.md`, and this handoff.
No Git repository, commit, push, deployment, Caddy reload, Firebase-provider change,
or production secret change was made. No continuous-integration provider was
assumed; the saved check command can be used in the user's chosen CI system.

## User intent and working preferences

- Preet is developing LotusBuild, an autonomous AI agent product comparable in purpose to Codex.
- Preet is learning FastAPI and has basic Python knowledge. Explain unfamiliar syntax and behaviour plainly.
- Explicitly requested the Preet KISS workflow: small, direct changes; readable code; reuse shared logic; avoid unnecessary abstractions and unrelated cleanup; complete coherent edits before relevant checks.
- Use independent judgment and explain actual tradeoffs. Do not simply agree or claim production readiness without evidence.
- User requested minimal diffs that are easy to review, friendly errors, and safe production authentication without hardcoded credentials or invented logic.
- No Firebase emulator is being used. The intended integration is real Firebase.
- Preserve unrelated changes. Do not commit, push, deploy, or change production without authorization.
- Relevant local skills: `C:/Users/Preet/.codex/skills/preet-kiss-workflow/SKILL.md`, `C:/Users/Preet/.codex/skills/lotusbuild-product-guardrails/SKILL.md`, and `C:/Users/Preet/.agents/skills/vibe-security/SKILL.md`.

## Conversation and decisions

1. User requested a FastAPI Python project using `uv add "fastapi[standard]"`.
2. The initially empty folder was initialized with `uv init --bare --no-workspace --vcs none`, then the dependency was installed. A starter `main.py` returned `{"message": "Hello World"}` at `/`. A `.gitignore` was added.
3. Explanations covered the root path, decorators, ordinary Python route functions, JSON responses, path/query parameters, and an addition example. The addition route was illustrative only and was never added to the source.
4. User requested removal of the placeholder root and Firebase built-in email/password authentication plus Google sign-in, asking for the design before implementation.
5. Recommended design: frontend Firebase SDK handles registration and sign-in; frontend sends its Firebase ID token as `Authorization: Bearer <token>`; FastAPI uses the official Admin SDK to verify it through one shared dependency. Do not build a separate password database or custom token system.
6. User asked for example code, then authorized applying it with a minimal, review-friendly diff, saying the Firebase dependency was already installed.
7. Only the backend authentication foundation was applied. There is no frontend in this workspace and no email/password or Google login UI was implemented.
8. User questioned the `FIREBASE_AUTH_EMULATOR_HOST` check. Explained that Firebase Admin switches to emulator mode if that environment variable is set, bypassing real signature verification. The added check rejects that configuration at startup; it does not enable or connect to an emulator. User has not requested removal of the check.
9. User requested this Markdown handoff for review and continuation in a new chat.

## Current files and repository state

- `main.py`: FastAPI app and Firebase authentication, described below.
- `pyproject.toml`: project `fastapipythonbackend`, version `0.1.0`, Python `>=3.14`, dependencies `fastapi[standard]>=0.163.1` and `firebase-admin>=7.8.0`.
- `uv.lock`: dependency lockfile generated/updated by uv.
- `.gitignore`: ignores `.venv/`, `__pycache__/`, `*.py[cod]`, and `.env`.
- Local `.venv/` and `__pycache__/` exist.
- Installed Firebase Admin version was confirmed as `7.8.0` when writing this document.
- No `.git` directory or `AGENTS.md` was observed in this workspace. Initialization explicitly used `--vcs none`. No commits or deployments were performed.
- The authentication implementation changed only `main.py`; Firebase Admin had already been added by the user. This handoff is a subsequent documentation addition.

## Authentication implementation in main.py

### Startup and shutdown

- FastAPI lifespan requires a nonblank `FIREBASE_PROJECT_ID` environment variable.
- Rejects a nonempty `FIREBASE_AUTH_EMULATOR_HOST` to prevent accidentally running with emulator verification behaviour.
- Uses `credentials.ApplicationDefault()` and resolves credentials at startup.
- Converts `DefaultCredentialsError` into an actionable startup error mentioning Application Default Credentials and the local `GOOGLE_APPLICATION_CREDENTIALS` option.
- Initializes one Firebase Admin app with the configured project ID and stores it on `app.state.firebase_app`.
- Deletes that SDK app during shutdown.
- Resolving credentials at startup does not prove their remote permissions, network connectivity, or live Firebase provider configuration.

### Shared dependency

- `HTTPBearer(auto_error=False)` parses the bearer header.
- `get_current_user` returns 401 for missing or malformed bearer authentication, with `WWW-Authenticate: Bearer` and `Please sign in to continue.`
- Calls `auth.verify_id_token(token.credentials, app=request.app.state.firebase_app, check_revoked=True)`.
- Official SDK handles signature, issuer, audience/project, and expiration verification. Revocation checking also checks disabled accounts and adds a remote Firebase user lookup, affecting latency and availability.
- Invalid, expired, revoked, disabled-user, and missing-user errors return 401 with `Your session is no longer valid. Please sign in again.`
- Certificate-fetch and other `FirebaseError` failures return 503 with `We couldn't check your sign-in. Please try again later.`
- Provider exception details and tokens are not exposed in HTTP responses. The code does not add token logging.
- The dependency is synchronous (`def`), allowing FastAPI to execute blocking SDK work in its thread pool.

### Routes

- Placeholder `GET /` was removed; it now returns 404.
- `GET /api/v1/users/account` requires the shared dependency and returns only `uid`, `email`, and `email_verified` from verified claims. The user explicitly requested this name in place of `/api/v1/users/me`; the route function is now `get_account`.
- UID comes from verified claims, not request parameters or client-supplied identity.
- Email may be null; absent email verification is reported as false. The endpoint reports verification status; it does not require verified email. No broader verified-email policy was agreed.
- FastAPI's default documentation/OpenAPI endpoints remain enabled; no production documentation policy was decided.
- No custom login/register backend endpoints, database, project routes, agent routes, roles, account linking rules, or authorization ownership rules were added.

## Local execution configuration

For local development, supply actual values through the shell:

```powershell
$env:FIREBASE_PROJECT_ID = "your-actual-project-id"
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\private\firebase-service-account.json"
uv run fastapi dev main.py
```

These are illustrative values, not configured credentials. Keep service-account files outside the repository. The application reads process environment variables; it does not explicitly load a `.env` file. Supported hosting may supply Application Default Credentials through an attached service identity instead of a downloaded key. `fastapi dev` is a development command, not the production deployment arrangement.

## Verification performed

During initial setup, TestClient verified the starter root response and `/docs`; that starter root was subsequently removed.

After authentication changes, an inline Python check using TestClient and mocked Firebase/credential operations passed:

- Missing and blank project configuration reject startup.
- Missing credentials produce the intended startup error.
- Emulator configuration rejects startup.
- `/` returns 404.
- Missing authorization, Basic authorization, and an empty Bearer header return 401 with the bearer challenge, without calling token verification.
- A simulated valid token returns the expected limited account fields.
- A supplied `uid` query parameter does not replace the verified UID.
- Verification is called with the correct SDK app and `check_revoked=True`.
- Simulated invalid, expired, revoked, disabled-account, and deleted-account failures return 401 without leaking provider details.
- Simulated certificate-fetch and Firebase availability failures return 503 without leaking provider details.
- SDK app cleanup runs on shutdown.

The first verification script needed a correction because the SDK's `ExpiredIdTokenError` constructor requires a `cause` argument. The corrected script passed. This was a test-harness issue, not an application fix. The inline check was not saved as a test file.

`uv pip check` passed: all 81 installed packages were compatible at that point.

These checks prove local routing and exception mapping with simulated SDK outcomes. They do not prove real signature verification, real wrong-project token rejection, remote permissions, actual Firebase login, provider availability, browser behaviour, or deployed security.

## Remaining work and boundaries

- Actual Firebase project ID, credentials, hosting setup, frontend location, and frontend origin have not been established in this conversation. Do not invent them or ask the user to paste private keys into chat.
- Enable/verify Email/Password and Google providers plus authorized frontend domains in the intended Firebase project before claiming login is complete.
- Inspect the actual frontend and reuse existing Firebase initialization if present. The browser example in chat was illustrative, not implemented. Firebase client configuration and Admin credentials have different exposure rules; Admin credentials must stay server-side.
- Configure explicit permitted CORS origins if browser frontend and API are cross-origin; none are currently configured.
- Verify actual login and token handling against real Firebase, including expiration, wrong-project tokens, disabled users, and revocation.
- Review network timeout/transport failure handling against installed SDK behaviour and real deployment evidence; the current mapped exceptions do not establish that every possible infrastructure failure gets a friendly 503.
- Production HTTPS, serving process, service identity permissions, operational logging, abuse controls, and deployment configuration remain unverified/unimplemented here. Do not call the entire API production-ready solely because the authentication structure is sound.
- Future project/agent routes need server-side ownership/permission enforcement in addition to authentication. No such resources exist yet in this workspace.
- Decisions about email verification requirements, account linking, persistence/session strategy, and authorization policies should be made from actual product requirements rather than guessed.
- This handoff is context, not an instruction to implement all remaining items automatically. Follow the user's next request.

## Subsequent refactor: reusable authentication

User requested shared, maintainable logic instead of duplicating authentication in future routes. Firebase lifespan initialization and `get_current_user` were moved from `main.py` into `auth.py`. `auth.py` also exports `CurrentUser = Annotated[dict, Depends(get_current_user)]` so protected routes can accept `user: CurrentUser`. `main.py` imports the lifespan and this dependency alias, and contains application creation plus the account route. Authentication behaviour and environment requirements are unchanged. Earlier descriptions of all authentication residing in `main.py` describe the pre-refactor state.

No frontend, database, extra abstraction layers, or new authentication policy were added. Future routes should import `CurrentUser`; they must still enforce resource ownership separately.

## Shared Redis rate limiting (subsequent authorized implementation)

User selected Redis and confirmed 60 requests per 60 seconds as the initial per-user default. Redis is self-hosted. User required configurable behaviour with no guessed defaults or fallback storage.

- `rate_limits.py` owns required environment configuration, one Redis connection pool per worker, moving-window enforcement via the `limits` library, and a reusable `RateLimit(policy_name)` FastAPI dependency.
- `main.py` composes the authentication and Redis lifespans. Its global `Depends(default_rate_limit)` applies one aggregate default quota across all application endpoints, including future routes. It also requires Firebase authentication for those routes. FastAPI's built-in docs/OpenAPI routes are outside this dependency; a genuinely public/webhook endpoint will need deliberate routing design before being added.
- The limiter and route share the cached Firebase dependency, so token verification is not repeated within one request.
- Redis keys use a SHA-256 digest of the verified Firebase UID, never an IP header, submitted user ID, or raw token. A user's default allowance is shared across endpoints rather than multiplied per endpoint.
- Policy names come from server declarations. Named additional policies can be declared using `dependencies=[Depends(RateLimit("agent"))]`; configure `agent` in the policies JSON first. A named policy supplements the aggregate default, rather than bypassing it. Missing referenced policies reject startup.
- Policy counts/windows, Redis connection URL, timeout, and deployment key prefix are required environment configuration. `.env.example` documents their names and the approved initial quota. The app still does not automatically load `.env`.
- Example PowerShell configuration, with a real Redis URL supplied separately through deployment secrets:

```powershell
$env:RATE_LIMIT_KEY_PREFIX = "lotusbuild:production:rate_limits"
$env:RATE_LIMIT_TIMEOUT_SECONDS = "5"
$env:RATE_LIMIT_POLICIES = '{"default":{"requests":60,"window_seconds":60}}'
```

- Set `RATE_LIMIT_REDIS_URL` to the actual `redis://` or `rediss://` URL. Query options/fragments are rejected to keep connection security and bounded timeout/retry behaviour explicit. No Redis credentials were supplied, stored, logged, or tested live during implementation.
- Use an authenticated, private Redis deployment, TLS when appropriate for the network, certificate verification for TLS, and suitable Redis ACLs. Never expose Redis publicly without protection. Connection details and network controls remain deployment responsibilities, not verified facts.
- Every worker must share the same Redis database, prefix, and policies. Isolate environments using different prefixes. Redis must not evict limiter keys before expiry; eviction or lost Redis state can reset allowances. This is a request throttle, not a durable billing quota or concurrency cap for agent jobs.
- Quota changes are configuration-driven and require coordinated process restart/redeployment; no live control-plane or automatic policy reload was requested or built. Because library keys incorporate quota/window, changing policies creates new counters. Keep worker configurations consistent. Moving-window timestamps use worker clocks; synchronize host clocks.
- Atomic Redis Lua scripts from `limits` prevent concurrent workers from independently exceeding a shared quota. Keys expire automatically.
- Quota exhaustion returns friendly 429 with `Retry-After`. Redis request failures return friendly 503. Startup requires Redis connectivity. There is no in-memory fallback, transport retry, or allow-on-error path. Requests rejected by an additional policy may already have consumed the aggregate default quota.
- This per-user throttle runs after Firebase verification. It does not limit invalid/unauthenticated traffic or protect the verification service itself; pre-authentication abuse protection needs the actual trusted proxy/gateway and a separate agreed policy. Do not trust arbitrary forwarded IP headers.
- Added production dependencies `limits[redis]` and `pydantic-settings`; added development-only `fakeredis[lua]` to execute library Lua scripts in tests without contacting production or enabling any Firebase emulator. The fake Redis is confined to tests.
- `tests/test_rate_limits.py` covers shared quotas across endpoints and simulated workers, separate users, named policies, 64 concurrent attempts against a quota of 10, expiry, Redis failure at startup and during requests, invalid/missing policies, expiring hashed keys, and reuse of authentication in the actual app. All 10 tests passed with `uv run python -m unittest discover -s tests -v`; `uv pip check` and `uv lock --check` also passed. This does not establish live self-hosted Redis connectivity, real multi-host performance, real Firebase behaviour, or production readiness.

## Official references consulted

- Rate-limiting library usage: https://limits.readthedocs.io/en/stable/quickstart.html
- Redis storage configuration: https://limits.readthedocs.io/en/stable/storage.html

- Firebase Admin setup: https://firebase.google.com/docs/admin/setup
- ID token verification: https://firebase.google.com/docs/auth/admin/verify-id-tokens
- Session revocation: https://firebase.google.com/docs/auth/admin/manage-sessions
- Python Admin auth reference: https://firebase.google.com/docs/reference/admin/python/firebase_admin.auth
- Google browser sign-in: https://firebase.google.com/docs/auth/web/google-signin
- Email/password browser authentication: https://firebase.google.com/docs/auth/web/password-auth

Installed Firebase Admin source was also inspected for verification and revocation behaviour before implementation. Recheck current source and documentation when extending this work.
