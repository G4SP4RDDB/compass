from graph.structures.DEXes import Chain

from .exceptions import UnsupportedChainError

# Domain IDs Circle (https://developers.circle.com/cctp/evm-smart-contracts),
# vérifiés en live le 2026-09-01. CCTP ne couvre pas toutes les chains de
# l'enum Chain (en particulier BSC n'est pas supporté).
CCTP_DOMAIN_BY_CHAIN: dict[Chain, int] = {
    Chain.ETHEREUM: 0,
    Chain.AVALANCHE: 1,
    Chain.OPTIMISM: 2,
    Chain.ARBITRUM: 3,
    Chain.BASE: 6,
    Chain.POLYGON: 7,
}

# Adresses TokenMessenger CCTP V1 (pas V2 : signature depositForBurn plus
# simple, pas de fee/finality param). Vérifiées une par une contre le label
# "Circle: Token Messenger" sur chaque block explorer (Etherscan, Arbiscan,
# Basescan, Snowtrace, Polygonscan, Optimistic Etherscan) le 2026-09-01 —
# contrairement à QuoterV2, ces adresses ne sont PAS partagées entre chains.
TOKEN_MESSENGER_ADDRESS_BY_CHAIN: dict[Chain, str] = {
    Chain.ETHEREUM: "0xBd3fa81B58Ba92a82136038B25aDec7066af3155",
    Chain.AVALANCHE: "0x6B25532e1060CE10cc3B0A99e5683b91BFDe6982",
    Chain.OPTIMISM: "0x2B4069517957735bE00ceE0fadAE88a26365528f",
    Chain.ARBITRUM: "0x19330d10D9Cc8751218eaf51E8885D058642E08A",
    Chain.BASE: "0x1682Ae6375C4E4A97e4B583BC394c861A46D8962",
    Chain.POLYGON: "0x9daf8c91aefae50b9c0e69629d3f6ca40ca3b3fe",
}

# Selector de depositForBurn(uint256,uint32,bytes32,address) = keccak256(signature)[:4].
# Constante calculée une fois hors-ligne plutôt qu'une dépendance keccak au
# runtime — même logique que _QUOTE_EXACT_INPUT_SINGLE_SELECTOR dans alchemy.py.
_DEPOSIT_FOR_BURN_SELECTOR = "6fd3504e"

# --- Mock pour l'estimation de gas (eth_estimateGas + stateOverride) ---
#
# depositForBurn fait un transferFrom(caller, TokenMessenger, amount) sur
# l'USDC : sans solde ni allowance réels, l'estimation reverterait. On mocke
# les deux via un stateOverride qui écrase directement le storage du contrat
# USDC pour une adresse bidon (MOCK_HOLDER_ADDRESS), plutôt que d'utiliser un
# vrai wallet financé.
#
# Slots de storage (mapping balances/allowed de FiatTokenV2) vérifiés en live
# le 2026-09-01 sur Ethereum, Arbitrum et Base (identiques sur les trois :
# balances=9, allowed=10 — cohérent avec un même bytecode d'implémentation
# déployé par Circle). Pas re-vérifiés indépendamment sur Avalanche/Optimism/
# Polygon : à confirmer si get_stable_token_address y gagne une entrée un jour.
MOCK_HOLDER_ADDRESS = "0x000000000000000000000000000000000000dEaD"
_USDC_BALANCE_SLOT = 9
_USDC_ALLOWANCE_SLOT = 10

# keccak256(pad32(MOCK_HOLDER_ADDRESS) . pad32(_USDC_BALANCE_SLOT)) : la clé de
# storage de balances[MOCK_HOLDER_ADDRESS], précalculée (voir slots ci-dessus).
# Un seul holder mocké pour toutes les chains => une seule clé, indépendante
# du TokenMessenger (donc de la chain).
_MOCK_BALANCE_STORAGE_KEY = "0x960b1051749987b45b5679007fff577a1c2f763ec21c15a6c5eb193075003785"

# keccak256(pad32(TokenMessenger) . keccak256(pad32(MOCK_HOLDER_ADDRESS) . pad32(_USDC_ALLOWANCE_SLOT)))
# par chain : la clé de storage de allowed[MOCK_HOLDER_ADDRESS][TokenMessenger]
# dépend du spender, donc varie par chain (TokenMessenger a une adresse
# différente sur chacune). Précalculées, mêmes slots que ci-dessus.
_MOCK_ALLOWANCE_STORAGE_KEY_BY_CHAIN: dict[Chain, str] = {
    Chain.ETHEREUM: "0xde4e4a484f156487feef0916f47b557a4371d9475f4d5df79631d6d72abaa3ac",
    Chain.AVALANCHE: "0x56f1d2ba5f27f5e44c190c3e969bccdd6db7469969c7b0c35fb8eecc0e987ff1",
    Chain.OPTIMISM: "0xdde274034518ec0c6099096250de9ac1f526c7a80d74878815f4c005ab6970d8",
    Chain.ARBITRUM: "0x97d365280dd638e169f0f8ef6b1a17ea363e0419480aea486de21242dbc940d4",
    Chain.BASE: "0xf49e2fd9aa9d11049779d94d151e31f33c88c430b4f76a1e8a159f93d3824767",
    Chain.POLYGON: "0x7b9f65a555cc8a7a12d8955818fa968d794c1736620f194b950ca58c30fba7f7",
}

