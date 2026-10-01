# Vector Logs viewer: install on EC2

*1 October 2026*

This guide installs the log viewer on the ES-API server (`172.0.58.49`) as a second Docker container. It serves plain HTTP on port **443**; ES-API keeps port 80. Nothing on ES-API changes.

## What you need

| You need | For |
| --- | --- |
| The code pushed to GitHub (`latish-fenix/vector-logs`) from `Desktop\vector-logs` | EC2 pulls it from there |
| A shell on the EC2 server with `sudo`, Docker and git (already there for ES-API) | Running the steps |
| AWS admin access (AWS console or CloudShell) | The IAM role, the security group and reading the first password |
| About 20 minutes | |

Push the code first, on your PC in Command Prompt:

```bat
cd %USERPROFILE%\Desktop\vector-logs
git add -A
git commit -m "Vector Logs viewer"
git push
```

## Step 1: Let the EC2 role read the logs

The server reads the logs with the EC2 instance role; no keys are stored anywhere.

1. In the AWS console open **IAM → Roles →** the role attached to the ES-API instance (EC2 console → the instance → *Security* tab → *IAM role*).
2. **Add permissions → Create inline policy → JSON**, paste [`docs/iam-policy.json`](iam-policy.json), replace `ACCOUNT_ID` with your 12-digit account id, and save it as `vector-logs`.

   It allows exactly this:

   | Statement | Allows |
   | --- | --- |
   | `ListLogFolders`, `ReadLogFiles` | **read-only**: list and read `s3://fenix-ecr-logs/vector/` |
   | `ListStatePrefix`, `ReadWriteState`, `DeleteLocksAndSavedSearchesOnly` | users and saved searches under `s3://fenix-es-config-api/vector-logs/` |
   | `SecretsManagerOwnPrefixOnly` | its own secrets `vector-logs/*` (session key, password hashes) |

3. If the logs bucket is encrypted with a customer-managed KMS key (S3 console → `fenix-ecr-logs` → *Properties* → *Default encryption*), also allow `kms:Decrypt` on that key. With the default *SSE-S3* nothing more is needed.
4. If the instance has no internet access, it reaches S3 through a VPC endpoint. A *gateway* endpoint only serves buckets in the instance's own region; the check in Step 4 tells you if the logs bucket (us-west-2) or the state bucket can't be reached.

> The state bucket is the one ES-API already uses. Check its name and region on EC2 with `grep -E '^(S3_BUCKET|AWS_REGION)=' ~/ES-API/es-config-api/.env` and use the same values in Step 3 (and in the policy, if the name differs).

## Step 2: Open port 443

EC2 console → the instance → *Security* → the security group → **Edit inbound rules → Add rule**: type *Custom TCP*, port **443**, source = the same office/VPN range that may reach ES-API on port 80. Save.

## Step 3: Get the code and settings on EC2

```bash
cd ~
git clone https://github.com/latish-fenix/vector-logs.git
cd vector-logs
cp .env.example .env
nano .env
```

In `.env` check these lines (the defaults match our setup):

| Setting | Value |
| --- | --- |
| `API_PORT` | `443` |
| `LOGS_BUCKET` / `LOGS_PREFIX` / `LOGS_REGION` | `fenix-ecr-logs` / `vector/` / `us-west-2` |
| `S3_BUCKET` / `AWS_REGION` | the ES-API state bucket and its region (see the note in Step 1) |
| `S3_PREFIX` / `SECRETS_PREFIX` | `vector-logs/` (separate from ES-API) |
| `BOOTSTRAP_ADMINS` | `latish.madapada@fenixcommerce.com` |
| `WORKERS` × `DUCKDB_MEMORY_MB` | keep well under the free RAM (`free -m`); 2 × 1024 MB is fine on a 4 GB+ instance |

Nothing secret goes in `.env`.

## Step 4: Start it and check the setup

```bash
docker compose up -d --build
docker compose ps                                    # vector-logs ... Up (healthy) after ~30 s
docker compose exec vector-logs python -m app.cli check
```

`check` prints one line per part. Expected:

```
OK    state store: 1 user(s) in s3://fenix-es-config-api/vector-logs/
OK    secrets: app secret present under vector-logs/
OK    logs: 1 cluster folder(s) in s3://fenix-ecr-logs/vector/: post-btp-01
OK    read vector/post-btp-01/dt=2026-10-01/hour=04/1790813101-....parquet (12 file(s) in that hour)
```

A `FAIL logs: ... LOGS_ACCESS_DENIED` line means Step 1 is missing or not yet active (wait a minute, run `check` again). If port 443 is already taken, `docker compose up` says *address already in use*; see who has it with `sudo ss -ltnp | grep ':443 '`.

## Step 5: First sign-in

1. Read the first admin password: AWS console → **Secrets Manager** → `vector-logs/app` → *Retrieve secret value* → `bootstrapAdminPassword`. (Or in CloudShell: `aws secretsmanager get-secret-value --secret-id vector-logs/app --query SecretString --output text`.)
2. Open **`http://172.0.58.49:443/ui/`**. Type the `http://` part: without it some browsers try HTTPS on port 443 and show a connection error.
3. Sign in with your email and that password, then **Change password** (bottom left).
4. **Administration → Clusters** lists every cluster folder found in S3.
5. **Administration → Users → Add users**: enter emails, tick the clusters each person may read (or *All clusters*), create, and download the CSV with their passwords. Send each password privately.

## Updating later

```bash
cd ~/vector-logs
git pull
docker compose up -d --build
```

Users, saved searches and secrets are in S3 and Secrets Manager, so they survive rebuilds. The cache volume (`log-cache`) only holds copies of log files; delete it any time with `docker compose down -v`.

## Day-to-day operations

| Task | Command (in `~/vector-logs`) |
| --- | --- |
| Logs of the app (sign-ins, searches, exports) | `docker compose logs -f --tail 100` |
| Someone (or the only admin) is locked out | `docker compose exec vector-logs python -m app.cli reset-password <email>` |
| Which secrets exist | `docker compose exec vector-logs python -m app.cli secrets-status` |
| Cache size | Administration → Clusters (top right), limit `CACHE_MAX_MB` |
| Restart | `docker compose restart` |

## How searches perform

Vector writes one file per server about every 5 minutes, in hourly folders. A search lists only the hour folders in its time range, downloads the files it hasn't seen yet (in parallel) and keeps them in the cache; repeated searches, paging, the histogram and exports then read the local copies. The first search over a long range is the slow one. If people often search 7 days at once and it feels slow, raise `DOWNLOAD_THREADS` or `CACHE_MAX_MB`, or ask for a nightly job that merges each hour's small files.

## Known limits

- Plain HTTP: passwords and log contents (tracking numbers, zip codes) cross the network unencrypted, as with ES-API today. When HTTPS comes, set `COOKIE_SECURE=true`.
- One search covers at most 7 days (`MAX_SEARCH_HOURS`); it can start anywhere in the 30 days the bucket keeps.
- An export holds at most 10,000 lines (`MAX_EXPORT_ROWS`).
