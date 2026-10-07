# Vector Logs viewer

A web console for the application logs that Vector ships from the Fenix app servers to S3 as Parquet. People sign in, pick a cluster and a time range, and search, filter, read and export log lines. It reuses the ES-API stack and look (FastAPI, React, the same sign-in, users and Secrets Manager setup) and runs as a second container next to ES-API.

| | |
| --- | --- |
| Logs (read-only) | `s3://fenix-ecr-logs/vector/<cluster>/dt=YYYY-MM-DD/hour=HH/*.parquet` (`LOGS_BUCKET`, `LOGS_PREFIX`) |
| Load balancer logs (read-only) | ALB access logs in `s3://fenix-vector-ecs-logs/loadbalancer-logs/AWSLogs/...` (`LB_LOGS_BUCKET`, `LB_LOGS_PREFIX`) |
| App state | users and saved searches in `s3://fenix-es-config-api/vector-logs/` |
| Secrets | AWS Secrets Manager `vector-logs/*` (session key, password hashes) |
| Runs on | EC2 `172.0.58.49`, Docker, plain HTTP on port **443**: `http://172.0.58.49:443/ui/` |

## What people can do

- **Logs** (the main page): the log lines fill the window and keep loading as you scroll; click a line to open it in place (every column, full stack trace, filter-for / filter-out buttons); a **Fields** panel with top values, **Wrap**, and **Full screen**.
- **Overview**: the same search as a chart and top values, for spotting when something started.
- **ECS health**: every ECS cluster, the load balancer target groups it sits behind and whether each target is healthy; services running fewer tasks than desired; clusters without a load balancer. Read live from AWS (read-only), refreshed every minute.
- **Load balancers**: every request through the Application Load Balancers, from their access logs: 2xx / 3xx / 4xx / 5xx over time (5xx split into "from the app" and "from the load balancer"), a table per target group with the top failing path, paths grouped across stores and ids, and single requests with every field. Up to 7 days per view within the last 30; 5–10 minutes behind.
- **Search** one cluster over any window of up to 7 days within the last 30: free text (`"exact phrase"`, `-exclude`, `column:value`) plus filters on any column.
- **See when it happened**: a histogram of log lines over time, stacked by level; click a bar to zoom into it.
- **Narrow down fast**: top values for level, service, host and exception; click to filter, `−` to exclude.
- **Read a line**: every column, the full stack trace or body, filter-for / filter-out buttons on each value.
- **Export** up to 10,000 lines as CSV, JSON or NDJSON.
- **Save searches** (per user) and **share links** (the URL holds cluster, time range, query and filters).
- Times in your browser's time zone (IST) or UTC, with one click.

Admins also see every cluster folder (new folders appear by themselves), add users, give each person access per cluster (or to all clusters, with exceptions), reset passwords and remove users.

## Documents

| File | For |
| --- | --- |
| [docs/INSTALL.md](docs/INSTALL.md) | Installing and updating on EC2 (IAM, port 443, first sign-in) |
| [docs/API_REFERENCE.md](docs/API_REFERENCE.md) | Every endpoint, with curl examples |
| [docs/iam-policy.json](docs/iam-policy.json) | What the EC2 role needs |
| [ui/README.md](ui/README.md) | Working on the web console |

## How it works

```
browser ──► FastAPI (app/) ──► list hour folders in S3 ──► download new files to the cache volume
                 │                                              │
                 └──── DuckDB runs the search over the local Parquet copies ◄┘
```

- **Only the hours that matter are read.** Vector's folders are by event time (UTC), so a search lists the hour folders its time range touches (recent hours are re-listed every 30 s; older ones are cached for 15 minutes).
- **Each file is downloaded once.** Files never change after Vector writes them, so they are kept in a local cache (`CACHE_MAX_MB`, oldest removed first). Paging, the histogram, the detail view and exports read the cached copies.
- **DuckDB** (embedded, no server) runs the query: one pass collects the matching lines' time, level, service, host and exception for the counts, then only the current page's rows are read in full.
- **Nothing is written to the logs bucket.** The role has read-only access there.

Load balancer logs work differently, because they are gzip text and many small files (about 2,000 a day for two ALBs):

```
S3 (5-minute .log.gz) ──► converter thread (one per container, every 5 min) ──► CACHE_DIR/lb/
                                                     ├─ hour/<lb>/<day>/<HH>.parquet    one file per ALB and hour
                                                     └─ minute/<lb>/<day>/<HH>.parquet  counts per minute, target group, status, latency bin
page ──► counts, chart, percentiles from the minute files; paths and single requests from the hour files
```

- The last `LB_WARM_DAYS` (7) are kept converted; an older range (up to 30 days) is converted when someone opens it, with a progress bar on the page.
- Measured on our two ALBs' volume (about 3 million requests a day): converting an hour takes about 2 s on one low-priority thread; a 7-day summary about 0.4 s; a page of requests about 0.1 s. Disk: about 130 MB a day.
- Members see only the target groups of clusters they may read (`tg-<cluster>` → `<cluster>`); admins see everything, including requests with no target group.

Code map:

