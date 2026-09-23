"""Déficit SAISI À LA MAIN dû à des retraits utilisateur (l'entreprise gère
les fonds pour compte de tiers, voir main.buildAndSolveGraph), persisté dans
connectors/wallet_deficits.json et appliqué au graphe avant de le résoudre --
même mécanique manuelle que connectors/dex_imbalances.py (un JSON édité
depuis un panel du frontend), mais EN USD et sans propriétaire (ni DEX, ni
stable, ni chain) : voir graph.node.WalletDeficitNode.

Format : {"kind": "deficit", "amountUsd": <float > 0>}, ou {} (pas de déficit).
UN SEUL déficit pour tout le wallet, pas un par stable : régler un retrait en
USDC ou en USDT ne fait aucune différence pour l'entreprise, donc le montant
lui-même est en USD, jamais rattaché à une stable précise.

Fongible entre TOUTES les chains ET TOUTES les stables (voir
Graph._linkWalletPayouts) -- contrairement à un déficit DEX, comblable
seulement depuis les chains de CE DEX (voir DEX.chains) et jamais depuis une
autre stable sans passer par un swap. Sémantique solveur identique à un
déficit DEX (voir graph.solver._addFlowConservation) : DOIT être comblé
exactement (WalletDeficitNode.balance = -amount)."""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_WALLET_DEFICITS_PATH = Path("connectors/wallet_deficits.json")

KIND_DEFICIT = "deficit"

WalletDeficitEntry = dict


def load_wallet_deficits(path: Path | str = DEFAULT_WALLET_DEFICITS_PATH) -> WalletDeficitEntry:
    filePath = Path(path)
    if not filePath.exists():
        return {}
    return json.loads(filePath.read_text(encoding="utf-8"))


def save_wallet_deficits(entry: WalletDeficitEntry, path: Path | str = DEFAULT_WALLET_DEFICITS_PATH) -> None:
    """Écriture atomique (tmp + rename), comme les autres fichiers de
    connectors/ édités depuis le frontend (voir dex_imbalances.save_dex_imbalances)."""
    filePath = Path(path)
    filePath.parent.mkdir(parents=True, exist_ok=True)
    tmpPath = filePath.with_suffix(filePath.suffix + ".tmp")
    tmpPath.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmpPath.replace(filePath)


def validate_wallet_deficits(payload: object) -> WalletDeficitEntry:
    """Nettoie/valide un payload {"kind": "deficit", "amountUsd": x} : kind
    "deficit", montant > 0. `null` ou {"kind": null} retire le déficit
    (retourne {}). Lève ValueError avec un message destiné à être affiché tel
    quel dans le frontend (voir dex_imbalances.validate_dex_imbalances, même
    contrat)."""
    if payload is None or (isinstance(payload, dict) and not payload.get("kind")):
        return {}
    if not isinstance(payload, dict):
        raise ValueError("wallet deficit must be a JSON object")
    if payload.get("kind") != KIND_DEFICIT:
        raise ValueError(f'kind must be "deficit", got {payload.get("kind")!r}')
    amount = payload.get("amountUsd")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
        raise ValueError("amountUsd must be a number > 0")
    return {"kind": KIND_DEFICIT, "amountUsd": round(float(amount), 2)}


def apply_wallet_deficits(entry: WalletDeficitEntry) -> float:
    """Convertit {"amountUsd": x} en -x, prêt pour
    Graph(walletDeficitUsd=...) -- toujours <= 0, même convention que
    DEX.inbalance pour un déficit (voir dex_imbalances.apply_dex_imbalances).
    {} -> 0.0 (pas de déficit)."""
    return -float(entry["amountUsd"]) if entry else 0.0


def total_wallet_deficit_usd(entry: WalletDeficitEntry) -> float:
    return float(entry["amountUsd"]) if entry else 0.0
