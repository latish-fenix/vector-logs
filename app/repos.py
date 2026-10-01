"""What the app keeps in its state store (S3): users, saved searches, the startup lock.

Log files are never written; they are only read from the logs bucket (see logs.py).
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from urllib.parse import quote

from .errors import bad_request, conflict, not_found
from .secret_store import user_secret
from .storage import ObjectStore, PreconditionFailed
from .util import iso, new_id

log = logging.getLogger("vector_logs.events")

def _update_with_retry(store: ObjectStore, key: str, mutate, default: dict, attempts: int = 5):
    """Read-modify-write with S3 conditional writes; retries on concurrent writers."""
    for _ in range(attempts):
        data, etag = store.get_json(key)
        current = data if data is not None else json.loads(json.dumps(default))
        result = mutate(current)
        try:
            if etag:
                store.put_json(key, current, if_match=etag)
            else:
                store.put_json(key, current, if_none_match=True)
            return result
        except PreconditionFailed:
            time.sleep(0.05)
    raise conflict("CONCURRENT_UPDATE", f"'{key}' kept changing; please retry")



# --------------------------------------------------------------------------- users
class UsersRepo:
    """Users and cluster access in S3 (state/users.json); password hashes in Secrets Manager.

    The S3 record only says whether a password is set (hasPassword) and when; the scrypt
    hash itself lives in the secret ``<prefix>users/<email>`` and is read only when someone
    signs in or changes their password."""

    KEY = "state/users.json"
    SECRET_FIELDS = ("passwordHash",)

    def __init__(self, store: ObjectStore, bootstrap_admins: list[str], secrets=None):
        self.store = store
        self.bootstrap_admins = set(bootstrap_admins)
        self.secrets = secrets

    # -- password hashes (Secrets Manager)
    def password_hash(self, username: str) -> str | None:
        if self.secrets is not None:
            doc = self.secrets.get(user_secret(username))
            if doc and doc.get("passwordHash"):
                return doc["passwordHash"]
        rec = self._all().get(username) or {}
        return rec.get("passwordHash")  # not yet migrated

    def _save_hash(self, username: str, password_hash: str) -> bool:
        """True when stored in the secret store (the S3 record then keeps no hash)."""
        if self.secrets is None:
            return False
        self.secrets.put(user_secret(username), {"passwordHash": password_hash},
                         f"Vector Logs viewer password hash for {username}")
        return True

    def migrate_hashes(self) -> list[str]:
        """Move any password hash still in S3 into Secrets Manager (idempotent)."""
        if self.secrets is None:
            return []
        legacy = {n: r["passwordHash"] for n, r in self._all().items() if r.get("passwordHash")}
        for name, h in legacy.items():
            self._save_hash(name, h)

        def mutate(doc):
            moved = []
            for name, rec in doc["users"].items():
                if rec.get("passwordHash") and name in legacy:
                    rec.pop("passwordHash", None)
                    rec["hasPassword"] = True
                    moved.append(name)
            return moved
        return _update_with_retry(self.store, self.KEY, mutate, {"users": {}}) if legacy else []

    def _all(self) -> dict[str, dict]:
        data, _ = self.store.get_json(self.KEY)
        return (data or {}).get("users", {})

    def _full(self, username: str, rec: dict | None) -> dict | None:
        if rec is None and username not in self.bootstrap_admins:
            return None
        rec = dict(rec or {"admin": False, "clusters": {}})
        rec["username"] = username
        rec["hasPassword"] = bool(rec.get("hasPassword") or rec.get("passwordHash"))
        rec["bootstrap"] = username in self.bootstrap_admins
        if rec["bootstrap"]:
            rec["admin"] = True
        return rec

    @classmethod
    def public(cls, rec: dict | None) -> dict | None:
        """A user record safe to return from the API: no password hash."""
        if rec is None:
            return None
        out = {k: v for k, v in rec.items() if k not in cls.SECRET_FIELDS}
        has = bool(rec.get("hasPassword") or rec.get("passwordHash"))
        out["hasPassword"] = has
        out["usingGeneratedPassword"] = bool(has and rec.get("passwordGenerated"))
        return out

    def list(self) -> list[dict]:
        users = self._all()
        names = sorted(set(users) | self.bootstrap_admins)
        return [self.public(self._full(n, users.get(n))) for n in names]

    def get(self, username: str) -> dict | None:
        return self.public(self._full(username, self._all().get(username)))

    def get_auth(self, username: str) -> dict | None:
        """Full S3 record (permissions, token version, lockout). The password hash is not
        in it: use password_hash(). Never return this from the API."""
        return self._full(username, self._all().get(username))

    def exists(self, username: str) -> bool:
        return username in self._all()

    def put(self, username: str, admin: bool, clusters: dict[str, Any], by: str,
            password_hash: str | None = None, generated: bool = False) -> dict:
        """Create or update admin flag + permissions. Password fields are kept unless
        a new hash is given (used when creating a user)."""
        in_secret = self._save_hash(username, password_hash) if password_hash else False

        def mutate(doc):
            prev = doc["users"].get(username)
            rec = dict(prev or {"createdAt": iso(), "createdBy": by, "tokenVersion": 0})
            rec.update({"admin": bool(admin), "clusters": clusters, "updatedAt": iso(),
                        "updatedBy": by})
            if password_hash:
                rec.update(hasPassword=True, passwordGenerated=generated,
                           passwordSetAt=iso(), failedLogins=0, lockedUntil=None,
                           tokenVersion=int(rec.get("tokenVersion", 0)) + 1)
                if in_secret:
                    rec.pop("passwordHash", None)
                else:
                    rec["passwordHash"] = password_hash
            doc["users"][username] = rec
            return prev
        prev = _update_with_retry(self.store, self.KEY, mutate, {"users": {}})
        return {"before": self.public(prev), "after": self.get(username)}

    def set_password(self, username: str, password_hash: str, generated: bool, by: str) -> int:
        """Set a password; bumps tokenVersion so every older session stops working."""
        in_secret = self._save_hash(username, password_hash)

        def mutate(doc):
            rec = doc["users"].get(username)
            if rec is None:
                if username not in self.bootstrap_admins:
                    raise not_found("USER_NOT_FOUND", f"Unknown user '{username}'")
                rec = {"admin": True, "clusters": {}, "createdAt": iso(), "tokenVersion": 0}
            version = int(rec.get("tokenVersion", 0)) + 1
            rec.update(hasPassword=True, passwordGenerated=generated,
                       passwordSetAt=iso(), passwordSetBy=by, failedLogins=0,
                       lockedUntil=None, tokenVersion=version)
            if in_secret:
                rec.pop("passwordHash", None)
            else:
                rec["passwordHash"] = password_hash
            doc["users"][username] = rec
            return version
        return _update_with_retry(self.store, self.KEY, mutate, {"users": {}})

    def bump_token_version(self, username: str) -> int:
        def mutate(doc):
            rec = doc["users"].get(username)
            if rec is None:
                raise not_found("USER_NOT_FOUND", f"Unknown user '{username}'")
            rec["tokenVersion"] = int(rec.get("tokenVersion", 0)) + 1
            return rec["tokenVersion"]
        return _update_with_retry(self.store, self.KEY, mutate, {"users": {}})

    def record_login(self, username: str, success: bool, max_attempts: int,
                     lock_minutes: int) -> dict:
        """Count failures; lock after max_attempts. Returns {locked, lockedUntil, failed}."""
        def mutate(doc):
            rec = doc["users"].get(username)
            if rec is None:
                return {"locked": False, "lockedUntil": None, "failed": 0}
            if success:
                rec.update(failedLogins=0, lockedUntil=None, lastLoginAt=iso())
                return {"locked": False, "lockedUntil": None, "failed": 0}
            if rec.get("lockedUntil") and rec["lockedUntil"] < time.time():
                rec["lockedUntil"] = None
            failed = int(rec.get("failedLogins", 0)) + 1
            rec["failedLogins"] = failed
            if failed >= max_attempts:
                rec["lockedUntil"] = time.time() + lock_minutes * 60
                rec["failedLogins"] = 0
            locked = bool(rec.get("lockedUntil") and rec["lockedUntil"] > time.time())
            return {"locked": locked, "lockedUntil": rec.get("lockedUntil"), "failed": failed}
        return _update_with_retry(self.store, self.KEY, mutate, {"users": {}})

    def set_permissions(self, username: str, clusters: dict[str, Any], by: str) -> dict:
        def mutate(doc):
            rec = doc["users"].get(username)
            if rec is None:
                if username not in self.bootstrap_admins:
                    raise not_found("USER_NOT_FOUND", f"Unknown user '{username}'")
                rec = {"admin": True, "clusters": {}, "createdAt": iso(), "tokenVersion": 0}
            before = dict(rec.get("clusters", {}))
            rec.update({"clusters": clusters, "updatedAt": iso(), "updatedBy": by})
            doc["users"][username] = rec
            return before
        before = _update_with_retry(self.store, self.KEY, mutate, {"users": {}})
        return {"before": before, "after": clusters}

    def delete(self, username: str) -> dict:
        if username in self.bootstrap_admins:
            raise bad_request("BOOTSTRAP_ADMIN", "Bootstrap admins are set by BOOTSTRAP_ADMINS "
                              "and cannot be deleted through the API")

        def mutate(doc):
            if username not in doc["users"]:
                raise not_found("USER_NOT_FOUND", f"Unknown user '{username}'")
            return doc["users"].pop(username)
        removed = self.public(_update_with_retry(self.store, self.KEY, mutate, {"users": {}}))
        if self.secrets is not None:
            self.secrets.delete(user_secret(username))
        return removed


# --------------------------------------------------------------------------- locks
class LockRepo:
    def __init__(self, store: ObjectStore, ttl_seconds: int):
        self.store = store
        self.ttl = ttl_seconds

    @staticmethod
    def key(cluster_id: str, config_type: str, resource: str) -> str:
        return f"locks/{cluster_id}/{config_type}/{resource}.lock"

    def acquire(self, cluster_id: str, config_type: str, resource: str, owner: str) -> str:
        key = self.key(cluster_id, config_type, resource)
        token = new_id()
        body = {"owner": owner, "token": token, "acquiredAt": iso(),
                "expiresAtEpoch": time.time() + self.ttl}
        try:
            self.store.put_json(key, body, if_none_match=True)
            return token
        except PreconditionFailed:
            pass
        existing, etag = self.store.get_json(key)
        if existing is None:  # released in between
            try:
                self.store.put_json(key, body, if_none_match=True)
                return token
            except PreconditionFailed:
                existing, etag = self.store.get_json(key)
        if existing and existing.get("expiresAtEpoch", 0) < time.time():
            try:  # take over an expired lock
                self.store.put_json(key, body, if_match=etag)
                return token
            except PreconditionFailed:
                existing, _ = self.store.get_json(key)
        owner_now = (existing or {}).get("owner", "another request")
        raise conflict("CHANGE_IN_PROGRESS",
                       f"{owner_now} is changing this resource right now; retry shortly",
                       {"lockedBy": owner_now, "since": (existing or {}).get("acquiredAt")})

    def verify(self, cluster_id: str, config_type: str, resource: str, token: str | None) -> None:
        """Abort if our lock expired and someone else took it."""
        existing, _ = self.store.get_json(self.key(cluster_id, config_type, resource))
        if not existing or existing.get("token") != token:
            raise conflict("LOCK_LOST", "This change took longer than LOCK_TTL_SECONDS and its "
                           "lock was taken over; check the resource and retry")

    def release(self, cluster_id: str, config_type: str, resource: str, token: str) -> None:
        key = self.key(cluster_id, config_type, resource)
        existing, _ = self.store.get_json(key)
        if existing and existing.get("token") == token:
            self.store.delete(key)


# ----------------------------------------------------------------- access rules
LEVELS = ("view",)
CLUSTER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_permissions(clusters: Any) -> dict[str, str]:
    """{clusterId | '*': 'view' | 'none'}.

    'view' lets the user search that cluster's logs. '*' applies to every cluster without
    its own entry (including clusters that appear later); 'none' on a cluster overrides '*'.
    Cluster ids are folder names under LOGS_PREFIX; one that has no folder (yet) is kept."""
    if not isinstance(clusters, dict):
        raise bad_request("INVALID_PERMISSIONS", "Permissions must be {clusterId or '*': 'view' | 'none'}")
    out: dict[str, str] = {}
    for cid, level in clusters.items():
        if cid != "*" and not CLUSTER_RE.match(str(cid)):
            raise bad_request("INVALID_PERMISSIONS", f"'{cid}' is not a valid cluster folder name", {cid: level})
        if level in (None, ""):
            continue
        if level not in ("view", "none"):
            raise bad_request("INVALID_PERMISSIONS", "Access must be 'view' or 'none'", {cid: level})
        if level == "none" and cid == "*":
            continue  # same as no '*' entry
        out[cid] = level
    if len(out) > 500:
        raise bad_request("INVALID_PERMISSIONS", "At most 500 cluster entries")
    return out


# --------------------------------------------------------------- saved searches
class SavedSearchRepo:
    """Per-user saved searches: saved/<email>.json = {"items": [...]}. A saved search keeps
    the cluster and the console's URL parameters (the same text a shareable link carries)."""

    MAX_ITEMS = 200

    def __init__(self, store: ObjectStore):
        self.store = store

    @staticmethod
    def key(username: str) -> str:
        return f"saved/{quote(username, safe='@._+-')}.json"

    def list(self, username: str) -> list[dict]:
        data, _ = self.store.get_json(self.key(username))
        items = (data or {}).get("items", [])
        return sorted(items, key=lambda x: x.get("name", "").lower())

    def add(self, username: str, name: str, cluster: str, params: str) -> dict:
        item = {"id": new_id()[:12], "name": name, "cluster": cluster, "params": params,
                "createdAt": iso(), "updatedAt": iso()}

        def mutate(doc):
            items = doc.setdefault("items", [])
            same = next((x for x in items if x.get("name", "").lower() == name.lower()), None)
            if same:  # saving under an existing name replaces it
                same.update(cluster=cluster, params=params, updatedAt=iso())
                return dict(same)
            if len(items) >= self.MAX_ITEMS:
                raise bad_request("TOO_MANY_SAVED_SEARCHES",
                                  f"You can keep at most {self.MAX_ITEMS} saved searches; delete some first")
            items.append(item)
            return item
        return _update_with_retry(self.store, self.key(username), mutate, {"items": []})

    def delete(self, username: str, item_id: str) -> dict:
        def mutate(doc):
            items = doc.setdefault("items", [])
            hit = next((x for x in items if x.get("id") == item_id), None)
            if hit is None:
                raise not_found("SAVED_SEARCH_NOT_FOUND", "No saved search with that id")
            items.remove(hit)
            return hit
        return _update_with_retry(self.store, self.key(username), mutate, {"items": []})

    def delete_all(self, username: str) -> None:
        try:
            self.store.delete(self.key(username))
        except Exception:  # pragma: no cover - best effort when a user is removed
            log.exception("could not delete saved searches of %s", username)


# ------------------------------------------------------------------ event log
class EventLog:
    """Sign-ins, user changes and searches, written as JSON lines to the container log
    (docker compose logs). Nothing is stored in S3, and log contents are never written."""

    def write(self, event: dict) -> dict:
        event = {"eventId": new_id(), "timestamp": iso(), **event}
        log.info(json.dumps(event, default=str, separators=(",", ":")))
        return event


__all__ = ["UsersRepo", "LockRepo", "SavedSearchRepo", "EventLog", "validate_permissions",
           "CLUSTER_RE"]
