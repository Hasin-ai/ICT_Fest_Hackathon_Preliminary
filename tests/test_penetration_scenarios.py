import time
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from unittest.mock import patch

from app.main import app
from app import cache
from app.auth import _revoked_tokens, _used_refresh_tokens
from app.services import reference

client = TestClient(app)

def _future(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0
    ).isoformat().replace("+00:00", "Z")


def test_unbounded_cache_dos_vulnerability():
    """
    Demonstrates that the room availability caching mechanism is unbounded
    and vulnerable to Cache Poisoning / Memory Exhaustion (DoS).
    An attacker can query random dates, causing the cache size to grow indefinitely.
    """
    # Register and login
    org_name = f"cache-dos-{time.time()}"
    client.post(
        "/auth/register",
        json={"org_name": org_name, "username": "admin", "password": "password123"},
    )
    token = client.post(
        "/auth/login",
        json={"org_name": org_name, "username": "admin", "password": "password123"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create a room
    room = client.post(
        "/rooms",
        json={"name": "Cache Room", "capacity": 5, "hourly_rate_cents": 1000},
        headers=headers,
    )
    room_id = room.json()["id"]

    # Measure starting cache size
    start_cache_size = len(cache._availability_cache)

    # Simulate an attacker scanning / querying availability for many distinct days
    # (e.g. 50 distinct requests)
    for i in range(50):
        # Must be valid dates since API checks format, let's use valid dates: 2030-01-01 to 2030-01-28 etc.
        month = 1 + (i // 28)
        day = 1 + (i % 28)
        fake_date = f"2030-{month:02d}-{day:02d}"
        res = client.get(f"/rooms/{room_id}/availability?date={fake_date}", headers=headers)
        assert res.status_code == 200

    end_cache_size = len(cache._availability_cache)
    added_entries = end_cache_size - start_cache_size

    # Verify that the cache grew by exactly the number of unique queries made,
    # proving there is no eviction policy (e.g. LRU or size limit)
    assert added_entries == 50
    print(f"\n[Vulnerability Verified] Cache grew from {start_cache_size} to {end_cache_size} entries with no limits.")


def test_cross_day_availability_inconsistency():
    """
    Demonstrates a logic bug where a booking spanning across calendar days
    (e.g., from 11:00 PM Day 1 to 2:00 AM Day 2) is not shown in the availability
    endpoint for Day 2, yet booking that room on Day 2 during those hours
    correctly triggers a 409 ROOM CONFLICT.
    This creates an inconsistent state where the API reports a room as available
    but refuses bookings for it.
    """
    org_name = f"cross-day-{time.time()}"
    client.post(
        "/auth/register",
        json={"org_name": org_name, "username": "admin", "password": "password123"},
    )
    token = client.post(
        "/auth/login",
        json={"org_name": org_name, "username": "admin", "password": "password123"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    room = client.post(
        "/rooms",
        json={"name": "Border Room", "capacity": 5, "hourly_rate_cents": 1000},
        headers=headers,
    ).json()
    room_id = room["id"]

    # Choose two consecutive future dates
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date()
    day_after = tomorrow + timedelta(days=1)

    # Book the room cross-day: e.g. starts tomorrow at 11:00 PM, ends day after at 2:00 AM (3 hours duration)
    start_time = f"{tomorrow.isoformat()}T23:00:00Z"
    end_time = f"{day_after.isoformat()}T02:00:00Z"

    booking_res = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": start_time, "end_time": end_time},
        headers=headers,
    )
    assert booking_res.status_code == 201

    # Invalidate caches to ensure we hit the database query for availability
    cache._availability_cache.clear()

    # Query availability for the day after
    avail_res = client.get(f"/rooms/{room_id}/availability?date={day_after.isoformat()}", headers=headers)
    assert avail_res.status_code == 200
    busy_intervals = avail_res.json()["busy"]

    # Since the booking ends on the day after at 02:00 AM, the room IS busy on the day after from 00:00 to 02:00.
    # However, because the query filters only by booking's start_time on that day, this interval will be missing.
    has_conflict_interval_in_avail = any(
        interval["end_time"] == f"{day_after.isoformat()}T02:00:00Z" for interval in busy_intervals
    )

    # Now attempt to book the room during the time it should be busy (00:00 to 01:00 AM on day_after)
    conflict_start = f"{day_after.isoformat()}T00:00:00Z"
    conflict_end = f"{day_after.isoformat()}T01:00:00Z"

    conflict_res = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": conflict_start, "end_time": conflict_end},
        headers=headers,
    )

    # Assert the corrected behavior:
    # 1. The booking conflicts and returns 409
    assert conflict_res.status_code == 409
    assert conflict_res.json()["code"] == "ROOM CONFLICT"

    # 2. And the availability endpoint correctly shows the room as busy
    assert has_conflict_interval_in_avail, "Availability endpoint correctly reported the busy slot from the previous day's booking"
    print("\n[Vulnerability Fixed] Cross-day booking correctly updates availability on both days.")


def test_concurrent_reference_code_conflict():
    """
    Demonstrates that unique constraint violations (like reference code collisions)
    are handled by retrying, or raising a 500 error instead of mapping to ROOM CONFLICT.
    """
    org_name = f"ref-conflict-{time.time()}"
    client.post(
        "/auth/register",
        json={"org_name": org_name, "username": "admin", "password": "password123"},
    )
    token = client.post(
        "/auth/login",
        json={"org_name": org_name, "username": "admin", "password": "password123"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create a room
    room = client.post(
        "/rooms",
        json={"name": "Room 1", "capacity": 5, "hourly_rate_cents": 1000},
        headers=headers,
    ).json()

    # We mock Session.commit to raise IntegrityError on every call, simulating a persistent unique constraint failure.
    # The application should retry and then raise a 500 INTERNAL_ERROR instead of 409 ROOM CONFLICT.
    with patch("sqlalchemy.orm.Session.commit") as mock_commit:
        mock_commit.side_effect = IntegrityError(None, None, None)
        
        res = client.post(
            "/bookings",
            json={"room_id": room["id"], "start_time": _future(2), "end_time": _future(3)},
            headers=headers,
        )
        assert res.status_code == 500
        assert res.json()["code"] == "INTERNAL ERROR"
        
    print("\n[Vulnerability Fixed] Database constraint errors on booking creation do not masquerade as 'ROOM CONFLICT'.")


def test_token_revocation_loss_on_restart():
    """
    Demonstrates that logged-out/revoked tokens are persisted in the database,
    preventing token replay even if the server restarts or in-memory sets are cleared.
    """
    org_name = f"token-loss-{time.time()}"
    client.post(
        "/auth/register",
        json={"org_name": org_name, "username": "user", "password": "password123"},
    )
    login_res = client.post(
        "/auth/login",
        json={"org_name": org_name, "username": "user", "password": "password123"},
    ).json()
    access_token = login_res["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    # Verify access works
    res_rooms = client.get("/rooms", headers=headers)
    assert res_rooms.status_code == 200

    # Logout to revoke access token
    logout_res = client.post("/auth/logout", headers=headers)
    assert logout_res.status_code == 200

    # Verify token is indeed revoked
    res_rooms_revoked = client.get("/rooms", headers=headers)
    assert res_rooms_revoked.status_code == 401

    # Simulate server restart / memory clear of _revoked_tokens set
    _revoked_tokens.clear()

    # Replay the revoked token (should still fail because of DB persistence)
    res_rooms_replayed = client.get("/rooms", headers=headers)
    assert res_rooms_replayed.status_code == 401
    print("\n[Vulnerability Fixed] Revoked tokens are persisted and cannot be replayed after a restart.")