_MOCK_BALANCE_OVERRIDE = format(10**18, "064x")  # largement assez pour n'importe quel montant testé
_MOCK_ALLOWANCE_OVERRIDE = format(2**256 - 1, "064x")

# Le gas de depositForBurn ne dépend pas du montant (pas de branchement sur sa
# valeur) : un montant nominal suffit pour l'estimation.
MOCK_DEPOSIT_AMOUNT = 1_000_000  # 1 USDC (6 décimales)


# Délais approximatifs (secondes) — contrairement aux coûts de gas, pas
# mesurables par simulation RPC : tirés de la doc Circle sur la finalité par
# chain (developers.circle.com/cctp/concepts/finality-and-block-confirmations
# + recoupement avec plusieurs sources tierces), 2026-09-01. À traiter comme
# des ordres de grandeur, pas des valeurs vérifiées en live comme les gas.
#
# V1 (Standard Transfer) attend la finalité de la chain SOURCE avant que la
# destination puisse minter : les L2 optimistic (Arbitrum/Optimism/Base)
# héritent du délai de finalité Ethereum (~13-19 min, on prend le milieu de
# la fourchette) car leur état n'est "safe" qu'une fois posté et finalisé sur
# L1 ; Avalanche et Polygon ont leur propre finalité native.
CCTP_V1_DELAY_SECONDS_BY_CHAIN: dict[Chain, float] = {
    Chain.ETHEREUM: 16 * 60,
    Chain.AVALANCHE: 60,
    Chain.OPTIMISM: 16 * 60,
    Chain.ARBITRUM: 16 * 60,
    Chain.BASE: 16 * 60,
    Chain.POLYGON: 20 * 60,
}


def is_v1_supported(chain: Chain) -> bool:
    return chain in TOKEN_MESSENGER_ADDRESS_BY_CHAIN


