// Terminal provider statuses that do not map to a more specific call outcome.
const FAILED_TERMINAL = new Set([
  'failed', 'error', 'rejected', 'timeout', 'timedout', 'cancel',
  'disconnected', 'ringtimeout', 'transferred', 'forwarded',
]);

export type CallStatus = 'completed' | 'busy' | 'no_answer' | 'cancelled' | 'failed' | 'calling';

export function callStatusFromProvider(raw: string): CallStatus {
  const status = String(raw ?? '').toLowerCase();
  if (status === 'completed') return 'completed';
  if (status === 'busy') return 'busy';
  if (status === 'no-answer' || status === 'no_answer' || status === 'unanswered') return 'no_answer';
  if (status === 'canceled' || status === 'cancelled') return 'cancelled';
  if (FAILED_TERMINAL.has(status)) return 'failed';
  return 'calling';
}
