import requests

from src.DEXes import Chain

from .chain_metadata import get_metadata
from .config import require_etherscan_api_key
from .exceptions import ConnectorAPIError, UnsupportedChainError
from .models import GasCost
from .prices import CoinGeckoPriceConnector

ETHERSCAN_V2_BASE_URL = "https://api.etherscan.io/v2/api"


class EtherscanConnector:
    def __init__(self, price_connector: CoinGeckoPriceConnector | None = None) -> None:
        self._prices = price_connector or CoinGeckoPriceConnector()

    def _chainid(self, chain: Chain) -> int:
        chainid = get_metadata(chain).etherscan_chainid
        if chainid is None:
            raise UnsupportedChainError("EtherscanConnector", chain)
        return chainid

    def _get(self, chain: Chain, params: dict) -> dict:
        query = {**params, "chainid": self._chainid(chain), "apikey": require_etherscan_api_key()}
        response = requests.get(ETHERSCAN_V2_BASE_URL, params=query, timeout=10)
        if not response.ok:
            raise ConnectorAPIError("EtherscanConnector", ETHERSCAN_V2_BASE_URL, response.text)

        data = response.json()
        if data.get("status") == "0" and data.get("message") != "NOTOK":
            # "NOTOK" without a real error (e.g. empty result) is treated per-caller;
            # anything else with status "0" is a real API-level error.
            raise ConnectorAPIError(
                "EtherscanConnector", ETHERSCAN_V2_BASE_URL, str(data.get("result", data))
            )
        return data

    def get_gas_price_gwei(self, chain: Chain) -> float:
        try:
            data = self._get(chain, {"module": "gastracker", "action": "gasoracle"})
            return float(data["result"]["ProposeGasPrice"])
        except (ConnectorAPIError, KeyError, TypeError, ValueError):
            data = self._get(chain, {"module": "proxy", "action": "eth_gasPrice"})
            wei = int(data["result"], 16)
            return wei / 1e9

    def get_gas_cost(self, chain: Chain, gas_limit: int) -> GasCost:
        gas_price_gwei = self.get_gas_price_gwei(chain)
        native_amount = gas_price_gwei * gas_limit * 1e-9

        metadata = get_metadata(chain)
        try:
            usd_price = self._prices.get_usd_price(chain)
            usd_amount = native_amount * usd_price
        except ConnectorAPIError:
            usd_amount = None

        return GasCost(
            chain=chain,
            native_amount=native_amount,
            native_symbol=metadata.native_symbol,
            usd_amount=usd_amount,
        )
