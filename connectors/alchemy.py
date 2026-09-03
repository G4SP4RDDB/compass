import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from graph.structures.DEXes import Chain

from .chain_metadata import get_metadata
from .config import ALCHEMY_RATE_LIMIT_PER_SECOND, require_alchemy_api_key
from .exceptions import ConnectorAPIError, UnsupportedChainError
from .models import GasCost, UniswapQuote

ALCHEMY_URL_TEMPLATE = "https://{network}.g.alchemy.com/v2/{api_key}"

# Session partagée (pas une par AlchemyConnector, voir _TokenBucket ci-dessous
# pour la même remarque) : l'edge Alchemy est mesuré en pratique comme
# franchement instable pour CETTE clé/route — TLS handshake qui ne se termine
# jamais (timeout côté client), ou une réponse 403 au corps vide, ou un 200
# parfaitement valide, en alternance sur des requêtes IDENTIQUES envoyées à la
# suite (voir investigation manuelle : ~40-60% d'échec par tentative selon le
# moment). Rien à voir avec la validité de la clé ou de la requête (un vrai
# refus d'accès renvoie un corps JSON d'erreur explicite, jamais un 403 vide)
# -- donc on retry AUSSI ce 403 vide, contrairement à l'usage habituel où 403
# signale un refus permanent qu'il ne sert à rien de reessayer. Sans retry,
# une seule connexion malchanceuse fait échouer toute la construction du
# graphe (des dizaines d'appels RPC/quotes) même quand Alchemy répond bien la
# moitié du temps. `Retry.connect` couvre les échecs au niveau connexion/TLS
# (avant qu'une réponse HTTP n'existe), backoff exponentiel pour ne pas
# marteler un vrai incident si jamais il y en a un.
_RETRY = Retry(
    total=8,
    connect=8,
    read=4,
    backoff_factor=0.5,
    status_forcelist=(403, 429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET", "POST"}),
)
_SESSION = requests.Session()
_SESSION.mount("https://", HTTPAdapter(max_retries=_RETRY))

# Prices API : endpoint global (pas par network) qui donne le prix USD d'un
# token à partir de son symbole. Remplace CoinGecko (rate limit public trop
# bas pour interroger toutes les chains à chaque build de graphe) par un
# endpoint qui partage la même clé API que le RPC.
# https://docs.alchemy.com/reference/get-token-prices-by-symbol
ALCHEMY_PRICES_URL_TEMPLATE = "https://api.g.alchemy.com/prices/v1/{api_key}/tokens/by-symbol"
PRICE_CACHE_TTL_SECONDS = 30


class _TokenBucket:
    """Bucket à jetons bloquant : ne laisse jamais passer plus de `rate`
    acquire() par seconde, en dormant plutôt qu'en levant une exception une
    fois le bucket vide. État partagé au niveau du module (pas par instance)
    : computeSwapCostBreakpoints (src/graph/costing.py) instancie un nouvel
    AlchemyConnector par edge de swap, un limiteur par-instance ne verrait
    donc pas les appels des instances voisines et sous-compterait le débit
    réel."""

    def __init__(self, rate: float) -> None:
        self._rate = rate
        self._tokens = rate
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self._rate, self._tokens + (now - self._last_refill) * self._rate)
            self._last_refill = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                time.sleep(wait)
                self._tokens = 0
                self._last_refill = time.monotonic()
            else:
                self._tokens -= 1


_ALCHEMY_RATE_LIMITER = _TokenBucket(ALCHEMY_RATE_LIMIT_PER_SECOND)

# Slugs vérifiés en live (réponse d'erreur Alchemy nomme la network qu'elle a
# résolue, ex: "ARB_MAINNET is not enabled..."). Chaque network doit en plus
# être activée manuellement pour la clé sur https://dashboard.alchemy.com/
# (Ethereum seul est activé par défaut sur une nouvelle app).
ALCHEMY_NETWORK_SLUG_BY_CHAIN: dict[Chain, str] = {
    Chain.ETHEREUM: "eth-mainnet",
    Chain.ARBITRUM: "arb-mainnet",
    Chain.OPTIMISM: "opt-mainnet",
    Chain.POLYGON: "polygon-mainnet",
    Chain.BASE: "base-mainnet",
    Chain.BSC: "bnb-mainnet",
    Chain.AVALANCHE: "avax-mainnet",
}

