"""Déficit SAISI À LA MAIN dû à des retraits utilisateur (l'entreprise gère
les fonds pour compte de tiers, voir main.buildAndSolveGraph), persisté dans
connectors/wallet_deficits.json et appliqué au graphe avant de le résoudre --
même mécanique manuelle que connectors/dex_imbalances.py (un JSON édité
depuis un panel du frontend), mais keyé par STABLE plutôt que par DEX : ce
déficit n'appartient à aucun DEX, voir graph.node.WalletDeficitNode.

Format : {"USDT": {"kind": "deficit", "amountUsd": <float > 0>}, ...}
Une stable absente du fichier = pas de déficit wallet sur cette stable.

Contrairement à un déficit DEX (comblable seulement depuis les chains de CE
DEX, voir DEX.chains), ce déficit est PARTAGÉ entre TOUTES les chains --
l'entreprise n'a pas de préférence sur celle qui sert à payer un retrait,
voir Graph._linkWalletPayouts. Sémantique solveur identique à un déficit DEX
(voir graph.solver._addFlowConservation) : DOIT être comblé exactement
(WalletDeficitNode.balance = -amount)."""

from __future__ import annotations

import json
from pathlib import Path

from graph.structures.DEXes import Stable

DEFAULT_WALLET_DEFICITS_PATH = Path("connectors/wallet_deficits.json")

KIND_DEFICIT = "deficit"

WalletDeficitsDict = dict[str, dict]


def load_wallet_deficits(path: Path | str = DEFAULT_WALLET_DEFICITS_PATH) -> WalletDeficitsDict:
    filePath = Path(path)
    if not filePath.exists():
        return {}
    return json.loads(filePath.read_text(encoding="utf-8"))


def save_wallet_deficits(deficits: WalletDeficitsDict, path: Path | str = DEFAULT_WALLET_DEFICITS_PATH) -> None:
    """Écriture atomique (tmp + rename), comme les autres fichiers de
    connectors/ édités depuis le frontend (voir dex_imbalances.save_dex_imbalances)."""
    filePath = Path(path)
    filePath.parent.mkdir(parents=True, exist_ok=True)
    tmpPath = filePath.with_suffix(filePath.suffix + ".tmp")
    tmpPath.write_text(json.dumps(deficits, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmpPath.replace(filePath)


def validate_wallet_deficits(payload: object) -> WalletDeficitsDict:
    """Nettoie/valide un payload {stableName: entry} contre le registre des
    stables connues : kind "deficit", montant > 0. Une entrée `null` ou
    {"kind": null} retire le déficit de cette stable. Lève ValueError avec un
    message destiné à être affiché tel quel dans le frontend (voir
    dex_imbalances.validate_dex_imbalances, même contrat)."""
    if not isinstance(payload, dict):
        raise ValueError("wallet deficits must be a JSON object keyed by stable name")
    cleaned: WalletDeficitsDict = {}
    for stableName, entry in payload.items():
        if stableName not in Stable.__members__:
            raise ValueError(f"unknown stable {stableName!r}")
        if entry is None or (isinstance(entry, dict) and not entry.get("kind")):
            continue  # pas de déficit sur cette stable : pas d'entrée
        if not isinstance(entry, dict):
            raise ValueError(f"{stableName}: entry must be an object")
        if entry.get("kind") != KIND_DEFICIT:
            raise ValueError(f'{stableName}: kind must be "deficit", got {entry.get("kind")!r}')
        amount = entry.get("amountUsd")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
            raise ValueError(f"{stableName}: amountUsd must be a number > 0")
        cleaned[stableName] = {"kind": KIND_DEFICIT, "amountUsd": round(float(amount), 2)}
    return cleaned


def apply_wallet_deficits(deficits: WalletDeficitsDict) -> dict[Stable, float]:
    """Convertit {stableName: {"amountUsd": x}} en {Stable.X: -x}, prêt pour
    Graph(walletDeficits=...) -- toujours <= 0, même convention que
    DEX.inbalance pour un déficit (voir dex_imbalances.apply_dex_imbalances)."""
    return {Stable[stableName]: -float(entry["amountUsd"]) for stableName, entry in deficits.items() if entry}


def total_wallet_deficit_usd(deficits: WalletDeficitsDict) -> float:
    return float(sum(entry["amountUsd"] for entry in deficits.values() if entry))
