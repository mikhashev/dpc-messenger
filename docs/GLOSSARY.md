# Glossary

One row per word the project uses in a specific sense. The **Meaning** column paraphrases
the document that owns the word; the **Defined in** column is that document, and it wins
whenever the two disagree. A row without a source does not belong here — this file points,
it does not define. Add a row when a word had to be explained twice; `tools/backlog/build.py
--check` reports a row whose link resolves to nothing, and an axis token that has no row.

The naming rule that this file serves is in [CLAUDE.md](../CLAUDE.md#vocabulary--read-the-projects-words-before-naming-anything):
read the vocabulary before naming anything new, build names from the words the model already
has, grep before you coin.

## Direction, decisions, and the board

| Term | Meaning | Defined in |
|---|---|---|
| **axis** | The direction a decision or a board entry serves, one word from a five-word vocabulary; one or two per record, three is a smell. The counter per axis of work finished and never seen working is the project's health signal. | [BACKLOG_FORMAT.md §4a](BACKLOG_FORMAT.md#4a-the-one-field-that-was-added-anyway--axis) |
| **collective** | Axis: from personal to collective — P2P, groups, identity, signatures, history. VISION's first vector. | [BACKLOG_FORMAT.md §4a](BACKLOG_FORMAT.md#4a-the-one-field-that-was-added-anyway--axis), [VISION.md — Direction](../VISION.md#direction) |
| **knowledge** | Axis: from passive to collaborative — memory, retrieval, the agent that works on it. VISION's second vector. | [BACKLOG_FORMAT.md §4a](BACKLOG_FORMAT.md#4a-the-one-field-that-was-added-anyway--axis), [VISION.md — Direction](../VISION.md#direction) |
| **network** | Axis: from local to networked — local inference, compute, cost, the gateway. VISION's third vector. | [BACKLOG_FORMAT.md §4a](BACKLOG_FORMAT.md#4a-the-one-field-that-was-added-anyway--axis), [VISION.md — Direction](../VISION.md#direction) |
| **honesty** | Axis: that the project's own numbers mean something — eval, CI, the board and its gates. Added by the board standard as a loop the project cannot be honest without; not one of VISION's vectors. | [BACKLOG_FORMAT.md §4a](BACKLOG_FORMAT.md#4a-the-one-field-that-was-added-anyway--axis) |
| **reach** | Axis: that somebody outside can find and use this — docs, releases, distribution. VISION's fourth vector, "from one practice to many", counted as a trace of adoption rather than as a number. | [BACKLOG_FORMAT.md §4a](BACKLOG_FORMAT.md#4a-the-one-field-that-was-added-anyway--axis), [VISION.md — Direction](../VISION.md#direction) |
| **constraint (C1–C10)** | One of VISION's ten non-negotiable boundaries, each either enforced by architecture or consciously defended. | [VISION.md — Foundation Constraints](../VISION.md#foundation-constraints) |
| **ADR** | Architecture Decision Record: a permanent record of a structural decision future contributors would otherwise re-litigate, one file per decision under `docs/decisions/`, with YAML front matter the checker validates. | [decisions/TEMPLATE.md](decisions/TEMPLATE.md), [protocol-13-public.md §5](../protocol-13-public.md#5-decision-artifact-system-3-layers) |
| **decision status** | One token from `proposed → accepted → implemented → deprecated → superseded-by-NNN` (or `rejected`); an accepted or implemented decision must carry an axis. | [decisions/TEMPLATE.md — Status Lifecycle](decisions/TEMPLATE.md#status-lifecycle) |
| **board / backlog** | The project's task list as a plain markdown file: one task per `###` heading with an envelope `(PRIORITY, STATUS, DATE — origin)`, prose below. It is a dated synthesis of what to check, never a statement that something is still true. | [BACKLOG_FORMAT.md §1](BACKLOG_FORMAT.md#1-the-entry) |
| **entry** | One board task: a `SCREAMING-KEBAB` name that states a claim, an envelope, and free prose (Observed / Inferred / First step). The name is the handle everything else cross-references. | [BACKLOG_FORMAT.md §1](BACKLOG_FORMAT.md#1-the-entry) |
| **origin** | Who raised an entry and when, in their words when there are words; the one field that may carry Russian. | [BACKLOG_FORMAT.md §1](BACKLOG_FORMAT.md#1-the-entry), [§8](BACKLOG_FORMAT.md#8-the-check) |
| **section / status** | The `##` heading an entry sits under is its status (`OPEN`, `IN PROGRESS`, `DONE — AWAITING OBSERVATION`, `BLOCKED ON DECISION`, `BACKLOG`, `IDEAS`); the heading duplicates it so a bare chunk still says what it is. | [BACKLOG_FORMAT.md §2](BACKLOG_FORMAT.md#2-status-lives-in-two-places-on-purpose) |
| **observation shelf** | The `DONE — AWAITING OBSERVATION` section: code written and not yet seen working in production. An entry leaves it only by recording an observation. | [BACKLOG_FORMAT.md §2](BACKLOG_FORMAT.md#2-status-lives-in-two-places-on-purpose) |
| **observation debt** | The number of entries on the observation shelf, printed per axis in the generated ROADMAP block. | [ROADMAP.md — Status by axis](../ROADMAP.md#status-by-axis) |
| **resolution** | Why an entry left the working file: `fixed`, `disproved`, `moot`, `superseded`, `duplicate`, `wontfix`; each requires its own kind of evidence in the closure line. | [BACKLOG_FORMAT.md §3](BACKLOG_FORMAT.md#3-closing-an-entry) |
| **closure line** | `**Closed:** S<session> · YYYY-MM-DD · <resolution> · <evidence>`, written when an entry moves to `backlog_closed.md`; never backfilled onto old entries. | [BACKLOG_FORMAT.md §3](BACKLOG_FORMAT.md#3-closing-an-entry) |
| **session identifier** | `S<YYYY-MM-DD>.<N>`: the UTC date a session opened and its position among that day's sessions, derived from the group's reset archive. Three older numbering families survive in old text and are not renumbered. | [BACKLOG_FORMAT.md §3](BACKLOG_FORMAT.md#the-session-identifier) |
| **stale reference** | A name-shaped token in an entry's body that resolves to no entry in either backlog file; reported, never refused, and an upper bound on real breakage. | [BACKLOG_FORMAT.md §8](BACKLOG_FORMAT.md#8-the-check) |
| **generated block** | The span of ROADMAP.md between the `generated by` markers, rendered from ADR front matter and the board by `build.py --roadmap`; a hand edit inside it is refused by the checker. | [ROADMAP.md — Status by axis](../ROADMAP.md#status-by-axis) |
| **Observed / Inferred** | The split every claim about cause has to make: facts anchored to a log, a file or a measurement, and the hypothesis with the check that would confirm it. An unverified inference is a hypothesis, whatever grammar it is written in. | [protocol-13-public.md §11 — Evidence Discipline](../protocol-13-public.md#evidence-discipline), [BACKLOG_FORMAT.md §1](BACKLOG_FORMAT.md#1-the-entry) |

## People, agents, and how they work together

| Term | Meaning | Defined in |
|---|---|---|
| **Protocol 13** | The operating agreement for a team of one human coordinator, one execute agent and one review agent: roles, interaction patterns, communication norms and the decision-artifact layers. | [protocol-13-public.md](../protocol-13-public.md) |
| **Human Coordinator** | The decision-maker: provides direction, approves scope, relays between agents, resolves disagreements. | [protocol-13-public.md §2](../protocol-13-public.md#human-coordinator) |
| **Execute Agent** | Writes code, runs tests, commits; decides *how* given a clear *what*; never approves its own work. | [protocol-13-public.md §2](../protocol-13-public.md#execute-agent) |
| **Review Agent** | Writes design rationale and reviews outcomes; works at the level of *why*; flags what is wrong and leaves *how* to the Execute Agent. | [protocol-13-public.md §2](../protocol-13-public.md#review-agent) |
| **explicit action (DDA)** | Work starts only after an explicit action verb; "agree", "fine", "makes sense" are not authorisation, and ambiguous consent is no consent. | [protocol-13-public.md §11](../protocol-13-public.md#explicit-action-required-dda) |
| **interaction pattern (A–E)** | The five named ways the three participants exchange work: collaborative design, standard execution, direct dialogue, automated @mention routing, and the external agent's monitoring loop. | [protocol-13-public.md §3](../protocol-13-public.md#3-interaction-patterns) |
| **sprint log** | Temporary working memory for the current team in `sprint-logs/`, four sections (design rationale, implementation notes, review findings, decisions made), extracted to ADRs and archived. | [protocol-13-public.md §5](../protocol-13-public.md#layer-2--sprint-logs) |
| **agent** | An embedded LLM participant that runs on one node with its own storage under `~/.dpc/agents/<id>/`, tools, memory and permission profile; addressed by display name. | [agent/DPC_AGENT_GUIDE.md](agent/DPC_AGENT_GUIDE.md#architecture) |
| **agent profile** | The per-agent block in `privacy_rules.json` that decides which tools the agent may use and which paths it may reach; an agent's own profile beats the global `dpc_agent` block. | [agent/DPC_AGENT_GUIDE.md — Tool Access Control](agent/DPC_AGENT_GUIDE.md#tool-access-control) |
| **sandbox** | The directory an agent may read and write by default, `~/.dpc/agents/<id>/`; everything else needs an extended path. | [agent/DPC_AGENT_GUIDE.md — Extended Sandbox Paths](agent/DPC_AGENT_GUIDE.md#extended-sandbox-paths) |
| **extended sandbox path** | A directory outside the sandbox granted to an agent as read-only or read-write in its profile. | [agent/DPC_AGENT_GUIDE.md — Extended Sandbox Paths](agent/DPC_AGENT_GUIDE.md#extended-sandbox-paths) |
| **shell tier (0/1/2)** | The three-way classification of a `run_shell` command: Tier 0 runs at once, Tier 1 waits for a person's approval in the chat, Tier 2 is hard-blocked and not overridable by config or agent. Not the same word as a connection tier. | [decisions/030 — Tier Model](decisions/030-run-shell-safety-guardrails.md#tier-model) |
| **external agent** | A second LLM session running outside DPC — Claude Code or any harness — that reads a conversation from disk and posts back over the local WebSocket API through the bridge scripts. | [agent/CC_INTEGRATION_GUIDE.md](agent/CC_INTEGRATION_GUIDE.md#what-the-external-agent-sees-what-it-does) |
| **tag** | The name an external agent answers to in a group, registered per node in Group Settings and stored in the group's metadata; in a 1:1 chat the configured display name is the only identity. | [agent/CC_INTEGRATION_GUIDE.md — Which name it answers to](agent/CC_INTEGRATION_GUIDE.md#which-name-it-answers-to) |
| **bridge** | One of the two helper scripts an external agent uses: the agent-chat bridge for 1:1 chats and the group bridge for groups. | [agent/CC_INTEGRATION_GUIDE.md — The bridge scripts](agent/CC_INTEGRATION_GUIDE.md#the-bridge-scripts) |
| **mention** | `@Name` in a message routes it to that agent; `@all` fans out to every agent in the group and is human-only; fenced code is stripped before matching. | [GROUP_CHAT.md — Mention routing](GROUP_CHAT.md#mention-routing) |
| **reasoning effort** | One word from the shared scale `off / low / medium / high / max` that asks a model how deeply to think; each provider folds it onto its own ladder. | [`providers/base.py` `REASONING_EFFORTS`](../dpc-client/core/dpc_client_core/providers/base.py) |
| **provider alias** | The name of one configured model endpoint in `~/.dpc/providers.json`; agents, chats and roles name a provider by its alias. | [CLAUDE.md — AI Providers](../CLAUDE.md#ai-providers) |

## Memory, knowledge, and sleep

| Term | Meaning | Defined in |
|---|---|---|
| **Active Recall** | Cross-layer retrieval that injects the most relevant knowledge into an agent's prompt without an explicit tool call: FAISS and BM25 fused, budget-aware. | [decisions/010 — Embeddings Active Recall](decisions/010-agent-memory-architecture.md#component-2-embeddings-active-recall--cross-layer-retrieval) |
| **memory layers (L1–L7)** | The seven layers agent memory spans, from L1 strategy documents through L5 agent-written knowledge and L6 human-committed knowledge to L7 the human; retrieval priority is L6 > L1 > L5 > L2-docs. | [decisions/010 — Layer Connection Map](decisions/010-agent-memory-architecture.md#layer-connection-map) |
| **knowledge commit** | A structured, attributed, versioned unit of knowledge extracted from a conversation, proposed, reviewed, voted on and committed; the human owns the extraction. | [KNOWLEDGE_ARCHITECTURE.md §6](KNOWLEDGE_ARCHITECTURE.md#6-knowledge-commit-protocol), [protocol-13-public.md §6](../protocol-13-public.md#6-knowledge-architecture) |
| **consensus / devil's advocate** | Multi-party voting on a knowledge commit; with three or more participants one is required to dissent. | [KNOWLEDGE_ARCHITECTURE.md §4.3](KNOWLEDGE_ARCHITECTURE.md#43-consensus-with-required-dissent) |
| **sleep consolidation** | An on-demand pipeline that reads the previous sessions' archives one at a time, extracts findings, writes them into the knowledge graph and produces a morning brief; it replaced the background "consciousness" worker and the autonomous evolution loop. | [decisions/014](decisions/014-sleep-consolidation-architecture.md), [protocol-13-public.md §7](../protocol-13-public.md#7-between-sessions) |
| **morning brief** | The output of a sleep cycle that the next session loads as context; in a group, each agent posts its own brief into the chat. | [decisions/014](decisions/014-sleep-consolidation-architecture.md), [GROUP_CHAT.md — Group Sleep](GROUP_CHAT.md#group-sleep--morning-briefs) |
| **compaction** | Summarising aged tool results inside an agent run once context usage crosses a threshold, so a long run keeps fitting; it shrinks tool rounds, not the conversation. | [decisions/033](decisions/033-agent-tool-loop-llm-compaction.md#decision) |
| **skill** | A static `SKILL.md` strategy file an agent can execute, share with peers and import; imports fork from their origin. | [agent/DPC_AGENT_GUIDE.md — Skills](agent/DPC_AGENT_GUIDE.md#skills--agent-discovery-tools) |

## Nodes, peers, and the wire

| Term | Meaning | Defined in |
|---|---|---|
| **node** | One running D-PC client with its own cryptographic identity: a human and their agents on one machine. Not the XML or DOM sense of the word used in other codebases. | [specs/dptp_v1.md §4](../specs/dptp_v1.md#4-node-identity-system) |
| **node_id** | `dpc-node-` plus the first 32 hex characters of the SHA-256 of the node's RSA public key; the certificate's Common Name and the key to everything that names a node. | [specs/dptp_v1.md §4](../specs/dptp_v1.md#node-id-format) |
| **peer** | Another node this node talks to over DPTP; its certificate is stored only after its key re-hashes to its claimed `node_id`. | [specs/dptp_v1.md §4](../specs/dptp_v1.md#identity-storage) |
| **DPTP** | D-PC Transfer Protocol: a 10-byte ASCII length header plus a JSON payload, carried over TLS, WebRTC data channels or DTLS. | [specs/dptp_v1.md §1–2](../specs/dptp_v1.md#1-overview) |
| **HELLO** | The first DPTP message on a connection, carrying the peer's `node_id` and display name; both sides send one. | [specs/dptp_v1.md §3.1](../specs/dptp_v1.md#31-connection-establishment) |
| **connection tier (1–6)** | One of six strategies the orchestrator tries in order — IPv6 direct, IPv4 direct, Hub WebRTC, UDP hole punching, volunteer relay, gossip store-and-forward. "Complete" in the roadmap means the code shipped, not that the tier carries traffic. | [CLAUDE.md — Connection Types](../CLAUDE.md#connection-types-6-tier-fallback-hierarchy---v0101), [ROADMAP.md — Decentralized Infrastructure](../ROADMAP.md#decentralized-infrastructure-v095---v0102) |
| **star topology** | The live shape of the three-node stand: every link goes through one node because host firewalls block inbound elsewhere; a configuration of a real user, not a defect to fix by opening ports. | [ROADMAP.md — Decentralized Infrastructure](../ROADMAP.md#decentralized-infrastructure-v095---v0102) |
| **peer cache** | `~/.dpc/peer_cache.json`, the last known address of each peer; what actually carries traffic when the DHT is empty. | [ROADMAP.md — Decentralized Infrastructure](../ROADMAP.md#decentralized-infrastructure-v095---v0102) |
| **DHT / seed nodes** | The Kademlia table a node uses to find peers, entered only through the `[dht] seed_nodes` config line; empty by default. | [CONFIGURATION.md `[dht]`](CONFIGURATION.md#dht) |
| **Hub** | The optional federation server: OAuth login and WebRTC signalling only, never message routing or storage; every direct path works without it. | [CLAUDE.md — Key Components](../CLAUDE.md#key-components), [specs/hub_api_v1.md](../specs/hub_api_v1.md) |
| **firewall / privacy rules** | `~/.dpc/privacy_rules.json`: what each peer or group may read of this node's context, whether compute and transcription are shared, what agents may use; hot-reloaded on save. | [CLAUDE.md — Context Firewall Rules](../CLAUDE.md#context-firewall-rules) |
| **remote inference / compute sharing** | Running a model on a trusted peer's hardware over the encrypted P2P channel; the requester picks the peer as compute host, the host's firewall decides whether to serve. | [REMOTE_INFERENCE.md](REMOTE_INFERENCE.md#overview) |
| **message signing / preimage** | A message signature covers a canonical preimage of ten fields in a fixed order, tagged `dptp-msg-v1`; signed by the author at send time, never re-signed by a receiver. | [specs/dptp_v1.md §4.1](../specs/dptp_v1.md#41-message-signing), [decisions/036](decisions/036-message-authenticity-signed-at-origin.md#decision) |
| **verdict (verified / unverified / legacy)** | What a receiver concludes about an arriving record: hash and signature check against a cached certificate; the same except the certificate is not yet cached; or no signature this receiver can recompute. The verdict is the receiver's, and absence of a signature is never acceptance. | [specs/dptp_v1.md §4.2](../specs/dptp_v1.md#42-verdicts-on-receipt) |

## Groups

| Term | Meaning | Defined in |
|---|---|---|
| **group** | A conversation fanned out to N participants under a `group_id`; each member keeps its own copy of metadata and history, reconciled on connect. | [GROUP_CHAT.md — Architecture](GROUP_CHAT.md#architecture) |
| **roster** | The group's `members` (human nodes) plus `agents` and `agent_names` per node, carried in `metadata.json` and converged by `GROUP_SYNC` on version with a content-hash tie-break. | [GROUP_CHAT.md — Data Model](GROUP_CHAT.md#data-model), [decisions/038](decisions/038-group-roster-as-signed-state.md#decision) |
| **per-reader role derivation** | In a group with several agents the same message is `assistant` to its author and `user` to everyone else, so roles are derived per reader when the payload is built, not stored. | [GROUP_CHAT.md — Per-Reader Role Derivation](GROUP_CHAT.md#per-reader-role-derivation-adr-031) |
| **history sync** | On connect, nodes exchange `{count, hash}` per group and per author, and ship only what differs; disk is the source of truth and `message_id` deduplicates. | [GROUP_CHAT.md — Cross-Node History Sync](GROUP_CHAT.md#cross-node-history-sync) |
| **New Session** | A vote to clear a conversation's history; in a group every member must be online and vote yes, silence times out as rejection, and a member gone for good blocks the reset until removed. | [GROUP_CHAT.md — Session Reset](GROUP_CHAT.md#session-reset) |
