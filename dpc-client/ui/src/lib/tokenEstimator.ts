/**
 * Token Estimation Utility
 *
 * Provides real-time token estimation using character-based heuristic (4 chars ≈ 1 token).
 * This matches the backend's fallback estimation method for consistency.
 *
 * Note: Estimation is approximate and excludes context data (personal, device, peer contexts)
 * since their sizes are unknown to the frontend. Server-side token counting remains authoritative.
 */

/**
 * Estimates token count for text using character-based heuristic.
 * Formula: 4 characters ≈ 1 token (industry standard approximation)
 *
 * @param text - The text to estimate tokens for
 * @returns Estimated token count (floor of text.length / 4)
 *
 * @example
 * estimateTokens("hello") // Returns 1 (5 chars / 4 = 1.25 → floor = 1)
 * estimateTokens("The quick brown fox") // Returns 4 (19 chars / 4 = 4.75 → floor = 4)
 */
export function estimateTokens(text: string): number {
	if (!text) return 0;
	return Math.floor(text.length / 4);
}

/**
 * Calculates estimated total token usage for a conversation.
 * Combines current usage (from backend) with estimated new input (frontend calculation).
 *
 * @param currentUsage - Current token usage from tokenUsageMap store
 * @param currentUsage.used - Current token count (from last server response)
 * @param currentUsage.limit - Token limit (model's context window)
 * @param inputText - Text currently being typed (not yet sent)
 * @returns Object with usage metrics
 *
 * @example
 * // User has sent 8000 tokens worth of messages, typing new message
 * estimateConversationUsage({ used: 8000, limit: 16000 }, "hello world")
 * // Returns: { current: 8000, estimated: 2, total: 8002, percentage: 0.50, isEstimated: true }
 *
 * // No input text
 * estimateConversationUsage({ used: 8000, limit: 16000 }, "")
 * // Returns: { current: 8000, estimated: 0, total: 8000, percentage: 0.50, isEstimated: false }
 */
export function estimateConversationUsage(
	currentUsage: { used: number; limit: number },
	inputText: string
): {
	current: number;
	estimated: number;
	total: number;
	percentage: number;
	isEstimated: boolean;
} {
	const current = currentUsage.used || 0;
	const limit = currentUsage.limit || 0;
	const estimated = estimateTokens(inputText);
	const total = current + estimated;
	const percentage = limit > 0 ? total / limit : 0;

	return {
		current,
		estimated,
		total,
		percentage,
		isEstimated: inputText.length > 0,
	};
}
