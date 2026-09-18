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
Le bouton "Run solver" (POST /api/recompute) fait, lui, l'inverse :
reconstruit tout depuis zéro à partir des déséquilibres SAISIS À LA MAIN
(GET/POST /api/imbalances <-> connectors/dex_imbalances.json, édités depuis
le panel "Details" d'un DEX — voir connectors.dex_imbalances), exactement
ce que fait `python src/main.py`. Plus aucun tirage aléatoire.

GET/POST /api/wallet-sources : par (chain, stable), le solveur peut-il
puiser dans le solde réel de l'operating wallet (et jusqu'à combien) --
connectors/wallet_sources.json, panel Details d'un nœud Wallet. Le solde
lui-même est lu on-chain au build (main.buildAndSolveGraph).

GET /api/execution-status dit au frontend si un bouton "Execute" (exécution
LIVE d'un hop choisi par le solveur, montant = flot du solveur) peut
fonctionner sur ce serveur (COMPASS_TEST_ALLOW_LIVE, wallet, caps) — la
route d'exécution elle-même reste POST /api/test-hop ci-dessous.

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
"Details" d'un DEX, À CÔTÉ du déséquilibre saisi à la main, comme repère
pour ne pas déclarer un surplus supérieur à ce que le compte détient ;
il ne remplit jamais le formulaire tout seul.

POST /api/test-hop EST la route qui peut réellement exécuter un hop
(bouton "Test This Edge" / "Run LIVE" du frontend, voir graph_template.html)
— dry-run par défaut, live seulement avec COMPASS_TEST_ALLOW_LIVE=1 côté
serveur ET un `confirm: "YES"` dans le corps de la requête. Voir cette route
plus bas pour le détail des trois gardes-fous, et
compass_test/README.md "Safety model".

Lancement : python -m visualization.server
"""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

from compass_test import balances as test_balances
from compass_test import config as test_config
from compass_test import executor as test_executor
from compass_test import metrics_db
from compass_test import reporter as test_reporter
from compass_test.hop_runner import HopValidationError, run_single_hop
from compass_test.models import HopType
from compass_test.wallet import OperatingWallet
from connectors.dex_imbalances import (
    InfeasibleImbalancesError,
    load_dex_imbalances,
    save_dex_imbalances,
    summarize_dex_imbalances,
    validate_dex_imbalances,
)
from connectors.dex_measured_delays import load_measured_delays
from connectors.wallet_sources import load_wallet_sources, save_wallet_sources, validate_wallet_sources
from connectors.dex_operational_params import (
    CONFIG_FIELDS,
    load_dex_operational_params,
    save_dex_operational_params,
)
from graph.structures.DEXes import Chain
from graph.solver import graphSolve
from graph.urgency import TimeWeightParams
from main import buildAndSolveGraph
from visualization.dex_branding import DEX_BRANDING
from visualization.graph_view import renderGraph
from visualization.web_view import graphToDict, renderGraphHtml, writeOperationsText

ROOT = Path(__file__).resolve().parent.parent
GRAPH_HTML_PATH = ROOT / "graph.html"
GRAPH_PNG_PATH = ROOT / "graph.png"
OPERATIONS_TXT_PATH = ROOT / "operations.txt"

app = Flask(__name__)

# Construit et résout le graphe une seule fois au démarrage du process, à
# partir de connectors/dex_imbalances.json (voir main.buildAndSolveGraph) ;
# reconstruit sur POST /api/recompute. Des déséquilibres infaisables sur
# disque au démarrage (déficits > surplus) ne doivent pas empêcher le
# serveur de se lancer -- sinon l'utilisateur ne pourrait plus les corriger
# depuis la page : on construit alors le graphe SANS déséquilibre (plan
# vide) et on garde le message pour le frontend (GET /api/imbalances).
_startupImbalanceProblem: str | None = None
try:
    _graph, _dexRegistry, _timeWeightParams = buildAndSolveGraph()
except InfeasibleImbalancesError as exc:
    _startupImbalanceProblem = str(exc)
    _graph, _dexRegistry, _timeWeightParams = buildAndSolveGraph(imbalancesPath=None)
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


@app.get("/api/measured-delays")
def getMeasuredDelays():
    """Lecture seule de connectors/dex_measured_delays.json (voir
    connectors.dex_measured_delays et compass_test/calibration.py) : le panel
    Config l'affiche à côté des délais configurés. Relu à chaque appel, pas
    de cache -- un run live lancé depuis "Test This Edge" le reconstruit
    (reporter.save_report), et la page doit pouvoir montrer la nouvelle
    moyenne sans redémarrer le serveur. Le GRAPHE en mémoire, lui, ne
    change qu'au prochain build (POST /api/recompute ou redémarrage), comme
    pour la config éditée via POST /api/config."""
    return jsonify(load_measured_delays())


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


@app.get("/api/imbalances")
def getImbalances():
    """Déséquilibres saisis à la main (connectors/dex_imbalances.json) +
    leur résumé (totaux surplus/déficit, faisabilité, message) pour le
    panel "Details" et la barre d'outils du frontend."""
    imbalances = load_dex_imbalances()
    return jsonify(
        {
            "imbalances": imbalances,
            "summary": summarize_dex_imbalances(imbalances).to_dict(),
            "startupProblem": _startupImbalanceProblem,
        }
    )


@app.post("/api/imbalances")
def postImbalances():
    """Remplace le fichier entier par {dexName: entry} (voir
    connectors.dex_imbalances pour le format ; null / kind null = DEX
    équilibré). Validé contre le registre courant. Ne reconstruit PAS le
    graphe : c'est POST /api/recompute ("Run solver") qui le fait, une
    fois tous les DEX saisis."""
    try:
        cleaned = validate_dex_imbalances(request.get_json(force=True, silent=False), _dexRegistry)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        return jsonify({"error": "request body must be valid JSON"}), 400
    save_dex_imbalances(cleaned)
    return jsonify({"imbalances": cleaned, "summary": summarize_dex_imbalances(cleaned).to_dict()})


@app.get("/api/wallet-sources")
def getWalletSources():
    """Réglages par (chain, stable) de ce que le solveur peut prendre dans
    l'operating wallet (connectors/wallet_sources.json, voir
    connectors.wallet_sources) -- édités depuis le panel Details d'un nœud
    Wallet. Le graphe en mémoire ne les relit qu'au prochain "Run solver"."""
    return jsonify(load_wallet_sources())


@app.post("/api/wallet-sources")
def postWalletSources():
    try:
        cleaned = validate_wallet_sources(request.get_json(force=True, silent=False))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        return jsonify({"error": "request body must be valid JSON"}), 400
    save_wallet_sources(cleaned)
    return jsonify(cleaned)


@app.get("/api/execution-status")
def getExecutionStatus():
    """Ce que le bouton "Execute" d'un hop choisi par le solveur a besoin
    de savoir AVANT de proposer une exécution live : le serveur autorise-t-il
    le live (COMPASS_TEST_ALLOW_LIVE=1 + wallet configuré, même test que
    POST /api/test-hop), quelle adresse va recevoir/signer, et les caps $
    que executor.run_hop appliquera de toute façon."""
    try:
        walletAddress = OperatingWallet(known_address=test_config.OPERATING_WALLET_ADDRESS).address
    except Exception:
        walletAddress = None
    return jsonify(
        {
            "liveAllowed": test_config.ALLOW_LIVE and walletAddress is not None,
            "walletAddress": walletAddress,
            "maxUsdPerHop": test_config.MAX_USD_PER_HOP,
            "maxUsdPerRun": test_config.MAX_USD_PER_RUN,
        }
    )


@app.post("/api/recompute")
def postRecompute():
    """Reconstruit tout le graphe from scratch à partir des déséquilibres
    saisis à la main (connectors/dex_imbalances.json, voir POST
    /api/imbalances), nouveau TimeWeightParams par défaut (voir
    main.buildAndSolveGraph) — exactement ce que fait `python src/main.py`,
    mais in-process : _graph/_dexRegistry/_timeWeightParams sont réassignés
    ici plutôt que lancés dans un sous-process, pour que POST /api/solve
    continue ensuite à re-solver CETTE instance à jour plutôt qu'une copie
    devenue périmée. Le JS recharge la page après cet appel (voir
    runSolverBtn côté frontend) : contrairement à /api/solve, dexNodes/paths
    changent aussi ici (nouveaux déséquilibres), un patch DOM en place ne
    suffirait pas. Déséquilibres infaisables -> 400 avec le message, le
    graphe en mémoire reste l'ancien."""
    global _graph, _dexRegistry, _timeWeightParams, _startupImbalanceProblem
    try:
        _graph, _dexRegistry, _timeWeightParams = buildAndSolveGraph()
    except InfeasibleImbalancesError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:  # solveur sans solution malgré la pré-vérification
        return jsonify({"error": f"solver failed: {exc}"}), 400
    _startupImbalanceProblem = None
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


def _metricsQueryArgs() -> tuple[int, bool]:
    days = request.args.get("days", default=30, type=int)
    live_only = request.args.get("live", default="false") == "true"
    return days, live_only


@app.get("/metrics")
def metricsDashboard():
    """Serves the standalone metrics dashboard (Chart.js via CDN, unlike
    graph.html which stays dependency-free) — see visualization/web/
    metrics_dashboard.html. It fetches its data from the /api/metrics/*
    routes below, all backed by TimescaleDB (see compass_test/metrics_db.py),
    which is additive to the JSON reports on disk (source of truth)."""
    return send_from_directory(ROOT / "visualization" / "web", "metrics_dashboard.html")


@app.get("/api/dex-branding")
def getDexBranding():
    """{dexName: brandColor} only (no logos — those are large base64 blobs
    only graph_template.html needs) so the dashboard's per-DEX charts use
    the same colors as the graph view's nodes (see dex_branding.py)."""
    return jsonify({name: branding["color"] for name, branding in DEX_BRANDING.items()})


@app.get("/api/metrics/summary")
def getMetricsSummary():
    days, live_only = _metricsQueryArgs()
    try:
        return jsonify(metrics_db.summary(days=days, live_only=live_only))
    except Exception as exc:  # noqa: BLE001 - DB down/unreachable must not crash the server
        return jsonify({"error": f"metrics DB unavailable: {exc}"}), 503


@app.get("/api/metrics/daily-counts")
def getMetricsDailyCounts():
    days, live_only = _metricsQueryArgs()
    try:
        return jsonify(metrics_db.daily_counts(days=days, live_only=live_only))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"metrics DB unavailable: {exc}"}), 503


