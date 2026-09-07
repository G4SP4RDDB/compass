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

L'onglet "Test Results" est servi en LECTURE SEULE par GET /api/test-runs
(liste) et GET /api/test-runs/<runId> (détail) : ces deux routes ne font que
lire les rapports JSON déjà écrits par `compass_test` (voir
compass_test/reporter.py) sur disque.

GET /api/wallet-balances lit, en direct à chaque appel (pas de cache), le
solde on-chain réel USDC/USDT de l'opérating wallet sur Arbitrum/BSC (voir
compass_test/balances.py::list_wallet_balances) — affiché dans le panneau
"Wallet" du frontend.

GET /api/dex-balances/<dexName> lit, en direct (pas de cache), l'équity
réelle d'UN DEX (voir compass_test/balances.py) — affiché dans le panneau
"Details" d'un DEX, À CÔTÉ de son solde/target mock généré par la démo
(main._generateMockImbalances), jamais à la place : les deux restent
visuellement distincts côté frontend.

POST /api/test-hop EST la route qui peut réellement exécuter un hop
(bouton "Test This Edge" / "Run LIVE" du frontend, voir graph_template.html)
— dry-run par défaut, live seulement avec COMPASS_TEST_ALLOW_LIVE=1 côté
serveur ET un `confirm: "YES"` dans le corps de la requête. Voir cette route
plus bas pour le détail des trois gardes-fous, et
compass_test/README.md "Safety model".

Lancement : python -m visualization.server
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from compass_test import balances as test_balances
from compass_test import config as test_config
from compass_test import executor as test_executor
from compass_test import reporter as test_reporter
from compass_test.hop_runner import HopValidationError, run_single_hop
from compass_test.models import HopType
from compass_test.wallet import OperatingWallet
from connectors.dex_operational_params import (
    CONFIG_FIELDS,
    load_dex_operational_params,
    save_dex_operational_params,
)
from graph.structures.DEXes import Chain
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
# déséquilibres de démo sont en partie mock, en partie ancrés sur de vrais
# soldes DEX, voir main._generateMockImbalances — re-générer à chaque requête
# donnerait un graphe différent sous les pieds
# de l'utilisateur à chaque déplacement du curseur k). `_state` est mutable
# et réassigné par postSolve ci-dessous à chaque nouveau k.
_graph, _dexRegistry, _timeWeightParams = buildAndSolveGraph()
renderGraphHtml(_graph, outputPath=str(GRAPH_HTML_PATH), timeWeightParams=_timeWeightParams)


_VALID_CHAIN_NAMES = {c.name for c in Chain}


def _validateConfig(payload: Any) -> dict[str, dict[str, dict[str, float]]]:
    """Valide {dexName: {chainName: {field: value}}} -- chainName doit être un
    membre de Chain (voir graph.structures.DEXes), field un des CONFIG_FIELDS
    connus, value un nombre. N'importe quel DEX/chain name est accepté même
    s'il n'existe pas dans le registre actuel (apply_dex_operational_params
    ignore silencieusement les entrées qui ne correspondent à aucun DEX/chain
    du graphe en cours) : ça permet de garder au chaud la config d'un DEX/
    chain temporairement retiré du registre plutôt que de la perdre au
    premier save."""
    if not isinstance(payload, dict):
        raise ValueError("config must be a JSON object keyed by DEX name")
    cleaned: dict[str, dict[str, dict[str, float]]] = {}
    for dexName, chainValues in payload.items():
        if not isinstance(dexName, str) or not isinstance(chainValues, dict):
            raise ValueError(f"invalid entry for {dexName!r}")
        cleanedChains: dict[str, dict[str, float]] = {}
        for chainName, values in chainValues.items():
            if chainName not in _VALID_CHAIN_NAMES or not isinstance(values, dict):
                raise ValueError(f"invalid chain {chainName!r} for {dexName!r}")
            cleanedValues: dict[str, float] = {}
            for field in CONFIG_FIELDS:
                if field not in values:
                    continue
                value = values[field]
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValueError(f"{dexName}.{chainName}.{field} must be a number")
                cleanedValues[field] = float(value)
            cleanedChains[chainName] = cleanedValues
        cleaned[dexName] = cleanedChains
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
    # seed=None: a genuinely fresh draw. buildAndSolveGraph()'s own default
    # (DEMO_IMBALANCE_SEED) is fixed, so calling it bare here reproduced the
    # EXACT SAME mock split every time — this endpoint's whole point (per its
    # docstring and the frontend button's title) is "new" imbalances, which
    # the fixed default silently defeated.
    _graph, _dexRegistry, _timeWeightParams = buildAndSolveGraph(seed=None)
    renderGraph(_graph, outputPath=str(GRAPH_PNG_PATH))
    renderGraphHtml(_graph, outputPath=str(GRAPH_HTML_PATH), timeWeightParams=_timeWeightParams)
    writeOperationsText(_graph, outputPath=str(OPERATIONS_TXT_PATH))
    return jsonify(graphToDict(_graph, _timeWeightParams))


@app.get("/api/test-runs")
def getTestRuns():
    """Lightweight listing for the run-picker dropdown — runId/createdAt/live
    only, not the full journeys (see GET /api/test-runs/<runId> for that),
    newest first. Read-only: reads whatever compass_test/reports/*.json
    already exist on disk, never runs anything (see module docstring)."""
    reports = test_reporter.list_reports()
    return jsonify(
        [{"runId": r.runId, "createdAt": r.createdAt, "live": r.live} for r in reports]
    )


