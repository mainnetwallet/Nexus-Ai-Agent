from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChainInfo:
    key: str
    display_name: str
    chain_id_hex: str
    chain_id_int: int


SUPPORTED_CHAINS: dict[str, ChainInfo] = {
    "ethereum": ChainInfo("ethereum", "Ethereum Mainnet", "0x1", 1),
    "polygon": ChainInfo("polygon", "Polygon", "0x89", 137),
    "arbitrum": ChainInfo("arbitrum", "Arbitrum One", "0xa4b1", 42161),
    "optimism": ChainInfo("optimism", "OP Mainnet", "0xa", 10),
    "base": ChainInfo("base", "Base", "0x2105", 8453),
    "bsc": ChainInfo("bsc", "BNB Smart Chain", "0x38", 56),
}

# Common alternate phrasings for the 6 hardcoded chains above. The intent
# classifier is told to normalize send_chain to one of the canonical keys
# (e.g. "optimism"), but LLMs don't always follow that constraint exactly
# -- a message saying "OP Mainnet" can come back as literal "op mainnet",
# which chain_by_key() would otherwise fail to recognize, sending it down
# the dynamic-registry path with zero RPC candidates. Matched case-
# insensitively in chain_by_key() below as a fallback, never as the
# primary lookup.
_CHAIN_ALIASES: dict[str, str] = {
    "op mainnet": "optimism", "op": "optimism", "optimism mainnet": "optimism",
    "eth mainnet": "ethereum", "eth": "ethereum", "ethereum mainnet": "ethereum",
    "mainnet": "ethereum",
    "pol": "polygon", "pol mainnet": "polygon", "matic": "polygon", "polygon mainnet": "polygon", "polygon pos": "polygon",
    "arb": "arbitrum", "arbitrum one": "arbitrum", "arbitrum mainnet": "arbitrum",
    "base mainnet": "base",
    "bnb": "bsc", "bnb chain": "bsc", "binance smart chain": "bsc", "bsc mainnet": "bsc",
}

_BY_CHAIN_ID_INT = {c.chain_id_int: c for c in SUPPORTED_CHAINS.values()}
_BY_CHAIN_ID_HEX = {c.chain_id_hex.lower(): c for c in SUPPORTED_CHAINS.values()}

# Chains discovered at runtime via chain_resolver.resolve_chain() get cached
# here (process-lifetime only, never persisted) so a chain the user mentions
# once doesn't have to be re-resolved on every message.
_DYNAMIC_CHAINS: dict[str, ChainInfo] = {}
_DYNAMIC_BY_CHAIN_ID_INT: dict[int, ChainInfo] = {}


def register_dynamic_chain(info: ChainInfo) -> None:
    """Cache a chain resolved at runtime (see wallet/chain_resolver.py)."""
    _DYNAMIC_CHAINS[info.key.lower()] = info
    _DYNAMIC_BY_CHAIN_ID_INT[info.chain_id_int] = info


def chain_from_hex(chain_id_hex: str | None) -> ChainInfo | None:
    if not chain_id_hex:
        return None
    hex_lc = chain_id_hex.lower()
    found = _BY_CHAIN_ID_HEX.get(hex_lc)
    if found:
        return found
    try:
        as_int = int(hex_lc, 16)
    except ValueError:
        return None
    return _DYNAMIC_BY_CHAIN_ID_INT.get(as_int)


def chain_by_key(key: str) -> ChainInfo | None:
    key_lc = key.lower()
    direct = SUPPORTED_CHAINS.get(key_lc) or _DYNAMIC_CHAINS.get(key_lc)
    if direct:
        return direct
    aliased = _CHAIN_ALIASES.get(key_lc)
    if aliased:
        return SUPPORTED_CHAINS.get(aliased)
    return None


def chain_by_id(chain_id_int: int) -> ChainInfo | None:
    return _BY_CHAIN_ID_INT.get(chain_id_int) or _DYNAMIC_BY_CHAIN_ID_INT.get(chain_id_int)


def all_known_chains() -> dict[str, ChainInfo]:
    """Static + dynamically-resolved chains, static takes precedence."""
    merged = dict(_DYNAMIC_CHAINS)
    merged.update(SUPPORTED_CHAINS)
    return merged
