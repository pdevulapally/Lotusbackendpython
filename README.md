# LotusBuild API

FastAPI with shared Firebase authentication, Redis rate limiting, validated
configuration, request safeguards, structured logs, and private health probes.

Shared Firestore resource authorization is documented in [AUTHORIZATION.md](AUTHORIZATION.md).
It uses the agreed owner-managed, team-capability-gated sharing model. Billing is
separate. The first resource endpoints support private projects.

## Development

Requires Python 3.14 or later, uv, real Firebase credentials, and Redis. No Firebase
emulator is used. Install the locked dependencies:

```powershell
uv sync --locked
Copy-Item .env.example .env
```

Fill the required values in `.env`. It is ignored by Git and excluded from Docker
builds. Never store a service-account key in this repository. Use an external key
file for local development or an attached service identity where supported.
If using an attached identity, remove/unset `GOOGLE_APPLICATION_CREDENTIALS`
rather than leaving it empty.

The application does not silently load `.env`; explicitly supply it:

```powershell
uv run --env-file .env python serve.py
```

Application construction is now `main:create_app`, an application factory.
There is no import-time application that reads deployment secrets. The earlier
`fastapi dev main.py` command has been replaced by the command above. An explicit
development reload command is also possible:

```powershell
uv run --env-file .env uvicorn main:create_app --factory --reload --host 127.0.0.1 --port 8000 --no-access-log --no-proxy-headers
```

Set `API_DOCS_ENABLED=true` to use `/docs` and `/openapi.json` during development.
When enabled, these framework documentation routes are public (with the IP guard),
not user-authenticated. Keep this an explicit deployment policy.

Local-only example settings, to be chosen explicitly rather than used as defaults:

```dotenv
API_ENVIRONMENT=development
REQUIRE_HTTPS=false
CORS_ALLOWED_ORIGINS=["http://localhost:3000"]
ALLOWED_HOSTS=["localhost","127.0.0.1"]
SERVER_HOST=127.0.0.1
SERVER_PORT=8000
SERVER_WORKERS=1
SERVER_LIMIT_CONCURRENCY=100
SERVER_KEEP_ALIVE_SECONDS=5
SERVER_GRACEFUL_SHUTDOWN_SECONDS=30
SERVER_TRUSTED_PROXY_IPS=[]
```

These are examples for a local machine, not inferred production domains or server
capacity. All application/server configuration fields remain required.

## Shared modules

| Module | Responsibility |
| --- | --- |
| `config.py` | Required, validated API/server settings and policies |
| `auth.py` | One Firebase initialization and verified `CurrentUser` dependency |
| `authorization.py` | Identity-bound resource permissions and protected mutations |
| `authorization_models.py` | Strict Firestore permission/resource/grant schemas |
| `database.py` / `firestore_access.py` | One Firestore client and bounded atomic access transactions |
| `rate_limits.py` | One Redis pool, shared user/IP counters, named policies |
| `observability.py` | Request IDs, structured logs, consistent application errors |
| `request_limits.py` | Host/origin checks, request-size limits, request timeout |
| `health.py` | Private operational liveness and readiness routes |
| `main.py` | App factory, shared middleware/lifespans, API routes |
| `serve.py` | Configured Uvicorn worker, proxy-trust, concurrency, shutdown settings |

Use `CurrentUser` in authenticated route parameters. All FastAPI API routes also
inherit the aggregate default rate limit and authentication requirement. Adding
`Depends(RateLimit("policy_name"))` applies an additional policy from
`RATE_LIMIT_POLICIES`. Policy names are server-owned, never request parameters.
Operational health probes are mounted separately so they do not inherit user
authentication or consume user/IP quota.

## Private projects

### Daytona workspace runs

Save a session question, then send an authenticated
`PUT /api/v1/projects/{project_id}/sessions/{session_id}/run`. The response is
202. Reuse the same URL to submit idempotently, or GET it for status, answer
and per-edit patches after reconnecting. An owner can submit one active run per
project. Run submission requires the configured project update permission and
uses both the API rate limit and the individual plan's shared Redis run limit.
Individual Free/paid plans are supported. Team spending awaits the shared ledger.

During an active run, send a correction with
`PUT .../run/messages/{message_id}` and a JSON body such as
`{"question":"Keep the existing layout and change the heading."}`. Generate a
UUIDv4 for each new message and reuse it on retry. A 202 response acknowledges
the saved message. GET the run to recover accepted messages in order. A correction
retains the run's permissions, deadline and model-call limit.