# Adresses QuoterV2 (Uniswap V3) vérifiées via le feed officiel
# https://docs.uniswap.org/deployments.json (protocol="v3", contract="QuoterV2").
# Ethereum/Arbitrum/Optimism/Polygon partagent la même adresse (déploiement
# CREATE2 déterministe), Base/BSC/Avalanche ont la leur.
QUOTER_V2_ADDRESS_BY_CHAIN: dict[Chain, str] = {
    Chain.ETHEREUM: "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
    Chain.ARBITRUM: "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
    Chain.OPTIMISM: "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
    Chain.POLYGON: "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
    Chain.BASE: "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
    Chain.BSC: "0x78D78E420Da98ad378D7799bE8f4AF69033EB077",
    Chain.AVALANCHE: "0xbe0F5544EC67e9B3b2D979aaA43f18Fd87E6257F",
}

# Sélecteur de quoteExactInputSingle((address,address,uint256,uint24,uint160))
# = keccak256(signature)[:4]. Vérifié en live contre Ethereum mainnet (1000
# USDC -> 999.77 USDT sur le pool 0.01%, résultat décodé cohérent) plutôt que
# recalculé à chaque appel — pas besoin d'une dépendance keccak au runtime
# pour une seule constante déjà confirmée correcte.
_QUOTE_EXACT_INPUT_SINGLE_SELECTOR = "c6a5026a"

DEFAULT_STABLE_POOL_FEE = 100  # 0.01%, tier standard des pools stable/stable


