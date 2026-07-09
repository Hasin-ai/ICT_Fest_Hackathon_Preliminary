import sys
import time
import requests
import concurrent.futures
from datetime import datetime, timedelta, timezone

BASE_URL = "http://localhost:8000"

def _future(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0
    ).isoformat().replace("+00:00", "Z")

def test_health():
    print("Testing /health...")
    res = requests.get(f"{BASE_URL}/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
    print("Health check OK!")

def test_error_code_spaces():
    print("Testing error code spaces...")
    # Attempt to register with empty username/password to trigger Pydantic validation (422)
    reg_empty = requests.post(
        f"{BASE_URL}/auth/register",
        json={"org_name": "acme", "username": "", "password": ""},
    )
    assert reg_empty.status_code == 422

    # Normal registration
    org_name = f"space-org-{time.time()}"
    reg = requests.post(
        f"{BASE_URL}/auth/register",
        json={"org_name": org_name, "username": "alice", "password": "password123"},
    )
    assert reg.status_code == 201

    # Duplicate registration to trigger USERNAME TAKEN
    reg2 = requests.post(
        f"{BASE_URL}/auth/register",
        json={"org_name": org_name, "username": "alice", "password": "password123"},
    )
    assert reg2.status_code == 409
    assert reg2.json()["code"] == "USERNAME TAKEN"

    # Bad login credentials to trigger INVALID CREDENTIALS
    login_bad = requests.post(
        f"{BASE_URL}/auth/login",
        json={"org_name": org_name, "username": "alice", "password": "wrongpassword"},
    )
    assert login_bad.status_code == 401
    assert login_bad.json()["code"] == "INVALID CREDENTIALS"
    print("Error code spaces OK!")

def test_datetime_z_format():
    print("Testing datetime Z format...")
    org_name = f"dt-org-{time.time()}"
    requests.post(
        f"{BASE_URL}/auth/register",
        json={"org_name": org_name, "username": "alice", "password": "password123"},
    )
    token = requests.post(
        f"{BASE_URL}/auth/login",
        json={"org_name": org_name, "username": "alice", "password": "password123"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create room
    room = requests.post(
        f"{BASE_URL}/rooms",
        json={"name": "Focus Room", "capacity": 4, "hourly_rate_cents": 1000},
        headers=headers,
    )
    assert room.status_code == 201
    room_id = room.json()["id"]

    # Book room
    start_time = _future(2)
    end_time = _future(3)
    booking = requests.post(
        f"{BASE_URL}/bookings",
        json={"room_id": room_id, "start_time": start_time, "end_time": end_time},
        headers=headers,
    )
    assert booking.status_code == 201
    b_data = booking.json()
    assert b_data["start_time"].endswith("Z")
    assert b_data["end_time"].endswith("Z")
    assert b_data["created_at"].endswith("Z")
    print("Datetime Z format OK!")

def test_room_validation():
    print("Testing room validations...")
    org_name = f"val-org-{time.time()}"
    requests.post(
        f"{BASE_URL}/auth/register",
        json={"org_name": org_name, "username": "alice", "password": "password123"},
    )
    token = requests.post(
        f"{BASE_URL}/auth/login",
        json={"org_name": org_name, "username": "alice", "password": "password123"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Try to create room with negative capacity
    res1 = requests.post(
        f"{BASE_URL}/rooms",
        json={"name": "Room A", "capacity": -5, "hourly_rate_cents": 100},
        headers=headers,
    )
    assert res1.status_code == 422

    # Try to create room with zero hourly rate
    res2 = requests.post(
        f"{BASE_URL}/rooms",
        json={"name": "Room B", "capacity": 5, "hourly_rate_cents": 0},
        headers=headers,
    )
    assert res2.status_code == 422
    print("Room validations OK!")

def test_export_and_admin_validation():
    print("Testing export & admin date range validation...")
    org_name = f"admin-org-{time.time()}"
    requests.post(
        f"{BASE_URL}/auth/register",
        json={"org_name": org_name, "username": "admin", "password": "password123"},
    )
    token = requests.post(
        f"{BASE_URL}/auth/login",
        json={"org_name": org_name, "username": "admin", "password": "password123"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Verify CSV headers
    export = requests.get(f"{BASE_URL}/admin/export", headers=headers)
    assert export.status_code == 200
    first_line = export.text.split("\n")[0]
    expected_header = "id,reference code,room_id,user id,start time,end time,status,price cents"
    assert first_line.strip() == expected_header

    # Usage report invalid date range
    today = datetime.now(timezone.utc).date().isoformat()
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    report = requests.get(
        f"{BASE_URL}/admin/usage-report?from={today}&to={yesterday}",
        headers=headers,
    )
    assert report.status_code == 400
    assert report.json()["code"] == "INVALID BOOKING WINDOW"
    print("Export & admin date range validation OK!")

def test_caching_and_invalidation():
    print("Testing caching and invalidation...")
    org_name = f"cache-org-{time.time()}"
    requests.post(
        f"{BASE_URL}/auth/register",
        json={"org_name": org_name, "username": "admin", "password": "password123"},
    )
    token = requests.post(
        f"{BASE_URL}/auth/login",
        json={"org_name": org_name, "username": "admin", "password": "password123"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Use a future date (tomorrow) to avoid booking in the past
    future_date_obj = datetime.now(timezone.utc) + timedelta(days=1)
    future_date = future_date_obj.date().isoformat()

    # Query usage report (should be empty first, and cached)
    r1 = requests.get(f"{BASE_URL}/admin/usage-report?from={future_date}&to={future_date}", headers=headers)
    assert r1.status_code == 200
    assert len(r1.json()["rooms"]) == 0

    # Create a room
    room = requests.post(
        f"{BASE_URL}/rooms",
        json={"name": "Room A", "capacity": 5, "hourly_rate_cents": 1000},
        headers=headers,
    )
    assert room.status_code == 201
    room_id = room.json()["id"]

    # Query usage report again (should invalidate and now show 1 room)
    r2 = requests.get(f"{BASE_URL}/admin/usage-report?from={future_date}&to={future_date}", headers=headers)
    assert r2.status_code == 200
    assert len(r2.json()["rooms"]) == 1
    assert r2.json()["rooms"][0]["confirmed_bookings"] == 0

    # Query availability (should be empty and cached)
    av1 = requests.get(f"{BASE_URL}/rooms/{room_id}/availability?date={future_date}", headers=headers)
    assert av1.status_code == 200
    assert len(av1.json()["busy"]) == 0

    # Book room
    start_time = f"{future_date}T10:00:00Z"
    end_time = f"{future_date}T12:00:00Z"
    booking = requests.post(
        f"{BASE_URL}/bookings",
        json={"room_id": room_id, "start_time": start_time, "end_time": end_time},
        headers=headers,
    )
    assert booking.status_code == 201

    # Query availability again (should show 1 busy slot)
    av2 = requests.get(f"{BASE_URL}/rooms/{room_id}/availability?date={future_date}", headers=headers)
    assert av2.status_code == 200
    assert len(av2.json()["busy"]) == 1
    print("Caching and invalidation OK!")

def test_concurrency_race():
    print("Testing concurrency and DB locks...")
    org_name = f"concur-org-{time.time()}"
    requests.post(
        f"{BASE_URL}/auth/register",
        json={"org_name": org_name, "username": "admin", "password": "password123"},
    )
    token = requests.post(
        f"{BASE_URL}/auth/login",
        json={"org_name": org_name, "username": "admin", "password": "password123"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    room = requests.post(
        f"{BASE_URL}/rooms",
        json={"name": "Focus", "capacity": 5, "hourly_rate_cents": 1000},
        headers=headers,
    )
    room_id = room.json()["id"]

    # Concurrently attempt to book the exact same slot. Only one should succeed.
    start_time = _future(10)
    end_time = _future(12)

    def do_book():
        return requests.post(
            f"{BASE_URL}/bookings",
            json={"room_id": room_id, "start_time": start_time, "end_time": end_time},
            headers=headers,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_book) for _ in range(5)]
        results = [f.result() for f in futures]

    successes = [r for r in results if r.status_code == 201]
    conflicts = [r for r in results if r.status_code == 409]

    assert len(successes) == 1
    assert len(conflicts) == 4
    for c in conflicts:
        assert c.json()["code"] == "ROOM CONFLICT"
    print("Concurrency and DB locks OK!")


def test_rate_limiting():
    print("Testing rate limiting...")
    org_name = f"rate-org-{time.time()}"
    requests.post(
        f"{BASE_URL}/auth/register",
        json={"org_name": org_name, "username": "bob", "password": "password123"},
    )
    token = requests.post(
        f"{BASE_URL}/auth/login",
        json={"org_name": org_name, "username": "bob", "password": "password123"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    status_codes = []
    for _ in range(25):
        res = requests.post(
            f"{BASE_URL}/bookings",
            json={"room_id": 99999, "start_time": "invalid", "end_time": "invalid"},
            headers=headers,
        )
        status_codes.append(res.status_code)

    assert status_codes[20] == 429
    assert status_codes[21] == 429
    print("Rate limiting OK!")


def test_booking_quota():
    print("Testing booking quota limit...")
    org_name = f"quota-org-{time.time()}"
    requests.post(
        f"{BASE_URL}/auth/register",
        json={"org_name": org_name, "username": "alice", "password": "password123"},
    )
    token = requests.post(
        f"{BASE_URL}/auth/login",
        json={"org_name": org_name, "username": "alice", "password": "password123"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    room = requests.post(
        f"{BASE_URL}/rooms",
        json={"name": "Conference", "capacity": 10, "hourly_rate_cents": 1000},
        headers=headers,
    )
    room_id = room.json()["id"]

    b1 = requests.post(
        f"{BASE_URL}/bookings",
        json={"room_id": room_id, "start_time": _future(2), "end_time": _future(3)},
        headers=headers,
    )
    assert b1.status_code == 201
    b2 = requests.post(
        f"{BASE_URL}/bookings",
        json={"room_id": room_id, "start_time": _future(4), "end_time": _future(5)},
        headers=headers,
    )
    assert b2.status_code == 201
    b3 = requests.post(
        f"{BASE_URL}/bookings",
        json={"room_id": room_id, "start_time": _future(6), "end_time": _future(7)},
        headers=headers,
    )
    assert b3.status_code == 201

    b4 = requests.post(
        f"{BASE_URL}/bookings",
        json={"room_id": room_id, "start_time": _future(8), "end_time": _future(9)},
        headers=headers,
    )
    assert b4.status_code == 409
    assert b4.json()["code"] == "QUOTA EXCEEDED"

    b1_id = b1.json()["id"]
    cancel_res = requests.post(f"{BASE_URL}/bookings/{b1_id}/cancel", headers=headers)
    assert cancel_res.status_code == 200

    b4_retry = requests.post(
        f"{BASE_URL}/bookings",
        json={"room_id": room_id, "start_time": _future(8), "end_time": _future(9)},
        headers=headers,
    )
    assert b4_retry.status_code == 201
    print("Booking quota limit OK!")


def test_refund_rounding_and_tiers():
    print("Testing refund rounding and tiers...")
    org_name = f"refund-org-{time.time()}"
    requests.post(
        f"{BASE_URL}/auth/register",
        json={"org_name": org_name, "username": "admin", "password": "password123"},
    )
    token = requests.post(
        f"{BASE_URL}/auth/login",
        json={"org_name": org_name, "username": "admin", "password": "password123"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    room = requests.post(
        f"{BASE_URL}/rooms",
        json={"name": "Rounding Room", "capacity": 5, "hourly_rate_cents": 1001},
        headers=headers,
    )
    room_id = room.json()["id"]

    b_100 = requests.post(
        f"{BASE_URL}/bookings",
        json={"room_id": room_id, "start_time": _future(50), "end_time": _future(51)},
        headers=headers,
    ).json()
    c_100 = requests.post(f"{BASE_URL}/bookings/{b_100['id']}/cancel", headers=headers)
    assert c_100.status_code == 200
    assert c_100.json()["refund_percent"] == 100
    assert c_100.json()["refund_amount_cents"] == 1001

    b_50 = requests.post(
        f"{BASE_URL}/bookings",
        json={"room_id": room_id, "start_time": _future(30), "end_time": _future(31)},
        headers=headers,
    ).json()
    c_50 = requests.post(f"{BASE_URL}/bookings/{b_50['id']}/cancel", headers=headers)
    assert c_50.status_code == 200
    assert c_50.json()["refund_percent"] == 50
    assert c_50.json()["refund_amount_cents"] == 501

    b_0 = requests.post(
        f"{BASE_URL}/bookings",
        json={"room_id": room_id, "start_time": _future(10), "end_time": _future(11)},
        headers=headers,
    ).json()
    c_0 = requests.post(f"{BASE_URL}/bookings/{b_0['id']}/cancel", headers=headers)
    assert c_0.status_code == 200
    assert c_0.json()["refund_percent"] == 0
    assert c_0.json()["refund_amount_cents"] == 0
    print("Refund rounding and tiers OK!")


def test_multi_tenancy_isolation():
    print("Testing multi-tenancy isolation...")
    org_a = f"org-a-{time.time()}"
    requests.post(
        f"{BASE_URL}/auth/register",
        json={"org_name": org_a, "username": "user-a", "password": "password123"},
    )
    token_a = requests.post(
        f"{BASE_URL}/auth/login",
        json={"org_name": org_a, "username": "user-a", "password": "password123"},
    ).json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    org_b = f"org-b-{time.time()}"
    requests.post(
        f"{BASE_URL}/auth/register",
        json={"org_name": org_b, "username": "user-b", "password": "password123"},
    )
    token_b = requests.post(
        f"{BASE_URL}/auth/login",
        json={"org_name": org_b, "username": "user-b", "password": "password123"},
    ).json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    room_a = requests.post(
        f"{BASE_URL}/rooms",
        json={"name": "Room A", "capacity": 5, "hourly_rate_cents": 1000},
        headers=headers_a,
    ).json()
    room_a_id = room_a["id"]

    res = requests.get(f"{BASE_URL}/rooms/{room_a_id}/availability?date=2026-07-10", headers=headers_b)
    assert res.status_code == 404
    assert res.json()["code"] == "ROOM NOT FOUND"

    res = requests.post(
        f"{BASE_URL}/bookings",
        json={"room_id": room_a_id, "start_time": _future(10), "end_time": _future(11)},
        headers=headers_b,
    )
    assert res.status_code == 404
    assert res.json()["code"] == "ROOM NOT FOUND"
    print("Multi-tenancy isolation OK!")


def test_token_revocation_and_rotation():
    print("Testing token revocation and rotation...")
    org_name = f"rot-org-{time.time()}"
    requests.post(
        f"{BASE_URL}/auth/register",
        json={"org_name": org_name, "username": "user", "password": "password123"},
    )
    login_res = requests.post(
        f"{BASE_URL}/auth/login",
        json={"org_name": org_name, "username": "user", "password": "password123"},
    ).json()
    access_token = login_res["access_token"]
    refresh_token = login_res["refresh_token"]

    headers = {"Authorization": f"Bearer {access_token}"}
    
    rooms_res = requests.get(f"{BASE_URL}/rooms", headers=headers)
    assert rooms_res.status_code == 200

    logout_res = requests.post(f"{BASE_URL}/auth/logout", headers=headers)
    assert logout_res.status_code == 200

    rooms_res_revoked = requests.get(f"{BASE_URL}/rooms", headers=headers)
    assert rooms_res_revoked.status_code == 401

    refresh_res = requests.post(f"{BASE_URL}/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh_res.status_code == 200
    new_refresh_token = refresh_res.json()["refresh_token"]

    refresh_res_old = requests.post(f"{BASE_URL}/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh_res_old.status_code == 401
    print("Token revocation and rotation OK!")


if __name__ == "__main__":
    try:
        test_health()
        test_error_code_spaces()
        test_datetime_z_format()
        test_room_validation()
        test_export_and_admin_validation()
        test_caching_and_invalidation()
        test_concurrency_race()
        test_rate_limiting()
        test_booking_quota()
        test_refund_rounding_and_tiers()
        test_multi_tenancy_isolation()
        test_token_revocation_and_rotation()
        print("\nAll live tests completed successfully!")
    except AssertionError as e:
        print(f"\nAssertion Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nUnexpected Error: {e}")
        sys.exit(1)