Send `PUT .../run/stop` to stop. An in-flight model call can finish, and its
usage can still count. The dispatcher discards obsolete decisions before reserving
another workspace action. A reserved write can complete. Its confirmed patch
remains in the run review. The API reports `stopping` until the worker settles
the run, or `awaiting_workspace_receipt` when a write still needs confirmation.
Repeat Stop to check an outstanding receipt. Run the updated dispatcher and API
together and provide the existing `WORKER_CONTEXT_MAX_BYTES` setting to both.

Append `?mode=read_only` to the first PUT when the agent should only inspect
the project and answer. Omitting the mode keeps the existing edit behavior.
The mode is fixed for the run. The dispatcher enforces it when selecting tools
and before executing a write.

Session messages must be nonempty UTF-8 text within the configured byte limit.
HTML and SQL examples are valid text. The API rejects unsupported control
characters and bidirectional formatting controls. Session and run responses
escape HTML-sensitive characters in JSON. After decoding JSON, clients must
render messages, answers and diffs as text or sanitize rendered Markdown.
Workspace observations are identified as untrusted data in Claude's context.
The dispatcher checks tool inputs and sandbox receipts independently of Claude.

Run `python -m workspace_dispatch` as a separate supervised backend service
using the application environment and the `WORKER_*` settings in `.env.example`.
Firestore needs a collection-scope index on the configured AI runs collection
for `status ASC, available ASC`. Synchronize dispatcher clocks. Set the run
deadline for the entire task, including sandbox startup. Size the context and
file limits within Firestore's document limit. The dispatcher persists observed
model usage, answers, tool observations (including file text), and review patches
in private Firestore records. Set retention for these records before production.

Build a private Linux desktop snapshot using `deploy/Daytona.Dockerfile` from
the repository root. Supply the approved desktop base image, its non-root
`WORKSPACE_USER`, and an absolute `WORKER_STATE_DIR` outside the project.
Publish the snapshot through your Daytona deployment and supply its identifier
in `WORKER_SNAPSHOT`. The writable workspace and state directories must have
existing writable parents. Configure the desktop to open `WORKER_WORKSPACE`.
Keep provider credentials on the backend. The worker image needs only Python 3
(3.10 or later) and Git. Each private sandbox has outbound networking blocked.

The first workspace action lazily creates or reopens the project sandbox and
initializes its Git working tree. A daemon records a starting file manifest,
applies only project-relative tools, and journals each command by ID. The model
can answer, list files, read visible UTF-8 files, or create/revise them in existing
directories. It must read the target before writing. The daemon compares the
observed revision, atomically installs the new file, and returns a Git patch
against the prior working copy. It leaves the user's index and branch untouched.
Desktop programs writing without coordinating with the daemon can still race
the final revision check. Edit the same file from one surface at a time.

Daytona activity calls accompany active processing. The snapshot uses a
five-minute idle setting from configuration. Idle daemon polling stays inside
the sandbox. A stopped sandbox resumes on demand. If a previously bound sandbox
is missing, the dispatcher stops rather than silently creating an empty project.
Automatic deletion and workspace replacement are disabled in this integration.
Snapshot/restore before recycling, GUI integration, binary editing and desktop
activity verification remain deployment or development work.

A reserved write is never resubmitted by the dispatcher. After a lease expires
or a write outcome is unclear, it checks the original bound sandbox for the
receipt of the same run and step. A valid write receipt saves history and review
together. Recovery then interrupts the run, honours Stop, or continues with a
pending correction. A completed write that exceeds the model context budget
retains its review and ends the run. A confirmed pre-mutation rejection releases
the reservation. An interrupted model call is never repeated automatically.

**Recovering a reserved project.** Recovery makes at most three automatic receipt
attempts per batch, spaced by the worker lease after a failure. After exhaustion
the API reports `awaiting_workspace_receipt`. Access denial also suspends recovery.
For an already-stranded run, repeat the existing run PUT (or Stop) as the
authorized owner. This starts receipt-only recovery without a schema migration.
Upgrade every dispatcher before doing this. Update the sandbox worker in existing
workspaces to obtain explicit pre-mutation rejection receipts for future commands.

