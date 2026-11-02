import { describe, it, expect } from "vitest";
import { effectiveAgentVisionModel, visionFollowingText } from "./agentVisionModel";

const rows = [
  { alias: "bonsai-2", model: "gemma-3-27b", supports_vision: true },
  { alias: "text-only", model: "qwen3-8b", supports_vision: false },
  { alias: "qwen-vl", model: "qwen3-vl:8b", supports_vision: true }
];

describe("effectiveAgentVisionModel", () => {
  it("uses the agent's own model when it can see", () => {
    const r = effectiveAgentVisionModel("bonsai-2", rows, "qwen-vl");
    expect(r.alias).toBe("bonsai-2");
    expect(r.isGlobal).toBe(false);
    expect(r.label).toBe("bonsai-2 (gemma-3-27b)");
  });

  it("falls back to the global vision provider when the agent cannot see", () => {
    const r = effectiveAgentVisionModel("text-only", rows, "qwen-vl");
    expect(r.alias).toBe("qwen-vl");
    expect(r.isGlobal).toBe(true);
    expect(r.label).toContain("global");
  });

  it("falls back when the agent alias is unknown locally (remote host)", () => {
    const r = effectiveAgentVisionModel("on-a-peer", rows, "qwen-vl");
    expect(r.alias).toBe("qwen-vl");
    expect(r.isGlobal).toBe(true);
  });

  it("falls back when no agent alias is set", () => {
    const r = effectiveAgentVisionModel("", rows, "qwen-vl");
    expect(r.alias).toBe("qwen-vl");
    expect(r.isGlobal).toBe(true);
  });

  it("says so when no global vision provider is configured", () => {
    const r = effectiveAgentVisionModel("text-only", rows, "");
    expect(r.label).toContain("none configured");
    expect(r.isGlobal).toBe(true);
  });
});

describe("effectiveAgentVisionModel with the text model on a peer", () => {
  const HOST = "dpc-node-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
  const peerRows = (r: { alias: string; model?: string; supports_vision?: boolean }[]) =>
    new Map([[HOST, r]]);

  it("reports the agent's own model, on the host, when the peer row can see", () => {
    const r = effectiveAgentVisionModel(
      "strata flash-next", rows, "qwen-vl", HOST,
      peerRows([{ alias: "strata flash-next", model: "flash-next-35b", supports_vision: true }]),
      "Johnny"
    );
    expect(r.alias).toBe("strata flash-next");
    expect(r.isGlobal).toBe(false);
    expect(r.label).toBe("strata flash-next (flash-next-35b) - on Johnny");
  });

  it("names the node id when the host has no display name", () => {
    const r = effectiveAgentVisionModel(
      "strata flash-next", rows, "qwen-vl", HOST,
      peerRows([{ alias: "strata flash-next", supports_vision: true }])
    );
    expect(r.isGlobal).toBe(false);
    expect(r.label).toBe(`strata flash-next - on ${HOST.slice(0, 20)}...`);
  });

  it("falls back to global when the peer row cannot see", () => {
    const r = effectiveAgentVisionModel(
      "strata flash-next", rows, "qwen-vl", HOST,
      peerRows([{ alias: "strata flash-next", model: "m", supports_vision: false }]),
      "Johnny"
    );
    expect(r.alias).toBe("qwen-vl");
    expect(r.isGlobal).toBe(true);
  });

  it("matches the alias in the host's rows, not a neighbour's", () => {
    const r = effectiveAgentVisionModel(
      "strata flash-next", rows, "qwen-vl", HOST,
      peerRows([{ alias: "other", supports_vision: true }])
    );
    expect(r.isGlobal).toBe(true);
  });

  it("falls back to global when the peer is not connected (no rows)", () => {
    const r = effectiveAgentVisionModel(
      "strata flash-next", rows, "qwen-vl", HOST, new Map(), "Johnny"
    );
    expect(r.alias).toBe("qwen-vl");
    expect(r.isGlobal).toBe(true);
  });

  it("ignores peer rows when the host is local", () => {
    const r = effectiveAgentVisionModel(
      "strata flash-next", rows, "qwen-vl", "local",
      peerRows([{ alias: "strata flash-next", supports_vision: true }])
    );
    expect(r.isGlobal).toBe(true);
  });
});

describe('visionFollowingText (plain AI chat)', () => {
  const rows = [{ uniqueId: 'local:strata_iq3_s' }, { uniqueId: 'local:gemma' }];
  it('takes the text provider when it reads images', () => {
    expect(visionFollowingText('local:strata_iq3_s', rows, 'gemma')).toBe('local:strata_iq3_s');
  });
  it('falls back to the global vision provider when the text one does not', () => {
    expect(visionFollowingText('local:deepseek_flash', rows, 'gemma')).toBe('local:gemma');
  });
  it('is empty when there is neither', () => {
    expect(visionFollowingText('local:deepseek_flash', rows, '')).toBe('');
  });
});

describe('the AI chat header wires the rule', () => {
  it('sets Vision from Text on change and at start', async () => {
    const src = (await import('./ProviderSelector.svelte?raw')).default as string;
    expect(src).toContain("selectedVisionProvider = visionFollowingText(chosen, mergedVisionProviders()");
    expect(src).toContain("selectedVisionProvider = visionFollowingText(selectedTextProvider, mergedVisionProviders()");
  });
});
