"""Ce que le solveur a le droit de prendre dans l'operating wallet, par
(chain, stable) — persisté dans connectors/wallet_sources.json, édité depuis
le panel "Details" d'un nœud Wallet du frontend (GET/POST /api/wallet-sources).

Par défaut (fichier absent, ou paire absente) : le solde on-chain RÉEL, lu
en direct au build du graphe (compass_test.balances.list_wallet_balances),
est utilisé tel quel comme source bornée (voir graph.node.WalletNode.balance).
Deux réglages par paire :
  - "enabled": false  -> ce solde est ignoré par le solveur (wallet = pur
                         transit), pour réserver cet argent à autre chose ;
  - "overrideUsd": x  -> x remplace le solde live (plafond manuel, ou
                         valeur de repli si la lecture RPC échoue).

Format : {"<CHAIN>/<STABLE>": {"enabled": bool, "overrideUsd": float|null}}.
"""

from __future__ import annotations

import json
from pathlib import Path

from graph.structures.DEXes import Chain, Stable

DEFAULT_WALLET_SOURCES_PATH = Path("connectors/wallet_sources.json")

WalletSourcesDict = dict[str, dict]


def pair_key(chain: Chain, stable: Stable) -> str:
    return f"{chain.name}/{stable.name}"


def load_wallet_sources(path: Path | str = DEFAULT_WALLET_SOURCES_PATH) -> WalletSourcesDict:
    filePath = Path(path)
    if not filePath.exists():
        return {}
    return json.loads(filePath.read_text(encoding="utf-8"))


def save_wallet_sources(settings: WalletSourcesDict, path: Path | str = DEFAULT_WALLET_SOURCES_PATH) -> None:
    filePath = Path(path)
    filePath.parent.mkdir(parents=True, exist_ok=True)
    tmpPath = filePath.with_suffix(filePath.suffix + ".tmp")
    tmpPath.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmpPath.replace(filePath)


def validate_wallet_sources(payload: object) -> WalletSourcesDict:
    if not isinstance(payload, dict):
        raise ValueError('wallet sources must be a JSON object keyed by "CHAIN/STABLE"')
    cleaned: WalletSourcesDict = {}
    for key, entry in payload.items():
        try:
            chainName, stableName = key.split("/")
            Chain[chainName]
            Stable[stableName]
        except (ValueError, KeyError) as exc:
            raise ValueError(f"invalid wallet key {key!r} (expected CHAIN/STABLE)") from exc
        if not isinstance(entry, dict):
            raise ValueError(f"{key}: entry must be an object")
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"{key}: enabled must be a boolean")
        override = entry.get("overrideUsd")
        if override is not None:
            if not isinstance(override, (int, float)) or isinstance(override, bool) or override < 0:
                raise ValueError(f"{key}: overrideUsd must be a number >= 0 or null")
            override = round(float(override), 2)
        cleaned[key] = {"enabled": enabled, "overrideUsd": override}
    return cleaned


def resolve_wallet_balances(
    liveBalances: dict[tuple[Chain, Stable], float | None], settings: WalletSourcesDict
) -> dict[tuple[Chain, Stable], float]:
    """Le solde que le solveur verra par paire : override si posé, sinon le
    live ; 0 si désactivé ou si le live est inconnu (lecture RPC échouée)
    sans override -- jamais un montant inventé."""
    resolved: dict[tuple[Chain, Stable], float] = {}
    for (chain, stable), live in liveBalances.items():
        entry = settings.get(pair_key(chain, stable), {})
        if not entry.get("enabled", True):
            resolved[(chain, stable)] = 0.0
            continue
        override = entry.get("overrideUsd")
        if override is not None:
            resolved[(chain, stable)] = float(override)
        else:
            resolved[(chain, stable)] = float(live) if live is not None else 0.0
    return resolved
