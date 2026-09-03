import json
from pathlib import Path

from graph.structures.DEXes import DEX

# Écrit/lu par le panel "Config" du frontend (visualization/web/graph_template.html)
# via visualization/server.py (GET/POST /api/config) : l'utilisateur édite les
# frais/délais de dépôt/retrait par DEX directement dans la page, le serveur
# persiste ici. Absent -> tout le monde reste à 0.0 (voir DEX.__init__).
DEFAULT_PARAMS_PATH = Path("connectors/dex_operational_params.json")

CONFIG_FIELDS = ["withdrawFeeUsd", "withdrawDelaySeconds", "depositFeeUsd", "depositDelaySeconds"]


def load_dex_operational_params(path: Path | str = DEFAULT_PARAMS_PATH) -> dict[str, dict[str, float]]:
    filePath = Path(path)
    if not filePath.exists():
        return {}
    return json.loads(filePath.read_text(encoding="utf-8"))


def save_dex_operational_params(
    params: dict[str, dict[str, float]], path: Path | str = DEFAULT_PARAMS_PATH
) -> None:
    """Écrit params sur disque de façon atomique (fichier temporaire + rename)
    pour qu'une requête POST /api/config concurrente ou un crash en cours
    d'écriture ne puisse jamais laisser un JSON à moitié écrit derrière lui."""
    filePath = Path(path)
    filePath.parent.mkdir(parents=True, exist_ok=True)
    tmpPath = filePath.with_suffix(filePath.suffix + ".tmp")
    tmpPath.write_text(json.dumps(params, indent=2, sort_keys=True), encoding="utf-8")
    tmpPath.replace(filePath)


def apply_dex_operational_params(dexList: list[DEX], params: dict[str, dict[str, float]]) -> None:
    for dex in dexList:
        values = params.get(dex.name)
        if not values:
            continue
        dex.withdrawFeeUsd = values.get("withdrawFeeUsd", dex.withdrawFeeUsd)
        dex.withdrawDelaySeconds = values.get("withdrawDelaySeconds", dex.withdrawDelaySeconds)
        dex.depositFeeUsd = values.get("depositFeeUsd", dex.depositFeeUsd)
        dex.depositDelaySeconds = values.get("depositDelaySeconds", dex.depositDelaySeconds)