If recovery stays blocked, preserve the original sandbox, working tree, and
private SQLite journal. Check the slot's sandbox ID, active run and generation,
then locate the command UUID (UUIDv5, URL namespace, name `<run key>/<step>`).
Inspect its .out file and matching journal entry against the stored action and
generation. Resolve access, storage capacity or connectivity issues before
requesting another batch. If only the .out file was lost, an operator can restore
the exact committed envelope from a verified matching journal entry in the
original sandbox while the daemon is quiescent. Legacy error envelopes, conflicting
replays and post-preparation errors cannot prove the target was untouched.
Escalate missing or ambiguous evidence for forensic recovery. Clearing the
slot, fabricating a receipt or rerunning the write can lose user changes.

All three endpoints use the verified Firebase identity and shared Redis limits.

| Request | Result |
| --- | --- |
| `POST /api/v1/projects` with `{"name":"My project"}` | Creates a private project and returns `{id, name}` with HTTP 201 |
| `GET /api/v1/projects/{project_id}` | Returns the owner's project. Missing and inaccessible projects both return 404 |
| `GET /api/v1/projects?limit=…&after=…` | Returns `{projects, next_cursor}`. Omit both parameters for the first page. Pass the previous cursor as `after` until it is null |

Set `PROJECT_RESOURCE_KIND`, `PROJECT_CREATE_PERMISSION`, and `PROJECT_READ_PERMISSION`
to the kind and corresponding actions provisioned in the Firestore authorization policy.
Set `PROJECT_NAME_MAX_BYTES` to the permitted UTF-8 size of a trimmed project name.
`AUTHORIZATION_MAX_BATCH_RESOURCES` controls the default and maximum page size.
Pages sort by document ID. Each page reads current data, so creation between requests
can affect a traversal. After an uncertain create response, refresh the project list
before resubmitting. Repeating a POST can create another project.

A client can save a question with `PUT /api/v1/projects/{project_id}/sessions/{session_id}`
and JSON `{"question":"Where does sign-in happen?"}`. Generate a UUIDv4 once on the
client and keep it for retries and the reopen URL. GET on the same URL returns the
saved question. Repeating a PUT with a different question returns 409.
Set `PROJECT_SESSIONS_COLLECTION`, `PROJECT_SESSION_WRITE_PERMISSION` (a project
update action), and `SESSION_QUESTION_MAX_BYTES`. Both routes verify the current
project owner inside the transaction. At this checkpoint saving a session stores
the question only. Background processing and file-backed answers are pending.

## Configuration

`.env.example` documents every required setting. Counts, time windows, origins,
hosts, resource limits, credentials locations, and Redis URLs are not embedded in
request handlers. The approved initial request policies are:

- Per user: 60 requests in a moving 60-second window, shared across API endpoints.
- Before authentication: 300 requests per source IP in a moving 60-second window.
- Request body: 1 MiB, checked both for declared size and while consuming a stream.
- Ordinary request timeout: 30 seconds.

Policies are validated at startup. Invalid configuration rejects startup with no
guessed defaults. Values are loaded per worker and require coordinated restarts
when changed. Changing a quota/window creates new library counter keys. Keep
settings and key prefixes consistent across workers and isolate environments.

`RATE_LIMIT_DEFAULT_POLICY` selects the aggregate policy by name from
`RATE_LIMIT_POLICIES`; the name is not fixed in Python. `REQUIRE_HTTPS` explicitly
controls HTTPS enforcement for requests and configured browser origins.
`API_ENVIRONMENT` is a deployment label, not an HTTP-policy switch. Set
`REQUIRE_HTTPS=true` for the public API behind Caddy and false only when deliberately
running an HTTP environment. `LOG_LEVEL` accepts supported Python logging levels.
Both new policy settings are required; neither has a runtime default.

Redis URL query options are rejected so they cannot override the configured
timeout, retries, or certificate verification. `rediss://` uses TLS certificate
verification. There are no application transport retries or memory-store
fallbacks in the Redis limiter. Redis failures reject protected requests with 503.
Quota exhaustion returns 429 and `Retry-After`.

The IP limiter reads only the ASGI peer address supplied by Uvicorn. Only Uvicorn
may interpret Caddy forwarded headers, and only for `SERVER_TRUSTED_PROXY_IPS`.
Do not trust `*`, an unrestricted network, or client-controlled forwarded headers.
An IP quota is shared by people behind the same NAT. It complements the user
quota; it is not an account or billing allowance.