@app.get("/api/metrics/cost-over-time")
def getMetricsCostOverTime():
    days, live_only = _metricsQueryArgs()
    try:
        return jsonify(metrics_db.cost_over_time(days=days, live_only=live_only))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"metrics DB unavailable: {exc}"}), 503


@app.get("/api/metrics/delay-over-time")
def getMetricsDelayOverTime():
    days, live_only = _metricsQueryArgs()
    try:
        return jsonify(metrics_db.delay_over_time(days=days, live_only=live_only))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"metrics DB unavailable: {exc}"}), 503


@app.get("/api/metrics/by-dex")
def getMetricsByDex():
    days, live_only = _metricsQueryArgs()
    try:
        return jsonify(metrics_db.by_dex(days=days, live_only=live_only))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"metrics DB unavailable: {exc}"}), 503


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
    upfront) when its Details panel opens, shown ALONGSIDE the hand-set
    imbalance form (connectors.dex_imbalances) as a reference for how much
    surplus the account can actually back — it never fills the form itself."""
    result = test_balances.get_real_balance(dex_name)
    return jsonify({"dex": result.dex, "balances": result.balances, "error": result.error})


@app.post("/api/test-hop")
def postTestHop():
    """Backs the graph UI's "Test This Edge" / "Run LIVE" buttons — the
    ONLY HTTP path that can trigger compass_test execution. A dry run (the
    default — `live` omitted or false) never moves funds and needs no
    confirmation: just a gas/quote estimate for a Deposit, a real CoW quote
    for a Swap, a stub for a Withdraw (see compass_test/executor.py — the
    exchange, not us, controls withdraw timing/fees, so a dry run can't
    simulate one).

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
    # Swap only: the stable BOUGHT (`stable` is the one sold). The `dex` of
    # a Swap hop is the venue name (CoW Swap) — see web_view._testableHopInfo.
    toStable = payload.get("toStable")
    live = bool(payload.get("live", False))

    if not all(isinstance(v, str) and v for v in (dex, hopTypeStr, chain, stable)):
        return jsonify({"ok": False, "error": "dex, hopType, chain, stable are required strings"}), 400
    if hopTypeStr not in ("Withdraw", "Deposit", "Swap"):
        return jsonify({"ok": False, "error": f'hopType must be "Withdraw", "Deposit" or "Swap", got {hopTypeStr!r}'}), 400
    if hopTypeStr == "Swap" and not (isinstance(toStable, str) and toStable):
        return jsonify({"ok": False, "error": "a Swap hop needs toStable (the stable bought)"}), 400
    if live and payload.get("confirm") != "YES":
        return jsonify({"ok": False, "error": 'live run requires confirm: "YES" in the request body'}), 400

    amount = payload.get("amount")
    try:
        amountUsd = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "amount must be a number"}), 400

    hopType = HopType(hopTypeStr)

    if live:
        # Streamed instead of the single buffered JSON response below: a
        # live run can take minutes (see executor.POLL_TIMEOUT_SECONDS) and
        # genuinely passes through distinct stages — e.g. "the deposit tx
        # confirmed on-chain" vs "now waiting on the DEX's own side" — that
        # a single before/after response can't convey. The frontend's
        # execution animation (runLiveHop in graph_template.html) reads
        # this as newline-delimited JSON: zero or more {"type":"stage",...}
        # events as executor.run_hop's on_stage callback fires, followed by
        # exactly one {"type":"done"|"error",...} terminal event. Because
        # the HTTP status is committed the moment streaming starts (200,
        # before we know if the hop will fail), errors that used to be a
        # 400/403/500 status are instead carried in that terminal event's
        # own "status" field — the frontend checks that, not res.ok.
        return Response(stream_with_context(_streamLiveHop(dex, hopType, chain, stable, amountUsd, toStable)), mimetype="application/x-ndjson")

    try:
        result = run_single_hop(dex, hopType, chain, stable, amount_usd=amountUsd, live=False, to_stable_name=toStable)
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

    return jsonify({"ok": True, **_testHopResultPayload(result, live=False)})


