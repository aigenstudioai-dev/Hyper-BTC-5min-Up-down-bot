"""
tests/conftest.py – Shared pytest configuration and markers.

Markers
-------
@pytest.mark.network
    Tests that require live network access (Gamma API, Pyth Hermes).
    Skipped by default; run them with:  pytest --network

Usage
-----
    pytest                   # fast offline suite only
    pytest --network         # include live network tests
    pytest -m "not network"  # explicitly skip network tests
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--network",
        action="store_true",
        default=False,
        help="Run tests that require live network access.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "network: mark test as requiring live network access (deselected by default).",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--network"):
        return   # --network flag present: run everything

    skip_network = pytest.mark.skip(reason="requires --network flag to run")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip_network)
