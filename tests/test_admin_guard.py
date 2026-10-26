"""The admin code is compared in constant time, and wrong guesses are throttled
per client address without ever locking the facilitator out."""

import asyncio
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import config
from app import AdminGuard, admin_code_ok, admin_guard, app

ADDR = "1.2.3.4"


@pytest.fixture
def client(fresh_state):
    with TestClient(app) as c:
        yield c


def _delay(seconds: float) -> None:
    config.patch_config("admin.fail_delay_s", seconds)
    config.patch_config("admin.fail_delay_max_s", seconds)


def test_code_comparison_is_exact():
    assert admin_code_ok(config.ADMIN_CODE)
    assert not admin_code_ok(config.ADMIN_CODE[:-1])
    assert not admin_code_ok(config.ADMIN_CODE + "x")
    assert not admin_code_ok("")
    assert not admin_code_ok(None)


def test_delay_doubles_per_failure_and_is_capped():
    config.patch_config("admin.fail_delay_s", 1)
    config.patch_config("admin.fail_delay_max_s", 10)
    assert [AdminGuard.delay_for(n) for n in (1, 2, 3, 4, 5, 9)] == [1, 2, 4, 8, 10, 10]


async def test_failures_accumulate_and_are_forgiven():
    guard = AdminGuard()
    _delay(0)
    await guard.punish(ADDR)
    await guard.punish(ADDR)
    assert guard.failures(ADDR) == 2
    assert guard.failures("5.6.7.8") == 0
    guard.forgive(ADDR)
    assert guard.failures(ADDR) == 0


async def test_wrong_attempts_from_one_address_wait_in_line():
    guard = AdminGuard()
    _delay(0.1)
    t0 = time.perf_counter()
    await asyncio.gather(*(guard.punish(ADDR) for _ in range(3)))
    assert time.perf_counter() - t0 >= 0.28  # three in a row, not three at once

    t0 = time.perf_counter()
    await asyncio.gather(guard.punish("a"), guard.punish("b"), guard.punish("c"))
    assert time.perf_counter() - t0 < 0.25  # other addresses never queue on each other


async def test_a_flood_from_one_address_is_refused_at_once():
    guard = AdminGuard()
    _delay(0.05)
    results = await asyncio.gather(
        *(guard.punish(ADDR) for _ in range(guard.max_waiting + 3)), return_exceptions=True
    )
    refused = [r for r in results if isinstance(r, HTTPException)]
    assert len(refused) == 3
    assert all(r.status_code == 429 for r in refused)


def test_wrong_code_is_delayed_but_the_right_code_never_waits(client):
    _delay(0.3)
    t0 = time.perf_counter()
    assert client.post("/api/admin/auth", json={"code": "wrong"}).status_code == 401
    assert time.perf_counter() - t0 >= 0.25

    t0 = time.perf_counter()
    r = client.get("/api/admin/state", headers={"X-Admin-Code": config.ADMIN_CODE})
    assert r.status_code == 200
    assert time.perf_counter() - t0 < 0.25

    # the success forgot the slip, so the next one costs the base delay again
    assert admin_guard.failures("testclient") == 0
    t0 = time.perf_counter()
    assert client.get("/api/admin/state", headers={"X-Admin-Code": "wrong"}).status_code == 401
    assert time.perf_counter() - t0 >= 0.25


def test_every_admin_endpoint_counts_a_bad_code(client):
    _delay(0)
    bad = {"X-Admin-Code": "nope"}
    for path in ("/api/admin/state", "/api/admin/cheatsheet", "/api/admin/duels"):
        assert client.get(path, headers=bad).status_code == 401
    assert client.post("/api/admin/llm/pause", headers=bad).status_code == 401
    assert admin_guard.failures("testclient") == 4
