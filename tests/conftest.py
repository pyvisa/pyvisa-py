# -*- coding: utf-8 -*-
"""Top-level pytest bootstrap and backend hook policies for pyvisa-py."""

from __future__ import annotations

import os
import pathlib
import sys
from collections.abc import Mapping, Sequence

import pytest

# Prefer local sibling pyvisa checkout when shared contract modules are not yet
# available in installed pyvisa packages.
try:
    from pyvisa.testing import InstrumentProfile
except ModuleNotFoundError:
    sibling_pyvisa = pathlib.Path(__file__).resolve().parents[2] / "pyvisa"
    if sibling_pyvisa.exists():
        sys.path.insert(0, str(sibling_pyvisa))
    sys.modules.pop("pyvisa", None)
    from pyvisa.testing import InstrumentProfile

pytest_plugins = ("pyvisa.testing.pytest_plugin",)

# Ensure contract tests target pyvisa-py backend when run in this repository.
os.environ.setdefault("PYVISA_LIBRARY", "@py")


def _keysight_profile_from_env() -> InstrumentProfile | None:
    """Build a profile from Keysight virtual instrument environment settings."""
    setting = os.environ.get("PYVISA_KEYSIGHT_VIRTUAL_INSTR")
    if setting is None:
        return None

    if setting == "0":
        addresses = {
            "TCPIP::INSTR": "TCPIP::127.0.0.1::INSTR",
            "TCPIP::SOCKET": "TCPIP::127.0.0.1::5025::SOCKET",
        }
    else:
        addresses = {
            "TCPIP::INSTR": "TCPIP::192.168.0.2::INSTR",
            "TCPIP::SOCKET": "TCPIP::192.168.0.2::5025::SOCKET",
        }

    return InstrumentProfile(
        name="keysight-virtual-instr",
        resource_addresses=addresses,
        command_map={
            "identity_query": "*IDN?",
            "shared_query": "QUERY?",
            "health_query": "SYST:HEALTH?",
            "binary_query_template": "DATA:BIN? {datatype},{count},{endian},{header},{termination},{pattern},{start}",
        },
        capabilities={
            "transport.vxi11": True,
            "transport.socket": True,
            "transport.hislip": False,
            "transport.usb": False,
        },
        metadata={"source": "pyvisa-py-tests"},
    )


@pytest.hookimpl(tryfirst=True)
def pytest_pyvisa_select_profile(
    config: pytest.Config, profile_name: str
) -> InstrumentProfile | None:
    """Prefer Keysight profile for shared contracts in pyvisa-py."""
    _ = (config, profile_name)
    return _keysight_profile_from_env()


@pytest.hookimpl(tryfirst=True)
def pytest_pyvisa_backend_capabilities(
    config: pytest.Config,
    backend_id: str,
    profile: InstrumentProfile | None,
) -> Mapping[str, bool]:
    """Declare pyvisa-py backend capabilities for shared contracts."""
    _ = (config, profile)
    if backend_id not in ("py", "ivi"):
        return {}

    return {
        "transport.vxi11": True,
        "transport.socket": True,
        "transport.hislip": False,
        "transport.usb": True,
        "events.srq": False,
        "locking.shared": False,
    }


@pytest.hookimpl(tryfirst=True)
def pytest_pyvisa_contract_exclusions(
    config: pytest.Config,
    backend_id: str,
    profile: InstrumentProfile | None,
) -> Sequence[tuple[str, str]]:
    """Replace duplicated xfail wiring with centralized contract exclusions."""
    _ = (config, profile)
    if backend_id not in ("py", "ivi"):
        return []

    return [
        ("identity.query.tcpip::hislip", "pyvisa-py does not support HiSLIP"),
        (
            "keysight.tcpip.query.tcpip::instr",
            "Keysight-style INSTR query contract remains unstable on pyvisa-py",
        ),
        (
            "resource_manager.open_close.tcpip::hislip",
            "pyvisa-py does not support HiSLIP",
        ),
        (
            "resource_manager.listed.tcpip::hislip",
            "pyvisa-py does not support HiSLIP",
        ),
        (
            "resource_manager.info.tcpip::hislip",
            "pyvisa-py does not support HiSLIP",
        ),
    ]
