"""Délais de retrait/dépôt MESURÉS par (DEX, chain), rechargés sur les objets
DEX pour primer sur les valeurs configurées à la main (voir
connectors.dex_operational_params) dans costing.computeDelay.

Source : connectors/dex_measured_delays.json, produit par
compass_test/calibration.py à partir des rapports compass_test/reports/*.json
(reconstruit automatiquement après chaque run live, ou à la demande via
`python -m compass_test.cli calibrate-delays`). Ce module ne fait QUE lire/
écrire ce fichier et l'appliquer au registre : `src`/`connectors` ne
dépendent jamais de compass_test (même découpage que chain_block_times.py :
la mesure vit d'un côté, la consommation de l'autre, le fichier JSON est le
contrat).

Format : {dexName: {chainName: {"withdrawDelaySeconds": <entry>,
                                 "depositDelaySeconds":  <entry>}}}
         + une clé spéciale SWAP_VENUE_KEY ("CoW Swap", pas un DEX) :
         {"CoW Swap": {chainName: {"swapDelaySeconds": <entry>}}} -- le
         délai réel d'exécution d'un swap CoW sur cette chain (ordre posté
         -> rempli), consommé par costing.computeDelay sur les edges Swap
         via measured_swap_delay() ci-dessous.
avec <entry> = {"meanSeconds", "n", "stdSeconds", "minSeconds", "maxSeconds",
"lastMeasuredAt", "samplesSeconds", "sampleRunIds"}. Un champ absent =
aucune mesure pour ce (DEX, chain, sens) -> la config reste effective.

Règle de calcul (décision produit, 2026-09-10) : moyenne arithmétique des
MAX_SAMPLES derniers runs live réussis (status "ok"). Un seul run suffit à
primer sur la config -- 1 mesure réelle vaut mieux que le "withdraw in
5 min" affiché par le frontend du DEX.
"""

from __future__ import annotations

import json
from pathlib import Path

from graph.structures.DEXes import DEX, Chain, MeasuredDelay

DEFAULT_MEASURED_DELAYS_PATH = Path("connectors/dex_measured_delays.json")

# Fenêtre glissante : au-delà, les runs les plus anciens sortent de la
# moyenne (un DEX qui change de régime -- batching, congestion -- finit par
# être rattrapé sans qu'on ait à purger l'historique à la main).
MAX_SAMPLES = 10

WITHDRAW_FIELD = "withdrawDelaySeconds"
DEPOSIT_FIELD = "depositDelaySeconds"
SWAP_FIELD = "swapDelaySeconds"
# Clé de premier niveau des délais de swap dans le JSON : le nom de la venue
# (connectors.cowswap.COWSWAP_VENUE_NAME), qui n'est PAS un DEX du registre
# -- apply_measured_delays l'ignore donc naturellement côté DEX et la range
# dans _MEASURED_SWAP_DELAY_BY_CHAIN à la place.
SWAP_VENUE_KEY = "CoW Swap"

MeasuredDelaysDict = dict[str, dict[str, dict[str, dict]]]


def measured_delay_to_dict(measured: MeasuredDelay) -> dict:
    return {
        "meanSeconds": measured.meanSeconds,
        "n": measured.n,
        "stdSeconds": measured.stdSeconds,
        "minSeconds": measured.minSeconds,
        "maxSeconds": measured.maxSeconds,
        "lastMeasuredAt": measured.lastMeasuredAt,
        "samplesSeconds": list(measured.samplesSeconds),
        "sampleRunIds": list(measured.sampleRunIds),
    }


def measured_delay_from_dict(d: dict) -> MeasuredDelay:
    return MeasuredDelay(
        meanSeconds=float(d["meanSeconds"]),
        samplesSeconds=[float(x) for x in d.get("samplesSeconds", [])],
        sampleRunIds=[str(x) for x in d.get("sampleRunIds", [])],
        lastMeasuredAt=d.get("lastMeasuredAt"),
    )


