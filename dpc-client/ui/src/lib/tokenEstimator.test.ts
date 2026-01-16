/**
 * Unit tests for Token Estimator
 */

import { describe, it, expect } from 'vitest';
import { estimateTokens, estimateConversationUsage } from './tokenEstimator';

describe('estimateTokens', () => {
	it('returns 0 for empty string', () => {
		expect(estimateTokens('')).toBe(0);
	});

	it('returns 0 for null/undefined input', () => {
		expect(estimateTokens(null as any)).toBe(0);
		expect(estimateTokens(undefined as any)).toBe(0);
	});

	it('estimates tokens for short text (< 4 chars)', () => {
		expect(estimateTokens('h')).toBe(0); // 1 / 4 = 0.25 → floor = 0
		expect(estimateTokens('hi')).toBe(0); // 2 / 4 = 0.5 → floor = 0
		expect(estimateTokens('hey')).toBe(0); // 3 / 4 = 0.75 → floor = 0
	});

	it('estimates tokens for 4+ character text', () => {
		expect(estimateTokens('hello')).toBe(1); // 5 / 4 = 1.25 → floor = 1
		expect(estimateTokens('hello world')).toBe(2); // 11 / 4 = 2.75 → floor = 2
	});

	it('estimates tokens for medium text', () => {
		const text = 'The quick brown fox jumps over the lazy dog'; // 43 chars
		expect(estimateTokens(text)).toBe(10); // 43 / 4 = 10.75 → floor = 10
	});

	it('estimates tokens for long text', () => {
		const text = 'a'.repeat(1000); // 1000 chars
		expect(estimateTokens(text)).toBe(250); // 1000 / 4 = 250
	});

	it('handles text with special characters', () => {
		const text = '你好世界 Hello 🌍'; // Mixed unicode
		expect(estimateTokens(text)).toBe(Math.floor(text.length / 4));
	});

	it('handles multiline text', () => {
		const text = 'Line 1\nLine 2\nLine 3';
		expect(estimateTokens(text)).toBe(Math.floor(text.length / 4));
	});
});

describe('estimateConversationUsage', () => {
	it('handles empty input with no current usage', () => {
		const result = estimateConversationUsage({ used: 0, limit: 1000 }, '');
		expect(result.current).toBe(0);
		expect(result.estimated).toBe(0);
		expect(result.total).toBe(0);
		expect(result.percentage).toBe(0);
		expect(result.isEstimated).toBe(false);
	});

	it('calculates estimated tokens from input text', () => {
		const result = estimateConversationUsage({ used: 0, limit: 1000 }, 'hello');
		expect(result.current).toBe(0);
		expect(result.estimated).toBe(1); // 5 chars / 4 = 1
		expect(result.total).toBe(1);
		expect(result.percentage).toBeCloseTo(0.001);
		expect(result.isEstimated).toBe(true);
	});

	it('combines current usage with estimated input', () => {
		const result = estimateConversationUsage({ used: 500, limit: 1000 }, 'hello world');
		expect(result.current).toBe(500);
		expect(result.estimated).toBe(2); // 11 chars / 4 = 2
		expect(result.total).toBe(502);
		expect(result.percentage).toBeCloseTo(0.502);
		expect(result.isEstimated).toBe(true);
	});

	it('calculates percentage at 50%', () => {
		const result = estimateConversationUsage({ used: 500, limit: 1000 }, '');
		expect(result.percentage).toBe(0.5);
	});

	it('calculates percentage at 80%', () => {
		const result = estimateConversationUsage({ used: 800, limit: 1000 }, '');
		expect(result.percentage).toBe(0.8);
	});

	it('calculates percentage at 90%', () => {
		const result = estimateConversationUsage({ used: 900, limit: 1000 }, '');
		expect(result.percentage).toBe(0.9);
	});

	it('calculates percentage at 100%', () => {
		const result = estimateConversationUsage({ used: 1000, limit: 1000 }, '');
		expect(result.percentage).toBe(1.0);
	});

	it('handles overflow (>100%)', () => {
		const result = estimateConversationUsage({ used: 950, limit: 1000 }, 'a'.repeat(400));
		expect(result.estimated).toBe(100); // 400 / 4 = 100
		expect(result.total).toBe(1050);
		expect(result.percentage).toBe(1.05);
		expect(result.isEstimated).toBe(true);
	});

	it('handles no token limit (limit = 0)', () => {
		const result = estimateConversationUsage({ used: 500, limit: 0 }, 'test');
		expect(result.percentage).toBe(0);
		expect(result.total).toBe(501); // 500 + 1 (from "test")
	});

	it('handles negative limit (defensive)', () => {
		const result = estimateConversationUsage({ used: 500, limit: -100 }, 'test');
		expect(result.percentage).toBe(0); // Negative limit treated as no limit
	});

	it('handles missing used value (defaults to 0)', () => {
		const result = estimateConversationUsage({ used: undefined as any, limit: 1000 }, 'test');
		expect(result.current).toBe(0);
		expect(result.total).toBe(1); // 0 + 1 from "test"
	});

	it('sets isEstimated to false when no input text', () => {
		const result = estimateConversationUsage({ used: 500, limit: 1000 }, '');
		expect(result.isEstimated).toBe(false);
	});

	it('sets isEstimated to true when input text exists', () => {
		const result = estimateConversationUsage({ used: 500, limit: 1000 }, 'a');
		expect(result.isEstimated).toBe(true);
	});

	it('handles large input text', () => {
		const largeInput = 'a'.repeat(10000); // 10,000 chars
		const result = estimateConversationUsage({ used: 5000, limit: 20000 }, largeInput);
		expect(result.estimated).toBe(2500); // 10,000 / 4
		expect(result.total).toBe(7500); // 5000 + 2500
		expect(result.percentage).toBe(0.375); // 7500 / 20000
	});

	it('handles edge case: exactly at limit with estimation', () => {
		const result = estimateConversationUsage({ used: 995, limit: 1000 }, 'abcdefghijklmnop'); // 16 chars
		expect(result.estimated).toBe(4); // 16 / 4
		expect(result.total).toBe(999);
		expect(result.percentage).toBeCloseTo(0.999);
	});

	it('handles edge case: just over limit with estimation', () => {
		const result = estimateConversationUsage({ used: 995, limit: 1000 }, 'abcdefghijklmnopqrst'); // 20 chars
		expect(result.estimated).toBe(5); // 20 / 4
		expect(result.total).toBe(1000);
		expect(result.percentage).toBe(1.0);
	});
});
