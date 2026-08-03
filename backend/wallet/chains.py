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

_BY_CHAIN_ID_INT = {c.chain_id_int: c for c in SUPPORTED_CHAINS.values()}
_BY_CHAIN_ID_HEX = {c.chain_id_hex.lower(): c for c in SUPPORTED_CHAINS.values()}


def chain_from_hex(chain_id_hex: str | None) -> ChainInfo | None:
    if not chain_id_hex:
        return None
    return _BY_CHAIN_ID_HEX.get(chain_id_hex.lower())


def chain_by_key(key: str) -> ChainInfo | None:
    return SUPPORTED_CHAINS.get(key.lower())
