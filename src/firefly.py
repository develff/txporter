"""
txporter - Firefly III API client
Imports transactions into Firefly III via REST API.
"""

import requests
import logging

logger = logging.getLogger(__name__)


class FireflyClient:
    def __init__(self, config: dict):
        self.base_url = config["url"].rstrip("/")
        self.token = config["token"]
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def import_transactions(self, transactions: list, account: dict):
        """Import a list of transactions into Firefly III."""
        for tx in transactions:
            self._create_transaction(tx, account)

    def _create_transaction(self, tx: dict, account: dict):
        """Create a single transaction in Firefly III."""
        # TODO: map transaction fields to Firefly III API format
        payload = {
            "transactions": [{
                "type": "withdrawal",  # or deposit/transfer
                "date": tx.get("date"),
                "amount": tx.get("amount"),
                "description": tx.get("description", ""),
                "source_name": account.get("name"),
            }]
        }
        response = requests.post(
            f"{self.base_url}/api/v1/transactions",
            headers=self.headers,
            json=payload
        )
        if not response.ok:
            logger.error(f"Failed to create transaction: {response.text}")
        else:
            logger.debug(f"Created transaction: {tx.get('description')}")