The library uses worker clocks for moving windows: synchronize host clocks. Redis
must be private, authenticated appropriately, and sized/configured not to evict
active counter keys. Redis restarts/state loss can reset allowances. This is not
a durable usage ledger or an agent-job concurrency limiter.

## Next.js / Firebase browser integration

Enable Email/Password and Google sign-in in the actual Firebase project. Authorize
the actual frontend domains in Firebase. Reuse the frontend's existing Firebase
app/auth instance; never send Admin credentials to the browser.

Set `CORS_ALLOWED_ORIGINS` to the exact frontend origins, including scheme and port.
Origins require HTTPS when `REQUIRE_HTTPS=true`. When HTTP is deliberately enabled,
only the exact configured origins are allowed; there is no built-in localhost
exception. Use bearer tokens, not browser cookies, for this API.
CORS allows the Authorization/Content-Type headers and exposes request IDs and
retry times. CORS is not authentication.

The browser call, using the existing signed-in Firebase user and configured API
origin/request timeout, is:

```javascript
await auth.authStateReady();
if (!auth.currentUser) throw new Error("Please sign in to continue.");
const token = await auth.currentUser.getIdToken();
const response = await fetch(new URL("/api/v1/users/account", apiOrigin), {
  headers: { Authorization: `Bearer ${token}` },
  redirect: "error",
  signal: AbortSignal.timeout(requestTimeoutMilliseconds),
});
if (!response.ok) throw new Error(`Account request failed (${response.status}).`);
const account = await response.json();
```

`auth`, `apiOrigin`, and `requestTimeoutMilliseconds` come from the real frontend's
SDK/configuration; this is a contract example, not an implemented login screen.
The SDK handles ID-token refresh. An expired/revoked token receives 401; do not
silently downgrade it to anonymous access or endlessly retry it.

The account response contains `uid`, nullable `email`, and `email_verified`.
Application errors contain:

```json
{
  "error": {"code": "unauthorized", "message": "Please sign in to continue."},
  "request_id": "a server-generated request identifier"
}
```

Use `X-Request-ID` when investigating failures. Reverse proxies and protocol/CORS
failures may produce different bodies; clients should not assume every failed
response is JSON. Actual frontend sign-in and cross-origin browser behaviour
still need verification with the real domains/project.

## Health, logging, and timeouts

- `GET /health/live`: the process can respond; it does not contact dependencies.
- `GET /health/ready`: startup completed and Redis currently responds. Firebase
  and Firestore clients were initialized at startup; this does not claim that a
  live login or Firestore resource check was performed. Those are checked on access.
- During shutdown, readiness is cleared before dependency cleanup.
- Probe the private API listener. The supplied Caddy configuration hides public
  `/health` URLs. Do not expose the API listener publicly to bypass Caddy.
- JSON application logs contain a generated request ID, method, route template,
  status, duration, and safe failure type. They omit bodies, raw URLs/query
  strings, identities, tokens, headers, and exception messages. Uvicorn access
  logging is disabled to avoid a second raw-URL log stream.
- Collect stdout in the deployment's chosen log system and alert on readiness
  failures, unexpected 5xx rates, sustained 429 rates, and Redis memory/eviction.
- Request timeout cancels async request handling. Python cannot forcibly stop a
  synchronous worker thread or undo completed side effects. SDK network timeouts
  and server concurrency are configured separately. Long agent jobs must use
  durable background execution, not an open request or background task here.
- Started response streams cannot be replaced with another JSON error; failures
  interrupt the stream and emit a safe correlated application event.

## Verification

Run the saved checks locally or from your chosen CI system:

```powershell
uv sync --locked
uv run python -m scripts.check
```

This runs Ruff formatting/lint, Pyright, tests, dependency compatibility, and lock
verification. Tool versions are locked. The normal suite uses fake Redis with Lua
support and mocked Firebase operations; it never enables a Firebase emulator or
connects to real services.

For an explicit real-service smoke check, configure dedicated real test
Firebase/Firestore/Redis services. Supply `INTEGRATION_TARGET_ENVIRONMENT` matching
`API_ENVIRONMENT`, an allowed `INTEGRATION_API_ORIGIN`, the simulated
`INTEGRATION_CLIENT_IP`, and a fresh test user's `INTEGRATION_ID_TOKEN` and
`INTEGRATION_EXPECTED_UID` in the process environment:

