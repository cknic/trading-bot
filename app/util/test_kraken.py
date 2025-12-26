import os
import yaml

from exchange.kraken_client import KrakenClient
from exchange.kraken_orders import resolve_pair_info

def load_yaml(path):
    with open(path, "r") as f:
        return yaml.safe_load(f)

def main():
    cfg = load_yaml("/config/kraken.yaml")
    kcfg = cfg["kraken"]
    configured_pairs = kcfg.get("pairs") or [kcfg["pair"]]

    k = KrakenClient(
        api_key=os.environ["KRAKEN_API_KEY"],
        api_secret=os.environ["KRAKEN_API_SECRET"],
        base_url=kcfg["base_url"],
    )

    for p in configured_pairs:
        pk, info = resolve_pair_info(k, p)
        print(f"Pair resolved as: {pk}")
        subset = {kk: info[kk] for kk in ("ordermin", "pair_decimals", "lot_decimals", "cost_decimals") if kk in info}
        print("Pair info (subset):", subset)

    bal = k.private("Balance", {})
    if bal.get("error"):
        raise RuntimeError(bal["error"])
    currencies = list((bal.get("result") or {}).keys())
    print("Balance ok; currencies:", currencies)

if __name__ == "__main__":
    main()