"""Fixtures for the tests that run against a real Home Assistant core.

The suite under ``tests/`` uses hand-written doubles of Home Assistant and runs
anywhere. This one needs ``pytest-homeassistant-custom-component`` (Python 3.13)
and checks what the doubles cannot: that flows, migrations and entry setup behave
the way the real core drives them.
"""

from __future__ import annotations

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/autoledger."""
    return enable_custom_integrations
