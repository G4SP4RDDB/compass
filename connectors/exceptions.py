class ConnectorError(Exception):
    """Base class for all connectors/ errors."""


class UnsupportedChainError(ConnectorError):
    def __init__(self, connector_name: str, chain) -> None:
        super().__init__(f"{connector_name} does not support chain {chain}")
        self.connector_name = connector_name
        self.chain = chain


class ConnectorAPIError(ConnectorError):
    def __init__(self, connector_name: str, endpoint: str, detail: str) -> None:
        super().__init__(f"{connector_name} request to {endpoint} failed: {detail}")
        self.connector_name = connector_name
        self.endpoint = endpoint
        self.detail = detail
