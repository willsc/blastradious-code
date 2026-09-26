# blastradius

blastradius reads carrier and AWS maintenance emails from Gmail and publishes each one as a notice that many people can read and subscribe to. Every notice shows the blast radius: which circuit, which Direct Connect connection or VPN, which AWS gateways and route tables, which subnets and instances, and which applications are affected, plus a plain-language reason for each.

Example: *"Zayo TTN-0003581234 takes down `OGYX/123456//ZYO` → `ld5-zayo-10g` → `transit-zayo` → `core-dxgw` loses 1 of 2 paths (still served by `transit-colt`) → 10.100.0.0/16 from `prod-app-a/b` and `prod-db-a/b` is **degraded** → payments-api, ledger, reporting."* And when Colt schedules work on the other leg the same night, it raises a **collision**: together they are an **outage**.

```
Gmail API ─┐                ┌─ BCOP iCalendar ─┐                    ┌─ web UI (dashboard, timeline, graphs)
drop dir  ─┼─> classify ───>├─ AWS Health      ├─> notice (merged   ├─ JSON API
POST /api ─┘  (message       ├─ 40 carriers*   │   reminders,       ├─ per-subscription iCal + Atom feeds
               types)        └─ free text/HTML ┘   reschedules,     ├─ Slack · Teams · email · signed webhook · SNS
                                                   cancellations)   └─ reminders · collision alerts
                                                        │
          AWS (DX, LAG, VIF, DXGW, TGW + route tables, VPN, CGW, VPC route tables, subnets, ENIs, instances)
          + inventory (circuits, sites, prefixes, applications) ──> dependency graph ──> impact engine
                                                                   * optional circuit-maintenance-parser
```

## Try it (no Gmail or AWS needed)

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/blastradius demo            # -> http://localhost:8470
```

The demo uses a fictional three-account AWS estate (Direct Connect on Zayo and Colt, a Lumen LAG, a DX gateway, a transit gateway, and a Site-to-Site VPN over a BT leased line) and eleven realistic notices. The dates are relative to today, so the windows are always live. The notices show:

| Notice | What the engine concludes |
|---|---|
| Zayo BCOP iCal, then a rescheduling mail | One notice with history. **Degraded**: redundancy lost on `core-dxgw`, still served by the Colt leg |
| Colt HTML table (dd/mm dates, circuit given by alias) | **Degraded** on its own; **collision → outage** with the rescheduled Zayo window |
| BT internet leased line under the HQ VPN | **Outage** for payments-api and reporting, but only for 10.200.0.0/16. ledger is **not** affected because its subnets don't route to HQ |
| Lumen, one member of a 2-link LAG with `minimumLinks=2` | **Outage** for data-warehouse: one member down takes the whole LAG down |
| Equinix LD5 UPS work, "no impact expected", in progress | **At risk** for everything that lands in LD5 |
| AWS Health: DX maintenance, VPN redundancy loss | Anchored directly on `dxcon-…` / the VPN tunnel |
| GTT notice for an unknown circuit, then cancelled | Unmapped, flagged "not in inventory"; cancellation merged |
| Zayo newsletter | Ignored (it matches no message type) |

Open a notice to see the "why" per application, the blast-radius graph, the affected resources with the destinations each one loses, and the original emails. `/topology` → any node → **What if this fails?** runs the same engine on a hypothetical failure.

### Topology map

`/map` draws the whole estate as a network diagram, left to right: on-prem sites → carrier circuits → AWS edge → DX gateways → transit and virtual private gateways → VPCs with their subnets and hosts. The AWS edge column holds each Direct Connect location, with its connections and LAGs and their VIFs and VLANs, plus the internet customer gateways and their VPNs. Route-table names label the gateway → VPC links.

- **Live maintenance is painted on top.** You pick the window: in progress now, or 24 hours / 7 / 30 / 90 days ahead. ⚠ badges mark where the work happens, and collisions are included.
- **One notice at a time:** choose it from the overlay list, or use **Show on map** on a notice. Everything that notice doesn't reach is dimmed.
- **Filters:** account, VPC or application. The VPC filter is routing-aware: a VPC attached to a shared transit gateway only shows the DX/VPN paths its route tables actually send traffic over.
- **Inventory gaps are visible:** a dashed DX connection has no circuit mapped to it.
- **Controls:** drag to pan, Ctrl+scroll to zoom. **Download SVG** gives a standalone diagram for runbooks or change tickets. **Show on map** from any topology node centres the map on it.

## How the correlation works

**1. Parse.** Parsers are tried in this order. BCOP iCalendar (`X-MAINTNOTE-*`). AWS Health. Network to Code's [circuit-maintenance-parser](https://github.com/networktocode/circuit-maintenance-parser) if installed (`pip install '.[ntc]'`). A heuristic parser that always runs and reads maintenance windows in dozens of formats and time zones (BST/CET/EDT, `UTC+1`, dd/mm vs mm/dd per carrier, split date and time fields, crossing midnight, backup windows). It also reads circuit IDs, maintenance IDs, impact wording ("switch hit < 50 ms" = degraded, "no impact expected" = at risk) and status (cancelled, completed, rescheduled). Forwarded mails are unwrapped.

**2. Merge.** A notice's identity is provider + maintenance ID (then iCal UID, circuits + date, thread). Reminders attach without changing anything. Reschedules, cancellations and impact changes bump the notice version and notify subscribers. Older mails arriving late (lower iCal `SEQUENCE`, or an older `Date`) only fill gaps.

**3. Match.** Instead of guessing each carrier's ID format, blastradius looks for identifiers it already knows. These are inventory circuit IDs and aliases, AWS IDs, DX connection, VIF and LAG names, DX device and location names, site names, customer-gateway and tunnel IPs, and circuit IP blocks. Matching ignores separators (`OGYX/123456//ZYO` = `OGYX 123456 ZYO`) but respects token boundaries. Context mentions are pruned:
- A facility named next to a circuit is just that circuit's location.
- A LAG named next to one member means the member is affected.
- A connection named next to its circuit derives its impact from the circuit.
- A notice that names specific circuits never falls back to anchoring a whole site.