def _testHopResultPayload(result, live: bool) -> dict:
    """The response shape both the dry-run (buffered) and live (streamed
    "done" event) paths send back — same fields either way so the
    frontend's renderHopTestResult doesn't care which one produced them."""
    hopComparison = result.comparison
    try:
        walletAddress = OperatingWallet(known_address=test_config.OPERATING_WALLET_ADDRESS).address
    except Exception:
        walletAddress = None  # no key/address configured at all — live can't work regardless, shown as liveAllowed below

    return {
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


def _streamLiveHop(dex: str, hopType: HopType, chain: str, stable: str, amountUsd: float | None, toStable: str | None):
    """Generator backing the streamed branch of postTestHop above: runs
    run_single_hop(live=True) on a background thread (it blocks on
    time.sleep-based polling for minutes, see executor._poll_until) and
    yields each on_stage(...) call plus the terminal outcome as they
    arrive, instead of buffering everything until the hop is done."""
    events: queue.Queue = queue.Queue()

    def on_stage(stage: str, message: str, domain: str) -> None:
        events.put({"type": "stage", "stage": stage, "message": message, "domain": domain})

    outcome: dict[str, Any] = {}

    def worker() -> None:
        try:
            result = run_single_hop(
                dex, hopType, chain, stable, amount_usd=amountUsd, live=True, to_stable_name=toStable, on_stage=on_stage
            )
            outcome["event"] = {"type": "done", "ok": True, **_testHopResultPayload(result, live=True)}
        except HopValidationError as exc:
            outcome["event"] = {"type": "error", "ok": False, "status": 400, "error": str(exc)}
        except test_executor.SafetyCapError as exc:
            outcome["event"] = {"type": "error", "ok": False, "status": 403, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - see the matching comment on the dry-run path above
            outcome["event"] = {"type": "error", "ok": False, "status": 500, "error": str(exc)}
        finally:
            events.put(None)  # sentinel: no more stage events, the terminal one is in `outcome`

    threading.Thread(target=worker, daemon=True).start()

    while True:
        event = events.get()
        if event is None:
            break
        yield json.dumps(event) + "\n"
    yield json.dumps(outcome["event"]) + "\n"


def main() -> None:
    # threaded=True: a live test-hop request can block for minutes (polling
    # for a withdrawal/deposit to actually land, see executor.py's
    # POLL_TIMEOUT_SECONDS) — single-threaded would freeze every other tab
    # (k-slider, Test Results, wallet balances) for the same duration.
    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True)


if __name__ == "__main__":
    main()
