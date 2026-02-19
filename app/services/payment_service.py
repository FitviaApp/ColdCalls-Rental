"""
Payment Service - USDT verification via Etherscan API
"""
from decimal import Decimal
from typing import Optional

import httpx

from app.config import get_settings

settings = get_settings()


class PaymentService:
    """Service for verifying USDT payments via Etherscan"""

    def __init__(self):
        self.api_key = settings.ETHERSCAN_API_KEY
        self.wallet_address = settings.USDT_WALLET_ADDRESS.lower() if settings.USDT_WALLET_ADDRESS else ""
        self.usdt_contract = settings.USDT_CONTRACT.lower()
        self.base_url = "https://api.etherscan.io/api"

    async def verify_usdt_transaction(self, tx_hash: str) -> dict:
        """
        Verify a USDT transaction on Ethereum

        Args:
            tx_hash: Transaction hash (0x...)

        Returns:
            dict: {valid: bool, amount: float, error: str}
        """
        if not self.api_key or not self.wallet_address:
            return {
                'valid': False,
                'amount': 0,
                'error': 'Payment verification not configured'
            }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Get transaction receipt
                response = await client.get(
                    self.base_url,
                    params={
                        'module': 'proxy',
                        'action': 'eth_getTransactionReceipt',
                        'txhash': tx_hash,
                        'apikey': self.api_key
                    }
                )

                data = response.json()

                if data.get('error') or not data.get('result'):
                    return {
                        'valid': False,
                        'amount': 0,
                        'error': 'Transaction not found or not yet confirmed'
                    }

                receipt = data['result']

                # Check if transaction was successful
                if receipt.get('status') != '0x1':
                    return {
                        'valid': False,
                        'amount': 0,
                        'error': 'Transaction failed or reverted'
                    }

                # Check confirmations
                confirmations = await self._get_confirmations(client, receipt.get('blockNumber'))
                if confirmations < 6:
                    return {
                        'valid': False,
                        'amount': 0,
                        'error': f'Insufficient confirmations ({confirmations}/6). Please wait a few more minutes.'
                    }

                # Parse logs for USDT transfer
                for log in receipt.get('logs', []):
                    # Check if this is USDT contract
                    if log.get('address', '').lower() != self.usdt_contract:
                        continue

                    # Transfer event topic (keccak256 of "Transfer(address,address,uint256)")
                    transfer_topic = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
                    if log.get('topics', [None])[0] != transfer_topic:
                        continue

                    # Check recipient (topic[2] is 'to' address, padded to 32 bytes)
                    if len(log.get('topics', [])) < 3:
                        continue

                    recipient = '0x' + log['topics'][2][-40:]
                    if recipient.lower() != self.wallet_address:
                        continue

                    # Parse amount (USDT has 6 decimals)
                    raw_amount = int(log['data'], 16)
                    amount = float(Decimal(raw_amount) / Decimal(10**6))

                    return {
                        'valid': True,
                        'amount': amount,
                        'error': None
                    }

                return {
                    'valid': False,
                    'amount': 0,
                    'error': 'No USDT transfer to our wallet found in this transaction'
                }

        except httpx.TimeoutException:
            return {
                'valid': False,
                'amount': 0,
                'error': 'Etherscan API timeout. Please try again.'
            }
        except Exception as e:
            return {
                'valid': False,
                'amount': 0,
                'error': f'Verification error: {str(e)}'
            }

    async def _get_confirmations(self, client: httpx.AsyncClient, block_number_hex: str) -> int:
        """Get number of confirmations for a block"""
        if not block_number_hex:
            return 0

        try:
            # Get current block number
            response = await client.get(
                self.base_url,
                params={
                    'module': 'proxy',
                    'action': 'eth_blockNumber',
                    'apikey': self.api_key
                }
            )

            data = response.json()
            current_block = int(data['result'], 16)
            tx_block = int(block_number_hex, 16)

            return current_block - tx_block
        except Exception:
            return 0

    def calculate_credits(self, usdt_amount: float) -> float:
        """
        Convert USDT to credits

        Args:
            usdt_amount: Amount in USDT

        Returns:
            Credits to add (with markup)
        """
        return usdt_amount * settings.USDT_TO_CREDITS_RATE


# Singleton instance
payment_service = PaymentService()
