# Regenerating `chain_registry_data.py`

`CHAIN_NAME_TO_ID` in `chain_registry_data.py` is generated from the
[ethereum-lists/chains](https://github.com/ethereum-lists/chains) registry
(the same dataset behind chainlist.org and MetaMask's "add network" search).
It covers every EVM chain in that registry -- mainnet, testnet, and devnet.

## Regenerate

```bash
git clone --depth 1 https://github.com/ethereum-lists/chains.git /tmp/chains-registry
python3 backend/wallet/generate_chain_data.py /tmp/chains-registry
```

## How keys are derived

For each chain in the registry (`_data/chains/eip155-<id>.json`), up to four
candidate keys are generated:

1. exact `name`, lowercased (e.g. `"monad testnet"`)
2. exact `shortName`, lowercased (e.g. `"mon-testnet"`)
3. if `name` ends in `" Mainnet"`, the bare stripped form (e.g. `"Somnia
   Mainnet"` -> `"somnia"`) -- bare name always means the mainnet
4. if `name` ends in `" Testnet"`/`" Devnet"`, the bare stripped form, but
   only to fill a gap left by rule 3 (e.g. `"OP Sepolia Testnet"` ->
   `"op sepolia"`)

When two chains would produce the same key (the registry has some
duplicate/legacy chain ids), the entry with a usable `https://` RPC and a
name that isn't `"(Legacy)"`/deprecated wins.

## One-off overrides

Don't hand-edit `chain_registry_data.py`. For a specific alias that needs to
differ from the generated table (e.g. `"sepolia"` alone, since the official
registry name is `"Ethereum Sepolia"` and the stripping rules above don't
produce a bare `"sepolia"` key), add it to `_ALIAS_TO_CHAIN_ID` in
`chain_resolver.py`, which is always checked first.
