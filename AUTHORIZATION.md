# Shared resource authorization

This is a new server-side resource contract. It replaces the proposed private
per-user path design; no existing resource schema or team/billing records were
found or migrated. No production documents, policies, IAM settings, or Firestore
rules have been changed.

## Agreed access rules

- Resources use shared collections with server-controlled ownership and grants.
- Only the owner can manage sharing. An edit grant does not grant sharing rights.
- Directly invited recipients must first be active members of the resource's team.
- Membership alone does not grant a resource: a resource's team grant or a direct
  grant is also required, intersected with the member's allowed actions.
- Public and shared-link access is explicitly read-only. Anonymous visitors cannot
  create, update, delete, grant, or revoke. Signed-in visitors cannot use public
  or link access as a write grant either.
- All sharing also requires a trusted team capability record with
  `sharing_enabled: true`, plus active team membership for the resource owner.
  Missing/disabled capability records disable shared access. Private owner access
  remains possible. Owners can revoke or clear sharing even while it is disabled.
- Billing/subscriptions are deliberately not implemented. There is no hardcoded
  plan-name comparison or inferred entitlement. A trusted future entitlement
  publisher must enable this capability only for eligible Teams accounts.
- New resources are private. Missing metadata is an error/denial, never a guessed
  owner, implicit public resource, or permissive grant.

Public/link read access can reach people outside the team because the owner has
explicitly enabled public/link access. The active-team-member requirement applies
to direct user/team grants and editing, not public visitors.

## Shared modules and binding

- `authorization_models.py` defines strict stored-record schemas.
- `authorization.py` provides one permission engine and identity-bound FastAPI
  dependencies: `Authorization` and `RequireResource`.
- `database.py` initializes one Firestore RPC client per worker, reusing the
  existing Firebase app's credentials and the configured project/database.
- `firestore_access.py` performs bounded, single-attempt transactions with explicit
  transaction IDs on document reads and commits. It reuses the locked Firestore
  SDK's value encoders/decoders rather than inventing a serialization format.

`Authorization` takes its principal from `CurrentUser`, never an HTTP user/owner
field. Its public methods have no caller-UID parameter. Resource IDs and parent
IDs select records but cannot change that authenticated principal.

Use `RequireResource(kind, permission, id_parameter, parent_parameter)` as a read
dependency. Kind/action/parameter names are server route declarations, not client
permission claims. `Authorization.read` returns only the resource's `data` after
the read transaction succeeds. Metadata and grant lists are not returned.

For writes, call the identity-bound `Authorization.update_data`, `delete`,
`create_private`, or sharing methods directly. Do not perform a read dependency
check and then issue an unprotected SDK write. The write methods recheck resource,
policy, capability, membership, and grant state inside the transaction that
commits the mutation. Concurrent changes produce 409 rather than a blind write or
an application retry. Reads of another user's resource and nonexistent resources
produce the same generic 404. Missing/invalid policy or provider failures fail
closed with a generic error; no personal data or provider details are returned.

## Required ENV configuration

`AUTHORIZATION_COLLECTIONS` is a JSON object with these six required keys:

| Key | Collection purpose |
| --- | --- |
| `resources` | Shared resource records |
| `teams` | Server-owned team sharing capability records |
| `members` | Membership subcollection under each team |
| `grants` | User-grant subcollection under each resource |
| `links` | Hashed link-grant records |
| `policies` | Server-owned resource/action policy records |

Actual collection names come from ENV, not Python. The four root collections
must be distinct. Membership/grant UID document IDs use `identity_key(uid)`:
unpadded URL-safe base64 of the exact Firebase UID, supporting UIDs that cannot
be used as raw Firestore path segments.

Also required: `FIRESTORE_DATABASE_ID`, `FIRESTORE_TIMEOUT_SECONDS`,
`AUTHORIZATION_POLICY_ID`, `AUTHORIZATION_LINK_TOKEN_BYTES`,
`AUTHORIZATION_MAX_PARENT_DEPTH`, and `AUTHORIZATION_MAX_BATCH_RESOURCES`.
The link entropy floor and Firestore's segment constraints are validation
invariants. No database, collection name, action catalog, or role is guessed.
Firebase/Firestore emulator configuration remains rejected.

