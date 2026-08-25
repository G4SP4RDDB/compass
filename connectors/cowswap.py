import time

import requests

from src.DEXes import Chain

from .chain_metadata import get_metadata
from .exceptions import ConnectorAPIError, UnsupportedChainError
from .models import SwapQuote

COWSWAP_API_BASE_URL = "https://api.cow.fi"

# Any address works as a placeholder "from"/"receiver" for a quote-only request;
# CoW's quote endpoint doesn't execute anything, it just prices the trade.
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class CowSwapConnector:
    def _slug(self, chain: Chain) -> str:
        slug = get_metadata(chain).cowswap_slug
        if slug is None:
            raise UnsupportedChainError("CowSwapConnector", chain)
        return slug

    def get_quote(
        self,
        chain: Chain,
        sell_token: str,
        buy_token: str,
        sell_amount: int,
        from_address: str = ZERO_ADDRESS,
    ) -> SwapQuote:
        slug = self._slug(chain)
        url = f"{COWSWAP_API_BASE_URL}/{slug}/api/v1/quote"

        body = {
            "sellToken": sell_token,
            "buyToken": buy_token,
            "receiver": from_address,
            "from": from_address,
            "sellAmountBeforeFee": str(sell_amount),
            "kind": "sell",
            "validTo": int(time.time()) + 1800,
        }

        response = requests.post(url, json=body, timeout=10)
        if not response.ok:
            raise ConnectorAPIError("CowSwapConnector", url, response.text)

        data = response.json()
        quote = data["quote"]

        return SwapQuote(
            chain=chain,
            sell_token=sell_token,
            buy_token=buy_token,
            buy_amount=int(quote["buyAmount"]),
            fee_amount=int(quote["feeAmount"]),
            valid_to=int(quote["validTo"]),
        )
