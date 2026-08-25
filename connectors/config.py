import os

from dotenv import load_dotenv

load_dotenv()

DEFAULT_SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"

ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", DEFAULT_SOLANA_RPC_URL)


def require_etherscan_api_key() -> str:
    if not ETHERSCAN_API_KEY:
        raise RuntimeError(
            "ETHERSCAN_API_KEY is not set. Get a free key at https://etherscan.io/apis "
            "and set it in your .env file (see .env.example)."
        )
    return ETHERSCAN_API_KEY
