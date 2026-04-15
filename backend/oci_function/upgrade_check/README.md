# ExpressLane Upgrade Check — Backend

This directory contains the OCI Function + API Gateway backend that
powers the ExpressLane upgrade-check feature described in the main
[README](../../../README.md#upgrade-check).

## Live deployment (us-ashburn-1)

The production backend is deployed in the **ExpressLane** compartment:

| Resource              | Display name                          |
|-----------------------|---------------------------------------|
| VCN                   | `expresslane-upgrade-vcn` (10.1.0.0/16) |
| Public subnet         | `expresslane-upgrade-subnet-public`   |
| Private subnet        | `expresslane-upgrade-subnet-private`  |
| Object Storage bucket | `expresslane-meta` (namespace `id2ldwjfc4sd`) |
| Fn application        | `expresslane-app`                     |
| Fn function           | `upgrade-check`                       |
| API Gateway           | `expresslane-upgrade-gw`              |
| API Deployment        | `expresslane-upgrade-deployment`      |
| Dynamic group         | `ExpressLaneUpgradeCheckFn`           |
| Policy                | `ExpressLaneUpgradeCheckPolicy`       |

**Public endpoint:**

```
https://kpbcyxd4d23n4ww5eqqyuszebi.apigateway.us-ashburn-1.oci.customer-oci.com/expresslane/v1/check
```

The ExpressLane client references this URL in `vm_migrator_oci/upgrade_check.py`
(constant `DEFAULT_UPGRADE_CHECK_URL`).

### Publishing a new release (the easy path)

Just re-upload `latest.json`:

```bash
cat > /tmp/latest.json <<EOF
{
    "latest_version": "1.3.0",
    "release_notes_url": "https://github.com/<org>/<repo>/releases/tag/v1.3.0",
    "download_url": "https://github.com/<org>/<repo>/releases/download/v1.3.0/expresslane-1.3.0.tar.gz"
}
EOF
oci os object put --bucket-name expresslane-meta --namespace id2ldwjfc4sd \
    --name latest.json --file /tmp/latest.json --force
```

The function caches `latest.json` for 5 minutes, so installs see the new
version on their next check after that.

### Rebuilding the function (if you change `func.py`)

```bash
cd backend/oci_function/upgrade_check
tar czf /tmp/upgrade-check-src.tgz func.py func.yaml requirements.txt
scp /tmp/upgrade-check-src.tgz opc@<build-host>:/tmp/
ssh opc@<build-host> 'bash -s' <<'REMOTE'
cd /tmp && rm -rf upgrade-check && mkdir upgrade-check && cd upgrade-check
tar xzf /tmp/upgrade-check-src.tgz
# Dockerfile + DB-IP Lite country DB
# (see "Fresh deploy" section below for the full one-shot)
REMOTE
# Then on your Mac:
oci fn function update --function-id <FN_OCID> \
    --image iad.ocir.io/id2ldwjfc4sd/expresslane/upgrade-check:0.0.2 --force
```

Resource OCIDs from the live deployment are saved in
`~/.oci/expresslane-upgrade-deploy.state` (mode 0600) on the machine
that performed the original deploy.

---

## Fresh deploy (from scratch, for anyone else)

**What this function does** (and nothing else):

1. Accepts a public HTTPS `GET /v1/check?version=<x>&install_id=<8-hex>`.
2. Validates the two query parameters with strict regexes.
3. Reads the current latest-release info from Object Storage
   (`expresslane-meta/latest.json`, cached for 5 minutes).
4. Writes one JSON log line to stdout (OCI Logging picks it up).
5. Returns the release info as JSON with a pinned schema.

The logged event contains **only** `{ts, version, install_id_prefix, country}`.
The raw caller IP is never logged, cached, or returned.

---

## 1. Prerequisites

- An OCI tenancy and a compartment you can create Functions in.
- `fn` CLI installed and configured (`fn --version`).
- `oci` CLI installed and configured (or Cloud Shell).
- A VCN + private subnet the Function can run in.
- No MaxMind account needed — country lookups use the free
  [DB-IP Lite Country](https://db-ip.com/db/download/ip-to-country-lite)
  database (CC BY 4.0, same MMDB format as MaxMind GeoLite2, downloaded
  fresh at build time). Attribution: *"IP Geolocation by DB-IP"*.

---

## 2. Create the Object Storage bucket and upload latest.json

```bash
# Private bucket — the function reads it with its resource principal.
oci os bucket create \
    --compartment-id <COMPARTMENT_OCID> \
    --name expresslane-meta

# Copy the sample and edit the version strings to match your current release.
cp latest.json.example latest.json
oci os object put \
    --bucket-name expresslane-meta \
    --name latest.json \
    --file latest.json \
    --force
```

To publish a new ExpressLane release later, just re-upload `latest.json` —
no function redeploy required. The function re-reads it every 5 minutes.

---

## 3. Bundle the DB-IP Lite country database

Fn packages every file next to `func.py` into `/function/` in the container.
Download the free DB-IP Lite country DB (no signup, no license key) and
drop it in the build dir. It's in the same MMDB format as MaxMind GeoLite2,
so the `geoip2` Python library reads it unchanged:

```bash
YM=$(date +%Y-%m)
curl -fsSL -o dbip.mmdb.gz "https://download.db-ip.com/free/dbip-country-lite-${YM}.mmdb.gz"
gunzip -f dbip.mmdb.gz
mv dbip.mmdb GeoLite2-Country.mmdb   # name the client code expects
```

If the DB is not present at deploy time, the function still runs and logs
`country: "unknown"` instead of an ISO code. DB-IP ships a new snapshot
around the 1st of each month; re-run this block any time to refresh.

The file is gitignored (`.gitignore`) — **do not commit it**.

---

## 4. Create the Fn application and deploy

```bash
# Select a context that points at your OCI tenancy.
fn list contexts
fn use context <your-oci-context>

# One-time: create the Fn application. Replace the subnet OCID with
# a private subnet in your VCN.
fn create app expresslane-app \
    --annotation 'oracle.com/oci/subnetIds=["<SUBNET_OCID>"]'

# Deploy the function.
cd backend/oci_function/upgrade_check
fn -v deploy --app expresslane-app
```

---

## 5. IAM: let the function read latest.json

Create a dynamic group that matches the Function, then a policy that
allows it to read objects in the `expresslane-meta` bucket.

```text
# Dynamic Group: ExpressLaneUpgradeCheckFn
ALL {resource.type = 'fnfunc', resource.compartment.id = '<COMPARTMENT_OCID>'}
```

```text
# Policy (in the same compartment or the root compartment)
Allow dynamic-group ExpressLaneUpgradeCheckFn to read buckets in compartment <NAME>
Allow dynamic-group ExpressLaneUpgradeCheckFn to read objects in compartment <NAME> where target.bucket.name='expresslane-meta'
```

---

## 6. Expose the function through API Gateway

Create a gateway in a public regional subnet, then a deployment whose
path prefix is `/expresslane`. The minimal deployment spec is:

```json
{
  "routes": [
    {
      "path": "/v1/check",
      "methods": ["GET"],
      "backend": {
        "type": "ORACLE_FUNCTIONS_BACKEND",
        "functionId": "<UPGRADE_CHECK_FUNCTION_OCID>"
      },
      "requestPolicies": {
        "rateLimiting": {
          "rateInRequestsPerSecond": 10,
          "rateKey": "CLIENT_IP"
        }
      }
    }
  ]
}
```

The resulting public URL pattern is:

```
https://<gateway-id>.apigateway.<region>.oci.customer-oci.com/expresslane/v1/check
```

Copy that URL into `vm_migrator_oci/upgrade_check.py` in place of the
`DEFAULT_UPGRADE_CHECK_URL` constant (or set `EXPRESSLANE_UPGRADE_CHECK_URL`
in the ExpressLane environment).

---

## 7. Smoke test

```bash
curl -sS 'https://<gateway>.apigateway.<region>.oci.customer-oci.com/expresslane/v1/check?version=1.0.0&install_id=a1b2c3d4' | jq
```

Expected response:

```json
{
  "latest_version": "1.2.0",
  "is_newer": true,
  "release_notes_url": "https://github.com/oracle-quickstart/expresslane/releases/tag/v1.2.0",
  "download_url": "https://github.com/oracle-quickstart/expresslane/releases/download/v1.2.0/expresslane-1.2.0.tar.gz",
  "checked_at": "2026-04-15T12:34:56+00:00"
}
```

Validation failure examples (both return HTTP 400 with the same schema):

```bash
curl -i '.../v1/check?version=bogus&install_id=a1b2c3d4'
curl -i '.../v1/check?version=1.0.0&install_id=not-hex'
```

---

## 8. View the anonymous adoption logs

In OCI Console: **Observability & Management → Logging → Search**, scoped to
the Function's log group.

CLI equivalent:

```bash
oci logging-search search-logs \
    --search-query "search \"<LOG_GROUP_OCID>\" | where data.event = 'upgrade_check' | summarize count() by data.version, data.country" \
    --time-start "$(date -u -v-7d +%Y-%m-%dT%H:%M:%SZ)" \
    --time-end   "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

Sample log lines:

```json
{"event":"upgrade_check","ts":"2026-04-15T12:34:56+00:00","version":"1.0.0","install_id_prefix":"a1b2c3d4","country":"US"}
{"event":"upgrade_check","ts":"2026-04-15T12:35:02+00:00","version":"1.2.0","install_id_prefix":"ff00aabb","country":"DE"}
{"event":"upgrade_check","ts":"2026-04-15T12:35:14+00:00","version":"1.1.0","install_id_prefix":"c0ffee01","country":"unknown"}
```

To count unique daily installs on a given version:

```text
search "<LOG_GROUP_OCID>"
| where data.event = 'upgrade_check' and data.version = '1.2.0'
| summarize dcount(data.install_id_prefix) by bin(datetime, 1d)
```

**Set the log group's retention policy** (30 days is a sensible default)
in the OCI Logging console. The log bodies contain no IP, no hostname,
no OCIDs, no tenancy info — only the four fields above.

---

## 9. Publishing a new release

1. Tag and publish the ExpressLane release on GitHub.
2. Edit `latest.json` locally with the new version + URLs.
3. Re-upload:

```bash
oci os object put \
    --bucket-name expresslane-meta \
    --name latest.json \
    --file latest.json \
    --force
```

Clients will pick up the new version on their next scheduled check
(within 24h + jitter).

---

## Privacy notes

- The raw IP address is read for country lookup, then discarded immediately
  inside the request handler. It is **never** logged, cached, or returned.
- `install_id_prefix` is 8 random hex characters — not derived from any
  host, user, or network identifier.
- The feature is **opt-out** on the client (`EXPRESSLANE_NO_UPGRADE_CHECK=true`).
  When opted out, no call ever reaches this function.
- The response is a simple cached read; no auth, no cookies, no sessions.
- The client refuses all HTTP redirects, so a compromised endpoint cannot
  bounce installs to a third-party host.