**4. Propagate.** The graph runs from dependency to dependent: circuit → DX connection (→ LAG) → VIF → DX gateway / VGW → TGW attachment → TGW route table → VPC attachment → VPC route table → subnet → ENI → instance → application. Each node combines its inputs in one of four ways:

| mode | used for | rule |
|---|---|---|
| `all` | most nodes | impaired if any dependency is |
| `any` | DX gateway (VIFs), VGW (VIFs, DXGWs, VPNs), VPN (tunnels) | down only if every path is down, otherwise **degraded (redundancy lost)** |
| `quorum` | DX LAG | down when fewer than `minimumLinks` members remain |
| `routes` | TGW and VPC route tables | per destination prefix: a subnet inherits impact only for prefixes that actually route over the broken path, each with its own severity |

Severities are **Outage** (no path left), **Degraded** (redundancy lost or quality impaired), and **At risk** (the provider says no impact, or a redundant component is in maintenance). A notice's severity is the worst severity reached by something people own (a subnet, workload or application), not the dead circuit itself.

**5. Collisions.** Overlapping windows are swept on a timeline and the union of their anchors is re-evaluated. Resources that end up worse together than under either notice alone produce a collision, which is shown on the dashboard and sent to subscribers.

**Re-correlation.** Active notices are recomputed whenever the topology is refreshed (AWS every `refresh_interval`) or the inventory file changes. If an application newly falls inside the blast radius, its subscribers get an `impact_changed` notification.

### Assumptions and limits

- Paths into the same VGW or DX gateway are treated as mutual backups, which matches AWS's own failover between DX and VPN for the same prefixes. AWS does not expose which prefixes each VIF carries.
- A TGW route table only shows the *best* route per prefix. A backup VPN that propagates into the same table is recognised as an alternate only when the inventory says which site owns the prefix (`sites[].prefixes`) and the VPN reaches that site. Otherwise impact is reported conservatively, with no failover assumed.
- The engine models reachability, not capacity. It doesn't consider security groups or NACLs (a link maintenance doesn't change them). TGW peering and Cloud WAN attachments are leaves.
- Correlation is only as good as the inventory. Unknown circuit IDs are shown on the notice so the gap can be fixed. Tagging DX connections and customer gateways with `CircuitId` is the lowest-effort way to close it.

## Production setup

### 1. Install

```bash
# systemd
python3 -m venv /opt/blastradius/.venv && /opt/blastradius/.venv/bin/pip install /opt/blastradius   # add [ntc] for more carrier formats
useradd --system --home /var/lib/blastradius blastradius && install -d -o blastradius /var/lib/blastradius
cp config.example.yaml /etc/blastradius/config.yaml; cp inventory.example.yaml /etc/blastradius/inventory.yaml
cp deploy/blastradius.service /etc/systemd/system/ && systemctl enable --now blastradius
```

Or use Docker: `docker-compose.yml` runs blastradius behind **oauth2-proxy** (Google sign-in). Only the proxy is published. Calendar and Atom feeds skip SSO because their URLs carry a secret token.

### 2. Gmail

- **OAuth (Gmail or Workspace):**
  1. In Google Cloud, enable the Gmail API and create an OAuth client of type *Desktop app*. Set `gmail.client_secrets`.
  2. Run `blastradius -c config.yaml gmail-auth`. On a headless box, forward the callback port first: `ssh -L 8765:localhost:8765 host`.
  3. The refresh token is stored in `gmail.token_file`, readable only by its owner (mode 0600).
- **Service account (Workspace):** grant domain-wide delegation for `https://www.googleapis.com/auth/gmail.readonly`, then set `auth: service_account`, `service_account_file` and `delegated_user` (for example a shared `noc@` mailbox).
- Each message type's `gmail_query` decides what gets fetched. A good pattern is to let a Gmail filter label carrier mail, then query `label:carriers`. Check the queries read-only with `blastradius gmail-test`.
- `label_processed` tags processed mail in Gmail. It needs the `gmail.modify` scope, so re-run `gmail-auth` after enabling it.

