import os

from dotenv import load_dotenv

load_dotenv()

DEFAULT_SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"

SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", DEFAULT_SOLANA_RPC_URL)
ZFUND_USERNAME = os.getenv("ZFUND_USERNAME")
ZFUND_PASSWORD = os.getenv("ZFUND_PASSWORD")
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY")
ALCHEMY_RATE_LIMIT_PER_SECOND = float(os.getenv("ALCHEMY_RATE_LIMIT_PER_SECOND", "25"))


def require_alchemy_api_key() -> str:
    if not ALCHEMY_API_KEY:
        raise RuntimeError(
            "ALCHEMY_API_KEY is not set. Get a free key at https://dashboard.alchemy.com/ "
            "and set it in your .env file (see .env.example). N'oublie pas d'activer chaque "
            "network nécessaire pour ton app dans le dashboard Alchemy (désactivées par défaut "
            "sauf Ethereum)."
        )
    return ALCHEMY_API_KEY


def require_zfund_credentials() -> tuple[str, str]:
    if not ZFUND_USERNAME or not ZFUND_PASSWORD:
        raise RuntimeError(
            "ZFUND_USERNAME / ZFUND_PASSWORD ne sont pas définies. "
            "À mettre dans .env (voir .env.example)."
        )
    return ZFUND_USERNAME, ZFUND_PASSWORD
