"""Petit serveur HTTP derrière les onglets "Config" et le curseur k de
graph_template.html.

src/main.py ne produit que des fichiers statiques (graph.html, graph.png,
operations.txt) — rien ne les sert, et l'onglet "Config" se contentait
d'afficher un JSON à copier-coller à la main dans
connectors/dex_operational_params.json. Ça ne marche pas pour quelqu'un qui,
en production, n'a qu'une URL vers cette page et pas accès au code.

Ce serveur sert graph.html, expose une petite API JSON que le JS de la page
appelle directement pour lire/écrire connectors/dex_operational_params.json
sur cette machine, et garde en mémoire le Graph construit au démarrage pour
re-solver à la volée quand l'utilisateur bouge le curseur k dans la barre
au-dessus du graphe (voir POST /api/solve) : Fee(e)/Time(e) par arête ne
dépendent pas de k (voir costing.py), seul le plan choisi par le solveur CP-
SAT en dépend, donc pas besoin de reconstruire tout le graphe à chaque fois.
Le bouton "Recompute routes" (POST /api/recompute) fait, lui, l'inverse :
reconstruit tout depuis zéro (nouveaux déséquilibres aléatoires de démo),
exactement ce que fait `python src/main.py`.

Lancement : python -m visualization.server
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from connectors.dex_operational_params import (
    CONFIG_FIELDS,
    load_dex_operational_params,
    save_dex_operational_params,
)
from graph.solver import graphSolve
from graph.urgency import TimeWeightParams
from main import buildAndSolveGraph
from visualization.graph_view import renderGraph
from visualization.web_view import graphToDict, renderGraphHtml, writeOperationsText

ROOT = Path(__file__).resolve().parent.parent
GRAPH_HTML_PATH = ROOT / "graph.html"
GRAPH_PNG_PATH = ROOT / "graph.png"
OPERATIONS_TXT_PATH = ROOT / "operations.txt"

app = Flask(__name__)

# Construit et résout le graphe une seule fois au démarrage du process (les
# déséquilibres de démo sont aléatoires, voir main._generateRandomImbalances
# — re-générer à chaque requête donnerait un graphe différent sous les pieds
# de l'utilisateur à chaque déplacement du curseur k). `_state` est mutable
# et réassigné par postSolve ci-dessous à chaque nouveau k.
_graph, _dexRegistry, _timeWeightParams = buildAndSolveGraph()
renderGraphHtml(_graph, outputPath=str(GRAPH_HTML_PATH), timeWeightParams=_timeWeightParams)


def _validateConfig(payload: Any) -> dict[str, dict[str, float]]:
    if not isinstance(payload, dict):
        raise ValueError("config must be a JSON object keyed by DEX name")
    cleaned: dict[str, dict[str, float]] = {}
    for dexName, values in payload.items():
        if not isinstance(dexName, str) or not isinstance(values, dict):
            raise ValueError(f"invalid entry for {dexName!r}")
        cleanedValues: dict[str, float] = {}
        for field in CONFIG_FIELDS:
            if field not in values:
                continue
            value = values[field]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{dexName}.{field} must be a number")
            cleanedValues[field] = float(value)
        cleaned[dexName] = cleanedValues
    return cleaned


@app.get("/")
def index():
    if not GRAPH_HTML_PATH.exists():
        return (
            "graph.html not found — run `python src/main.py` first to generate it.",
            404,
        )
    return send_from_directory(ROOT, "graph.html")


@app.get("/api/config")
def getConfig():
    return jsonify(load_dex_operational_params())


@app.post("/api/config")
def postConfig():
    try:
        cleaned = _validateConfig(request.get_json(force=True, silent=False))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        return jsonify({"error": "request body must be valid JSON"}), 400
    save_dex_operational_params(cleaned)
    return jsonify(cleaned)


@app.post("/api/solve")
def postSolve():
    """Re-solve le graphe déjà en mémoire avec un nouveau k, sans le
    reconstruire (voir le commentaire au chargement du module). Retourne le
    même format que graphToDict pour que le JS puisse patcher operations/
    journeys/timeWeight en place (voir refreshFromSolveResult côté frontend)."""
    global _timeWeightParams
    try:
        payload = request.get_json(force=True, silent=False)
        k = float(payload["k"])
    except Exception:
        return jsonify({"error": "request body must be JSON {\"k\": <number>}"}), 400
    if k <= 0:
        return jsonify({"error": "k must be strictly positive"}), 400

    _timeWeightParams = TimeWeightParams(
        lambda_min=_timeWeightParams.lambda_min,
        lambda_max=_timeWeightParams.lambda_max,
        k=k,
        epsilon=_timeWeightParams.epsilon,
    )
    graphSolve(_graph, _timeWeightParams)  # remplace edge.flow sur _graph.edgeList
    renderGraphHtml(_graph, outputPath=str(GRAPH_HTML_PATH), timeWeightParams=_timeWeightParams)
    return jsonify(graphToDict(_graph, _timeWeightParams))


@app.post("/api/recompute")
def postRecompute():
    """Reconstruit tout le graphe from scratch — nouveaux déséquilibres
    aléatoires de démo, nouveau TimeWeightParams par défaut (voir
    main.buildAndSolveGraph) — exactement ce que fait `python src/main.py`,
    mais in-process : _graph/_dexRegistry/_timeWeightParams sont réassignés
    ici plutôt que lancés dans un sous-process, pour que POST /api/solve
    continue ensuite à re-solver CETTE instance à jour plutôt qu'une copie
    devenue périmée. Le JS recharge la page après cet appel (voir
    recomputeBtn côté frontend) : contrairement à /api/solve, dexNodes/paths
    changent aussi ici (nouvelles balances), un patch DOM en place ne
    suffirait pas."""
    global _graph, _dexRegistry, _timeWeightParams
    _graph, _dexRegistry, _timeWeightParams = buildAndSolveGraph()
    renderGraph(_graph, outputPath=str(GRAPH_PNG_PATH))
    renderGraphHtml(_graph, outputPath=str(GRAPH_HTML_PATH), timeWeightParams=_timeWeightParams)
    writeOperationsText(_graph, outputPath=str(OPERATIONS_TXT_PATH))
    return jsonify(graphToDict(_graph, _timeWeightParams))


def main() -> None:
    app.run(host="127.0.0.1", port=8765, debug=False)


if __name__ == "__main__":
    main()