def _pad_address(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def _pad_uint(value: int) -> str:
    return format(value, "x").rjust(64, "0")


def encode_deposit_for_burn(amount: int, destination_domain: int, mint_recipient: str, burn_token: str) -> str:
    """calldata de depositForBurn(uint256,uint32,bytes32,address). mint_recipient
    est une address EVM encodée en bytes32 (left-pad), comme l'exige CCTP."""
    return (
        "0x"
        + _DEPOSIT_FOR_BURN_SELECTOR
        + _pad_uint(amount)
        + _pad_uint(destination_domain)
        + _pad_address(mint_recipient)
        + _pad_address(burn_token)
    )


def build_deposit_for_burn_estimate_call(
    source_chain: Chain, destination_chain: Chain, burn_token_address: str
) -> tuple[dict, dict]:
    """Construit (callObject, stateOverride) pour un eth_estimateGas simulant
    depositForBurn de source_chain vers destination_chain, avec solde/allowance
    USDC mockés sur MOCK_HOLDER_ADDRESS (voir commentaire plus haut)."""
    token_messenger = TOKEN_MESSENGER_ADDRESS_BY_CHAIN.get(source_chain)
    destination_domain = CCTP_DOMAIN_BY_CHAIN.get(destination_chain)
    if token_messenger is None or destination_domain is None:
        raise UnsupportedChainError("cctp", source_chain if token_messenger is None else destination_chain)

    calldata = encode_deposit_for_burn(
        MOCK_DEPOSIT_AMOUNT, destination_domain, MOCK_HOLDER_ADDRESS, burn_token_address
    )
    call_object = {"from": MOCK_HOLDER_ADDRESS, "to": token_messenger, "data": calldata}

    allowance_key = _MOCK_ALLOWANCE_STORAGE_KEY_BY_CHAIN[source_chain]
    state_override = {
        burn_token_address: {
            "stateDiff": {
                _MOCK_BALANCE_STORAGE_KEY: "0x" + _MOCK_BALANCE_OVERRIDE,
                allowance_key: "0x" + _MOCK_ALLOWANCE_OVERRIDE,
            }
        }
    }
    return call_object, state_override


# --- CCTP V2 ---
#
# TokenMessengerV2 (contrairement à V1) est déployé à la MÊME adresse sur
# toutes les chains (déploiement déterministe) : vérifié le 2026-09-01 via
# eth_getCode sur les 6 chains ci-dessus (même longueur de bytecode partout).
# Les domain IDs sont partagés avec V1 (registre Circle, indépendant de la
# version du protocole) : voir CCTP_DOMAIN_BY_CHAIN.
TOKEN_MESSENGER_V2_ADDRESS = "0x28b5a0e9C621a5BadaA536219b3a228C8168cf5d"

# Selector de
# depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)
# (source: TokenMessengerV2.sol, evm-cctp-contracts) — calculé hors-ligne,
# même logique que _DEPOSIT_FOR_BURN_SELECTOR.
_DEPOSIT_FOR_BURN_V2_SELECTOR = "8e0250ee"

# On modélise le Fast Transfer (la fonctionnalité différenciante de V2 :
# règlement en secondes plutôt que les ~13-19 min de V1/Standard). minFinalityThreshold
# <= 1000 => Fast ; maxFee est le fee (en unités burnToken) que l'utilisateur
# accepte de payer au mint, prélevé côté destination — n'affecte pas le gas de
# la tx de burn elle-même (vérifié : gas quasi identique entre maxFee=0/thr=2000
# et maxFee=1/thr=1000 en simulation), donc une valeur nominale suffit ici.
# destinationCaller=0 => n'importe qui peut relayer receiveMessage.
V2_FAST_TRANSFER_MIN_FINALITY_THRESHOLD = 1000
_MOCK_MAX_FEE = 1
_MOCK_DESTINATION_CALLER = "0x0000000000000000000000000000000000000000"

# keccak256(pad32(TOKEN_MESSENGER_V2_ADDRESS) . keccak256(pad32(MOCK_HOLDER_ADDRESS) . pad32(_USDC_ALLOWANCE_SLOT)))
# TokenMessengerV2 ayant une adresse unique partagée par toutes les chains,
# une seule clé suffit (contrairement à V1).
_MOCK_ALLOWANCE_STORAGE_KEY_V2 = "0x500a0efb5c6dc422dd0539bfb33c4a56b310624a9ec43f30571c85b93f25a8e5"

# Fast Transfer (le point différenciant de V2) : attestation "soft finality"
# de Circle en quelques secondes, quasi indépendante de la chain source
# (contrairement à V1) — pas de table par chain, une seule constante suffit.
CCTP_V2_FAST_TRANSFER_DELAY_SECONDS = 15.0


def is_v2_supported(chain: Chain) -> bool:
    return chain in CCTP_DOMAIN_BY_CHAIN


def encode_deposit_for_burn_v2(
    amount: int,
    destination_domain: int,
    mint_recipient: str,
    burn_token: str,
    destination_caller: str = _MOCK_DESTINATION_CALLER,
    max_fee: int = _MOCK_MAX_FEE,
    min_finality_threshold: int = V2_FAST_TRANSFER_MIN_FINALITY_THRESHOLD,
) -> str:
    """calldata de TokenMessengerV2.depositForBurn (Fast Transfer par défaut).
    mint_recipient/destination_caller sont des address EVM encodées en
    bytes32 (left-pad), comme l'exige CCTP."""
    return (
        "0x"
        + _DEPOSIT_FOR_BURN_V2_SELECTOR
        + _pad_uint(amount)
        + _pad_uint(destination_domain)
        + _pad_address(mint_recipient)
        + _pad_address(burn_token)
        + _pad_address(destination_caller)
        + _pad_uint(max_fee)
        + _pad_uint(min_finality_threshold)
    )


def build_deposit_for_burn_v2_estimate_call(
    source_chain: Chain, destination_chain: Chain, burn_token_address: str
) -> tuple[dict, dict]:
    """Équivalent V2 de build_deposit_for_burn_estimate_call : eth_estimateGas
    sur TokenMessengerV2.depositForBurn (Fast Transfer), même mock de
    solde/allowance USDC."""
    destination_domain = CCTP_DOMAIN_BY_CHAIN.get(destination_chain)
    if not is_v2_supported(source_chain) or destination_domain is None:
        raise UnsupportedChainError(
            "cctp_v2", source_chain if not is_v2_supported(source_chain) else destination_chain
        )

    calldata = encode_deposit_for_burn_v2(
        MOCK_DEPOSIT_AMOUNT, destination_domain, MOCK_HOLDER_ADDRESS, burn_token_address
    )
    call_object = {"from": MOCK_HOLDER_ADDRESS, "to": TOKEN_MESSENGER_V2_ADDRESS, "data": calldata}

    state_override = {
        burn_token_address: {
            "stateDiff": {
                _MOCK_BALANCE_STORAGE_KEY: "0x" + _MOCK_BALANCE_OVERRIDE,
                _MOCK_ALLOWANCE_STORAGE_KEY_V2: "0x" + _MOCK_ALLOWANCE_OVERRIDE,
            }
        }
    }
    return call_object, state_override
