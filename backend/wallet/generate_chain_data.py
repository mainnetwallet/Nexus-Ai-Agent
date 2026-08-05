#!/usr/bin/env python3
"""
Regenerates chain_registry_data.py from a local clone of
https://github.com/ethereum-lists/chains.

Usage:
    git clone --depth 1 https://github.com/ethereum-lists/chains.git /tmp/chains-registry
    python3 backend/wallet/generate_chain_data.py /tmp/chains-registry

See README_chain_data.md for how keys are derived.
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path


def _clean_rpcs(raw: list) -> list[str]:
    out = []
    for url in raw:
        if not isinstance(url, str) or not url.startswith("https://"):
            continue
        if "${" in url:
            continue
        out.append(url)
    return out


def _load_entries(registry_dir: str):
    files = glob.glob(f"{registry_dir}/_data/chains/eip155-*.json")
    entries = []
    for f in files:
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        cid = d.get("chainId")
        name = d.get("name")
        short = d.get("shortName")
        if cid is None or not name:
            continue
        name = name.strip()
        short = (short or "").strip()
        nrpc = len(_clean_rpcs(d.get("rpc", [])))
        is_legacy = "(legacy)" in name.lower() or "deprecated" in name.lower()
        entries.append((cid, name, short, nrpc, is_legacy))
    return entries


def _build_name_to_id(entries) -> dict[str, int]:
    candidates: dict[str, list[tuple]] = {}

    def add_candidate(key: str, cid: int, nrpc: int, is_legacy: bool, rank: int) -> None:
        key = key.strip()
        if not key or key.isdigit():
            return
        candidates.setdefault(key, []).append((nrpc > 0, not is_legacy, -rank, cid))

    for cid, name, short, nrpc, is_legacy in entries:
        lname, lshort = name.lower(), short.lower()
        add_candidate(lname, cid, nrpc, is_legacy, rank=0)
        if lshort:
            add_candidate(lshort, cid, nrpc, is_legacy, rank=0)
        if lname.endswith(" mainnet"):
            add_candidate(lname[: -len(" mainnet")], cid, nrpc, is_legacy, rank=1)
        for suffix in (" testnet", " devnet"):
            if lname.endswith(suffix):
                add_candidate(lname[: -len(suffix)], cid, nrpc, is_legacy, rank=2)

    name_to_id: dict[str, int] = {}
    for key, cands in candidates.items():
        best = sorted(cands, key=lambda t: (not t[0], not t[1], -t[2], t[3]))[0]
        name_to_id[key] = best[3]
    return name_to_id


def _write_module(name_to_id: dict[str, int], out_path: str) -> None:
    items = sorted(name_to_id.items())
    lines = [
        '"""',
        "Auto-generated from ethereum-lists/chains (https://github.com/ethereum-lists/chains).",
        "Maps every known EVM chain name / shortName (mainnet + testnet + devnet, ~2675",
        "chains) to its numeric chain id, so chain_resolver.resolve_chain() can look up any",
        "chain a user names in chat -- not just the hand-curated shortlist in",
        "_ALIAS_TO_CHAIN_ID.",
        "",
        "Built from three key derivations per chain (exact name, exact shortName, and",
        'bare name with a trailing "Mainnet"/"Testnet"/"Devnet" stripped, e.g. "Somnia',
        'Mainnet" -> "somnia"). When multiple chains would produce the same key (registry',
        "has some duplicate/legacy chain ids), the entry with a usable https RPC and a",
        'non-"(Legacy)"/deprecated name wins, so e.g. bare "plume" resolves to the live',
        "Plume Mainnet rather than an abandoned legacy chain id with no RPC.",
        "",
        "Regenerate by cloning ethereum-lists/chains and re-running the generation script",
        "(see backend/wallet/README_chain_data.md). Do not hand-edit; add one-off overrides",
        "to _ALIAS_TO_CHAIN_ID in chain_resolver.py instead, which takes priority over this",
        "table when both match.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        f"# {len(items)} entries",
        "CHAIN_NAME_TO_ID: dict[str, int] = {",
    ]
    for k, v in items:
        ks = k.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'    "{ks}": {v},')
    lines.append("}")
    lines.append("")
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    registry_dir = sys.argv[1]
    entries = _load_entries(registry_dir)
    if not entries:
        print(f"No chain files found under {registry_dir}/_data/chains -- wrong path?")
        sys.exit(1)
    name_to_id = _build_name_to_id(entries)
    out_path = str(Path(__file__).parent / "chain_registry_data.py")
    _write_module(name_to_id, out_path)
    print(f"Wrote {len(name_to_id)} aliases from {len(entries)} chains -> {out_path}")


if __name__ == "__main__":
    main()
