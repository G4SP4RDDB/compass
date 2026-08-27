import requests

from graph.structures.DEXes import Chain

from .config import require_thegraph_api_key
from .exceptions import ConnectorAPIError, UnsupportedChainError
from .models import PoolReserves

GRAPH_GATEWAY_URL = "https://gateway.thegraph.com/api/{api_key}/subgraphs/id/{subgraph_id}"

# Uniswap V2 (constant-product x*y=k) : un seul déploiement officiel, sur
# Ethereum mainnet. Pas de subgraph V2 vérifié sur les autres chains pour
# l'instant — à compléter avant d'étendre le swap à d'autres chains.
UNISWAP_V2_SUBGRAPH_ID_BY_CHAIN: dict[Chain, str] = {
    Chain.ETHEREUM: "A3Np3RQbaBA6oKJgiwDJeo5T3zrYfGHPWFYayMwtNDum",
}

_PAIR_QUERY = """
query($token0: String!, $token1: String!) {
  pairs(where: {token0: $token0, token1: $token1}, first: 1) {
    reserve0
    reserve1
  }
}
"""


class UniswapConnector:
    def get_reserves(self, chain: Chain, sell_token: str, buy_token: str) -> PoolReserves:
        subgraphId = UNISWAP_V2_SUBGRAPH_ID_BY_CHAIN.get(chain)
        if subgraphId is None:
            raise UnsupportedChainError("UniswapConnector", chain)

        url = GRAPH_GATEWAY_URL.format(api_key=require_thegraph_api_key(), subgraph_id=subgraphId)

        # Une pair Uniswap V2 trie ses deux tokens par adresse croissante
        # (token0 < token1) ; il faut donc trier avant de requêter, puis
        # réorienter reserve0/reserve1 selon le sens vente->achat demandé.
        sellLower, buyLower = sell_token.lower(), buy_token.lower()
        token0, token1 = sorted((sellLower, buyLower))

        response = requests.post(
            url,
            json={"query": _PAIR_QUERY, "variables": {"token0": token0, "token1": token1}},
            timeout=10,
        )
        if not response.ok:
            raise ConnectorAPIError("UniswapConnector", url, response.text)

        data = response.json()
        pairs = data.get("data", {}).get("pairs") or []
        if not pairs:
            raise ConnectorAPIError(
                "UniswapConnector", url, f"aucune pair trouvée pour {sell_token}/{buy_token}"
            )

        reserve0 = float(pairs[0]["reserve0"])
        reserve1 = float(pairs[0]["reserve1"])
        if sellLower == token0:
            reserveIn, reserveOut = reserve0, reserve1
        else:
            reserveIn, reserveOut = reserve1, reserve0

        return PoolReserves(chain=chain, reserve_in=reserveIn, reserve_out=reserveOut)