## Stored schemas

The field names below are this API's explicit data contract, not ENV configuration.
All fields are required unless represented by an explicit JSON null. Extra fields
are rejected: keep unrelated team-profile/plan metadata in its own domain records.

### Policy: `{policies}/{AUTHORIZATION_POLICY_ID}`

`resources` maps each supported resource kind to:

- `read_permissions`, `update_permissions`, `delete_permissions`,
  `create_permissions`: explicit action-name arrays. Each action must belong to
  exactly one operation category. An endpoint cannot use a read action to update
  or delete data. There is no wildcard permission or built-in role catalog.
- `shareable_permissions`: actions owners may grant, drawn from the operation
  action sets.
- `public_read_permissions`, `link_read_permissions`: actions eligible for those
  modes, restricted to shareable read actions.

Provision the actual endpoint action catalog server-side. No production policy
or example roles are automatically seeded. Changing policy takes effect on the
next transaction. Stored stale/malformed grants do not become broader grants.

### Resource: `{resources}/{resource_id}`

Fields: `kind`, `owner_uid`, `team_id` (string or null), `parent_id` (string or null),
`access_epoch`, `data` (JSON-compatible object), `team_permissions` (action array),
and `public_permissions` (read-action array).

`create_private` generates the opaque resource ID and access epoch server-side,
takes ownership from the bound Firebase principal, and sets both sharing arrays
empty. The caller must explicitly choose a team ID or null. Team creation requires
an existing team and an active membership permitting the configured create action.
It currently creates root resources; a child-creation workflow requires its own
parent-create permission contract before being exposed.

`update_data` replaces only `data`. Submitted `owner_uid`, `team_id`, or permission
keys inside that object remain ordinary payload; they cannot modify the protected
metadata. Resource endpoints must separately validate their business payloads.
Ownership/team transfers are not exposed. Any future transfer must rotate the
access epoch atomically so old grants and links cannot regain access.

Parent relationships are checked against stored metadata and any parent ID bound
by the route. Ancestors must exist, stay in the same team, and, for personal
resources, have the same owner. Cycles and configured depth overflow fail closed.
Permissions do not implicitly inherit from parents; a directly shared child need
not expose its parent's data.

`set_sharing` changes the team-wide/public grants; it does not silently remove
individual invitations or links. Use the owner-only `make_private` operation to
clear team/public grants and rotate the access epoch atomically, invalidating all
previous direct grants and links. No unbounded subcollection scan is required.
This affects the selected resource; children have independent permissions, so
subtree-wide privacy changes require an explicit future contract.

### Team: `{teams}/{team_id}`

Field: `sharing_enabled` (strict boolean). Only trusted server administration or
the future entitlement publisher may create/update it. Resource owners and
membership/grant API callers cannot enable this flag through this service.

### Membership: `{teams}/{team_id}/{members}/{identity_key(uid)}`

Fields: `uid`, `active` (strict boolean), `permissions` (action array). Pending,
absent, inactive, or mismatched-UID records do not authorize access. Team invitation
issuance/acceptance, email delivery, and team membership administration are not
implemented here. That future workflow must verify identity and write the accepted
Firebase UID; an email string alone is not a membership.

### User grant: `{resources}/{resource_id}/{grants}/{identity_key(uid)}`

Fields: `uid`, `issued_by_uid`, `team_id`, `access_epoch`, `permissions`,
`expires_at` (timezone-aware ISO timestamp string or explicit null).

Only the resource owner can grant/revoke. Recipients must already be active team
members. Grants are bounded by both policy and current member permissions. Revoked,
expired, wrong-team, wrong-issuer, or stale-epoch grants do not authorize access.

### Link: `{links}/{sha256(secret_token)}`

