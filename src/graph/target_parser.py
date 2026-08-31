from connectors.zfund import ZFundConnector
from graph.structures.DEXes import DEX

# Le nom de compte côté zfund ne correspond pas toujours au nom du DEX dans le
# registre (src/graph/structures/dex_registry.py) : mapping explicite plutôt
# que de deviner une normalisation de string.
ACCOUNT_NAME_TO_DEX_NAME: dict[str, str] = {
    "aster": "Aster",
    "dydx": "dYdX",
    "extended": "Extended",
    "gateperp": "Gate (Perp DEX)",
    "hyperliquid": "Hyperliquid",
    "lighter": "Lighter",
    "mexcspot": "MEXC",
    "ondo": "Ondo Perps",
}


def fetchAndApplyTargets(dexRegistry: dict[str, DEX], zfundConnector: ZFundConnector | None = None) -> list[str]:
    connector = zfundConnector or ZFundConnector()
    ensemble = connector.get_live_ensemble()
    return applyTargets(ensemble, dexRegistry)


def applyTargets(ensemble: dict, dexRegistry: dict[str, DEX]) -> list[str]:
    """Assigne target/inbalance à chaque DEX du registre à partir d'un payload
    /margin/live.ensemble (modifie les DEX en place). Retourne les avertissements
    de correspondance (compte zfund sans DEX, ou DEX sans compte dans la réponse)
    plutôt que de les avaler silencieusement.

    inbalance suit la convention du reste du code : positif = surplus
    (collateral au-dessus de la cible, à faire sortir), négatif = déficit (à
    faire rentrer) — c'est l'opposé du champ "transfer" de zfund
    (transfer = target_collateral - collateral).
    """
    accounts = ensemble.get("accounts", {})
    matchedDexNames: set[str] = set()
    warnings: list[str] = []

    for accountName, account in accounts.items():
        dexName = ACCOUNT_NAME_TO_DEX_NAME.get(accountName)
        dex = dexRegistry.get(dexName) if dexName else None
        if dex is None:
            warnings.append(f"compte zfund '{accountName}' sans DEX correspondant dans le registre")
            continue

        dex.target = account["target_collateral"]
        dex.inbalance = account["collateral"] - account["target_collateral"]
        matchedDexNames.add(dexName)

    for dexName in dexRegistry:
        if dexName not in matchedDexNames:
            warnings.append(f"DEX '{dexName}' présent dans le registre mais absent de la réponse zfund")

    return warnings
