"""Users, cluster access rules and saved searches (header mode)."""
from __future__ import annotations

from conftest import ROOT_H, add_user, as_user

ANA = "ana@fenixcommerce.com"


def test_admin_cluster_list_shows_all_folders_and_readers(client):
    add_user(client, ANA, {"post-btp-01": "view"})
    add_user(client, "raj@fenixcommerce.com", {"*": "view", "post-btp-01": "none"})
    r = client.get("/api/v1/admin/clusters", headers=ROOT_H)
    assert r.status_code == 200
    items = {c["id"]: c["users"] for c in r.json()["items"]}
    assert items == {"post-btp-01": [ANA], "post-btp-02": ["raj@fenixcommerce.com"],
                     "pre-prod-01": ["raj@fenixcommerce.com"]}
    assert client.get("/api/v1/admin/clusters", headers=as_user(ANA)).status_code == 403


def test_create_user_returns_password_once_and_validates(client):
    r = client.post("/api/v1/admin/users", json={"username": "x@fenixcommerce.com", "clusters": {"post-btp-01": "edit"}},
                    headers=ROOT_H)
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_PERMISSIONS"
    r = client.post("/api/v1/admin/users", json={"username": "x@fenixcommerce.com", "clusters": {"bad name!": "view"}},
                    headers=ROOT_H)
    assert r.status_code == 400
    u = add_user(client, ANA, {"post-btp-01": "view", "*": "none", "future-cluster": "view"})
    assert u["user"]["clusters"] == {"post-btp-01": "view", "future-cluster": "view"}
    r = client.post("/api/v1/admin/users", json={"username": ANA}, headers=ROOT_H)
    assert r.status_code == 409


def test_update_and_delete_user(client):
    add_user(client, ANA, {"post-btp-01": "view"})
    r = client.put(f"/api/v1/admin/users/{ANA}", json={"admin": False, "clusters": {"post-btp-02": "view"}},
                   headers=ROOT_H)
    assert r.status_code == 200 and r.json()["clusters"] == {"post-btp-02": "view"}
    ids = [c["id"] for c in client.get("/api/v1/clusters", headers=as_user(ANA)).json()["items"]]
    assert ids == ["post-btp-02"]
    r = client.put(f"/api/v1/admin/users/{ANA}/permissions", json={"*": "view"}, headers=ROOT_H)
    assert r.json()["clusters"] == {"*": "view"}
    client.post("/api/v1/saved-searches", json={"name": "errors", "cluster": "post-btp-01", "params": "q=x"},
                headers=as_user(ANA))
    assert client.delete(f"/api/v1/admin/users/{ANA}", headers=ROOT_H).status_code == 200
    assert client.get("/api/v1/me", headers=as_user(ANA)).status_code == 403
    assert client.delete("/api/v1/admin/users/root", headers=ROOT_H).status_code == 400


def test_bulk_add_is_all_or_nothing(client):
    add_user(client, ANA, {})
    r = client.post("/api/v1/admin/users/bulk", json={"users": [
        {"username": "new@fenixcommerce.com"}, {"username": ANA}]}, headers=ROOT_H)
    assert r.status_code == 409 and r.json()["error"]["details"]["existing"] == [ANA]
    assert client.get("/api/v1/admin/users/new@fenixcommerce.com", headers=ROOT_H).status_code == 404


def test_saved_searches_are_per_user(client):
    add_user(client, ANA, {"post-btp-01": "view"})
    a = as_user(ANA)
    r = client.post("/api/v1/saved-searches", json={"name": "UPS errors", "cluster": "post-btp-01",
                                                   "params": "?range=now-24h&q=carrier%3AUPS+level%3Aerror"}, headers=a)
    assert r.status_code == 201
    item = r.json()
    assert item["params"] == "range=now-24h&q=carrier%3AUPS+level%3Aerror"
    # same name replaces
    r = client.post("/api/v1/saved-searches", json={"name": "ups errors", "cluster": "post-btp-01",
                                                   "params": "range=now-1h"}, headers=a)
    assert r.json()["id"] == item["id"]
    items = client.get("/api/v1/saved-searches", headers=a).json()["items"]
    assert len(items) == 1 and items[0]["params"] == "range=now-1h"
    assert client.get("/api/v1/saved-searches", headers=ROOT_H).json()["items"] == []
    r = client.post("/api/v1/saved-searches", json={"name": "x", "cluster": "post-btp-02", "params": ""}, headers=a)
    assert r.status_code == 403
    assert client.delete(f"/api/v1/saved-searches/{item['id']}", headers=ROOT_H).status_code == 404
    assert client.delete(f"/api/v1/saved-searches/{item['id']}", headers=a).status_code == 200
    assert client.get("/api/v1/saved-searches", headers=a).json()["items"] == []


def test_unknown_user_is_refused(client):
    r = client.get("/api/v1/clusters", headers=as_user("nobody@fenixcommerce.com"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "UNKNOWN_USER"
