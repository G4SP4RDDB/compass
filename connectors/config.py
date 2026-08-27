import os

from dotenv import load_dotenv

load_dotenv()

DEFAULT_SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"

ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", DEFAULT_SOLANA_RPC_URL)
THEGRAPH_API_KEY = os.getenv("THEGRAPH_API_KEY")


def require_etherscan_api_key() -> str:
    if not ETHERSCAN_API_KEY:
        raise RuntimeError(
            "ETHERSCAN_API_KEY is not set. Get a free key at https://etherscan.io/apis "
            "and set it in your .env file (see .env.example)."
        )
    return ETHERSCAN_API_KEY


def require_thegraph_api_key() -> str:
    if not THEGRAPH_API_KEY:
        raise RuntimeError(
            "THEGRAPH_API_KEY is not set. Le hosted service The Graph est mort "
            "(redirige vers error.thegraph.com) ; il faut une clé du gateway payant, "
            "voir https://thegraph.com/studio/apikeys/, et la mettre dans .env."
        )
    return THEGRAPH_API_KEY
