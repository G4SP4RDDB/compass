"""Déséquilibres SAISIS À LA MAIN par DEX (panel "Details" du frontend, un
clic sur un DEX -> formulaire "Imbalance"), persistés dans
connectors/dex_imbalances.json et appliqués au registre avant de construire
le graphe (voir main.buildAndSolveGraph). Remplace l'ancien tirage aléatoire
de démo (main._generateMockImbalances, supprimé le 2026-09-10) : ce que le
solveur voit est exactement ce que l'utilisateur a tapé, rien d'autre.

Format : {dexName: {"kind": "surplus" | "deficit",
                    "amountUsd": <float > 0>,
                    "stable": "USDT" | "USDC"      (surplus seulement : LA stable retirable),
                    "chain":  "BSC" | ... | null   (surplus seulement, et seulement pour un
                                                    DEX à DEX.requiresSameChainWithdraw :
                                                    sur quelle chain ce solde est crédité)}}
Un DEX absent du fichier = équilibré (ni surplus ni déficit) : aucun flot ne
part de lui ni n'arrive chez lui.

Sémantique côté solveur (voir graph.solver._addFlowConservation) :
  - déficit  : DOIT être comblé exactement (SourceNode.balance = -amount) ;
  - surplus  : AU PLUS ce montant peut être retiré (WithdrawNode.balance =
               amount, contrainte <=) — le solveur ne prend que ce dont les
               déficits ont besoin, chez les sources les moins chères.
Donc la seule condition de faisabilité est total surplus >= total déficit,
vérifiée par check_feasibility AVANT d'appeler le solveur (message lisible
plutôt qu'un INFEASIBLE CP-SAT opaque).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from graph.structures.DEXes import DEX, Chain, Stable

DEFAULT_IMBALANCES_PATH = Path("connectors/dex_imbalances.json")

KIND_SURPLUS = "surplus"
KIND_DEFICIT = "deficit"
KINDS = (KIND_SURPLUS, KIND_DEFICIT)

ImbalancesDict = dict[str, dict]


def load_dex_imbalances(path: Path | str = DEFAULT_IMBALANCES_PATH) -> ImbalancesDict:
    filePath = Path(path)
    if not filePath.exists():
        return {}
    return json.loads(filePath.read_text(encoding="utf-8"))


def save_dex_imbalances(imbalances: ImbalancesDict, path: Path | str = DEFAULT_IMBALANCES_PATH) -> None:
    """Écriture atomique (tmp + rename), comme les autres fichiers de
    connectors/ édités depuis le frontend."""
    filePath = Path(path)
    filePath.parent.mkdir(parents=True, exist_ok=True)
    tmpPath = filePath.with_suffix(filePath.suffix + ".tmp")
    tmpPath.write_text(json.dumps(imbalances, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmpPath.replace(filePath)


def validate_dex_imbalances(payload: object, dexRegistry: dict[str, DEX]) -> ImbalancesDict:
    """Nettoie/valide un payload {dexName: entry} (voir format en tête de
    module) contre le registre : DEX connu, kind valide, montant > 0, stable
    supportée par ce DEX, chain supportée (et exigée seulement si
    DEX.requiresSameChainWithdraw). Une entrée `null` ou {"kind": null}
    retire le déséquilibre de ce DEX. Lève ValueError avec un message
    destiné à être affiché tel quel dans le frontend."""
    if not isinstance(payload, dict):
        raise ValueError("imbalances must be a JSON object keyed by DEX name")
    cleaned: ImbalancesDict = {}
    for dexName, entry in payload.items():
        if dexName not in dexRegistry:
            raise ValueError(f"unknown DEX {dexName!r}")
        if entry is None or (isinstance(entry, dict) and not entry.get("kind")):
            continue  # équilibré : pas d'entrée
        if not isinstance(entry, dict):
            raise ValueError(f"{dexName}: entry must be an object")
        kind = entry.get("kind")
        if kind not in KINDS:
            raise ValueError(f'{dexName}: kind must be "surplus" or "deficit", got {kind!r}')
        amount = entry.get("amountUsd")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
            raise ValueError(f"{dexName}: amountUsd must be a number > 0")
        dex = dexRegistry[dexName]
        cleanedEntry: dict = {"kind": kind, "amountUsd": round(float(amount), 2)}
        if kind == KIND_SURPLUS:
            stableName = entry.get("stable") or (dex.stables[0].name if dex.stables else None)
            if stableName not in {s.name for s in dex.stables}:
                raise ValueError(f"{dexName}: stable must be one of {[s.name for s in dex.stables]}, got {stableName!r}")
            cleanedEntry["stable"] = stableName
            chainName = entry.get("chain") or None
            if dex.requiresSameChainWithdraw and chainName is None:
                raise ValueError(
                    f"{dexName}: this DEX only withdraws to the chain the balance sits on — pick a chain"
                )
            if chainName is not None and chainName not in {c.name for c in dex.chains}:
                raise ValueError(f"{dexName}: chain must be one of {[c.name for c in dex.chains]}, got {chainName!r}")
            cleanedEntry["chain"] = chainName
        cleaned[dexName] = cleanedEntry
    return cleaned


def apply_dex_imbalances(dexList: list[DEX], imbalances: ImbalancesDict) -> None:
    """Pose inbalance / withdrawBalances / withdrawChainByStable sur chaque
    DEX (remis à zéro d'abord : un DEX absent du fichier est équilibré).
    Convention : inbalance > 0 = surplus (montant retirable, aussi porté par
    withdrawBalances[stable] — c'est CE dict que Graph lit pour le
    WithdrawNode), inbalance < 0 = déficit (lu par SourceNode)."""
    for dex in dexList:
        dex.inbalance = 0.0
        dex.withdrawBalances = {}
        dex.withdrawChainByStable = {}
        entry = imbalances.get(dex.name)
        if not entry:
            continue
        amount = float(entry["amountUsd"])
        if entry["kind"] == KIND_DEFICIT:
            dex.inbalance = -amount
            continue
        stable = Stable[entry["stable"]]
        dex.inbalance = amount
        dex.withdrawBalances = {stable: amount}
        if entry.get("chain"):
            dex.withdrawChainByStable = {stable: Chain[entry["chain"]]}


@dataclass(frozen=True)
class ImbalanceSummary:
    totalSurplusUsd: float
    totalDeficitUsd: float
    surplusDexes: list[str]
    deficitDexes: list[str]

    @property
    def feasible(self) -> bool:
        # Cents : même arrondi que le solveur (voir solver.SCALE), pour ne
        # pas déclarer infaisable un écart de flottant.
        return round(self.totalSurplusUsd * 100) >= round(self.totalDeficitUsd * 100)

    @property
    def problem(self) -> str | None:
        if not self.deficitDexes:
            return None  # rien à combler : le solveur renverra un plan vide, ce n'est pas une erreur
        if not self.surplusDexes:
            return "no DEX has a surplus to draw from — set at least one surplus"
        if not self.feasible:
            return (
                f"total deficit ${self.totalDeficitUsd:.2f} exceeds total surplus ${self.totalSurplusUsd:.2f} "
                f"— raise a surplus or lower a deficit by ${self.totalDeficitUsd - self.totalSurplusUsd:.2f}"
            )
        return None

    def to_dict(self) -> dict:
        return {
            "totalSurplusUsd": self.totalSurplusUsd,
            "totalDeficitUsd": self.totalDeficitUsd,
            "surplusDexes": self.surplusDexes,
            "deficitDexes": self.deficitDexes,
            "feasible": self.feasible,
            "problem": self.problem,
        }


def summarize_dex_imbalances(imbalances: ImbalancesDict) -> ImbalanceSummary:
    surplus = {name: e["amountUsd"] for name, e in imbalances.items() if e and e.get("kind") == KIND_SURPLUS}
    deficit = {name: e["amountUsd"] for name, e in imbalances.items() if e and e.get("kind") == KIND_DEFICIT}
    return ImbalanceSummary(
        totalSurplusUsd=float(sum(surplus.values())),
        totalDeficitUsd=float(sum(deficit.values())),
        surplusDexes=sorted(surplus),
        deficitDexes=sorted(deficit),
    )


class InfeasibleImbalancesError(ValueError):
    """Les déséquilibres saisis ne peuvent pas être résolus (voir
    ImbalanceSummary.problem) — levé AVANT de construire le modèle CP-SAT."""


def check_feasibility(imbalances: ImbalancesDict) -> ImbalanceSummary:
    summary = summarize_dex_imbalances(imbalances)
    if summary.problem is not None:
        raise InfeasibleImbalancesError(summary.problem)
    return summary
