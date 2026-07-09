import time
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

def _future(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0
    ).isoformat().replace("+00:00", "Z")

def test_rate_limiting():
    # Register and login a user specifically for rate limiting test
    org_name = f"rate-org-{time.time()}"
    client.post(
        "/auth/register",
        json={"org_name": org_name, "username": "bob", "password": "password123"},
    )
    token = client.post(
        "/auth/login",
        json={"org_name": org_name, "username": "bob", "password": "password123"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Send 20 requests (which might be invalid bookings, but rate limit checks user.id first)
    # The API contract says: "POST /bookings is limited to 20 requests per rolling 60 seconds per user (all requests count, successful or not)"
    # Since they count, even bad requests should increment the bucket and eventually return 429
    status_codes = []
    for i in range(25):
        res = client.post(
            "/bookings",
            json={"room_id": 99999, "start_time": "invalid", "end_time": "invalid"},
            headers=headers,
        )
        status_codes.append(res.status_code)

    # The first 20 should not be 429 (they will probably be 400 due to invalid payload)
    # The 21st onwards should be 429
    assert status_codes[20] == 429
    assert status_codes[21] == 429


def test_booking_quota():
    org_name = f"quota-org-{time.time()}"
    client.post(
        "/auth/register",
        json={"org_name": org_name, "username": "alice", "password": "password123"},
    )
    token = client.post(
        "/auth/login",
        json={"org_name": org_name, "username": "alice", "password": "password123"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create room
    room = client.post(
        "/rooms",
        json={"name": "Conference", "capacity": 10, "hourly_rate_cents": 1000},
        headers=headers,
    )
    room_id = room.json()["id"]

    # Book 3 slots in the next 24 hours
    b1 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(2), "end_time": _future(3)},
        headers=headers,
    )
    assert b1.status_code == 201
    b2 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(4), "end_time": _future(5)},
        headers=headers,
    )
    assert b2.status_code == 201
    b3 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(6), "end_time": _future(7)},
        headers=headers,
    )
    assert b3.status_code == 201

    # The 4th booking in the 24h window should fail with 409 QUOTA EXCEEDED
    b4 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(8), "end_time": _future(9)},
        headers=headers,
    )
    assert b4.status_code == 409
    assert b4.json()["code"] == "QUOTA EXCEEDED"

    # Cancel one booking
    b1_id = b1.json()["id"]
    cancel_res = client.post(f"/bookings/{b1_id}/cancel", headers=headers)
    assert cancel_res.status_code == 200

    # Now the 4th booking should succeed
    b4_retry = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(8), "end_time": _future(9)},
        headers=headers,
    )
    assert b4_retry.status_code == 201


def test_refund_rounding_and_tiers():
    org_name = f"refund-org-{time.time()}"
    client.post(
        "/auth/register",
        json={"org_name": org_name, "username": "admin", "password": "password123"},
    )
    token = client.post(
        "/auth/login",
        json={"org_name": org_name, "username": "admin", "password": "password123"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create a room with rate 1001 cents (so 50% refund is 500.5 -> 501 cents due to half-up rounding)
    room = client.post(
        "/rooms",
        json={"name": "Rounding Room", "capacity": 5, "hourly_rate_cents": 1001},
        headers=headers,
    )
    room_id = room.json()["id"]

    # 1. Booking starting in 50 hours (notice >= 48 hours -> 100% refund)
    b_100 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(50), "end_time": _future(51)},
        headers=headers,
    ).json()
    c_100 = client.post(f"/bookings/{b_100['id']}/cancel", headers=headers)
    assert c_100.status_code == 200
    assert c_100.json()["refund_percent"] == 100
    assert c_100.json()["refund_amount_cents"] == 1001

    # 2. Booking starting in 30 hours (24 <= notice < 48 -> 50% refund)
    b_50 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(30), "end_time": _future(31)},
        headers=headers,
    ).json()
    c_50 = client.post(f"/bookings/{b_50['id']}/cancel", headers=headers)
    assert c_50.status_code == 200
    assert c_50.json()["refund_percent"] == 50
    assert c_50.json()["refund_amount_cents"] == 501  # 50% of 1001 = 500.5 -> 501

    # 3. Booking starting in 10 hours (notice < 24 -> 0% refund)
    b_0 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(10), "end_time": _future(11)},
        headers=headers,
    ).json()
    c_0 = client.post(f"/bookings/{b_0['id']}/cancel", headers=headers)
    assert c_0.status_code == 200
    assert c_0.json()["refund_percent"] == 0
    assert c_0.json()["refund_amount_cents"] == 0


def test_multi_tenancy_isolation():
    # Register Org A
    org_a = f"org-a-{time.time()}"
    client.post(
        "/auth/register",
        json={"org_name": org_a, "username": "user-a", "password": "password123"},
    )
    token_a = client.post(
        "/auth/login",
        json={"org_name": org_a, "username": "user-a", "password": "password123"},
    ).json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Register Org B
    org_b = f"org-b-{time.time()}"
    client.post(
        "/auth/register",
        json={"org_name": org_b, "username": "user-b", "password": "password123"},
    )
    token_b = client.post(
        "/auth/login",
        json={"org_name": org_b, "username": "user-b", "password": "password123"},
    ).json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # Org A creates a room
    room_a = client.post(
        "/rooms",
        json={"name": "Room A", "capacity": 5, "hourly_rate_cents": 1000},
        headers=headers_a,
    ).json()
    room_a_id = room_a["id"]

    # Org B trying to view Org A's room availability -> 404 ROOM NOT FOUND
    res = client.get(f"/rooms/{room_a_id}/availability?date=2026-07-10", headers=headers_b)
    assert res.status_code == 404
    assert res.json()["code"] == "ROOM NOT FOUND"

    # Org B trying to book Org A's room -> 404 ROOM NOT FOUND
    res = client.post(
        "/bookings",
        json={"room_id": room_a_id, "start_time": _future(10), "end_time": _future(11)},
        headers=headers_b,
    )
    assert res.status_code == 404
    assert res.json()["code"] == "ROOM NOT FOUND"


def test_token_revocation_and_rotation():
    org_name = f"rot-org-{time.time()}"
    client.post(
        "/auth/register",
        json={"org_name": org_name, "username": "user", "password": "password123"},
    )
    login_res = client.post(
        "/auth/login",
        json={"org_name": org_name, "username": "user", "password": "password123"},
    ).json()
    access_token = login_res["access_token"]
    refresh_token = login_res["refresh_token"]

    headers = {"Authorization": f"Bearer {access_token}"}
    
    # Verify access token works
    rooms_res = client.get("/rooms", headers=headers)
    assert rooms_res.status_code == 200

    # Logout
    logout_res = client.post("/auth/logout", headers=headers)
    assert logout_res.status_code == 200

    # Access token should be revoked now -> 401
    rooms_res_revoked = client.get("/rooms", headers=headers)
    assert rooms_res_revoked.status_code == 401

    # Refresh token should rotate
    refresh_res = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh_res.status_code == 200
    new_refresh_token = refresh_res.json()["refresh_token"]

    # Old refresh token should be invalidated (single-use) -> 401
    refresh_res_old = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh_res_old.status_code == 401
