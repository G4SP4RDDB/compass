import requests

from .config import require_zfund_credentials
from .exceptions import ConnectorAPIError

ZFUND_BASE_URL = "https://simulator.zfund.io"
LIVE_ENSEMBLE_ENDPOINT = "/margin/live.ensemble"


class ZFundConnector:
    def get_live_ensemble(self) -> dict:
        url = f"{ZFUND_BASE_URL}{LIVE_ENSEMBLE_ENDPOINT}"
        response = requests.get(
            url,
            headers={"accept": "application/json"},
            auth=require_zfund_credentials(),
            timeout=10,
        )
        if not response.ok:
            raise ConnectorAPIError("ZFundConnector", url, response.text)
        return response.json()