| File | What |
| --- | --- |
| `app/logs.py` | S3 listing, the file cache, query building (free text, filters) and DuckDB search / export |
| `app/routes_logs.py` | `/me`, `/clusters`, search, record, export, saved searches |
| `app/ecs_health.py`, `app/routes_health.py` | ECS clusters → services / container instances → target groups → target health (`/health/ecs`) |
| `app/lb_logs.py`, `app/routes_lb.py` | ALB access logs: S3 listing, the converter (5-minute files → hour and minute Parquet), queries (`/lb/...`) |
| `app/routes_admin.py` | Users and cluster access, the admin cluster list |
| `app/routes_auth.py`, `auth.py`, `identity.py` | Sign-in, sessions, lockout, cluster access check (from ES-API) |
| `app/repos.py`, `storage.py`, `secret_store.py` | Users and saved searches in S3, secrets in Secrets Manager (from ES-API) |
| `app/cli.py` | `check`, `reset-password`, `secrets-status` |
| `ui/` | React 18 + TypeScript + Vite console, built into `app/static/ui` |
| `local-test/` | Run it on a Windows PC; synthetic sample logs and load balancer logs |
| `tests/` | pytest (synthetic Parquet files, moto for S3 and Secrets Manager) |

## Run it on your PC

Windows, Command Prompt, in `Desktop\vector-logs`:

```bat
local-test\start-local.cmd
```

It creates a Python virtual environment, generates synthetic sample logs (three clusters, the last 30 hours and one hour 25 days ago) and starts the viewer at `http://localhost:8081/ui/`. The first admin password is printed in the window. To browse the real logs instead (needs AWS credentials that may read the bucket):

```bat
local-test\start-local.cmd -RealLogs -AwsProfile <your-profile>
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

They cover search results against DuckDB run directly on the same files (free text, phrases, exclusions, `column:value`, every filter), paging, time ranges (including 25 days back and the 7-day cap), the detail view, exports and their cap, cluster access rules, users and saved searches, password sign-in with Secrets Manager, the S3 source (discovery, caching, eviction, access-denied errors), the ECS health page, and the load balancer logs (line parsing, the converter including late files and on-demand ranges, counts against the generated requests, filters, per-cluster access, paging, export).

## Configuration

All settings are environment variables in `.env` (see [.env.example](.env.example)). The ones you may change:

| Variable | Default | Meaning |
| --- | --- | --- |
| `API_PORT` | `443` | Host port |
| `LOGS_BUCKET` / `LOGS_PREFIX` / `LOGS_REGION` | `fenix-ecr-logs` / `vector/` / `us-west-2` | Where Vector writes |
| `MAX_SEARCH_HOURS` | `168` | Longest window per search |
| `MAX_FILES_PER_SEARCH` | `30000` | Refuse searches that would read more files |
| `MAX_EXPORT_ROWS` | `10000` | Export cap |
| `CACHE_MAX_MB` | `8192` | Size of the local file cache (Vector logs) |
| `WORKERS`, `DUCKDB_THREADS`, `DUCKDB_MEMORY_MB`, `DOWNLOAD_THREADS` | `2`, `2`, `768`, `32` | Capacity (the container is capped at 2 GB in `docker-compose.yml`) |
| `HEALTH_ENABLED`, `ECS_REGION`, `HEALTH_CACHE_SECONDS` | `true`, `us-west-2`, `60` | ECS health page |
| `LB_ENABLED`, `LB_LOGS_BUCKET`, `LB_LOGS_PREFIX`, `LB_LOGS_REGION` | `true`, `fenix-vector-ecs-logs`, `loadbalancer-logs/`, `us-west-2` | Load balancers page |
| `LB_WARM_DAYS`, `LB_RETENTION_DAYS`, `LB_POLL_SECONDS`, `LB_CONVERTER_MEMORY_MB` | `7`, `30`, `300`, `256` | Load balancer converter |
| `S3_BUCKET` / `S3_PREFIX` / `AWS_REGION` | `fenix-es-config-api` / `vector-logs/` / `us-east-1` | App state |
| `SECRETS_PREFIX` | `vector-logs/` | Secrets Manager names |
| `BOOTSTRAP_ADMINS` | your email | Always admins |
| `SESSION_HOURS`, `LOCKOUT_ATTEMPTS`, `LOCKOUT_MINUTES`, `COOKIE_SECURE` | `12`, `5`, `15`, `false` | Sign-in |

## Columns in the logs

Written by the `parse_fenix` transform in `vector.yaml`, schema `/etc/vector/fenix_app_log.schema`:

`log_time` (UTC text), `log_time_ms`, `ingest_time_ms`, `cluster`, `instance_id`, `host`, `service`, `container_id`, `source_file`, `app`, `logger`, `level`, `class`, `method`, `line`, `msg`, `exception`, `body`, `error_identifier`, `error_module`, `error_code`, `error_message`, `status_code`, `parent_log`, `child_log`, `tenant_id`, `request_uuid`, `web_id`, `buyer_zip`, `shipper_zip`, `carrier`, `page_type`, `response_time_ms`, `kv_json`, `format` (track / delest / generic / unparsed), `parse_ok`, `raw`.

The viewer reads whatever columns the files have, so a column added to the Vector schema later shows up without code changes.
