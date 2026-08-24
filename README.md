# ota-server

The hub. Devices, CI and the web UI all talk to this.

---

## Three jobs, deliberately separated

People build this as one monolith and regret it at scale. It is three things:

| Job | Component | Traffic | Scaling |
|---|---|---|---|
| **Serve content** | nginx over an `archive`-mode OSTree repo | gigabytes | static files, content-addressed, CDN-friendly |
| **Decide policy** | FastAPI app (`api/`) | kilobytes | one heartbeat per device per interval |
| **Sign metadata** | offline/CI signer | negligible | keys never on the API host |

The important consequence: **`ostree pull` never touches the application server.** The agent
does plain HTTP GETs against `/repo`. The API only ever answers "which commit should this
device be on". A fleet-wide rollout is a traffic spike on a static file server, not on your
Python process.

Signing lives outside the API host on purpose. If the API is compromised, an attacker can
mis-direct devices — annoying, recoverable. If they can also sign, they own the fleet.

---

## API surface

### Device routes — mTLS client certificate

```
POST   /api/v1/devices/register        bootstrap: CSR in, signed cert + device_id out
POST   /api/v1/devices/{id}/heartbeat  current commit, app version, health, uptime
GET    /api/v1/devices/{id}/update     -> signed target, or 204 if nothing to do
POST   /api/v1/devices/{id}/report     install outcome: success | *_failed | rollback
```

### Operator routes — OIDC bearer token

```
POST   /api/v1/targets                 CI registers a published build
GET    /api/v1/targets?branch=…        list builds
POST   /api/v1/rollouts                {target, group, strategy, thresholds}
GET    /api/v1/rollouts/{id}           live progress
POST   /api/v1/rollouts/{id}/pause     emergency stop
POST   /api/v1/rollouts/{id}/resume
GET    /api/v1/fleet                   device list, versions, last seen
WS     /api/v1/events                  live device + rollout events for the UI
```

**Keep these on separate routers with separate auth dependencies.** A device credential must
never be able to create a rollout, and a leaked operator token must not be usable as a device.
The scaffold enforces this structurally (`devices` / `operators` routers in
[api/main.py](api/main.py)) and nginx enforces it again at the edge by requiring a verified
client cert for `/api/v1/devices/`.

---

## The one interesting endpoint

`GET /devices/{id}/update` is where all the product logic lives. Everything else is CRUD.

Order of checks:

1. Device known, registered, not decommissioned.
2. Device **pinned**? → 204. (Pinning happens automatically after a rollback, so a broken
   device is never re-offered the commit that broke it.)
3. Device's group has an active, non-paused rollout? → else 204.
4. Device is in the current wave of that rollout? → else 204.
5. Inside the device's maintenance window? → else 204.
6. Device already on the target commit? → else 204.
7. Return the signed target: commit, repo URL, size, signature, hardware-id.

Every 204 path is a policy decision. Log which one fired — "why is this device not updating"
is the single most common operational question, and without that log line the answer takes
an hour.

---

## Rollout strategies

| Strategy | Behaviour |
|---|---|
| `canary` | N devices or X%, soak, then require explicit promotion |
| `staged` | waves (e.g. 5% → 25% → 100%) with a soak time between them |
| `all` | everything at once — dev/test only |

Cross-cutting controls:

- **Auto-halt** — failure rate above threshold, or any rollback, pauses the rollout. Default to
  halting; a rollout that keeps going through failures is a fleet-wide outage.
- **Maintenance windows** — per group or device-local time.
- **Pinning** — device held on a known-good commit, never offered updates.
- **Manual promotion gate** — production waves need a human (enforced in CI too, see
  `ota-infra`).

Concrete thresholds are undecided — `DECISIONS.md` D12.

---

## Data model sketch

```
device      id, cert_fingerprint, hardware_id, group_id, branch,
            current_commit, pending_commit, app_version, healthy,
            last_seen, pinned, pin_reason
group       id, name, maintenance_window, default_branch
target      branch, commit, version, size, signature, created_at, created_by
rollout     id, target, group, strategy, waves, soak, thresholds, state
rollout_dev rollout_id, device_id, wave, state, result, detail, updated_at
event       device_id, kind, payload, ts        -- append-only audit trail
```

`event` is append-only on purpose. When a rollout goes wrong the postmortem needs the ordering
of what each device reported, not the current state.

---

## Running locally

```bash
ostree init --repo=./ostree-repo --mode=archive
docker compose -f deploy/compose.yml up --build

# API   http://localhost:8000/api/v1   (docs at /docs)
# Repo  http://localhost:8080/repo
```

`archive` mode is required for a served repo — objects are individually compressed files
fetchable over HTTP. `bare`/`bare-user` modes are for repos that are checked out, i.e. on the
device.

Sanity-check the content path without any device:

```bash
ostree --repo=/tmp/testrepo init --mode=archive
ostree --repo=/tmp/testrepo remote add --no-gpg-verify ota http://localhost:8080/repo
ostree --repo=/tmp/testrepo pull ota <branch>
```

If that works, the agent will work. If it does not, no amount of agent debugging helps.

---

## Repo maintenance

The object store needs housekeeping the API should own:

- `ostree summary --update` after every publish — the summary is what clients read to discover
  refs and deltas.
- `ostree prune --keep-younger-than=…` / `--refs-only` to reclaim space from superseded
  commits. **Never prune a commit a device might still be running** — that device can no longer
  compute a delta, and worse, cannot re-pull its own current version for repair.
- Static delta generation between recently published commits (`DECISIONS.md` D8).

Retention policy is a product decision: how far back must a device be able to update *from*?
That number is your prune horizon.

---

## Deployment

`deploy/` holds a working single-host setup: `api` (uvicorn), `repo` (nginx), `db` (postgres).

nginx config ([deploy/nginx.conf](deploy/nginx.conf)) does three things worth noting:

- serves `/repo/` as static files, `immutable` cache headers on `objects/` and `deltas/`,
  `no-cache` on `refs/` and `summary` — correct because object paths are content hashes
- terminates mTLS for `/api/v1/devices/` and rejects unverified clients before they reach Python
- passes the client cert CN through as `X-Device-CN` for the API to bind to a device row

Production shape: repo objects on S3/MinIO with a CDN, API behind a normal load balancer,
postgres managed. See `DECISIONS.md` D7.

---

## Layout

```
api/            FastAPI app — routers, models, rollout engine, signing client
ostree-repo/    local archive repo (gitignored; a volume or bucket in real deployments)
migrations/     alembic
deploy/         Dockerfile, compose.yml, nginx.conf
tests/          device-flow integration tests
docs/
```

---

## Testing

The flows worth integration tests, because they are the ones that hurt:

- Full device lifecycle: register → heartbeat → offer → report success.
- Rollback report auto-pauses the rollout and pins the device.
- A device credential is rejected on every operator route (and vice versa).
- Two devices in different waves get different answers from `/update` at the same moment.
- Repo publish is atomic from a reader's perspective — a device polling mid-publish never sees
  a ref pointing at objects that are not there yet.
