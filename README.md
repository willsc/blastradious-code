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

## Configuration

blastradius reads two YAML files:

| File | Holds | Template |
|---|---|---|
| `config.yaml` | how the app runs: database, web and auth, mail sources, message types, AWS accounts, notifications, subscriptions | `config.example.yaml` |
| `inventory.yaml` | facts AWS can't tell it: circuits, sites, applications, account names | `inventory.example.yaml` |

```bash
cp config.example.yaml config.yaml
cp inventory.example.yaml inventory.yaml
$EDITOR config.yaml inventory.yaml
blastradius -c config.yaml check        # validate both files before starting
blastradius -c config.yaml run          # web UI + worker
```

**How the config is loaded**

- Pass the config with `-c/--config`. With no config file, built-in defaults apply. Those defaults are for the demo only: Gmail and AWS are off, auth is `none`, and the database is `./blastradius.db`.
- Every key is optional. Each section falls back to the values shown in `config.example.yaml`. If you leave out `message_types`, the built-in AWS Health, carrier-maintenance and carrier-outage types are used.
- Relative paths (`database`, `inventory`, `gmail.client_secrets`, `gmail.token_file`, `directory.path`, …) are resolved against the directory that contains the config file, not the current working directory.
- `${VAR}` and `${VAR:-default}` are replaced from the environment. If a plain `${VAR}` is unset, startup fails. Use this for every secret so the YAML stays safe to commit.
- Durations can be written as a number of seconds or with a unit: `90`, `30s`, `15m`, `6h`, `2d`, `1w` (`poll_interval`, `refresh_interval`, `reminders`).
- An unknown key produces a `config …: unknown key 'x' ignored` warning, so typos show up in the log. Bad regexes, unknown parsers, a bad `kind` or `auth.mode`, and duplicate message-type names stop startup with an error.
- Config changes take effect on restart. The inventory file is watched, and edits apply within about a minute with no AWS calls. To reprocess stored mail after changing message types, providers or parsers, run `blastradius -c config.yaml rebuild`.

**Sections at a glance** (`config.example.yaml` documents each key inline)

| Section | What it controls |
|---|---|
| top level | `database` (SQLite path), `base_url` (links in notifications and feeds), `display_timezone`, `secret_key`, `inventory` (path to inventory.yaml) |
| `web` | `host` and `port`. `auth.mode` is `none` or `header`, with `auth.header`, `auth.admins` and `auth.api_tokens` (sent as `Authorization: Bearer <token>`) |
| `gmail` | `enabled`, `auth` (`oauth` or `service_account`), credential and token files, `poll_interval`, `backfill_days`, `overlap_hours`, `max_messages_per_poll`, `label_processed` |
| `directory` | a drop folder of `*.eml` files (`path`, `poll_interval`) for procmail, SES→S3 syncs or replays |
| `message_types` | an ordered list where the first match wins. Each type has a `gmail_query` (what gets fetched) and `match` rules (`senders`, `subject`, `not_subject`, `body`, `has_calendar`) that recognise it. It also sets `kind` (`maintenance`, `outage` or `advisory`), `parsers` (`bcop`, `ntc`, `aws_health`, `generic`) and `default_impact` |
| `providers` | per-carrier hints for the free-text parser: `senders`, `dayfirst`, default `timezone`, `circuit_patterns`, `maintenance_id_patterns` |
| `aws` | `enabled`, `regions`, `refresh_interval`, `accounts` (each with `role_arn` + `external_id`, a named `profile`, or ambient credentials, plus optional per-account `regions`), the tag keys used for applications, owners, circuits, providers and sites, and `collect_instances` / `collect_enis` |
| `correlation` | identifier matching thresholds (`min_identifier_length`, `min_name_length`, `match_names`), `collision_lookahead_days`, `open_outage_hours`, `recent_days` |
| `notifications` | `reminders`, `notify_on_start`, `default_min_severity`, webhook safety (`webhook_allow`, `allow_private_targets`), `timeout`, `max_attempts`, `smtp` |
| `subscriptions` | subscriptions as code: `name`, `filter`, `events` and `channels` (`slack`, `teams`, `email`, `webhook`, `sns`). They show as read-only in the UI |

**Secrets and environment variables**

`config.example.yaml` reads these variables. You can rename them or add your own.

| Variable | Used for |
|---|---|
| `BLASTRADIUS_SECRET_KEY` | signs webhooks and derives the secret feed-URL tokens. If it's empty, a key is generated and stored in the database. Set it explicitly so feed URLs survive a database rebuild |
| `BLASTRADIUS_API_TOKEN` | the `automation` API token |
| `BLASTRADIUS_EXTERNAL_ID` | the ExternalId for the AWS role assumption |
| `SMTP_PASSWORD` | the SMTP password for email notifications |
| `SLACK_NOC_WEBHOOK`, `PAGER_HOOK_SECRET` | the example subscriptions' Slack URL and webhook signing secret |

Where to set them:

- **systemd:** put `KEY=value` lines in `/etc/blastradius/env` (read through `EnvironmentFile=`) with mode 0600.
- **Docker Compose:** use a `.env` file next to `docker-compose.yml`. It also needs `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_COOKIE_SECRET`, `OAUTH_REDIRECT_URL` and `ALLOWED_EMAIL_DOMAIN` for oauth2-proxy. Inside the container, set `database: /data/blastradius.db` and `gmail.token_file: /data/gmail-token.json` so state lands on the volume.
- **AWS credentials** come from the standard boto3 chain: an instance or task role, environment variables, or `~/.aws` profiles. The IAM roles listed under `aws.accounts` are then assumed from those credentials.

**A minimal production config**

```yaml
database: /var/lib/blastradius/blastradius.db
base_url: https://blastradius.example.com
display_timezone: Europe/London
secret_key: ${BLASTRADIUS_SECRET_KEY}
inventory: inventory.yaml

web:
  auth: {mode: header, header: X-Forwarded-Email, admins: [noc-lead@example.com]}

gmail:
  enabled: true
  client_secrets: /etc/blastradius/gmail-client.json
  token_file: /var/lib/blastradius/gmail-token.json

aws:
  enabled: true
  regions: [eu-west-2]
  accounts:
    - {name: network, role_arn: "arn:aws:iam::111111111111:role/BlastRadiusReadOnly", external_id: "${BLASTRADIUS_EXTERNAL_ID}"}
```

The steps below go through each part in order.

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