@app.get("/api/test-runs/<run_id>")
def getTestRun(run_id: str):
    try:
        report = test_reporter.load_report(run_id)
    except FileNotFoundError:
        return jsonify({"error": f"no such run {run_id!r}"}), 404
    return jsonify(report.to_dict())


@app.get("/api/wallet-balances")
def getWalletBalances():
    """Real on-chain USDC/USDT balances of the operating wallet (see
    compass_test/wallet.py), Arbitrum + BSC only for now — a live
    balanceOf() read per (chain, stable), never cached, never a demo/mock
    value (see compass_test/balances.py::list_wallet_balances). Read-only,
    same spirit as /api/test-runs above."""
    results = test_balances.list_wallet_balances()
    return jsonify(
        [
            {"chain": r.chain, "stable": r.stable, "balanceUsd": r.balanceUsd, "error": r.error}
            for r in results
        ]
    )


@app.get("/api/dex-balances/<dex_name>")
def getDexBalance(dex_name: str):
    """Real, live equity for ONE DEX, broken down per stablecoin (see
    compass_test/balances.py — direct call to that DEX's own API, no
    zfund/sentinel, never cached). Fetched on-demand per DEX (not all 8
    upfront) when its Details panel opens, shown ALONGSIDE the demo graph's
    mock-generated target/withdrawable figures (main._generateMockImbalances)
    — the two are deliberately kept visually distinct in the frontend, since
    only this one is real."""
    result = test_balances.get_real_balance(dex_name)
    return jsonify({"dex": result.dex, "balances": result.balances, "error": result.error})


@app.post("/api/test-hop")
def postTestHop():
    """Backs the graph UI's "Test This Edge" / "Run LIVE" buttons — the
    ONLY HTTP path that can trigger compass_test execution. A dry run (the
    default — `live` omitted or false) never moves funds and needs no
    confirmation: just a gas/quote estimate for a Deposit, a stub for a
    Withdraw (see compass_test/executor.py — the exchange, not us, controls
    withdraw timing/fees, so a dry run can't simulate one).

    A LIVE run needs THREE independent things, defense in depth:
      1. COMPASS_TEST_ALLOW_LIVE=1 in THIS SERVER's environment — checked
         inside executor.run_hop, the exact same gate the CLI's `--live`
         uses, not re-implemented or loosened here.
      2. The request body's `confirm` field must be the literal string
         "YES" — checked below, BEFORE compass_test is touched at all. The
         frontend only ever sends this after its own typed-confirmation UI;
         curl-ing this endpoint directly still needs to know and send it.
      3. executor.run_hop's own $ caps (MAX_USD_PER_HOP/MAX_USD_PER_RUN),
         enforced regardless of the above two.
    See compass_test/README.md "Safety model".
    """
    payload = request.get_json(force=True, silent=True) or {}
    dex = payload.get("dex")
    hopTypeStr = payload.get("hopType")
    chain = payload.get("chain")
    stable = payload.get("stable")
    live = bool(payload.get("live", False))

    if not all(isinstance(v, str) and v for v in (dex, hopTypeStr, chain, stable)):
        return jsonify({"ok": False, "error": "dex, hopType, chain, stable are required strings"}), 400
    if hopTypeStr not in ("Withdraw", "Deposit"):
        return jsonify({"ok": False, "error": f'hopType must be "Withdraw" or "Deposit", got {hopTypeStr!r}'}), 400
    if live and payload.get("confirm") != "YES":
        return jsonify({"ok": False, "error": 'live run requires confirm: "YES" in the request body'}), 400

    amount = payload.get("amount")
    try:
        amountUsd = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "amount must be a number"}), 400

    hopType = HopType.WITHDRAW if hopTypeStr == "Withdraw" else HopType.DEPOSIT
    try:
        result = run_single_hop(dex, hopType, chain, stable, amount_usd=amountUsd, live=live)
    except HopValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except test_executor.SafetyCapError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 403
    except Exception as exc:  # noqa: BLE001 - a connector/API surprise must
        # still come back as JSON, not Flask's HTML 500 page: an unhandled
        # exception here used to reach the frontend's `res.json()` as
        # "Unexpected token '<'" (the HTML page's `<!doctype...`), hiding
        # the actual error (e.g. a DEX API response shape this connector
        # didn't expect) behind a useless parse error.
        return jsonify({"ok": False, "error": str(exc)}), 500

    hopComparison = result.comparison
    try:
        walletAddress = OperatingWallet(known_address=test_config.OPERATING_WALLET_ADDRESS).address
    except Exception:
        walletAddress = None  # no key/address configured at all — live can't work regardless, shown as liveAllowed below

    return jsonify(
        {
            "ok": True,
            "live": live,
            "resolvedAmountUsd": result.resolvedAmountUsd,
            "usedConfiguredMinimum": result.usedConfiguredMinimum,
            "liveAllowed": test_config.ALLOW_LIVE and walletAddress is not None,
            "walletAddress": walletAddress,
            "planned": hopComparison.planned.to_dict(),
            "executed": hopComparison.executed.to_dict(),
            "costErrorPct": hopComparison.costErrorPct,
            "timeErrorPct": hopComparison.timeErrorPct,
            "reportId": result.report.runId,
        }
    )


def main() -> None:
    # threaded=True: a live test-hop request can block for minutes (polling
    # for a withdrawal/deposit to actually land, see executor.py's
    # POLL_TIMEOUT_SECONDS) — single-threaded would freeze every other tab
    # (k-slider, Test Results, wallet balances) for the same duration.
    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True)


if __name__ == "__main__":
    main()
