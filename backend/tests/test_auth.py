from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from backend.app.auth import SESSION_COOKIE_NAME, create_session_token
from backend.app.main import app


def _sample_project(pid: str, name: str) -> dict:
    return {
        "project_info": {
            "project_id": pid,
            "project_name": name,
            "location": "Manila, Philippines",
            "design_code": "NSCP 2015",
            "materials": {
                "fc_mpa": 28.0,
                "fy_mpa": 415.0,
                "concrete_density_kn_m3": 23.54,
            },
        },
        "levels": [{"id": "BASE", "elevation_m": 0.0}],
        "columns": [],
    }


def test_auth_disabled_keeps_api_open_with_existing_data_dir_layout(tmp_path, monkeypatch):
    vol = tmp_path / "open_vol"
    monkeypatch.setenv("DATA_DIR", str(vol))
    monkeypatch.setenv("AUTH_ENABLED", "false")

    client = TestClient(app)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json() == {"auth_enabled": False, "authenticated": False, "username": None}
    assert "no-store" in (me.headers.get("cache-control") or "")

    r_save = client.post("/api/projects", json=_sample_project("LOCAL-01", "Local Open Project"))
    assert r_save.status_code == 200
    assert (vol / "projects" / "LOCAL-01.json").is_file()
    assert not (vol / "users").exists()

    r_list = client.get("/api/projects")
    assert r_list.status_code == 200
    assert any(p["project_id"] == "LOCAL-01" for p in r_list.json())


def test_auth_enabled_gates_api_and_isolates_per_user_storage(tmp_path, monkeypatch):
    vol = tmp_path / "beta_vol"
    monkeypatch.setenv("DATA_DIR", str(vol))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_SECRET", "test-secret-key-0123456789abcdef")
    monkeypatch.setenv("BETA_USERS", "Alice:alicepass, bob : bobpass, bad user:x, malformed")
    monkeypatch.setenv("BOT_API_KEY", "super-bot-key-xyz")
    monkeypatch.setenv("BOT_API_USER", "Paul")

    anon = TestClient(app)

    # 1. Public routes remain accessible when AUTH_ENABLED=true
    h = anon.get("/api/health")
    assert h.status_code == 200
    me_anon = anon.get("/api/auth/me")
    assert me_anon.status_code == 200
    assert me_anon.json() == {"auth_enabled": True, "authenticated": False, "username": None}
    assert "no-store" in (me_anon.headers.get("cache-control") or "")

    # OPTIONS preflight is never blocked with 401
    opt = anon.options(
        "/api/projects",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert opt.status_code != 401

    # Unauthenticated /api/projects returns 401 JSON
    r_unauth = anon.get("/api/projects")
    assert r_unauth.status_code == 401
    assert r_unauth.json()["detail"] == "Authentication required"

    # Bad login returns 401
    r_bad_login = anon.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
    assert r_bad_login.status_code == 401

    # 2. Login as Alice (case-insensitive username) -> creates project under DATA_DIR/users/alice/
    alice_client = TestClient(app)
    r_alice_login = alice_client.post(
        "/api/auth/login", json={"username": "ALICE", "password": "alicepass"}
    )
    assert r_alice_login.status_code == 200
    assert r_alice_login.json() == {"status": "ok", "username": "alice"}
    assert SESSION_COOKIE_NAME in alice_client.cookies

    me_alice = alice_client.get("/api/auth/me")
    assert me_alice.json() == {"auth_enabled": True, "authenticated": True, "username": "alice"}

    r_alice_save = alice_client.post(
        "/api/projects", json=_sample_project("ALICE-01", "Alice Tower")
    )
    assert r_alice_save.status_code == 200
    assert (vol / "users" / "alice" / "projects" / "ALICE-01.json").is_file()
    assert not (vol / "projects" / "ALICE-01.json").exists()

    # 3. Login as Bob -> cannot see or access Alice's project
    bob_client = TestClient(app)
    r_bob_login = bob_client.post("/api/auth/login", json={"username": "bob", "password": "bobpass"})
    assert r_bob_login.status_code == 200

    bob_list = bob_client.get("/api/projects")
    assert bob_list.status_code == 200
    assert bob_list.json() == []
    assert bob_client.get("/api/projects/ALICE-01").status_code == 404

    # 4. Expired or tampered sao_session cookie rejected with 401
    expired_tok = create_session_token("alice", ttl_seconds=-10, now=time.time())
    exp_client = TestClient(app)
    exp_client.cookies.set(SESSION_COOKIE_NAME, expired_tok)
    assert exp_client.get("/api/projects").status_code == 401

    valid_tok = create_session_token("alice")
    tampered_tok = valid_tok[:-4] + ("0000" if not valid_tok.endswith("0000") else "ffff")
    tamp_client = TestClient(app)
    tamp_client.cookies.set(SESSION_COOKIE_NAME, tampered_tok)
    assert tamp_client.get("/api/projects").status_code == 401

    # 5. Valid X-API-Key authenticates as BOT_API_USER ('paul') without browser cookie
    bot_client = TestClient(app)
    r_bot_me = bot_client.get("/api/auth/me", headers={"X-API-Key": "super-bot-key-xyz"})
    assert r_bot_me.status_code == 200
    assert r_bot_me.json() == {"auth_enabled": True, "authenticated": True, "username": "paul"}

    r_bot_save = bot_client.post(
        "/api/projects",
        headers={"X-API-Key": "super-bot-key-xyz"},
        json=_sample_project("BOT-01", "Bot Building"),
    )
    assert r_bot_save.status_code == 200
    assert (vol / "users" / "paul" / "projects" / "BOT-01.json").is_file()

    # Logout clears cookie
    r_out = alice_client.post("/api/auth/logout")
    assert r_out.status_code == 200
    assert alice_client.get("/api/projects").status_code == 401
