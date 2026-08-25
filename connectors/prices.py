import time

import requests

from graph.structures.DEXes import Chain

from .chain_metadata import get_metadata
from .exceptions import ConnectorAPIError

COINGECKO_SIMPLE_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
CACHE_TTL_SECONDS = 30


class CoinGeckoPriceConnector:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, float]] = {}

    def get_usd_price(self, chain: Chain) -> float:
        coingecko_id = get_metadata(chain).coingecko_id

        cached = self._cache.get(coingecko_id)
        if cached is not None:
            price, fetched_at = cached
            if time.monotonic() - fetched_at < CACHE_TTL_SECONDS:
                return price

        response = requests.get(
            COINGECKO_SIMPLE_PRICE_URL,
            params={"ids": coingecko_id, "vs_currencies": "usd"},
            timeout=10,
        )
        if not response.ok:
            raise ConnectorAPIError(
                "CoinGeckoPriceConnector", COINGECKO_SIMPLE_PRICE_URL, response.text
            )

        data = response.json()
        try:
            price = float(data[coingecko_id]["usd"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConnectorAPIError(
                "CoinGeckoPriceConnector",
                COINGECKO_SIMPLE_PRICE_URL,
                f"unexpected response shape for {coingecko_id}: {data}",
            ) from exc

        self._cache[coingecko_id] = (price, time.monotonic())
        return price
