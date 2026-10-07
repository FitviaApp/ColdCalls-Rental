import type { Env } from '../middleware';

const BASE_URL = 'https://api.etherscan.io/api';
const TRANSFER_TOPIC = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef';

export interface VerifyUsdtResult {
  valid: boolean;
  amount: number;
  error: string | null;
}

interface EtherscanReceipt {
  status?: string;
  blockNumber?: string;
  logs?: Array<{ address?: string; topics?: string[]; data?: string }>;
}

async function getConfirmations(apiKey: string, blockNumberHex: string | undefined): Promise<number> {
  if (!blockNumberHex) return 0;
  try {
    const response = await fetch(
      `${BASE_URL}?module=proxy&action=eth_blockNumber&apikey=${encodeURIComponent(apiKey)}`,
      { signal: AbortSignal.timeout(30000) },
    );
    const data = (await response.json()) as { result?: string };
    if (!data.result) return 0;
    return parseInt(data.result, 16) - parseInt(blockNumberHex, 16);
  } catch {
    return 0;
  }
}

export const paymentService = {
  async verify_usdt_transaction(env: Env, tx_hash: string): Promise<VerifyUsdtResult> {
    const apiKey = env.ETHERSCAN_API_KEY;
    const walletAddress = env.USDT_WALLET_ADDRESS ? env.USDT_WALLET_ADDRESS.toLowerCase() : '';
    const usdtContract = (env.USDT_CONTRACT ?? '').toLowerCase();

    if (!apiKey || !walletAddress) {
      return { valid: false, amount: 0, error: 'Payment verification not configured' };
    }

    try {
      const response = await fetch(
        `${BASE_URL}?module=proxy&action=eth_getTransactionReceipt&txhash=${encodeURIComponent(tx_hash)}&apikey=${encodeURIComponent(apiKey)}`,
        { signal: AbortSignal.timeout(30000) },
      );
      const data = (await response.json()) as { error?: string; result?: EtherscanReceipt | null };

      if (data.error || !data.result) {
        return { valid: false, amount: 0, error: 'Transaction not found or not yet confirmed' };
      }

      const receipt = data.result;

      if (receipt.status !== '0x1') {
        return { valid: false, amount: 0, error: 'Transaction failed or reverted' };
      }

      const confirmations = await getConfirmations(apiKey, receipt.blockNumber);
      if (confirmations < 6) {
        return {
          valid: false,
          amount: 0,
          error: `Insufficient confirmations (${confirmations}/6). Please wait a few more minutes.`,
        };
      }

      for (const log of receipt.logs ?? []) {
        if ((log.address ?? '').toLowerCase() !== usdtContract) continue;
        if ((log.topics ?? [])[0] !== TRANSFER_TOPIC) continue;
        const topics = log.topics ?? [];
        if (topics.length < 3) continue;
        const recipient = `0x${topics[2].slice(-40)}`;
        if (recipient.toLowerCase() !== walletAddress) continue;
        const rawAmount = parseInt(log.data ?? '0', 16);
        return { valid: true, amount: rawAmount / 1e6, error: null };
      }

      return {
        valid: false,
        amount: 0,
        error: 'No USDT transfer to our wallet found in this transaction',
      };
    } catch (e) {
      const name = e instanceof Error ? e.name : '';
      if (name === 'TimeoutError' || name === 'AbortError') {
        return { valid: false, amount: 0, error: 'Etherscan API timeout. Please try again.' };
      }
      return {
        valid: false,
        amount: 0,
        error: `Verification error: ${e instanceof Error ? e.message : String(e)}`,
      };
    }
  },
};
