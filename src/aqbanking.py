"""
txporter - AqBanking CLI wrapper
Fetches transactions from financial accounts using AqBanking.
"""

import subprocess
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class AqBankingClient:
    def __init__(self, account: dict):
        self.account = account

    def fetch_transactions(self, days: int = 30) -> list:
        """Fetch transactions for the last N days."""
        account_type = self.account.get("type", "fints")
        if account_type == "fints":
            return self._fetch_fints(days)
        elif account_type == "paypal":
            return self._fetch_paypal(days)
        else:
            raise ValueError(f"Unsupported account type: {account_type}")

    def _fetch_fints(self, days: int) -> list:
        """Fetch transactions via FinTS using aqbanking-cli."""
        from_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        account_id = self.account["id"]

        cmd = [
            "aqbanking-cli", "request",
            "--account", account_id,
            "--fromdate", from_date,
            "--transactions"
        ]

        logger.info(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError(f"aqbanking-cli failed: {result.stderr}")

        return self._parse_output(result.stdout)

    def _fetch_paypal(self, days: int) -> list:
        """Fetch transactions via AqBanking PayPal backend."""
        # TODO: implement PayPal fetch
        raise NotImplementedError("PayPal backend not yet implemented")

    def _parse_output(self, output: str) -> list:
        """Parse aqbanking-cli CSV output into transaction dicts."""
        # TODO: implement proper CSV parsing based on aqbanking-cli output format
        transactions = []
        for line in output.strip().split("\n"):
            if not line or line.startswith("#"):
                continue
            # Placeholder — actual parsing depends on aqbanking-cli output format
            transactions.append({"raw": line})
        return transactions
