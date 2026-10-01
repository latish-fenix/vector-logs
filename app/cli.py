"""Admin command line, run inside the container:

    docker compose exec vector-logs python -m app.cli check
        Checks the setup: state bucket, Secrets Manager, and read access to the logs bucket
        (lists the cluster folders and the newest hour of one cluster).

    docker compose exec vector-logs python -m app.cli reset-password someone@fenixcommerce.com
        Prints a new generated password (ends the user's sessions). Use it if the only admin
        is locked out or has forgotten their password.

    docker compose exec vector-logs python -m app.cli secrets-status
        Lists which secrets exist (names only, never values).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from .auth import generate_password, hash_password, normalize_username
from .logs import LogService
from .repos import EventLog, UsersRepo
from .secret_store import build_secret_store
from .settings import Settings
from .storage import build_store


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    settings = Settings.from_env()
    secrets = build_secret_store(settings)
    cmd, args = argv[0], argv[1:]

    if cmd == "reset-password" and len(args) == 1:
        users = UsersRepo(build_store(settings), settings.bootstrap_admins, secrets)
        name = normalize_username(args[0])
        if users.get(name) is None:
            print(f"Unknown user '{name}'", file=sys.stderr)
            return 1
        password = generate_password()
        users.set_password(name, hash_password(password), True, "cli")
        EventLog().write({"action": "ADMIN_PASSWORD_RESET", "actor": "cli", "outcome": "SUCCESS",
                          "targetUser": name})
        print(f"username,password\n{name},{password}")
        return 0

    if cmd == "secrets-status":
        users = UsersRepo(build_store(settings), settings.bootstrap_admins)
        names = ["app"] + [f"users/{u['username']}" for u in users.list()]
        for n in names:
            print(f"{'present' if secrets.get(n) else 'MISSING':8}  {secrets.full_name(n)}")
        return 0

    if cmd == "check":
        ok = True
        try:
            users = UsersRepo(build_store(settings), settings.bootstrap_admins)
            print(f"OK    state store: {len(users.list())} user(s) in "
                  f"{'s3://' + settings.s3_bucket + '/' + settings.s3_prefix if settings.storage_backend == 's3' else settings.local_store_dir}")
        except Exception as e:  # noqa: BLE001 - report and continue
            ok = False
            print(f"FAIL  state store: {e}")
        try:
            print(f"OK    secrets: app secret {'present' if secrets.get('app') else 'not created yet'} "
                  f"under {secrets.full_name('')}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"FAIL  secrets: {e}")
        try:
            svc = LogService(settings)
            clusters = svc.clusters(refresh=True)
            print(f"OK    logs: {len(clusters)} cluster folder(s) in {svc.source.describe()}: "
                  f"{', '.join(clusters) or '(none)'}")
            if clusters:
                now = datetime.now(timezone.utc)
                for back in range(0, 6):
                    h = (now - timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
                    files = svc.source.list_prefix(svc._hour_prefix(clusters[0], h))
                    if files:
                        path, _ = svc.source.fetch(files[-1][0])
                        print(f"OK    read {files[-1][0]} ({len(files)} file(s) in that hour)")
                        break
                else:
                    print(f"WARN  no files in the last 6 hours for {clusters[0]}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"FAIL  logs: {getattr(e, 'message', e)}")
        return 0 if ok else 1

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
