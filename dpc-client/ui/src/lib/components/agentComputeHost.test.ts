import { describe, it, expect } from "vitest";
import { agentHostDisplay } from "./agentComputeHost";

const HOST = "dpc-node-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

describe("agentHostDisplay", () => {
  it("is Local when the agent has no compute_host", () => {
    for (const v of [undefined, "", "local"]) {
      const d = agentHostDisplay(v, []);
      expect(d.host).toBe("local");
      expect(d.label).toBe("Local");
      expect(d.connected).toBe(true);
    }
  });

  it("shows the connected peer by name and node id", () => {
    const d = agentHostDisplay(HOST, [{ node_id: HOST, name: "Johnny", is_connected: true }]);
    expect(d.host).toBe(HOST);
    expect(d.label).toBe(`Johnny | ${HOST.slice(0, 20)}...`);
    expect(d.connected).toBe(true);
    expect(d.shortName).toBe("Johnny");
  });

  it("prefers display_name over name", () => {
    const d = agentHostDisplay(HOST, [{ node_id: HOST, name: "j", display_name: "Johnny" }]);
    expect(d.shortName).toBe("Johnny");
  });

  it("keeps a disconnected peer as its node id instead of falling back to Local", () => {
    const d = agentHostDisplay(HOST, [{ node_id: "dpc-node-other", name: "Other" }]);
    expect(d.host).toBe(HOST);
    expect(d.connected).toBe(false);
    expect(d.label).toBe(`${HOST.slice(0, 20)}... (not connected)`);
  });

  it("treats a listed peer with is_connected false as not connected, keeping its name", () => {
    const d = agentHostDisplay(HOST, [{ node_id: HOST, name: "Johnny", is_connected: false }]);
    expect(d.connected).toBe(false);
    expect(d.label).toContain("Johnny");
    expect(d.label).toContain("(not connected)");
  });

  it("copes with a missing peer list", () => {
    expect(agentHostDisplay(HOST, undefined).host).toBe(HOST);
    expect(agentHostDisplay(HOST, null).connected).toBe(false);
  });
});
