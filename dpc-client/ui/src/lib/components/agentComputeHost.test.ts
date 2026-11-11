import { describe, it, expect } from "vitest";
import {
  agentHostDisplay,
  heldModelLabel,
  missingModelBadge,
  agentsNamingAlias,
  deleteProviderPrompt,
  type AgentAliasRow,
} from "./agentComputeHost";

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

describe("heldModelLabel", () => {
  it("names the alias, the host by its known name, and that it is offline", () => {
    const label = heldModelLabel("llamacpp_qwen38_27b", HOST, [{ node_id: HOST, display_name: "Old PC", is_connected: false }]);
    expect(label).toBe("llamacpp_qwen38_27b @ Old PC (offline)");
  });

  it("falls back to the node id when the host has no known name", () => {
    const label = heldModelLabel("a", HOST, []);
    expect(label).toBe(`a @ ${HOST.slice(0, 20)}... (offline)`);
  });

  it("does not say offline for a connected host that does not offer the alias", () => {
    const label = heldModelLabel("a", HOST, [{ node_id: HOST, name: "Johnny", is_connected: true }]);
    expect(label).toBe("a @ Johnny (not offered by it now)");
  });

  it("has no host to name for a local agent", () => {
    for (const v of [undefined, "", "local"]) {
      expect(heldModelLabel("a", v, [])).toBe("a (not available now)");
    }
  });
});

describe("missingModelBadge", () => {
  it("is null while the main model is held, even if another role is not", () => {
    expect(missingModelBadge({ missing_aliases: [] })).toBeNull();
    expect(missingModelBadge({})).toBeNull();
    expect(missingModelBadge({ missing_aliases: ["sleep"] })).toBeNull();
  });

  it("says 'not available now' and the tooltip names the alias and every missing role in order", () => {
    const badge = missingModelBadge({
      provider_alias: "qwen-gone",
      alias_roles: { main: "qwen-gone", sleep: "qwen-gone" },
      missing_aliases: ["snapshot", "main", "sleep"],
    });
    expect(badge?.text).toBe("⚠ not available now");
    expect(badge?.title).toContain("qwen-gone (not available now)");
    expect(badge?.title).toContain("Roles without a model: main, sleep, snapshot");
  });
});

describe("deleteProviderPrompt", () => {
  const agents: AgentAliasRow[] = [
    { name: "Kotler", alias_roles: { main: "X", sleep: "X", snapshot: "X", compaction: "Y" } },
    { name: "Muse", alias_roles: { main: "X" } },
    { name: "Johnny", alias_roles: { main: "Y" } },
    { name: "OnPeer", alias_roles: {} },
  ];

  it("asks the plain question when no agent names the alias", () => {
    expect(deleteProviderPrompt("Z", agents)).toBe("Delete provider 'Z'?");
    expect(deleteProviderPrompt("Z", undefined)).toBe("Delete provider 'Z'?");
  });

  it("names each agent by display name with the roles that use the alias", () => {
    expect(deleteProviderPrompt("X", agents)).toBe(
      "Delete provider 'X'? Used by: Kotler (main, sleep, snapshot); Muse (main)"
    );
  });

  it("lists only the roles that name this alias", () => {
    expect(agentsNamingAlias(agents, "Y")).toEqual([
      { name: "Kotler", roles: ["compaction"] },
      { name: "Johnny", roles: ["main"] },
    ]);
  });
});