class AlchemyConnector:
    def __init__(self) -> None:
        self._price_cache: dict[str, tuple[float, float]] = {}

    def _network_url(self, chain: Chain) -> str:
        slug = ALCHEMY_NETWORK_SLUG_BY_CHAIN.get(chain)
        if slug is None:
            raise UnsupportedChainError("AlchemyConnector", chain)
        return ALCHEMY_URL_TEMPLATE.format(network=slug, api_key=require_alchemy_api_key())

    def _rpc(self, chain: Chain, method: str, params: list) -> str:
        url = self._network_url(chain)
        _ALCHEMY_RATE_LIMITER.acquire()
        response = _SESSION.post(
            url, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=10
        )
        if not response.ok:
            raise ConnectorAPIError("AlchemyConnector", url, response.text)

        data = response.json()
        if "error" in data:
            raise ConnectorAPIError("AlchemyConnector", url, str(data["error"]))
        return data["result"]

    def get_gas_price_gwei(self, chain: Chain) -> float:
        result = self._rpc(chain, "eth_gasPrice", [])
        return int(result, 16) / 1e9

    def get_block_number(self, chain: Chain) -> int:
        result = self._rpc(chain, "eth_blockNumber", [])
        return int(result, 16)

    def get_block_timestamp(self, chain: Chain, block_number: int) -> int:
        result = self._rpc(chain, "eth_getBlockByNumber", [hex(block_number), False])
        if result is None:
            raise ConnectorAPIError("AlchemyConnector", self._network_url(chain), f"block {block_number} not found")
        return int(result["timestamp"], 16)

    def get_block_time_seconds(self, chain: Chain, block_lookback: int = 100) -> float:
        """Temps de bloc moyen mesuré en live sur les `block_lookback` derniers
        blocks (timestamp du block courant moins timestamp du block
        block_lookback plus tôt, divisé par le nombre de blocks) — même
        logique que SolanaRPCConnector.get_slot_time_ms pour Solana."""
        latest_number = self.get_block_number(chain)
        older_number = max(latest_number - block_lookback, 0)
        blocks_elapsed = latest_number - older_number
        if blocks_elapsed == 0:
            raise ConnectorAPIError(
                "AlchemyConnector", self._network_url(chain), "not enough block history to measure block time"
            )

        latest_timestamp = self.get_block_timestamp(chain, latest_number)
        older_timestamp = self.get_block_timestamp(chain, older_number)
        return (latest_timestamp - older_timestamp) / blocks_elapsed

    def get_usd_price(self, chain: Chain) -> float:
        symbol = get_metadata(chain).native_symbol

        cached = self._price_cache.get(symbol)
        if cached is not None:
            price, fetched_at = cached
            if time.monotonic() - fetched_at < PRICE_CACHE_TTL_SECONDS:
                return price

        url = ALCHEMY_PRICES_URL_TEMPLATE.format(api_key=require_alchemy_api_key())
        _ALCHEMY_RATE_LIMITER.acquire()
        response = _SESSION.get(url, params={"symbols": symbol}, timeout=10)
        if not response.ok:
            raise ConnectorAPIError("AlchemyConnector (Prices)", url, response.text)

        data = response.json()
        try:
            entry = next(item for item in data["data"] if item["symbol"] == symbol)
            if entry.get("error"):
                raise ConnectorAPIError("AlchemyConnector (Prices)", url, str(entry["error"]))
            price = float(next(p["value"] for p in entry["prices"] if p["currency"] == "usd"))
        except (KeyError, TypeError, StopIteration, ValueError) as exc:
            raise ConnectorAPIError(
                "AlchemyConnector (Prices)", url, f"unexpected response shape for {symbol}: {data}"
            ) from exc

        self._price_cache[symbol] = (price, time.monotonic())
        return price

    def estimate_gas(self, chain: Chain, call_object: dict, state_override: dict | None = None) -> int:
        params: list = [call_object, "latest"]
        if state_override is not None:
            params.append(state_override)
        result = self._rpc(chain, "eth_estimateGas", params)
        return int(result, 16)

    def get_gas_cost(self, chain: Chain, gas_limit: int) -> GasCost:
        gas_price_gwei = self.get_gas_price_gwei(chain)
        native_amount = gas_price_gwei * gas_limit * 1e-9

        metadata = get_metadata(chain)
        try:
            usd_price = self.get_usd_price(chain)
            usd_amount = native_amount * usd_price
        except ConnectorAPIError:
            usd_amount = None

        return GasCost(
            chain=chain,
            native_amount=native_amount,
            native_symbol=metadata.native_symbol,
            usd_amount=usd_amount,
        )

    def get_quote(
        self,
        chain: Chain,
        token_in: str,
        token_out: str,
        amount_in: int,
        fee: int = DEFAULT_STABLE_POOL_FEE,
    ) -> UniswapQuote:
        quoterAddress = QUOTER_V2_ADDRESS_BY_CHAIN.get(chain)
        if quoterAddress is None:
            raise UnsupportedChainError("AlchemyConnector (QuoterV2)", chain)

        calldata = "0x" + _QUOTE_EXACT_INPUT_SINGLE_SELECTOR + _encodeQuoteParams(token_in, token_out, amount_in, fee)
        result = self._rpc(chain, "eth_call", [{"to": quoterAddress, "data": calldata}, "latest"])
        buyAmount, gasEstimate = _decodeQuoteResult(result)

        return UniswapQuote(chain=chain, sell_token=token_in, buy_token=token_out, buy_amount=buyAmount, gas_estimate=gasEstimate)


def _encodeQuoteParams(tokenIn: str, tokenOut: str, amountIn: int, fee: int) -> str:
    # Tous les champs de QuoteExactInputSingleParams sont de taille fixe
    # (address, uint256, uint24, uint160) : simple concaténation, pas
    # d'offset/encodage dynamique nécessaire pour ce tuple unique.
    return _padAddress(tokenIn) + _padAddress(tokenOut) + _padUint(amountIn) + _padUint(fee) + _padUint(0)


def _padAddress(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def _padUint(value: int) -> str:
    return format(value, "x").rjust(64, "0")


def _decodeQuoteResult(hexResult: str) -> tuple[int, int]:
    # returns (uint256 amountOut, uint160 sqrtPriceX96After, uint32
    # initializedTicksCrossed, uint256 gasEstimate) : 4 mots de 32 bytes.
    data = hexResult.removeprefix("0x")
    amountOut = int(data[0:64], 16)
    gasEstimate = int(data[192:256], 16)
    return amountOut, gasEstimate