```powershell
uv run --env-file .env.staging python -m scripts.check_services
```

It verifies real Firebase account access, rejected missing/invalid tokens, the
configured Firestore authorization policy in a transaction, and Redis-backed
request handling. It uses separate expiring Redis counter keys and
does not clear existing counters. It runs the ASGI app in-process; it does not
verify real TLS, Caddy routing, browser Google login, revocation changes, or
multi-host performance. Do not print or commit the supplied token.

## Production behind Caddy

`deploy/Caddyfile` is a reviewable configuration to merge into the existing Caddy
setup, not a command to replace or reload that setup. Supply the actual
`API_DOMAIN`, `API_UPSTREAM`, `REQUEST_MAX_BODY_BYTES`,
`REQUEST_TIMEOUT_SECONDS`, and `CADDY_UPSTREAM_TIMEOUT_SECONDS` to the Caddy
process. The upstream header timeout must exceed the API request timeout so the
API can return its own timeout response first. No actual domain is assumed.

1. Use your production environment label, `REQUIRE_HTTPS=true`, exact production hosts/origins, real Firebase
   service credentials with appropriate permissions, and a private Redis connection.
2. Bind the API to loopback when Caddy shares the host, or to a private interface
   when they are separate. Restrict network access to Caddy and private probes.
3. Set the Uvicorn trusted proxy IPs to the actual Caddy peer addresses/networks.
   Do not broaden trust just to fix an incorrectly configured network. If another
   proxy/CDN precedes Caddy, explicitly configure its trust boundary separately.
4. Caddy handles certificates/HTTPS redirection, forwarded headers, header/body
   read deadlines, the body-size ceiling, and upstream header timeout. The API
   also enforces HTTPS for non-health requests when `REQUIRE_HTTPS=true`, based on the
   trusted ASGI scheme. Do not disable that check to conceal proxy mistakes.
5. With the deployment's installed Caddy version and real environment, run
   `caddy validate --config deploy/Caddyfile --adapter caddyfile` before an authorized
   reload. Caddy versions must support the directives shown. If the existing
   Caddyfile has a global options block, merge its server timeouts into that block.
6. Restrict health endpoints at the edge as shown and probe them privately. Check
   a real browser login and API request through HTTPS, including a 401 and 429.

The IP limiter is inside the API and protects Firebase verification; it does not
prevent a volumetric network attack from reaching Caddy. Keep the actual host's
network/firewall protections and connection resource limits in place.

## Container, release, and rollback

`Dockerfile` builds a non-root Linux image and copies only application files and
locked production dependencies. `.dockerignore` uses an allowlist so credentials,
environment files, tests, local caches, and future unrelated files are not sent
in the build context.

Supply approved immutable image references for `PYTHON_IMAGE` (a Python 3.14+
Debian slim image) and `UV_IMAGE` (the official Astral uv image). The required build
arguments intentionally have no guessed image/digest defaults. Both Python stages
use the same image reference. Build with:

```text
docker build --build-arg PYTHON_IMAGE=<approved-python-image@sha256:digest> --build-arg UV_IMAGE=<approved-uv-image@sha256:digest> -t <release-image> .
```

Supply secrets at runtime, never through build arguments or image layers. Run
with a read-only root filesystem, a writable temporary filesystem if required,
dropped capabilities, no privilege escalation, a restart policy, and explicit
memory/CPU limits selected for the host. Publish only a loopback/private listener.
When using containers, configure the API bind address and Caddy peer trust for
that network rather than copying the same-host loopback example.

The runtime runs `serve.py`: no reload/debug mode, configured workers/concurrency,
bounded keep-alive, and graceful shutdown. Give the container/process supervisor
a stop grace period longer than `SERVER_GRACEFUL_SHUTDOWN_SECONDS`. Use the private
health routes as startup/readiness/liveness probes. Choose the restart/probe
mechanisms in the actual host/orchestrator; no hosting platform is assumed here.

Before releasing: pass the saved checks, real staging verification, Caddy config
validation, actual container build, and a staging restart/shutdown smoke check.
Deploy an identifiable image and retain the previous working image/configuration
for rollback. Rollback replaces the release with the previous verified version;
it must not invent older credentials or discard stored data. This backend has no
database schema migrations yet.

No deployment, Caddy reload, DNS/certificate change, or provider configuration
change is authorized merely by these files. Those operations require the actual
deployment details and explicit operational authorization.
