# Vector Logs viewer: API reference

Every console action is a JSON API under `/api/v1`, so scripts can do the same. The interactive version (with every field) is at `http://172.0.58.49:443/docs`.

The examples use bash and `jq`. Set the address once:

```bash
API=http://172.0.58.49:443/api/v1
```

## Sign in

```bash
TOKEN=$(curl -s -X POST $API/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"you@fenixcommerce.com","password":"..."}' | jq -r .token)
AUTH="Authorization: Bearer $TOKEN"
```

A token lasts 12 hours (`SESSION_HOURS`). Changing or resetting the password ends every older token. Five wrong passwords lock the account for 15 minutes.

| Method and path | Does |
| --- | --- |
| `POST /auth/login` | `{username, password}` → `{token, expiresAt, user}` (also sets the browser cookie) |
| `POST /auth/logout` | Sign out of this browser |
| `POST /auth/logout-all` | End all your sessions |
| `POST /auth/change-password` | `{currentPassword, newPassword}` (12+ characters, letters and digits) |
| `GET /me` | Your account, cluster access and limits |

## Clusters

```bash
curl -s -H "$AUTH" $API/clusters
# {"items":[{"id":"post-btp-01"}]}
```

Lists the cluster folders under the logs prefix that you may read (admins: all).

## Search

`POST /clusters/{cluster}/logs/_search`

```bash
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  $API/clusters/post-btp-01/logs/_search -d '{
    "start": "now-24h", "end": "now",
    "query": "\"Tracking Number not found\" -DEBUG",
    "filters": [{"field": "level", "op": "is", "value": "ERROR"},
                {"field": "service", "op": "one_of", "values": ["fenix-track-processor", "fenix-order-sync"]}],
    "order": "desc", "offset": 0, "size": 50
  }' | jq '{total, files, tookMs, first: .hits[0].msg}'
```

| Field | Meaning |
| --- | --- |
| `start`, `end` | `now`, `now-15m`, `now-4h`, `now-7d`, an ISO date-time (`2026-09-06T04:00:00Z`; no offset = UTC) or epoch milliseconds. At most 7 days apart; can be anywhere in the 30 days S3 keeps |
| `query` | Free text. Every word must appear (case-insensitive) in the message, body, exception, error message, class, logger, request/tenant id…; `"exact phrase"`; `-word` excludes; `column:value` matches one column (`*` wildcard, case-insensitive), e.g. `level:error service:fenix-track*` |
| `filters` | `[{field, op, value / values / gte, lte}]`; ops: `is`, `is_not`, `one_of`, `not_one_of`, `contains`, `not_contains`, `gte`, `lte`, `between`, `exists`, `not_exists`. `is` is exact and case-sensitive |
| `order` | `desc` (newest first, default) or `asc` |
| `offset`, `size` | Paging; `size` up to 500 |
| `aggregations` | `false` skips the histogram and top values (faster paging) |

The response:

| Field | Meaning |
| --- | --- |
| `total` | Matching log lines |
| `hits` | The page; each has every column (long texts cut at 1,500 characters, then `_truncated: true`) and `_ref: {key, row}` |
| `columns` | Column names and types found in the files |
| `histogram` | `{interval, buckets: [{t, count, levels: {ERROR: n, ...}}]}` |
| `facets` | Top 8 values with counts for `level`, `service`, `host`, `exception` |
| `start`, `end` | The resolved time window (epoch ms); send these back when paging so the window doesn't slide |
| `files`, `bytes`, `cached`, `tookMs` | How many Parquet files were read, how many came from the server's cache, and the time |

## One complete log line

```bash
curl -s -H "$AUTH" "$API/clusters/post-btp-01/logs/_record?key=vector/post-btp-01/dt=2026-10-01/hour=00/1790813101-aaa17b5e-7184-4693-b759-dd7c110c914e.parquet&row=3"
```

`key` and `row` come from a hit's `_ref`. Returns every column, untruncated.

## Export

`POST /clusters/{cluster}/logs/_export` takes the search fields plus:

| Field | Meaning |
| --- | --- |
| `format` | `csv` (UTF-8 with BOM, opens in Excel), `json` or `ndjson` |
| `columns` | Column list; omit for all |
| `limit` | Up to 10,000 (`MAX_EXPORT_ROWS`), in the search's order |

```bash
curl -s -H "$AUTH" -H 'Content-Type: application/json' -OJ \
  $API/clusters/post-btp-01/logs/_export \
  -d '{"start":"now-1h","end":"now","query":"level:error","format":"csv","limit":5000}'
```

The response header `X-Export-Rows` gives the number of lines.

## Saved searches (your own)

| Method and path | Does |
| --- | --- |
| `GET /saved-searches` | Your saved searches |
| `POST /saved-searches` | `{name, cluster, params}`; `params` is the console URL's query string (`start=now-24h&q=...&f=[...]`). The same name replaces |
| `DELETE /saved-searches/{id}` | Delete one |

## Administration (admins only)

| Method and path | Does |
| --- | --- |
| `GET /admin/clusters?refresh=true` | Every cluster folder, who may read it, the logs location and cache size |
| `GET /admin/users` | All users |
| `POST /admin/users` | `{username, admin, clusters}` → the generated password, **once** (`?format=csv` for a CSV) |
| `POST /admin/users/bulk` | `{users: [...]}`; all or nothing |
| `PUT /admin/users/{email}` | Change admin flag and cluster access |
| `PUT /admin/users/{email}/permissions` | Change only the cluster access |
| `POST /admin/users/{email}/reset-password` | New generated password, once; ends their sessions and unlocks |
| `DELETE /admin/users/{email}` | Remove a user and their saved searches |

Cluster access is `{"<cluster>": "view", "*": "view", "<cluster>": "none"}`:

| Example | Can read |
| --- | --- |
| `{"post-btp-01": "view"}` | only post-btp-01 |
| `{"*": "view"}` | every cluster, including folders that appear later |
| `{"*": "view", "pre-prod-01": "none"}` | every cluster except pre-prod-01 |

## Errors

Every error is `{"error": {"code", "message", "details"}}`. The common codes:

| Code | HTTP | Meaning |
| --- | --- | --- |
| `NOT_AUTHENTICATED`, `SESSION_EXPIRED` | 401 | Sign in (again) |
| `ACCOUNT_LOCKED` | 423 | Too many wrong passwords; wait or ask an admin to reset |
| `PERMISSION_DENIED`, `ADMIN_REQUIRED` | 403 | No access to that cluster / admins only |
| `RANGE_TOO_LARGE`, `INVALID_TIME` | 400 | More than 7 days, or a time that can't be read |
| `UNKNOWN_FIELD`, `INVALID_FILTER` | 400 | A filter names a column that isn't in the logs, or a bad value |
| `TOO_MANY_FILES` | 400 | The range has more files than `MAX_FILES_PER_SEARCH`; pick a shorter one |
| `SEARCH_TOO_BIG` | 507 | The search needed more than `DUCKDB_MEMORY_MB` |
| `LOGS_ACCESS_DENIED` | 500 | The EC2 role can't read the logs bucket (see INSTALL.md, Step 1) |