def measured_delay_from_samples(
    samplesSeconds: list[float], runIds: list[str], measuredAts: list[float]
) -> MeasuredDelay | None:
    """Construit l'entrée à partir des échantillons ordonnés du plus ancien
    au plus récent ; seuls les MAX_SAMPLES derniers sont retenus. None si
    aucun échantillon (pas d'entrée -> la config reste effective)."""
    if not samplesSeconds:
        return None
    kept = samplesSeconds[-MAX_SAMPLES:]
    keptIds = runIds[-MAX_SAMPLES:]
    keptAts = measuredAts[-MAX_SAMPLES:]
    return MeasuredDelay(
        meanSeconds=sum(kept) / len(kept),
        samplesSeconds=list(kept),
        sampleRunIds=list(keptIds),
        lastMeasuredAt=max(keptAts) if keptAts else None,
    )


def load_measured_delays(path: Path | str = DEFAULT_MEASURED_DELAYS_PATH) -> MeasuredDelaysDict:
    filePath = Path(path)
    if not filePath.exists():
        return {}
    return json.loads(filePath.read_text(encoding="utf-8"))


def save_measured_delays(measured: MeasuredDelaysDict, path: Path | str = DEFAULT_MEASURED_DELAYS_PATH) -> None:
    """Écriture atomique (tmp + rename), comme save_dex_operational_params :
    reporter.save_report reconstruit ce fichier après chaque run live pendant
    que visualization/server.py peut le relire à tout moment."""
    filePath = Path(path)
    filePath.parent.mkdir(parents=True, exist_ok=True)
    tmpPath = filePath.with_suffix(filePath.suffix + ".tmp")
    tmpPath.write_text(json.dumps(measured, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmpPath.replace(filePath)


# Délais de swap mesurés, par chain -- état au niveau module (pas d'objet DEX
# à accrocher : un swap est WalletNode -> WalletNode, voir graph.node), rempli
# par apply_measured_delays et lu par costing.measuredDelay.
_MEASURED_SWAP_DELAY_BY_CHAIN: dict[Chain, MeasuredDelay] = {}


def measured_swap_delay(chain: Chain) -> MeasuredDelay | None:
    return _MEASURED_SWAP_DELAY_BY_CHAIN.get(chain)


def apply_measured_delays(dexList: list[DEX], measured: MeasuredDelaysDict) -> None:
    """Remplit DEX.measuredWithdrawDelayByChain / measuredDepositDelayByChain
    (vidés d'abord : un (DEX, chain) qui n'a plus de mesure retombe sur la
    config). Une entrée pour un DEX/chain absent du registre est ignorée,
    comme dans apply_dex_operational_params. Les délais de swap
    (SWAP_VENUE_KEY) vont dans measured_swap_delay(), même règle de
    remise à zéro."""
    _MEASURED_SWAP_DELAY_BY_CHAIN.clear()
    for chainName, entry in (measured.get(SWAP_VENUE_KEY) or {}).items():
        swapEntry = (entry or {}).get(SWAP_FIELD)
        if swapEntry and chainName in Chain.__members__:
            _MEASURED_SWAP_DELAY_BY_CHAIN[Chain[chainName]] = measured_delay_from_dict(swapEntry)
    for dex in dexList:
        dex.measuredWithdrawDelayByChain = {}
        dex.measuredDepositDelayByChain = {}
        chainEntries = measured.get(dex.name)
        if not chainEntries:
            continue
        for chain in dex.chains:
            entry = chainEntries.get(chain.name)
            if not entry:
                continue
            withdrawEntry = entry.get(WITHDRAW_FIELD)
            if withdrawEntry:
                dex.measuredWithdrawDelayByChain[chain] = measured_delay_from_dict(withdrawEntry)
            depositEntry = entry.get(DEPOSIT_FIELD)
            if depositEntry:
                dex.measuredDepositDelayByChain[chain] = measured_delay_from_dict(depositEntry)