Fields: `resource_id`, `resource_kind`, `issued_by_uid`, `team_id`, `access_epoch`,
`permissions`, `expires_at` (timezone-aware ISO timestamp string or explicit null),
and `revoked` (strict boolean).

`create_link` generates a cryptographic token and returns it once as `SecretStr`.
Only its SHA-256 digest is stored as the document ID. Permissions are read-only,
resource-bound, issuer/team/epoch-bound, and rechecked against current capability
and policy. `revoke_link` remains available to the owner when sharing is disabled.
`revoke_link_id` also permits owner revocation by the stored digest ID without
retaining or recovering the original token. Future owner-only management endpoints
can use those IDs without exposing link secrets.
Expiry is an explicit caller choice: future timezone-aware time or null for no
expiry. There is no guessed lifetime or silent default. Revocation and expiry are
checked on every access; already downloaded content cannot be recalled.

## Lists, streams, and route integration

`read_many` checks a bounded explicit batch in one transaction and returns nothing
if any requested resource is unauthorized. It is not an unrestricted Firestore
query. The private project routes use `create_private`, `read_owned`, and `list_owned`.
Each list query filters the resources collection by verified owner UID and configured
kind, orders by document ID, and reads at most one extra document to detect another
page. The cursor supplies only a position. Each page checks the current read policy
and verifies ownership, kind, and ancestry before returning project IDs and names.
Confirm index availability for the configured collection in staging. Invitation,
sharing-management, and public-view HTTP routes remain future work.

The engine supports an explicitly constructed anonymous read principal for public
or link views. No anonymous dependency or public API route was automatically
enabled; the current API's global authentication remains intact. A public route
must be deliberately mounted with IP/request safeguards and read-only handling.
If accepting a signed-in token there, invalid tokens must fail verification rather
than silently becoming anonymous. Pass link secrets via a protected header/body,
not a logged query string. Browser CORS permissions remain ENV-configured.

Long-running streams/jobs must recheck authorization before later protected work
and handle revocation; a one-time read decision is not a permanent permission.
Root-resource deletion makes its links/direct grants unusable and makes descendants
unavailable through ancestor checks. Firestore does not automatically delete
subcollections; orphan cleanup/retention is a separate explicit lifecycle task.

## Production verification boundary

`deploy/firestore.rules` denies direct client reads/writes for a server-only
database. Review it against the actual database and existing consumers before any
authorized deployment. Do not combine it with another broad allow rule: matching
allow rules can reopen access. Server SDKs bypass Security Rules, so IAM, private
credentials, and the application checks are all required. See
[Firebase's server-client rules guidance](https://firebase.google.com/docs/firestore/security/rules-structure).

Reads and writes use the same Firestore transaction. Every begin/read/commit RPC
gets an explicit deadline and `retry=None`; no read-modify-write fallback or
automatic application retry exists. Failed transactions attempt a separately
bounded rollback, and rollback failures are logged. A commit timeout has an
uncertain outcome and is never reported as success. Future create HTTP endpoints
need an idempotency contract before supporting retries. The code currently uses
the locked Firestore SDK's internal value codec helpers; keep codec tests and
review compatibility during dependency upgrades.

The normal tests use a test-only snapshot/commit simulator and mocked RPCs, not
real Firestore or a Firebase emulator. They verify cross-user/team denial, exact
action categories, route identity binding, membership/grant/capability revocation,
hashed/resource-bound links, expiry, metadata protection, ancestry, batch reads,
and aborted writes when permission state changes before commit. RPC tests verify
transaction IDs, deadlines, preconditions, rollback, and disabled retries.

The explicit real-service smoke command now also reads/validates the configured
Firestore policy in a transaction. It still requires real configuration and does
not itself prove deployed Rules/IAM, multi-user isolation, or concurrent revocation
against the live provider. Those checks, Caddy/TLS, actual API route integration,
and a deployed Firestore rules review remain release gates. No production-ready
deployment claim is established by local test results alone.