### 3. AWS

Create a read-only role in each account from `deploy/iam-policy.json`, trusted by the collector (`deploy/trust-policy.json`, with an ExternalId). List the roles under `aws.accounts`. Include the account that owns the DX connections, DX gateway and transit gateway; VPC accounts can be added gradually. Run `blastradius -c config.yaml discover` to see node counts and any coverage gaps, such as `AccessDenied` or an unresolved transit-gateway attachment.

### 4. Inventory

`inventory.example.yaml` documents every field. The most valuable entries are:

- **Circuits:** the carrier ID as it appears in their mails, plus `aliases` for alternate forms, and what the circuit `carries` (a `dxcon-…`, or the `cgw-…` of a VPN on an internet line). Alternatively, tag the AWS resource with `CircuitId`.
- **Sites:** the facilities' names and `dx_locations`, for facility-wide work. Also the on-prem `prefixes`, which enable backup-path inference.
- **Applications:** usually picked up automatically from `Application` tags. Selectors (CIDRs, subnets, VPCs, resource IDs, name regex) cover untagged resources. `notify:` subscribes the owners.

Check a real mail before relying on it: `blastradius -c config.yaml parse notice.eml`. This shows the classification, parsed fields and blast radius, and stores nothing.

### 5. People: subscriptions and access

- **Self-service** is at `/subscriptions`. Filter by application, owner, provider, circuit, AWS resource, account or region, with a minimum severity and a set of events (new, updated, rescheduled, cancelled, reminder, started, impact_changed, collision). Deliver to Slack, Teams (Workflows webhook), email, a webhook signed with `X-BlastRadius-Signature: sha256=HMAC(body)`, or Amazon SNS. SNS messages carry `applications`, `severity`, `provider` and `event` attributes, so teams can use SNS filter policies.
- **Feeds:** every subscription has an iCalendar URL (maintenance windows in your own calendar, with cancellations removed) and an Atom URL.
- **Centrally managed:** `subscriptions:` in the config, plus `applications[].notify` in the inventory. These show as read-only in the UI.
- **Auth:** `web.auth.mode: header` trusts an SSO proxy's identity header (oauth2-proxy `X-Forwarded-Email`, Google IAP, …). Keep blastradius bound to localhost or a private network. `admins` can upload mail, rebuild and see the mail log. `api_tokens` are for automation. Form posts are checked for a same-origin `Origin`/`Referer` header.
- **Webhook safety:** webhook targets that resolve to private or link-local addresses are refused unless `allow_private_targets` is set. `webhook_allow` restricts targets to known hosts.

## Operations

| Command | Purpose |
|---|---|
| `run` / `serve` / `worker` | web + daemon in one process / web only / daemon only (they can share one database) |
| `demo [--no-serve]` | seed a disposable demo database |
| `gmail-auth`, `gmail-test` | OAuth consent; read-only preview of what each message type's query finds |
| `discover [--no-aws]` | build a topology snapshot now (AWS + inventory, or inventory only) |
| `parse FILE` | dry run on one mail |
| `ingest FILE…` | process mail files |
| `whatif TARGET…` | blast radius of a hypothetical failure (circuit ID, AWS ID, IP or name) |
| `notices`, `recorrelate`, `rebuild`, `check` | list notices, re-run correlation, replay all stored mail after parser/config changes, validate config |

- `GET /healthz` returns 503 if the worker's heartbeat is stale. `GET /metrics` exposes Prometheus gauges: notices by phase and severity, messages, deliveries, active collisions, topology size and age, Gmail poll age. Worker task failures (Gmail auth, AWS access) also show on the dashboard.
- Storage is SQLite in WAL mode. Raw mails are kept compressed so they can be reprocessed. The last 20 topology snapshots are kept, and the latest AWS collection is reused when only the inventory changes. Back up with `sqlite3 blastradius.db ".backup backup.db"`.
- API docs are at `/api/docs`.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest          # parsing, engine, AWS collection (moto), pipeline, notifications, Gmail, web
.venv/bin/ruff check src tests
```

Module map:

| Module | Role |
|---|---|
| `mailparse` | MIME, HTML to text, forwarded mail |
| `parsing/` | `timewin`, `ical`, `bcop`, `aws_health`, `generic`, `ntc` |
| `topology/` | `aws` collector and normaliser, `inventory`, `build`, `index` (identifier matching), `store` (snapshots) |
| `impact` | the engine |
| `collisions` | overlapping-window analysis |
| `notices` | merge and lifecycle |
| `pipeline` | processing flow |
| `notify/` | subscriptions, rendering, channels |
| `feeds` | iCal and Atom output |
| `worker` | the daemon loop |
| `web/` | FastAPI app, server-rendered SVG graph and timeline |
