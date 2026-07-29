import logging
from ingestion import blockscout_client as bs

logger = logging.getLogger(__name__)

def detect_sybil_networks(wallets: list[str], max_pages_per_wallet: int = 3) -> dict[str, list[str]]:
    """
    Cluster wallets by their initial gas funding source.
    Returns a dict mapping parent_address -> list of child_wallets funded by it.
    """
    funding_map = {}
    
    logger.info("Starting Sybil detection for %d candidate wallets...", len(wallets))
    
    for w in wallets:
        w_clean = w.strip().lower()
        try:
            tx = bs.get_oldest_funding_transaction(w_clean, max_pages=max_pages_per_wallet)
            if tx:
                from_info = tx.get("from") or {}
                parent = from_info.get("hash", "").lower().strip()
                is_contract = from_info.get("is_contract", False)
                
                # Ignore smart contracts, pools, and empty values
                if parent and not is_contract:
                    funding_map.setdefault(parent, []).append(w_clean)
        except Exception as e:
            logger.warning("Failed to trace gas funder for wallet %s: %s", w, e)
            
    # Filter clusters to only include those with 2 or more wallets
    sybil_clusters = {parent: children for parent, children in funding_map.items() if len(children) >= 2}
    
    if sybil_clusters:
        logger.info("Detected %d Sybil cluster(s):", len(sybil_clusters))
        for parent, children in sybil_clusters.items():
            logger.info("  Parent %s funded child wallets: %s", parent, ", ".join(children))
            
    return sybil_clusters
