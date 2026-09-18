import { describe, it, expect } from "vitest";
import { effectiveAgentVisionModel } from "./agentVisionModel";

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
