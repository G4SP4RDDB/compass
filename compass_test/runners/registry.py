"""Maps a DEX name (graph.structures.DEXes.DEX.name, e.g. "MEXC", matching
src/graph/structures/dex_registry.py) to its connector — instantiated lazily
so importing this module never requires every DEX's credentials to be
present, only the ones actually used in a given run."""

from __future__ import annotations

from .aden import AdenConnector
from .aster import AsterConnector
from .base import DexConnector
from .extended import ExtendedConnector
from .hyperliquid import HyperliquidConnector
from .lighter import LighterConnector
from .mexc import MexcConnector
from .ondo import OndoConnector
from .unsupported import UnsupportedConnector

_CONNECTOR_CLASSES: dict[str, type[DexConnector]] = {
    "MEXC": MexcConnector,
    "Aster": AsterConnector,
    "Aden": AdenConnector,
    "Hyperliquid": HyperliquidConnector,
    # Withdraw only (fast path) — build_deposit_tx raises, see
    # runners/lighter.py's own docstring for why.
    "Lighter": LighterConnector,
    # Withdraw only (bridged via Rhino.fi) — build_deposit_tx raises, see
    # runners/extended.py's own docstring for why.
    "Extended": ExtendedConnector,
    "Ondo Perps": OndoConnector,
}

_instances: dict[str, DexConnector] = {}


def get_connector(dex_name: str) -> DexConnector:
    if dex_name in _instances:
        return _instances[dex_name]

    connector_class = _CONNECTOR_CLASSES.get(dex_name)
    connector = connector_class() if connector_class is not None else UnsupportedConnector(dex_name)
    _instances[dex_name] = connector
    return connector


def is_supported(dex_name: str) -> bool:
    return dex_name in _CONNECTOR_CLASSES


def supported_dex_names() -> list[str]:
    return sorted(_CONNECTOR_CLASSES)
