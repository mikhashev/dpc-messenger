# Glossary

One heading per word the project uses in a specific sense. The **Meaning** line paraphrases
the document that owns the word; the **Defined in** line is that document, and it wins
whenever the two disagree. A term without a source does not belong here — this file points,
it does not define. Add a term when a word had to be explained twice; `tools/backlog/build.py
--check` reports a term whose link resolves to nothing, and an axis token that has no term.

Each term is a `###` heading with labelled lines under it, in this order: **Term note**
(only when the term is written with a parenthetical, kept as written), **Met in**,
**Meaning**, **Defined in**. `tools/backlog/glossary_check.py` reads exactly this layout
and refuses a term that has no **Defined in** line. The index lists every term
alphabetically; the sections below group the same terms by topic.

The **Met in** line says where a reader meets the word, in one or more of four
tokens. `code` — an identifier or a comment in the source. `rules` — a document the
project works by or a file its owner edits by hand: CLAUDE.md, the board standard, an
ADR, `privacy_rules.json`, `config.ini`. `screen` — text the application puts in front
of a person. `wire` — a DPTP field, command or value that crosses between nodes. Two
tokens on one term are two places one word is met, never two meanings: the term holds one
meaning, and a word that means two things gets two headings, or a sentence naming the other
sense the way **node** and **shell tier** do. The tokens are read against the sense the
term defines and no other — the axis called **knowledge** is `code, rules` because the
Knowledge panel on screen is the other word.

The naming rule that this file serves is in [CLAUDE.md](../CLAUDE.md#vocabulary--read-the-projects-words-before-naming-anything):
read the vocabulary before naming anything new, build names from the words the model already
has, grep before you coin.

## Index

Every term, alphabetically; a leading underscore or backtick is ignored for the order.

**A** — [accepted_request](#accepted_request) · [Active Recall](#active-recall) · [ADR](#adr) · [agent](#agent) · [agent alias role](#agent-alias-role) · [agent profile](#agent-profile) · [agent_lifetime_tokens](#agent_lifetime_tokens) · [already_received](#already_received) · [`archive_include_groups`](#archive_include_groups) · [asker / who asked](#asker--who-asked) · [available / content_verified](#available--content_verified) · [awaiting_permission](#awaiting_permission) · [axis](#axis)

**B** — [background call](#background-call) · [`before_message_id`](#before_message_id) · [billing](#billing) · [Block / Unblock](#block--unblock) · [`block_peer` / `unblock_peer`](#block_peer--unblock_peer) · [`blocked_nodes`](#blocked_nodes) · [board / backlog](#board--backlog) · [bridge](#bridge) · [burst](#burst) · [bytes_per_second](#bytes_per_second)

**C** — [caller_kind / route](#caller_kind--route) · [caller_window](#caller_window) · [cancel_knowledge_extraction](#cancel_knowledge_extraction) · [card ask](#card-ask) · [card hold](#card-hold) · [card lease](#card-lease) · [card queue](#card-queue) · [card-borrow](#card-borrow) · [CDN manifest](#cdn-manifest) · [CDN refusal](#cdn-refusal) · [chat_post_file](#chat_post_file) · [CLI](#cli) · [closure line](#closure-line) · [collective](#collective) · [command job](#command-job) · [commit_mb](#commit_mb) · [common_part](#common_part) · [compaction](#compaction) · [composer / pending file](#composer--pending-file) · [compute.currency](#computecurrency) · [connection tier (1–6)](#connection-tier-16) · [consensus / devil's advocate](#consensus--devils-advocate) · [consent_required](#consent_required) · [constraint (C1–C10)](#constraint-c1c10) · [context window](#context-window) · [Control](#control) · [conversation_id / chat](#conversation_id--chat) · [conversation_spill_mib](#conversation_spill_mib) · [cost_amount](#cost_amount) · [cost_reference](#cost_reference) · [counts_source](#counts_source) · [creator](#creator) · [cut call](#cut-call)

**D** — [decision status](#decision-status) · [deferred](#deferred) · [DHT / seed nodes](#dht--seed-nodes) · [document pipeline](#document-pipeline) · [DPTP](#dptp)

**E** — [effort channel](#effort-channel) · [endpoint_not_served](#endpoint_not_served) · [engine](#engine) · [engine backend](#engine-backend) · [engine time / round trip](#engine-time--round-trip) · [entry](#entry) · [Execute Agent](#execute-agent) · [exit reason](#exit-reason) · [explicit action (DDA)](#explicit-action-dda) · [extended sandbox path](#extended-sandbox-path) · [external agent](#external-agent) · [extraction target](#extraction-target) · [extraction_not_running](#extraction_not_running)

**F** — [feed](#feed) · [FILE_OFFER `text`](#file_offer-text) · [FILE_REQUEST / FILE_REQUEST_DECLINED](#file_request--file_request_declined) · [firewall / privacy rules](#firewall--privacy-rules) · [free commit](#free-commit) · [free_nodes / free_groups](#free_nodes--free_groups)

**G** — [gateway / IDE door](#gateway--ide-door) · [generated block](#generated-block) · [group](#group) · [group `chat_context`](#group-chat_context) · [guest_conversation_cache](#guest_conversation_cache)

**H** — [`has_older`](#has_older) · [HELLO](#hello) · [HELLO preimage](#hello-preimage) · [history sync](#history-sync) · [honesty](#honesty) · [host_scope](#host_scope) · [Hub](#hub) · [Human Coordinator](#human-coordinator)

**I** — [in flight](#in-flight) · [in_local_networks](#in_local_networks) · [Inference Sharing](#inference-sharing) · [`instructions.json:<set_key>.primary`](#instructionsjsonset_keyprimary) · [interaction pattern (A–E)](#interaction-pattern-ae) · [is_connected](#is_connected)

**K** — [knowledge](#knowledge) · [knowledge base](#knowledge-base) · [knowledge commit](#knowledge-commit) · [knowledge_extraction_cancelled](#knowledge_extraction_cancelled) · [KV snapshot](#kv-snapshot)

**L** — [launcher / launcher_kind](#launcher--launcher_kind) · [`layer_audience`](#layer_audience) · [lease line](#lease-line) · [`limit` (local API)](#limit-local-api) · [`link_ping`](#link_ping) · [`LINK_PING`, `LINK_PONG`](#link_ping-link_pong) · [`link_silence_timeout`](#link_silence_timeout) · [`list_cached_peers`](#list_cached_peers) · [live-history boundary](#live-history-boundary) · [local engine swap](#local-engine-swap) · [`local_addresses`](#local_addresses)

**M** — [`MAX_HELLO_FRAME_BYTES`](#max_hello_frame_bytes) · [max_output_tokens](#max_output_tokens) · [`max_proved_connections_per_ip`](#max_proved_connections_per_ip) · [memory layers (L1–L7)](#memory-layers-l1l7) · [memory limit per command](#memory-limit-per-command) · [memory system](#memory-system) · [mention](#mention) · [mention_depth](#mention_depth) · [menu row / menu](#menu-row--menu) · [message signing / preimage](#message-signing--preimage) · [message source](#message-source) · [model download consent](#model-download-consent) · [model files folder](#model-files-folder) · [month summary](#month-summary) · [morning brief](#morning-brief) · [mp.qa/1](#mpqa1) · [music job `variants`](#music-job-variants) · [music QA gate](#music-qa-gate) · [music_* tools](#music_-tools)

**N** — [network](#network) · [New Session](#new-session) · [no_answer](#no_answer) · [node](#node) · [node ledger](#node-ledger) · [node_id](#node_id) · [not_held / busy / not_the_requested_file](#not_held--busy--not_the_requested_file) · [not_offered / upload_limit](#not_offered--upload_limit)

**O** — [observation debt](#observation-debt) · [observation shelf](#observation-shelf) · [Observed / Inferred](#observed--inferred) · [on_reasoning](#on_reasoning) · [origin](#origin) · [output limit](#output-limit) · [output_includes_thinking](#output_includes_thinking) · [own / burn](#own--burn) · [owner_approval_for_others](#owner_approval_for_others) · [owner_only](#owner_only) · [`own_node_group`](#own_node_group)

**P** — [peer](#peer) · [peer cache](#peer-cache) · [peer_proved](#peer_proved) · [per-reader role derivation](#per-reader-role-derivation) · [pin](#pin) · [prefix_hashes, prefix_chars, call_chars](#prefix_hashes-prefix_chars-call_chars) · [process registry](#process-registry) · [processed message ids](#processed-message-ids) · [Protocol 13](#protocol-13) · [proved connection](#proved-connection) · [proven_on_this_machine](#proven_on_this_machine) · [provider alias](#provider-alias) · [provider_kind](#provider_kind) · [provider_row_facts](#provider_row_facts)

**Q** — [qa_report](#qa_report) · [quota block](#quota-block)

**R** — [reach](#reach) · [reasoning effort](#reasoning-effort) · [refusal code](#refusal-code) · [remote inference](#remote-inference) · [`remote:?:<alias>`](#remotealias) · [request_attachment / file_request_status](#request_attachment--file_request_status) · [requested_by](#requested_by) · [resident budget](#resident-budget) · [resolution](#resolution) · [`resolved_via`](#resolved_via) · [resource peaks](#resource-peaks) · [Review Agent](#review-agent) · [roster](#roster) · [round](#round) · [run gate](#run-gate) · [rung](#rung)

**S** — [sandbox](#sandbox) · [saved_locally / offline_members](#saved_locally--offline_members) · [seconds_left](#seconds_left) · [section / status](#section--status) · [self-wait rule](#self-wait-rule) · [send_without_asking](#send_without_asking) · [served_by](#served_by) · [served_effort](#served_effort) · [serves_images_with_tools](#serves_images_with_tools) · [serving lists](#serving-lists) · [session archive](#session-archive) · [session identifier](#session-identifier) · [session marker](#session-marker) · [shared_proposal_not_editable](#shared_proposal_not_editable) · [shell tier (0/1/2)](#shell-tier-012) · [shell-gate refusal](#shell-gate-refusal) · [skill](#skill) · [sleep consolidation](#sleep-consolidation) · [slot queue](#slot-queue) · [`slow_command`](#slow_command) · [spawn origin](#spawn-origin) · [speed_summary](#speed_summary) · [sprint log](#sprint-log) · [stale reference](#stale-reference) · [star topology](#star-topology) · [stretch](#stretch) · [successor](#successor) · [switch](#switch)

**T** — [tag](#tag) · [take_id](#take_id) · [tariff](#tariff) · [task_type](#task_type) · [`telegram_chat_id`](#telegram_chat_id) · [`telegram_owner_user_id`](#telegram_owner_user_id) · [thinking_source](#thinking_source) · [tokens_paid_by](#tokens_paid_by) · [transcription send-to](#transcription-send-to) · [turn](#turn) · [turn command](#turn-command) · [turn_end](#turn_end) · [turn_id](#turn_id)

**U** — [unit](#unit) · [unpriceable](#unpriceable) · [unpriced](#unpriced) · [unrated](#unrated) · [untariffed](#untariffed) · [usage row](#usage-row)

**V** — [variant](#variant) · [vendor account](#vendor-account) · [vendor_request_quotas](#vendor_request_quotas) · [verdict (verified / unverified / legacy)](#verdict-verified--unverified--legacy) · [voice-memory](#voice-memory) · [`vram_free_mib`, `vram_total_mib`](#vram_free_mib-vram_total_mib)

**W** — [wait_reason](#wait_reason) · [waiting](#waiting) · [where](#where) · [written call](#written-call)

## Direction, decisions, and the board

### axis

- **Met in:** code, rules
- **Meaning:** The direction a decision or a board entry serves, one word from a five-word vocabulary; one or two per record, three is a smell. The counter per axis of work finished and never seen working is the project's health signal.
- **Defined in:** [BACKLOG_FORMAT.md §4a](BACKLOG_FORMAT.md#4a-the-one-field-that-was-added-anyway--axis)

### collective

- **Met in:** code, rules
- **Meaning:** Axis: from personal to collective — P2P, groups, identity, signatures, history. VISION's first vector.
- **Defined in:** [BACKLOG_FORMAT.md §4a](BACKLOG_FORMAT.md#4a-the-one-field-that-was-added-anyway--axis), [VISION.md — Direction](../VISION.md#direction)

### knowledge

- **Met in:** code, rules
- **Meaning:** Axis: from passive to collaborative — memory, retrieval, the agent that works on it. VISION's second vector.
- **Defined in:** [BACKLOG_FORMAT.md §4a](BACKLOG_FORMAT.md#4a-the-one-field-that-was-added-anyway--axis), [VISION.md — Direction](../VISION.md#direction)

### network

- **Met in:** code, rules
- **Meaning:** Axis: from local to networked — local inference, compute, cost, the gateway. VISION's third vector.
- **Defined in:** [BACKLOG_FORMAT.md §4a](BACKLOG_FORMAT.md#4a-the-one-field-that-was-added-anyway--axis), [VISION.md — Direction](../VISION.md#direction)

### honesty

- **Met in:** code, rules
- **Meaning:** Axis: that the project's own numbers mean something — eval, CI, the board and its gates. Added by the board standard as a loop the project cannot be honest without; not one of VISION's vectors.
- **Defined in:** [BACKLOG_FORMAT.md §4a](BACKLOG_FORMAT.md#4a-the-one-field-that-was-added-anyway--axis)

### reach

- **Met in:** code, rules
- **Meaning:** Axis: that somebody outside can find and use this — docs, releases, distribution. VISION's fourth vector, "from one practice to many", counted as a trace of adoption rather than as a number.
- **Defined in:** [BACKLOG_FORMAT.md §4a](BACKLOG_FORMAT.md#4a-the-one-field-that-was-added-anyway--axis), [VISION.md — Direction](../VISION.md#direction)

### constraint (C1–C10)

- **Met in:** rules
- **Meaning:** One of VISION's ten non-negotiable boundaries, each either enforced by architecture or consciously defended.
- **Defined in:** [VISION.md — Foundation Constraints](../VISION.md#foundation-constraints)

### ADR

- **Met in:** code, rules
- **Meaning:** Architecture Decision Record: a permanent record of a structural decision future contributors would otherwise re-litigate, one file per decision under `docs/decisions/`, with YAML front matter the checker validates.
- **Defined in:** [decisions/TEMPLATE.md](decisions/TEMPLATE.md), [protocol-13-public.md §5](../protocol-13-public.md#5-decision-artifact-system-3-layers)

### decision status

- **Met in:** code, rules
- **Meaning:** One token from `proposed → accepted → implemented → deprecated → superseded-by-NNN` (or `rejected`); an accepted or implemented decision must carry an axis.
- **Defined in:** [decisions/TEMPLATE.md — Status Lifecycle](decisions/TEMPLATE.md#status-lifecycle)

### board / backlog

- **Met in:** code, rules
- **Meaning:** The project's task list as a plain markdown file: one task per `###` heading with an envelope `(PRIORITY, STATUS, DATE — origin)`, prose below. It is a dated synthesis of what to check, never a statement that something is still true.
- **Defined in:** [BACKLOG_FORMAT.md §1](BACKLOG_FORMAT.md#1-the-entry)

### entry

- **Met in:** code, rules
- **Meaning:** One board task: a `SCREAMING-KEBAB` name that states a claim, an envelope, and free prose (Observed / Inferred / First step). The name is the handle everything else cross-references.
- **Defined in:** [BACKLOG_FORMAT.md §1](BACKLOG_FORMAT.md#1-the-entry)

### origin

- **Met in:** code, rules
- **Meaning:** Who raised an entry and when, in their words when there are words; the one field that may carry Russian. Not the **spawn origin** of a process registry entry.
- **Defined in:** [BACKLOG_FORMAT.md §1](BACKLOG_FORMAT.md#1-the-entry), [§8](BACKLOG_FORMAT.md#8-the-check)

### section / status

- **Met in:** code, rules
- **Meaning:** The `##` heading an entry sits under is its status (`OPEN`, `IN PROGRESS`, `DONE — AWAITING OBSERVATION`, `BLOCKED ON DECISION`, `BACKLOG`, `IDEAS`); the heading duplicates it so a bare chunk still says what it is.
- **Defined in:** [BACKLOG_FORMAT.md §2](BACKLOG_FORMAT.md#2-status-lives-in-two-places-on-purpose)

### observation shelf

- **Met in:** code, rules
- **Meaning:** The `DONE — AWAITING OBSERVATION` section: code written and not yet seen working in production. An entry leaves it only by recording an observation.
- **Defined in:** [BACKLOG_FORMAT.md §2](BACKLOG_FORMAT.md#2-status-lives-in-two-places-on-purpose)

### observation debt

- **Met in:** code, rules
- **Meaning:** The number of entries on the observation shelf, printed per axis in the generated ROADMAP block.
- **Defined in:** [ROADMAP.md — Status by axis](../ROADMAP.md#status-by-axis)

### resolution

- **Met in:** code, rules
- **Meaning:** Why an entry left the working file: `fixed`, `disproved`, `moot`, `superseded`, `duplicate`, `wontfix`; each requires its own kind of evidence in the closure line.
- **Defined in:** [BACKLOG_FORMAT.md §3](BACKLOG_FORMAT.md#3-closing-an-entry)

### closure line

- **Met in:** code, rules
- **Meaning:** `**Closed:** S<session> · YYYY-MM-DD · <resolution> · <evidence>`, written when an entry moves to `backlog_closed.md`; never backfilled onto old entries.
- **Defined in:** [BACKLOG_FORMAT.md §3](BACKLOG_FORMAT.md#3-closing-an-entry)

### session identifier

- **Met in:** code, rules
- **Meaning:** `S<YYYY-MM-DD>.<N>`: the UTC date a session opened and its position among that day's sessions, derived from the group's reset archive. Three older numbering families survive in old text and are not renumbered.
- **Defined in:** [BACKLOG_FORMAT.md §3](BACKLOG_FORMAT.md#the-session-identifier)

### stale reference

- **Met in:** code, rules
- **Meaning:** A name-shaped token in an entry's body that resolves to no entry in either backlog file; reported, never refused, and an upper bound on real breakage.
- **Defined in:** [BACKLOG_FORMAT.md §8](BACKLOG_FORMAT.md#8-the-check)

### generated block

- **Met in:** code, rules
- **Meaning:** The span of ROADMAP.md between the `generated by` markers, rendered from ADR front matter and the board by `build.py --roadmap`; a hand edit inside it is refused by the checker.
- **Defined in:** [ROADMAP.md — Status by axis](../ROADMAP.md#status-by-axis)

### Observed / Inferred

- **Met in:** code, rules
- **Meaning:** The split every claim about cause has to make: facts anchored to a log, a file or a measurement, and the hypothesis with the check that would confirm it. An unverified inference is a hypothesis, whatever grammar it is written in.
- **Defined in:** [protocol-13-public.md §11 — Evidence Discipline](../protocol-13-public.md#evidence-discipline), [BACKLOG_FORMAT.md §1](BACKLOG_FORMAT.md#1-the-entry)

## People, agents, and how they work together

### Protocol 13

- **Met in:** rules
- **Meaning:** The operating agreement for a team of one human coordinator, one execute agent and one review agent: roles, interaction patterns, communication norms and the decision-artifact layers.
- **Defined in:** [protocol-13-public.md](../protocol-13-public.md)

### Human Coordinator

- **Met in:** rules
- **Meaning:** The decision-maker: provides direction, approves scope, relays between agents, resolves disagreements.
- **Defined in:** [protocol-13-public.md §2](../protocol-13-public.md#human-coordinator)

### Execute Agent

- **Met in:** rules
- **Meaning:** Writes code, runs tests, commits; decides *how* given a clear *what*; never approves its own work.
- **Defined in:** [protocol-13-public.md §2](../protocol-13-public.md#execute-agent)

### Review Agent

- **Met in:** rules
- **Meaning:** Writes design rationale and reviews outcomes; works at the level of *why*; flags what is wrong and leaves *how* to the Execute Agent.
- **Defined in:** [protocol-13-public.md §2](../protocol-13-public.md#review-agent)

### explicit action (DDA)

- **Met in:** rules
- **Meaning:** Work starts only after an explicit action verb; "agree", "fine", "makes sense" are not authorisation, and ambiguous consent is no consent.
- **Defined in:** [protocol-13-public.md §11](../protocol-13-public.md#explicit-action-required-dda)

### interaction pattern (A–E)

- **Met in:** rules
- **Meaning:** The five named ways the three participants exchange work: collaborative design, standard execution, direct dialogue, automated @mention routing, and the external agent's monitoring loop.
- **Defined in:** [protocol-13-public.md §3](../protocol-13-public.md#3-interaction-patterns)

### sprint log

- **Met in:** rules
- **Meaning:** Temporary working memory for the current team in `sprint-logs/`, four sections (design rationale, implementation notes, review findings, decisions made), extracted to ADRs and archived.
- **Defined in:** [protocol-13-public.md §5](../protocol-13-public.md#layer-2--sprint-logs)

### agent

- **Met in:** code, rules, screen
- **Meaning:** An embedded LLM participant that runs on one node with its own storage under `~/.dpc/agents/<id>/`, tools, memory and permission profile; addressed by display name.
- **Defined in:** [agent/DPC_AGENT_GUIDE.md](agent/DPC_AGENT_GUIDE.md#architecture)

### agent profile

- **Met in:** code, rules, screen
- **Meaning:** The per-agent block in `privacy_rules.json` that decides which tools the agent may use and which paths it may reach; an agent's own profile beats the global `dpc_agent` block.
- **Defined in:** [agent/DPC_AGENT_GUIDE.md — Tool Access Control](agent/DPC_AGENT_GUIDE.md#tool-access-control)

### sandbox

- **Met in:** code, rules, screen
- **Meaning:** The directory an agent may read and write by default, `~/.dpc/agents/<id>/`; everything else needs an extended path.
- **Defined in:** [agent/DPC_AGENT_GUIDE.md — Extended Sandbox Paths](agent/DPC_AGENT_GUIDE.md#extended-sandbox-paths)

### extended sandbox path

- **Met in:** code, rules, screen
- **Meaning:** A directory outside the sandbox granted to an agent as read-only or read-write in its profile.
- **Defined in:** [agent/DPC_AGENT_GUIDE.md — Extended Sandbox Paths](agent/DPC_AGENT_GUIDE.md#extended-sandbox-paths)

### shell tier (0/1/2)

- **Met in:** code, rules, screen
- **Meaning:** The three-way classification of a `run_shell` command: Tier 0 runs at once, Tier 1 waits for a person's approval in the chat, Tier 2 is hard-blocked and not overridable by config or agent. Not the same word as a connection tier.
- **Defined in:** [decisions/030 — Tier Model](decisions/030-run-shell-safety-guardrails.md#tier-model)

### CDN refusal

- **Met in:** code, rules
- **Meaning:** A record that a page tried to reach a host and the route gate refused it — either because the site's CDN manifest does not name that host (`unlisted`) or on method or navigation before the manifest is read (`blocked`) — written per agent to `~/.dpc/agents/<id>/web_cdn_refusals.json`. Evidence only: the gate reads the manifest and never the refusals, and a host becomes allowed solely when a human promotes it. Deliberately not called an *observation*; that word is the board's shelf.
- **Defined in:** [decisions/028 §3](decisions/028-agent-web-auth-cookie-sharing.md#3-the-route-gate-now-constrains-the-destination-not-only-the-initiator)

### CDN manifest

- **Met in:** code, rules
- **Meaning:** `~/.dpc/web_cdn_manifest.json`, node level and outside every agent's sandbox: per site, the exact hosts its pages may pull subresources from. The authority the route gate reads, grown only by a human promoting CDN refusals into it.
- **Defined in:** [decisions/028 §3](decisions/028-agent-web-auth-cookie-sharing.md#3-the-route-gate-now-constrains-the-destination-not-only-the-initiator)

### external agent

- **Met in:** code, rules
- **Meaning:** A second LLM session running outside DPC — Claude Code or any harness — that reads a conversation from disk and posts back over the local WebSocket API through the bridge scripts. It can belong to this node or to a peer; a group's participant list says which (`[external agent on peer X]`).
- **Defined in:** [agent/EXTERNAL_HARNESS_INTEGRATION_GUIDE.md](agent/EXTERNAL_HARNESS_INTEGRATION_GUIDE.md#what-the-external-agent-sees-what-it-does)

### tag

- **Met in:** code, rules, screen
- **Meaning:** The name an external agent answers to in a group, registered per node in Group Settings and stored in the group's metadata; in a 1:1 chat there is no registration, and the name is the bridge's `--as`, else the configured display name.
- **Defined in:** [agent/EXTERNAL_HARNESS_INTEGRATION_GUIDE.md — Which name it answers to](agent/EXTERNAL_HARNESS_INTEGRATION_GUIDE.md#which-name-it-answers-to)

### bridge

- **Met in:** code, rules
- **Meaning:** One of the two helper scripts an external agent uses: the agent-chat bridge for 1:1 chats and the group bridge for groups.
- **Defined in:** [agent/EXTERNAL_HARNESS_INTEGRATION_GUIDE.md — The bridge scripts](agent/EXTERNAL_HARNESS_INTEGRATION_GUIDE.md#the-bridge-scripts)

### mention

- **Met in:** code, rules, screen
- **Meaning:** `@Name` in a message routes it to that agent; `@all` fans out to every agent in the group and is human-only; fenced code is stripped before matching.
- **Defined in:** [GROUP_CHAT.md — Mention routing](GROUP_CHAT.md#mention-routing)

### reasoning effort

- **Met in:** code, rules, screen, wire
- **Meaning:** One word from the shared scale `off / low / medium / high / max` that a caller asks a model in. A provider with an **effort channel** folds the word onto a **rung** of its own ladder; a provider without one refuses every word, `off` included, because a class that cannot ask for thinking cannot say no to it either. The scale is two constants on purpose: `off` is deliberately not a member of `REASONING_EFFORTS`, since it is not an amount of thinking, and code that iterates the levels must not offer it as one.
- **Defined in:** [`providers/base.py` `REASONING_EFFORTS` / `REASONING_OFF`](../dpc-client/core/dpc_client_core/providers/base.py), [specs/dptp_v1.md §3.4](../specs/dptp_v1.md#34-remote-ai-inference)

### provider alias

- **Met in:** code, rules, screen, wire
- **Meaning:** The name of one configured model endpoint in `~/.dpc/providers.json`; agents, chats and roles name a provider by its alias. A **usage row** calls the same thing `alias`, and an agent's runtime budget calls it `provider_alias`.
- **Defined in:** [docs/CONFIGURATION.md — AI providers](CONFIGURATION.md#ai-providers-providersjson)

### agent alias role

- **Term note:** (`alias_roles`, `missing_aliases`)
- **Met in:** code, screen
- **Meaning:** One of the five places an agent's `config.json` names a **provider alias**: main (`provider_alias`), sleep (`sleep_provider_alias`), snapshot (`snapshot_summarize_provider`), compaction (`compaction_provider`) and voice (`voice_provider_alias`, who transcribes the person's voice for this agent; always runs on this node). `list_agents` gives each agent `alias_roles` (role to alias) and `missing_aliases` (the roles whose alias is no longer in `providers.json`); the agents panel marks an agent "not available now" only when main is missing and names the other missing roles in the tooltip. Not the people roles of `protocol-13-public.md`.
- **Defined in:** [provider_alias_refs.py](../dpc-client/core/dpc_client_core/provider_alias_refs.py)

### music_* tools

- **Term note:** (`music_capabilities`, `music_propose_job`, `music_check`, `music_wait`, `music_cancel_job`, `music_list_takes`, `music_list_library`, `music_get_lyrics`, `music_playlist_add`)
- **Met in:** code, rules
- **Meaning:** The agent tools over the music service, ten names with **chat_post_file**, each declared with a timeout and a wait budget, and each off by default (`default_enabled=False`, as the ComfyUI tools). `music_capabilities`, `music_check` (state and the `card_wait` holders), `music_list_takes`, `music_list_library`, `music_get_lyrics` and `music_playlist_add` wrap one service call each; `music_propose_job` makes one job per call and asks the owner's approval; `music_cancel_job` cancels only a job the agent proposed (**requested_by**); `music_wait` polls the job every 2 s with `timeout` 600 s by default and 900 s at most, `timeout_sec = timeout + 15` (mandatory, the per-call default of 120 s would cut it), and answers by the **self-wait rule**; a turn has 900 s of `music_wait` in all, then it is refused for the rest of the turn. `music_propose_job` has 100 s (the approval's 60 s plus the calls), the other seven 30 s each, and each service call 20 s. `schedule_task` comes with the music tools, for `check_back`; where only a music tool brings it (the firewall does not grant it) it is music-return-only: it takes nothing but a `check_back` naming `music_job_id` of a job the agent proposed that is still `queued` or `running`, and refuses every other task type. Such a return asks no approval in either mode, and core sets its delay (the job's ETA plus 25 s, or 180 s plus 25 s when the ETA is not known) and its wake text; the agent's own delay and text are not used. `music_propose_job` refuses before the card whatever core would refuse, a job core would make `unsupported` included, and a Stop while the card is up withdraws it (follow-up 12, built at `98ceed3a` and `94a6710b`, not observed live). Since ADR-053 they carry **owner_approval_for_others**, not **owner_only**: every call asked for by someone other than the owner shows the owner a card first, and each tool checks again per call as a second line; a take is reached by **take_id**. Their descriptions say the music-return-only rule (D49): without `schedule_task` the only call open is a `check_back` to the agent's own queued or running music job. The music job's card reads YuE2's time as an upper bound ("up to N s", its estimate is cap-based); a Stop or Kill after the person's "yes" makes no job. Built (ADR-051 D42, parts 0-3 and the follow-up), not observed live.
- **Defined in:** [decisions/051 — D42, the agent tools](decisions/051-the-music-player-is-a-dpc-window-over-one-core-music-service.md#decided-mikes-calls-2026-11-24-ai-studio-group-chat-the-agent-tools)

### chat_post_file

- **Met in:** code, rules
- **Meaning:** `chat_post_file(path | take_id, caption?)`, the agent tool that posts a file into the agent's own chat; for music, a take as an `audio/*` attachment, only when the person asks. A take is named by `take_id` instead of a path: core resolves it to the take's file (**take_id**), and the attachment carries `music: {track_id, take_id, job_id}`, `track_id` being the library entry id the player opens; `path` and `take_id` go one at a time. Limits: at most 4 files per turn; the file-transfer size cap `file_transfer_cap_mb(rules)` (100 MB by default) checked before the copy, and the file is copied, never moved; 1:1 agent chats only (`agent_*`), a group, Telegram or Discord turn is refused with a reason; the path goes through `_resolve_file_path` as `read_file` does, never a bare `validate_extended_path`; on a request of someone other than the owner it asks the owner's card first (**owner_approval_for_others**, ADR-053); only audio (flac wav mp3 ogg m4a), image (png jpg webp gif), pdf and plain text (txt md json csv) are posted, each checked by its first bytes as well as its name (follow-up 13; it replaced the deny-list of executables); a copy cancelled part way leaves no file; a take only of a finished job, kept and not hidden; the files a turn copied anew are removed when its answer is not written. Timeout 120 s, no wait budget. Built (ADR-051 D41; the follow-up 12 limits at `e2cef2da`), not observed live.
- **Defined in:** [decisions/051 — D41, the agent tools](decisions/051-the-music-player-is-a-dpc-window-over-one-core-music-service.md#decided-mikes-calls-2026-11-24-ai-studio-group-chat-the-agent-tools)

### requested_by

- **Met in:** code, wire
- **Meaning:** `requested_by {kind, agent_id, conversation_id, turn_id}` on the `mp.job/1` record: who asked for the job, from which chat and in which turn. It is what lets `cancel_music_job` refuse an agent a job it did not propose, or one proposed in another chat (follow-up 12, built at `98ceed3a`, not observed live), and what the turn cap and a turn's Stop count by; the person's own cancel in the UI is unchanged. A store written before the field must still open, since the schema is closed to undeclared keys. Built (ADR-051 D46), not observed live.
- **Defined in:** [decisions/051 — D46, the agent tools](decisions/051-the-music-player-is-a-dpc-window-over-one-core-music-service.md#decided-mikes-calls-2026-11-24-ai-studio-group-chat-the-agent-tools)

### asker / who asked

- **Term note:** (`asker`)
- **Met in:** code, rules, screen
- **Meaning:** The person or agent whose message started an agent's turn, as against the room it was written in. The **turn class** (ADR-048) comes from the room's members and decides what the agent may say; the asker decides what it may do (ADR-053). An asker belongs to a node, and a node is hardware plus its human plus its agents and the external agents running on it. The owner is this node's human (`ui`, `telegram_owner`), this node's agents and external agents, and the nodes in the agent's **`own_node_group`**; any other human or agent is foreign. Humans and agents are treated alike. A scheduled wake keeps the asker of the turn that scheduled it, and an agent woken by an own agent's post on this node inherits the asker of the turn that wrote it ("Miron via Ark", Q2). Decided by Mike, 2026-11-25 (AI Studio group chat #105, #108, #123, #124, #126, #128, #135). CC's derivation from the code (ADR-053 D-a, not Mike's words): on a peer's post the asker is read from the verified author node and the signed `sender_type`; a post that is not verified is a foreign asker of unknown kind, whatever node it claims. In the code: `dpc_agent.agent.Asker`, `ToolContext.asker`, `DpcAgent.resolve_asker`; the approval cards show it as "Asked by". Built on `dev` 2026-11-25, not observed live.
- **Defined in:** [decisions/053 — Decision, point 2](decisions/053-who-asked-decides-what-an-agent-may-do.md#decision)

### owner_approval_for_others

- **Met in:** code, rules, screen
- **Meaning:** A field on a tool's `ToolEntry`, beside **owner_only**, that sends every call of the tool to the owner as an approval card when the **asker** is not the owner. The card names the asker (name, node id, human or agent, "not verified", "via" a relaying own agent), the chat, the tool and what exactly, with resolved paths for `run_shell` and the file tools, and the GPU cost line for a tool that spends the card; 60 s without an answer is a refusal, and the agent is told the owner is not in focus. Checked in one place, `ToolRegistry.execute` (`owner_approval.open_gate`), so a tool added later cannot skip it; a registry test requires every tool to be flagged or named in its no-card table. Flagged: `run_shell` at every tier, `write_file`, every git write, `repo_delete`, `schedule_task`, `send_user_message`, the browser actions, the web fetchers `browse_page`, `fetch_json`, `check_url`, `search_web` (Mike #148) and `list_auth_domains`, by Mike's answer to Q3 (#138) `execute_skill`, `import_skill_from_agent`, `register_task_type`, `unregister_task_type`, `describe_image` and `transcribe_audio_file`, and the tools that were **owner_only** (`music_*`, ComfyUI, `chat_post_file`). A tool with its own card (a Tier 1 command, a `check_back`, a music job) shows that one card with the asker on it. At most one card is pending per chat, and at most two across the process (ADR-053 D-i); an approval is for one call, and one that comes after the call timed out starts nothing. Decided by Mike, 2026-11-25; built on `dev` the same day, not observed live.
- **Defined in:** [decisions/053 — Decision, point 2.5](decisions/053-who-asked-decides-what-an-agent-may-do.md#decision)

### owner_only

- **Met in:** code, rules
- **Meaning:** A field on a tool's `ToolEntry` that refuses the tool to every turn class but the owner's, written once and checked in `registry.execute` and when the offered tool list is built; each tool checks the class again as a second line. Refused: `other_node` and `stranger` (a peer, or a group with a foreign node in it); a group of only the person's own nodes is owner class. Owner sources: `ui`, `external_agent`, `telegram_owner`, so a bridge counts as the owner. There is no per-tool switch. With a refused turn no job file or file copy exists and no `music_*` schema is offered. Built (ADR-051 D43), not observed live. Since ADR-053 (built on `dev` 2026-11-25) no tool carries it: the tools that did ask the owner when someone else asked (**owner_approval_for_others**); the field and its filter stay for a later tool.
- **Defined in:** [decisions/051 — D43, the agent tools](decisions/051-the-music-player-is-a-dpc-window-over-one-core-music-service.md#decided-mikes-calls-2026-11-24-ai-studio-group-chat-the-agent-tools)

### take_id

- **Met in:** code, rules
- **Meaning:** The handle by which an agent names a take, resolved to the take's file by core; the agent gets no directory access to `~/.dpc/music/generated/` (a read path outside the **sandbox**, which would need a `sandbox_extensions` path), and the tools need no `sandbox_extensions` entry for it. A job's request JSON is not in `generated/` but in `music/jobs/<job>/job.json` and its `payload/` (Zcode's correction to D44). It also gives "only takes in `review`" a meaning: `chat_post_file` posts a take only of a job in `review` or `accepted`, not rejected, whose library entry is not hidden (follow-up 12, built at `e2cef2da`, not observed live). Built (ADR-051 D44; `chat_post_file` posts a take by it), not observed live.
- **Defined in:** [decisions/051 — D44, the agent tools](decisions/051-the-music-player-is-a-dpc-window-over-one-core-music-service.md#decided-mikes-calls-2026-11-24-ai-studio-group-chat-the-agent-tools)

### music job `variants`

- **Met in:** code, screen
- **Meaning:** `params.variants` of a music job: the number of takes one job makes, a whole number from 1 to 4, default 2 (`VARIANTS_MIN`, `VARIANTS_MAX`, `VARIANTS_DEFAULT`). Not the **variant** of a **menu row**, which is a build name.
- **Defined in:** [music/job_schemas.py — Limits per engine](../dpc-client/core/dpc_client_core/music/job_schemas.py)

### music QA gate

- **Met in:** code, screen
- **Meaning:** One of the four deterministic stage-1 checks of a music take: `length`, `clipping`, `true_peak`, `silence`. Each is `pass`, `fail` or `not_measured` in the take's `qa_report.gates`; the verdict is `failed` if any failed, `passed` only if all four passed, and `not_measured` otherwise, so an unmeasured gate is never a pass. A gate flags the take (the review's "QA failed: clipping" badge) and never rejects it. A quiet tail of 1-5 s is not a silence failure; the seam metrics and lyrics WER are advisory and are not gates.
- **Defined in:** [music/qa.py — the gates and why these numbers](../dpc-client/core/dpc_client_core/music/qa.py)

### qa_report

- **Met in:** code, wire
- **Meaning:** The take's field beside `qa`: `{stage, verdict, gates, advisory, measurements, thresholds, measured_by, measured_at}`, the stage-1 gates with the numbers that judged them. It exists because **mp.qa/1** has no room for a pass or fail. The runner writes one for every take it makes; a failed measurement is a report whose verdict is `not_measured` with the reason, and a take made before stage-1 QA has none.
- **Defined in:** [music/job_schemas.py — `mp.take/1.qa_report`](../dpc-client/core/dpc_client_core/music/job_schemas.py)

### mp.qa/1

- **Met in:** code, wire
- **Meaning:** The SPEC §8 record of the measured numbers of a take (`integrated_lufs`, `true_peak_dbtp`, `loudness_delta_lu`, the three seam fields, `clipped_samples`, `lyrics_wer`, `measured_by`): every field required, no others. A take's `qa` is exactly this or null; it is null when a required number (loudness, true peak, clipped samples) could not be measured, never filled with a made-up one. The edit-only fields and `lyrics_wer` are null for a new take. The loudness-only `loudness` record is not an `mp.qa/1`.
- **Defined in:** [music/job_schemas.py — `check_qa`](../dpc-client/core/dpc_client_core/music/job_schemas.py), [decisions/051 — stage-1 QA row of the Implementation Status](decisions/051-the-music-player-is-a-dpc-window-over-one-core-music-service.md)

## Memory, knowledge, and sleep

### Active Recall

- **Met in:** code, rules, screen
- **Meaning:** Cross-layer retrieval that injects the most relevant knowledge into an agent's prompt without an explicit tool call: FAISS and BM25 fused, budget-aware. Its own switch is `memory.active_recall` in the agent's profile in `privacy_rules.json` (on screen "Active Recall"), and it works only while the **memory system** is on; `memory.max_recall_results` (1–10, default 3) is how many hints a turn gets. Both are read on each turn.
- **Defined in:** [decisions/010 — Embeddings Active Recall](decisions/010-agent-memory-architecture.md#component-2-embeddings-active-recall--cross-layer-retrieval), [agent/ACTIVE_RECALL.md](agent/ACTIVE_RECALL.md)

### memory system

- **Term note:** (`memory.enabled`)
- **Met in:** code, rules, screen
- **Meaning:** What `memory.enabled` in an agent's own profile in `privacy_rules.json` (`agent_profiles.<id>.memory`) switches, on screen "Enable Memory System": the agent's memory index, the `memory_search` tool and **Active Recall** together. Off, recall injects nothing and `memory_search` answers that memory is off for this agent; the setting is read on each turn, and an agent with no profile of its own has it off (the code default, not the global `dpc_agent` block; Mike's call, 2026-11-07).
- **Defined in:** [`memory_config.py`](../dpc-client/core/dpc_client_core/dpc_agent/memory_config.py)

### message source

- **Term note:** (`message_source`, `is_owner_caller`, `TELEGRAM_OWNER_MESSAGE_SOURCE`)
- **Met in:** code, rules
- **Meaning:** Where an agent turn came from, named by the door that starts it: `ui` (the owner's chat and screenshots, the Task Board, the local `dpc_agent` provider), `external_agent` (the CC bridge), `group`, `telegram`, `telegram_owner` or `discord`. `telegram_owner` is a turn of an agent's own Telegram bot whose every message came from that agent's **`telegram_owner_user_id`** in a private chat with the bot; any other turn of that bot, and every turn of the main `[telegram]` bot, is `telegram`. The string lives in one constant, `TELEGRAM_OWNER_MESSAGE_SOURCE` in `agent.py`. `is_owner_caller` in `agent.py` names the owner's own doors: `ui`, `external_agent` and `telegram_owner`. A `group` turn is the owner's only when its members are (see **`own_node_group`**); otherwise it sees only the layers marked shared in **`layer_audience`**. `telegram`, Discord and a turn that names no source see no memory layer: no **Active Recall** hints, a one-line `memory_search` refusal, no memory sections in the prompt, nothing in the agent's directory but the turn's own tool results, and a Discord turn also reads nothing outside it (ADR-048). A scheduled task runs with the source of the turn that scheduled it, never one named in its own payload. The same word in the gateway (`is_owner_caller=True`, ADR-041) means the owner's own loopback client. Since 2026-11-08 the history records it too: each user record an agent turn writes, and the assistant record that answers it, carry `message_source` (the legacy `source: "telegram"` stays beside it), and a turn CC carries after an `@` mention takes the source of the records since CC's previous message, `external_agent` only when all of them are the owner's (an unmarked record with `source: "telegram"` counts as not). Mike's names, 2026-11-07; `telegram_owner` Mike's word, 2026-11-08; the history records and CC's turn, Mike's call, 2026-11-08 (review by Zcode, Johnny, Ark).
- **Defined in:** [`agent.py`](../dpc-client/core/dpc_client_core/dpc_agent/agent.py)

### `telegram_owner_user_id`

- **Met in:** code, rules, screen
- **Meaning:** On an agent's row in `~/.dpc/agents/_registry.json`, beside `telegram_allowed_chat_ids`: the owner's own Telegram user id, digits only, set in Edit Telegram Configuration as "Your Telegram user ID (owner)" (command parameter `owner_user_id` of `link_agent_telegram`). A message to that agent's bot counts as the owner's only when it comes from this user id and from a private chat with the bot (whose chat id is that user id); a group chat with the owner in it does not, and a **burst** counts only when every message in it does. Such a turn's **message source** is `telegram_owner`. Absent or empty: no Telegram turn is the owner's. Anything but digits is refused on save; Unlink clears it. A chat id says only where a message was written, which is why the user id decides. Mike's name and its per-agent place, 2026-11-07. It also decides who may press the bridge's vote and approval buttons (knowledge vote, shell, schedule): with it set only that user, in any allowed chat; empty, only a private chat with the presser, never a group. Mike's call, 2026-11-08. With `telegram_unified_conversation` on, only such a turn joins the agent's 1:1 conversation with the owner; every other Telegram turn runs in the chat's own `telegram-<chat_id>` conversation, so with the field empty all of them do. Mike's call, 2026-11-08 (review by Zcode, Johnny, Ark).
- **Defined in:** [`agent_telegram_bridge.py`](../dpc-client/core/dpc_client_core/managers/agent_telegram_bridge.py), [`utils.py`](../dpc-client/core/dpc_client_core/dpc_agent/utils.py)

### `telegram_chat_id`

- **Term note:** (on a history record)
- **Met in:** code
- **Meaning:** On a user record an agent's Telegram bot wrote: the chat the message came from, as the bot's parameter of that name. Its reader is the reply routing: CC's reply to an `@CC` message and the agent's answer in a CC chain go to the chat recorded on the message that woke them, never to the first of `telegram_allowed_chat_ids`; with none recorded, or a chat no longer allowed, nothing is sent and the log says so in one line. Mike's call, 2026-11-08 (review by Zcode, Johnny, Ark).
- **Defined in:** [`service.py`](../dpc-client/core/dpc_client_core/service.py), [`agent_telegram_bridge.py`](../dpc-client/core/dpc_client_core/managers/agent_telegram_bridge.py)

### `resolved_via`

- **Met in:** code
- **Meaning:** On a closed shell approval, where it was closed: `desktop` (the approval dialog, through the WebSocket commands `shell_approve_command` / `shell_reject_command`), `telegram` (the bridge's Yes/No buttons, through `resolve_shell_approval`), `timeout` (the agent stopped waiting) or `superseded` (the other surface had answered before the request arrived). The first two are decisions and the last two are closures. It sits beside `resolution` (`approved`, `rejected`, `expired`, `superseded`) and has three readers: the `shell_approval_resolved` payload (and the UI log line from it), the edit that withdraws the Telegram message when the other surface decided ("✅ Approved on the desktop.", "❌ Rejected from Telegram."), and the agent's `run_shell` result ("Approved by <owner> from Telegram." before the output, "Command rejected by <owner> on the desktop."). A client cannot set it: the desktop commands fix it to `desktop` and take no such parameter, and `resolve_shell_approval`, which takes it, is not in `ALLOWED_COMMANDS`. The queue-approval (schedule) path has no such field yet. Mike's word, 2026-11-08; the values Mike's call, 2026-10-31.
- **Defined in:** [`service.py`](../dpc-client/core/dpc_client_core/service.py), [`agent_telegram_bridge.py`](../dpc-client/core/dpc_client_core/managers/agent_telegram_bridge.py)

### model download consent

- **Met in:** code, rules, screen, wire
- **Meaning:** The `model_download_required` / `_started` / `_progress` / `_completed` / `_failed` event family: one dialog shape for any large download this node may fetch — the models (Whisper, the `BAAI/bge-m3` embedding model) and, since 2026-10-21, the Camoufox browser binary under the name `camoufox`, and since 2026-11-01 the engines' pins under the names `llama.cpp` and `strata` — asked at most once per process session per name, and never downloaded without it: `EmbeddingProvider` and `LocalWhisperProvider` raise `ModelNotCachedError` on a cache miss, every browser launch refuses while the binary is missing, and an engine start with no pin raises `PinNotInstalledError` (the engines' refusal: never retried, and a peer's call neither asks nor fetches), instead of fetching silently. For these non-Hugging-Face downloads the `required` payload also carries `free_bytes` (free space on the destination's volume) and `installed_bytes` (what the download occupies once unpacked), and the download is refused while `free_bytes` is under (`size_bytes` + `installed_bytes`) × 1.1, where an absent `installed_bytes` reads "not stated" and never 0, and an unknown `free_bytes` refuses nothing but is never shown as enough. `size_bytes` is always the download and `installed_bytes` what it occupies; neither is folded into the other, and an absent one reads "not stated", never 0. `size_source` is `measured` or `hf_api` for a model and null for an exact size, except `package_max` for `strata`: its engine package is the one Strata's updater picks by the card, so `size_bytes` and `installed_bytes` count the largest package of the platform listed in the pinned release's `strata-feed.json`, shown "up to", and the payload adds `size_bytes_min` / `installed_bytes_min` for the smallest; an alias whose **engine backend** names exactly one package (`hip-<build>`, `metal`, `sycl`) is asked with that package's exact sizes and `size_source` `package` (no minima), and `cuda`, which still leaves CUDA 12 or 13 to Strata, with the largest cuda package and `package_max`; DPC refuses only a download that cannot fit even the smallest package, and between the two the dialog warns without refusing (its "May not fit" line). The exact check is Strata's own: its updater, before it downloads, refuses a package whose download plus unpacked size the volume cannot hold, and DPC reports that as "not enough free disk space for the Strata engine" with Strata's figures. The progress counts against the exact package size from the updater's first progress line. The events keep their `model_download_*` names for the binary too. The name `strata-model` is one Strata model installed through Strata's own `setup.py`: the command `install_strata_model` only raises this dialog, the owner's confirm runs the install, and it is asked again for every choice, whatever "once per session" or an earlier "don't remind me" says. Its `required` payload adds `strata_model` = `{family, model, images, gguf_dir}` (`gguf_dir` null when absent), the choice the confirm does not carry back, which the service keeps in `_last_required_payload`. Its `size_source` is `model_table` (a new value): Strata's own rounded `download_gb` for the size, shown "about", and 0 when a `gguf_dir` holds the files, which Strata takes in place; its `installed_bytes` is not stated. Its `model_download_completed` adds `run_config`, the absolute path of the `strata-<id>.json` that setup wrote. `model_download_progress` (`bytes_done`, `total_bytes`, `progress_percent`, the last two null when unknown) is sent at most once per `PROGRESS_MIN_INTERVAL_S` (0.5 s), Mike's word, 2026-10-21. Only an explicit "don't remind me" persists, to `[model_downloads]` in `config.ini`. Mike's call, 2026-09-24, replacing Whisper's own dialog and the embedding model's silent download; widened by Mike, 2026-10-21.
- **Defined in:** [`model_download_service.py`](../dpc-client/core/dpc_client_core/model_download_service.py), [`strata_model_fetcher.py`](../dpc-client/core/dpc_client_core/managers/strata_model_fetcher.py)

### memory layers (L1–L7)

- **Met in:** code, rules
- **Meaning:** The seven layers agent memory spans, from L1 strategy documents through L5 agent-written knowledge and L6 human-committed knowledge to L7 the human; retrieval priority is L6 > L1 > L5 > L2-docs. For who may see them, three count: L5 (the agent's `knowledge/` and `memory/` folders), L6 and EXT (the granted extended paths); a row's layer is read from its index key, so a graph hit labelled L7 counts as the layer of the document it came from (**`layer_audience`**).
- **Defined in:** [decisions/010 — Layer Connection Map](decisions/010-agent-memory-architecture.md#layer-connection-map), [decisions/048](decisions/048-memory-layer-audience.md)

### `layer_audience`

- **Term note:** (`private`, `shared`)
- **Met in:** code, rules
- **Meaning:** `agent_profiles.<id>.memory.layer_audience` in `privacy_rules.json`: who sees each memory layer, `{"L5": "private", "L6": "private", "EXT": "private"}` by default; an absent field or key keeps its default, and `firewall.validate_config` refuses any other key or value. `private` is the owner's turns only (his doors and groups of his own nodes); `shared` is also a group with another person's node; a stranger's turn (Telegram other than the owner's chat, Discord, no source) sees no layer whatever the mark. L5 includes `memory/`, so closing it changes how the agent behaves, not only what it tells: on such a turn it has no Scratchpad, Identity, Morning Brief, Dialogue Summary or knowledge index, and does not write them. `private` here is not `personal_context_access`, which governs `personal.json`; `personal.json` is read on the owner's turns only. L6 was `shared` until 2026-11-09: it is the owner's curated knowledge and team knowledge was to be ADR-049's store (ADR-049 was reserved and never written, see [ADR-053](decisions/053-who-asked-decides-what-an-agent-may-do.md), Decision 5; open board card A-GROUP-HAS-NO-AUDIENCE-OF-ITS-OWN-SO-ITS-PRIVACY-FOLLOWS-ITS-ROSTER, `backlog.md:1478`), so with default marks a group with another node opens no layer. Mike's call, 2026-11-08 (review by Zcode, Johnny, Ark), L6 default Mike's call (a), 2026-11-09 (review by Zcode, Johnny); the field and the values, Mike's words, 2026-11-08.
- **Defined in:** [decisions/048](decisions/048-memory-layer-audience.md), [`agent.py`](../dpc-client/core/dpc_client_core/dpc_agent/agent.py)

### `own_node_group`

- **Met in:** code, rules
- **Meaning:** `agent_profiles.<id>.memory.own_node_group` in `privacy_rules.json`: the name of a group in the top-level `node_groups` that lists the owner's own nodes (his Linux node, an old PC). A chat group whose every member is this node or a node of that group, and which is not a Discord bridge, counts as the owner; an empty roster or a group that cannot be found never does. Absent means no own nodes besides this one. Save refuses a name that is not in `node_groups`, and at startup each agent logs one line naming the group it uses (or why none) and how every chat group resolves. The roster it reads has no owner (GROUP-ROSTER-HAS-NO-OWNER): a member list a peer changed changes the answer. Mike's call (a2), 2026-11-08 (review by Zcode, Johnny, Ark); the name, Mike's word, 2026-11-08.
- **Defined in:** [decisions/048](decisions/048-memory-layer-audience.md), [`agent.py`](../dpc-client/core/dpc_client_core/dpc_agent/agent.py)

### session archive

- **Term note:** (`session_archive`)
- **Met in:** code, rules
- **Meaning:** One closed session of a conversation, kept as `~/.dpc/conversations/<conversation>/archive/<YYYY>/<MM>/<timestamp>_<reason>_session.json` when the conversation is reset or a new session is voted; it never leaves the node. An archive of a group conversation carries `group` (`group_id`, `members`, `version`, copied from the group's metadata at that moment), so a later reader knows who witnessed the session; a 1:1 archive has no such key, and a group whose metadata cannot be read gets none and one log line. The agent reads archives through `read_session_archive`, `read_session_detail` and `search_session_archives`: a stranger only its own `telegram-<chat id>` conversation; the owner's turn the current conversation and, with **`archive_include_groups`**, the groups this agent is in; a turn in a group with another node that group only, and only sessions whose recorded `members` include every current member (the answer counts the rest in one line). Every result names its `conversation_id`, and `read_session_detail` opens a file only inside the conversation it names, because filenames repeat across conversations. `search_session_archives` ranks from a BM25 index of each conversation's sessions kept beside them in `archive/bm25/`: derived data on this node only, rebuilt from the session files, removed with the archive; every reader of an archive folder globs `*_session.json`. The name, Mike's word, 2026-11-07; the rule, Mike's call, 2026-11-09 (review by Johnny and Zcode); the index location, Mike's call, 2026-11-09.
- **Defined in:** [decisions/048 — Session archives](decisions/048-memory-layer-audience.md#session-archives), [`archive.py`](../dpc-client/core/dpc_client_core/dpc_agent/tools/archive.py)

### `archive_include_groups`

- **Met in:** code, rules, screen
- **Meaning:** `agent_profiles.<id>.history.archive_include_groups` in `privacy_rules.json`, beside `preserve_on_reset`, read the same way (the profile, then the global `dpc_agent.history`); false by default, and `firewall.validate_config` accepts a bool only. Shown as "Include group sessions" in Agent Permissions → Session History. True lets the owner's turns read the **session archives** of every group conversation whose `metadata.json` agents map lists this agent under this node; it is the agent's side of the permission, the agents map the group's, and both must allow. A group with no `metadata.json`, an unreadable one or an empty agents map is not included. It changes nothing on a turn that is not the owner's. The name, Mike's word, 2026-11-07; the rule, Mike's call, 2026-11-09 (review by Johnny and Zcode).
- **Defined in:** [decisions/048 — Session archives](decisions/048-memory-layer-audience.md#session-archives), [`firewall.py`](../dpc-client/core/dpc_client_core/firewall.py)

### knowledge commit

- **Met in:** code, rules, screen, wire
- **Meaning:** A structured, attributed, versioned unit of knowledge extracted from a conversation, proposed, reviewed, voted on and committed; the human owns the extraction.
- **Defined in:** [KNOWLEDGE_ARCHITECTURE.md §6](KNOWLEDGE_ARCHITECTURE.md#6-knowledge-commit-protocol), [protocol-13-public.md §6](../protocol-13-public.md#6-knowledge-architecture)

### consensus / devil's advocate

- **Met in:** code, rules, screen, wire
- **Meaning:** Multi-party voting on a knowledge commit, counted over the participants the proposal names — never over the votes that arrived; with three or more participants one is required to dissent. A participant that cannot judge answers `abstain` with a reason, and a deadline never approves.
- **Defined in:** [KNOWLEDGE_ARCHITECTURE.md §4.3](KNOWLEDGE_ARCHITECTURE.md#43-consensus-with-required-dissent)

### sleep consolidation

- **Met in:** code, rules, screen
- **Meaning:** An on-demand pipeline that reads the previous sessions' archives one at a time, extracts findings, writes them into the knowledge graph and produces a morning brief; it replaced the background "consciousness" worker and the autonomous evolution loop.
- **Defined in:** [decisions/014](decisions/014-sleep-consolidation-architecture.md), [protocol-13-public.md §7](../protocol-13-public.md#7-between-sessions)

### morning brief

- **Met in:** code, rules
- **Meaning:** The output of a sleep cycle that the next session loads as context; in a group, each agent posts its own brief into the chat.
- **Defined in:** [decisions/014](decisions/014-sleep-consolidation-architecture.md), [GROUP_CHAT.md — Group Sleep](GROUP_CHAT.md#group-sleep--morning-briefs)

### month summary

- **Term note:** (`month_summary_*.json`, `MONTH_SUMMARY_PROMPT`, `MONTH_SUMMARY_CEILING_SHARE`)
- **Met in:** code, rules
- **Meaning:** When the per-session findings of a sleep do not fit one synthesis prompt, one model call condenses each calendar month's raw findings (never other summaries) into a month summary, and the synthesis reads those in date order instead of the findings. A month larger than the ceiling (half the prompt limit less the map template) is split into consecutive parts. It is cached in `sleep_results/` under a hash of exactly its successful findings; a month with an errored session is not frozen. The brief's `period`, counts and last date come from raw sessions, not from it.
- **Defined in:** [decisions/014](decisions/014-sleep-consolidation-architecture.md)

### compaction

- **Met in:** code, rules, screen
- **Meaning:** Summarising aged tool results inside an agent run once context usage crosses a threshold, so a long run keeps fitting; it shrinks tool rounds, not the conversation. With compaction off the loop cuts old tool results to short notes instead, after `OFF_ROUND_BOUND` uncut rounds or at `UNDER_PRESSURE` of the window, keeping the last rounds as they are; the progress strip shows the run's last cut (`last_context_cut`).
- **Defined in:** [decisions/033](decisions/033-agent-tool-loop-llm-compaction.md#decision)

### skill

- **Met in:** code, rules, screen, wire
- **Meaning:** A static `SKILL.md` strategy file an agent can execute, share with peers and import; imports fork from their origin.
- **Defined in:** [agent/DPC_AGENT_GUIDE.md — Skills](agent/DPC_AGENT_GUIDE.md#skills--agent-discovery-tools)

### knowledge base

- **Met in:** rules
- **Meaning:** What a user's document collection becomes once the **document pipeline** has read it: one record per unit, carrying the verdict on what was read and how, indexed and citable. Mike's word, 2026-09-22, against *library*, which is taken twice over here — the cryptography sense in this source tree and the user's own books on disk. The contour of the layers beyond the record is dpc-library's `docs/knowledge-flow.md`, which settles nothing until a decision stands on it.
- **Defined in:** [decisions/043 — Open Questions](decisions/043-one-reader-now-the-module-in-dpc-messenger-later.md#open-questions)

### document pipeline

- **Met in:** rules
- **Meaning:** The stages that turn a source file into records — scan, classify, route, read, record, summary — reading through the one reader `read_document` uses, so a refusal rate is a number about the corpus and not about which of two readers ran. Accepted by Mike on 2026-09-22 as the name of both the stages and the package that will hold them, `document_pipeline`; the pilot runs in dpc-library until the user flow is known.
- **Defined in:** [decisions/043 — The eventual home](decisions/043-one-reader-now-the-module-in-dpc-messenger-later.md#the-eventual-home)

## Nodes, peers, and the wire

### node

- **Met in:** code, rules, screen, wire
- **Meaning:** One running D-PC client with its own cryptographic identity: a human and their agents on one machine. A container does not change that: one machine runs one node, and a container is a way to run it, never a way to run several (Mike's call, 2026-11-01). Not the XML or DOM sense of the word used in other codebases.
- **Defined in:** [specs/dptp_v1.md §4](../specs/dptp_v1.md#4-node-identity-system)

### node_id

- **Met in:** code, rules, screen, wire
- **Meaning:** `dpc-node-` plus the first 32 hex characters of the SHA-256 of the node's RSA public key; the certificate's Common Name and the key to everything that names a node.
- **Defined in:** [specs/dptp_v1.md §4](../specs/dptp_v1.md#node-id-format)

### peer

- **Met in:** code, rules, screen, wire
- **Meaning:** Another node this node talks to over DPTP; its certificate is stored only after its key re-hashes to its claimed `node_id`.
- **Defined in:** [specs/dptp_v1.md §4](../specs/dptp_v1.md#identity-storage)

### DPTP

- **Met in:** code, rules, wire
- **Meaning:** D-PC Transfer Protocol: a 10-byte ASCII length header plus a JSON payload, carried over TLS, WebRTC data channels or DTLS.
- **Defined in:** [specs/dptp_v1.md §1–2](../specs/dptp_v1.md#1-overview)

### HELLO

- **Met in:** code, rules, wire
- **Meaning:** The first DPTP message on a connection, carrying the sender's `node_id` and display name. On direct TLS only the dialler sends one, in answer to the listener's HELLO_CHALLENGE, and proves its key with a **HELLO preimage** signature; the listener answers HELLO_ACK. On Hub WebRTC it is a lighter, unsigned display-name exchange.
- **Defined in:** [specs/dptp_v1.md §3.1](../specs/dptp_v1.md#31-connection-establishment)

### HELLO preimage

- **Met in:** code, rules, wire
- **Meaning:** What a direct-TLS HELLO signature covers (`hello_preimage`, `hello_content_hash` in `dpc_protocol/message_signing.py`): the tag `HELLO_PREIMAGE_VERSION` (`dptp-hello-v1`), the challenge nonce in base64 (exactly `HELLO_NONCE_BYTES`, 32), the listener's `node_id` and the dialler's, length-prefixed as a message preimage is. The dialler signs its hex SHA-256 with `CommitSigner.sign_commit` and sends it as `hello_signature` beside `hello_preimage_version`; it never signs the nonce it was handed. Replaced `nonce_signature` in DPTP 1.11, a hard cut. Mike's go, 2026-10-27.
- **Defined in:** [specs/dptp_v1.md §3.1](../specs/dptp_v1.md#31-connection-establishment)

### `MAX_HELLO_FRAME_BYTES`

- **Met in:** code, rules
- **Meaning:** The largest frame either side reads before the peer has proved its key (HELLO_CHALLENGE, HELLO, HELLO_ACK): 64 KiB, beside `MAX_FRAME_BYTES` (64 MiB) in `dpc_protocol/protocol.py`. A real HELLO with its certificate is 1.7–1.9 KB; a larger declared length is refused at the header without reading the body and counts as a failed HELLO. After HELLO_ACK the frame cap is `MAX_FRAME_BYTES` again. Mike's go, 2026-10-27.
- **Defined in:** [specs/dptp_v1.md](../specs/dptp_v1.md)

### connection tier (1–6)

- **Met in:** code, rules
- **Meaning:** One of six strategies the orchestrator tries in order — IPv6 direct, IPv4 direct, Hub WebRTC, UDP hole punching, volunteer relay, gossip store-and-forward. "Complete" in the roadmap means the code shipped, not that the tier carries traffic.
- **Defined in:** [docs/ARCHITECTURE.md — Connection tiers](ARCHITECTURE.md#connection-tiers-6-tier-fallback-hierarchy), [ROADMAP.md — Decentralized Infrastructure](../ROADMAP.md#decentralized-infrastructure-v095---v0102)

### star topology

- **Met in:** rules
- **Meaning:** The live shape of the three-node stand: every link goes through one node because host firewalls block inbound elsewhere; a configuration of a real user, not a defect to fix by opening ports.
- **Defined in:** [ROADMAP.md — Decentralized Infrastructure](../ROADMAP.md#decentralized-infrastructure-v095---v0102)

### peer cache

- **Met in:** code, rules
- **Meaning:** `~/.dpc/peer_cache.json`: what this node knows of each peer it has talked to, and what actually carries traffic when the DHT is empty. Its **`addresses`** are every address a direct connection with the peer worked on, one entry per ip with `port`, `last_seen` and `direction` ("in" or "out"), at most 8, the oldest by `last_seen` evicted; a dial from the cache tries local-network addresses first, then the rest newest first, and stops on the first that answers. `last_direct_ip`/`last_direct_port` mirror the newest entry for older builds. Mike's word, 2026-10-28.
- **Defined in:** [ROADMAP.md — Decentralized Infrastructure](../ROADMAP.md#decentralized-infrastructure-v095---v0102)

### `local_addresses`

- **Met in:** code, wire
- **Meaning:** An optional field of the direct-TLS `HELLO` (dialler) and `HELLO_ACK` (listener): the sender's own LAN addresses as `"ip:port"` strings with its P2P listen port, RFC 1918 IPv4 only, at most 4. The receiver keeps the checked list per peer in the **peer cache** under `metadata.local_addresses`, replaced on every handshake, and dials it first with a 2 s budget; it is unsigned, a hint where to dial, and an advertised address enters **`addresses`** only after a dial over it succeeds. Sent only to a peer the sender knows — one it has dialled successfully (a peer-cache address with direction `out`) or one in a group with it; a cache entry written by inbound connections alone does not count — and left out for any other node (Mike's call, 2026-10-30). DPTP 1.12. Approved by Mike, 2026-10-29.
- **Defined in:** [specs/dptp_v1.md §3.1](../specs/dptp_v1.md#31-connection-establishment)

### `list_cached_peers`

- **Met in:** code, screen
- **Meaning:** The local API command that lists the **peer cache** by name: `node_id`, `display_name`, `last_seen` and `is_connected` per peer, newest first, and never the cached address. Behind the sidebar's cached-peers list; not an agent tool (who a node has talked to is the node's own metadata). Its two siblings change the cache: **`remove_cached_peer`** forgets one peer by `node_id` and refuses a peer connected now, because the connection would write it back at once; **`clear_peer_cache`** forgets every cached peer except those connected now and answers `removed` and `kept_connected`. Mike's go, 2026-10-27.
- **Defined in:** code: `service.py`

### `before_message_id`

- **Met in:** code
- **Meaning:** The cursor of a `get_conversation_history` page request: a history record's `id` (the same value as its `message_id`). The answer holds the live messages strictly older than that record, in the order the history is presented in. Never `msg_index`, which `rechain()` renumbers. A cursor that is not in the live history (reset, archived, merged away) is refused with an error the UI pager stops on, never answered with the newest page. Approved by Mike, 2026-10-28.
- **Defined in:** [`history_page.py`](../dpc-client/core/dpc_client_core/history_page.py)

### `has_older`

- **Met in:** code
- **Meaning:** In a `get_conversation_history` answer: older live messages exist before the first message of the page, and the next request names that first message as its **`before_message_id`**. False at the start of the live history, which is the **live-history boundary** of a chat that was reset; what lies before it is in the archive, and the chat window says so in a line at its top. Approved by Mike, 2026-10-28.
- **Defined in:** [`history_page.py`](../dpc-client/core/dpc_client_core/history_page.py)

### `limit` (local API)

- **Met in:** code
- **Meaning:** The page size of `get_conversation_history`, in messages: 100 when absent, at most 500, and 0 or less is refused, so there is no "all". A page is also cut at 512 KiB of messages, with **`has_older`** still true. Not the `limit` of the agent tool `chat_history`, where 0 means every message. Approved by Mike, 2026-10-28.
- **Defined in:** [`history_page.py`](../dpc-client/core/dpc_client_core/history_page.py)

### `slow_command`

- **Met in:** code
- **Meaning:** The decorator that takes a local API handler off the UI socket's read loop, where one waiting command held back every request behind it on the same socket, Stop included. Marked for waiting on something outside this process (an agent turn, a model, a peer, a remote API), not for being slow. It changes ordering — the command may finish after commands sent later — which is why `send_group_image` stays inline: a text sent after the image must not land before it in the group. It also lets two of the same command overlap, so a handler that must not overlap with itself takes its own lock (the Telegram link commands per agent, the Hub login). `3fc86a2d`, `1617e9e6`, 2026-10-29.
- **Defined in:** code: `dpc-client/core/dpc_client_core/local_api.py`

### DHT / seed nodes

- **Met in:** code, rules, screen
- **Meaning:** The Kademlia table a node uses to find peers, entered only through the `[dht] seed_nodes` config line; empty by default.
- **Defined in:** [CONFIGURATION.md, the dht section](CONFIGURATION.md#dht)

### Hub

- **Met in:** code, rules, screen
- **Meaning:** The optional federation server: OAuth login and WebRTC signalling only, never message routing or storage; every direct path works without it.
- **Defined in:** [docs/ARCHITECTURE.md — Key components](ARCHITECTURE.md#key-components), [specs/hub_api_v1.md](../specs/hub_api_v1.md)

### firewall / privacy rules

- **Met in:** code, rules, screen
- **Meaning:** `~/.dpc/privacy_rules.json`: what each peer or group may read of this node's context, whether compute and transcription are shared, what agents may use; hot-reloaded on save.
- **Defined in:** [docs/CONFIGURATION.md — Firewall rules](CONFIGURATION.md#firewall-rules-privacy_rulesjson)

### `instructions.json:<set_key>.primary`

- **Met in:** code, rules
- **Meaning:** The rule path for the primary text of one instruction set in `instructions.json`: whether that text goes into a prompt run on a peer that hosts the inference. Matched like the `personal.json:` paths in a `nodes` or `groups` entry, so `instructions.json:*` allows every set. No node group has it by default; with no rule a peer host gets the prompt without instruction text, and local inference is unchanged (Mike's go, 2026-10-30).
- **Defined in:** [docs/CONFIGURATION.md — Firewall rules](CONFIGURATION.md#firewall-rules-privacy_rulesjson)

### awaiting_permission

- **Met in:** code, screen
- **Meaning:** An incoming file offer — a file, a pasted screenshot, a voice message or a group file — from a peer no File Transfer entry names, a peer on a **proved connection** included unless the owner set that entry to allow: instead of being refused it is put to the user as "Allow files from X?", the word taken from the firewall's own *File Transfer Permissions*. "Always allow" writes a node entry through the editor's save; a decline reaches the sender as `user_rejected` with a sentence in `denial_reason`; no answer in 120 s reaches it as **no_answer**. The flag rides on the `file_transfer_offered` event and on the transfer, never on the wire (Mike's call, 2026-10-25).
- **Defined in:** [docs/CONFIGURATION.md — Firewall rules](CONFIGURATION.md#firewall-rules-privacy_rulesjson), [`file_transfer_manager.py`](../dpc-client/core/dpc_client_core/managers/file_transfer_manager.py)

### transcription send-to

- **Met in:** code, rules, screen
- **Meaning:** The outgoing half of transcription sharing: `transcription.send_to_nodes` / `send_to_groups` in `privacy_rules.json` name the peers this node may hand its **own** audio to when local transcription fails. The opposite of `allow_nodes`, which lets peers use our Whisper; empty means never. Proposed 2026-09-07 with the fallback step — the vocabulary had a word for serving and none for sending.
- **Defined in:** [docs/CONFIGURATION.md — Firewall rules](CONFIGURATION.md#firewall-rules-privacy_rulesjson)

### remote inference

- **Met in:** code, rules, screen, wire
- **Meaning:** Running a model on a trusted peer's hardware over the encrypted P2P channel, seen from the side that asks: the requester picks the peer as compute host, and the host's firewall decides whether to serve. The host's side of the same exchange is **Inference Sharing**; "compute sharing" was that tab's name until 2026-09-14 and is kept only by the `compute.*` config keys.
- **Defined in:** [REMOTE_INFERENCE.md — Overview](REMOTE_INFERENCE.md#overview)

### Inference Sharing

- **Met in:** code, rules, screen
- **Meaning:** The firewall tab and the door that share this node's models with peers (ADR-041): which aliases are served, who may call them, who calls for free, at what tariff, and which models the door accepts. Named "Compute Sharing" until 2026-09-14 (Mike's call: the old name suggested lending a GPU, while what crosses is inference on the host's own model); the config section keeps the name `compute` until the key rename is decided.
- **Defined in:** [REMOTE_INFERENCE.md — Enabling Inference Sharing](REMOTE_INFERENCE.md#enabling-inference-sharing)

### gateway / IDE door

- **Met in:** code, rules, screen
- **Meaning:** The OpenAI- and Anthropic-compatible HTTP surface this client serves on the consumer's own loopback (ADR-041 D1), so that any tool on this machine — Continue, Cursor, Claude Code, a `curl` line — reaches this node's aliases, and its peers' under a `remote:<node_id>:<alias>` name. **IDE door** is what the Inference Sharing tab's block calls it, and the door is the machine's rather than an editor's: an IDE is the common caller, not the definition. The **peer door** is a different door — DPTP on 8888, where another *node* asks — and the two switches over them do not overlap: `compute.enabled` closes both, `[gateway] enabled` closes this one alone, and neither governs what this node *asks* for, so a node that shares nothing still calls its peers through its own gateway.
- **Defined in:** [decisions/041 D1](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d1--the-openai-surface-is-a-component-of-the-dpc-client-on-the-consumers-loopback)

### node ledger

- **Met in:** code, rules
- **Meaning:** `~/.dpc/ledger/usage-YYYY-MM.jsonl`: the node's own record of every model call it made or served — an agent's, a peer's or a gateway client's — kept by the node because the card and the vendor key are the node's. Monthly files, append-only, never rotated: a financial record does not lose rows on a schedule.
- **Defined in:** [decisions/041 D3](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d3--one-usage-row-per-model-call-on-the-node-that-ran-it-carrying-who-called)

### usage row

- **Met in:** code, rules, wire
- **Meaning:** One line of the node ledger: who called (`caller`, **caller_kind**), what ran (`alias`, `model`, **route**), what it took, whose counts those are (**counts_source**), and what it cost — priced once, at the moment of the call, by the node that made it, and never re-derived. The columns added through 2026-09-14/15 carry the rest: **served_by**, the host of a call this node only consumed; **peer_proved** with `peer_connection_type`, whether the far end's key was proved and over which tier; **output_includes_thinking** and **thinking_source**, what is inside the counts and where the split came from; **served_effort**, the rung the call actually ran at; `content_chars` and `tool_calls` (2026-10-06, the words of `eval/loop/round_metrics.py`), the stripped visible text's length and the number of tool calls in the response, null where the writer did not see that half; `finish_reason`, `max_tokens`, `prefilled_tokens`, `cached_tokens`, `prompt_ms` and `predicted_ms` (2026-10-19, `node_ledger.ENGINE_FIELDS`), the engine's own word on the round — why it stopped, the ceiling the request carried, the prompt tokens re-read and reused, and each phase's raw clock in the engine's own names, counters and milliseconds rather than a rate — null where the provider reported none; a vendor fills `finish_reason` and `max_tokens` where it records usage (2026-11-05; an OpenAI-compatible alias and GigaChat the word alone), and `cached_tokens` and `prefilled_tokens` from the prompt cache split it priced the call on — the hit (`prompt_cache_hit_tokens`, else `cache_read_input_tokens`) and the prompt read fresh, Anthropic's cache creation among it (2026-10-19, `node_ledger.prompt_cache_split`), and a vendor that reported no split leaves both null; and the **tariff** group (`tariff_in`, `tariff_out`, `tariff_currency`, `tariff_at`, `tariff_amount`), what the owner charged for it.
- **Defined in:** [decisions/041 D3](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d3--one-usage-row-per-model-call-on-the-node-that-ran-it-carrying-who-called)

### caller_kind / route

- **Met in:** code, rules
- **Meaning:** The two columns of a **usage row** that together say whose call it was and where it ran: `caller_kind` is `agent`, `peer` or `gateway` — who asked — and `route` is `local` or `peer` — whose engine answered. There is no `gateway` route: the gateway is a caller, never a destination. The **KV snapshot** record of the last call per alias (`kv_snapshots.SNAPSHOT_CALLER_KINDS`, built as the ledger's three words plus one) uses the same word with a fourth value, `owner`, for the owner's own chat, which writes no usage row; `owner` never appears in the ledger, and `node_ledger` refuses it. Three independent axes use the word: who called the engine (this record's `owner`), whose turn it is for memory layers (`OWNER_TURN`, `dpc_agent/agent.py`), and which door a message came through (`is_owner_caller`, the UI, an external agent or the owner's Telegram) (team naming check, 2026-11-13). The pair is what the ledger reads itself by (`node_ledger.usage_by_role`): `route=local` with `caller_kind=peer` is what this node *served*, `route=peer` is what it *consumed*, and the rest is its *own*. A value outside either vocabulary is refused at write time rather than written, because a row nothing can read is not a record.
- **Defined in:** [decisions/041 D3](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d3--one-usage-row-per-model-call-on-the-node-that-ran-it-carrying-who-called)

### provider_kind

- **Met in:** code, rules, wire
- **Meaning:** In an agent's runtime budget: what kind of model answers — `self_hosted` (on the serving node's own hardware), `vendor` (a paid API) or `unknown`. Not `local`, because **route** already says local-or-peer about *who* answered: a peer serving its own model is `route: peer, provider_kind: self_hosted`. Since 2026-09-28 also a **menu row** field: the host's statement of which serving list the alias stands on (`vendor` for `serving_vendor`, `self_hosted` for `serving_local`), which the guest reads over its own type table — only the host can tell an `openai_compatible` vendor from a server on its own card — and which the guest's screen turns into «vendor model — your prompt goes to <vendor>».
- **Defined in:** [`dpc_agent/provider_facts.py`](../dpc-client/core/dpc_client_core/dpc_agent/provider_facts.py), [specs/dptp_v1.md §3.5](../specs/dptp_v1.md#35-provider-discovery)

### host_scope

- **Term note:** (`url_host_scope`, `base_url_host`)
- **Met in:** code
- **Meaning:** A fact the backend computes for a local row of the agent dialog's providers list: where the host of the provider's URL (`base_url`, or `host`) is, when that is not the vendor's. `this_machine`: `localhost`, `0.0.0.0`, a loopback address or an address this node owns. `lan`: a private-network address (**in_local_networks**). `unknown`: a one-label or LAN-suffixed name, or digits that are no address. Absent for anything else, and for a peer's row. A generic-type (`openai_compatible`) row with no scope and a host other than OpenAI's carries the host as `base_url_host`. The dialog reads it (through the UI's **where**), and since 2026-11-14 so does the Extract Knowledge question (**consent_required**), which sends without asking only when it is `this_machine`; it never feeds **provider_kind** or `tokens_paid_by`, and no redaction code reads it yet. ADR-050 P4 (proposed, not decided) would have the redaction exemption read `host_scope` together with **route** and **provider_kind**, so "display only" describes its use today, not a rule for it.
- **Defined in:** [`dpc_agent/provider_facts.py`](../dpc-client/core/dpc_client_core/dpc_agent/provider_facts.py)

### provider_row_facts

- **Met in:** code
- **Meaning:** The function that adds `route`, `provider_kind` and, where it applies, `host_scope` or `base_url_host` to each row of the providers list in the agent configuration dialog (`get_agent_model_config`). `provider_kind` is exactly what `provider_kind(type, base_url)` returns; a peer's row takes the host's own word.
- **Defined in:** [`dpc_agent/provider_facts.py`](../dpc-client/core/dpc_client_core/dpc_agent/provider_facts.py)

### in_local_networks

- **Met in:** code
- **Meaning:** `peer_cache.in_local_networks(address)`: true for an address in a private network (RFC 1918, ULA), false for link-local and public ones. The test **host_scope** uses for `lan`.
- **Defined in:** [`peer_cache.py`](../dpc-client/core/dpc_client_core/peer_cache.py)

### where

- **Term note:** (`ModelRoute.where`)
- **Met in:** code, screen
- **Meaning:** Field of the dialog's `ModelRoute`: where a chosen model's prompt goes, `local`, `lan`, `vendor`, `peer` or `unknown`. Display only: billing (`tokens_paid_by`) and any future redaction decision must not read it; they read **route**, **provider_kind** and **host_scope** in the backend. The screen words beside each value, as the option tag and the line under the select: *local* (runs on this machine), *your network* (a machine in the local network), *cloud* (a named vendor receives the prompt), *cloud?* (a generic-type URL that names no vendor: this node cannot tell whether a vendor or a machine of yours answers), *peer node* (another node receives it), and no tag with the Unknown line when nothing is known.
- **Defined in:** [`agentModelRoute.ts`](../dpc-client/ui/src/lib/components/agentModelRoute.ts)

### consent_required

- **Term note:** (`extract_consent`, `provider_alias`, `fallback_needs_consent`, `extraction_in_progress`)
- **Met in:** code, screen, wire
- **Meaning:** The answer `end_conversation_session` gives, before anything is touched or sent, when the **extraction target** is not proven to run on this machine: a peer host, a provider whose **provider_kind** is not `self_hosted` (`vendor` or `unknown`), or a `self_hosted` one whose URL (`base_url`, or an ollama `host`) has a **host_scope** other than `this_machine` (a local engine with no URL is at its default endpoint, this machine). It names the target in `provider_alias`, `provider_type`, `provider_kind`, `host_scope`, `compute_host` (the peer's node id, else null; for a peer `provider_type`, `host_scope` and `url` are null and `provider_kind` is the host's own word; a local `remote_peer` alias, or a `dpc_agent` provider with a peer, is a peer target too, with the peer's alias, and the call still goes through the local alias, kept in the pin as `via_alias`, which is not one of the six fields; `via_alias`: Mike's call, 2026-11-18) and `url` (the alias's `base_url`, or an ollama `host`; null for an engine with no URL), and nothing is extracted, broadcast or proposed. The question names the URL's host beside the alias ("lan_qwen at 192.168.1.20:11434"), as the editor names a remembered entry, printed as the browser's `URL.host` prints it (no user info, no default port, an IPv6 address in brackets); the backend's logs and refusals print it the same way. The printed host may differ from the stored `url`, and nothing compares it: the identity compares the full stored `url`. The request field `extract_consent` has three forms. Absent or `false`: ask. An object echoing the six target fields: yes to exactly that target; the UI resends the command with it, and the backend proceeds only while the echo still names the target it resolves, pinned to it for the whole extraction. The echo is compared (`same_target`) on the one identity a remembered yes uses (**send_without_asking**): a local alias on all six fields, a peer on alias and peer (`compute_host`), so a peer whose kind changed between the question and the yes (a restart) is not asked again, and the entry the yes writes matches the echo it was written from. A changed target is asked about again, a local host that moved inside the same host scope included, because `url` is one of the six. `true`: yes to any target, with the old fallback retries; the Telegram `/extract_knowledge` command sends it (its /help says so), and any local API client can too, because `local_api` spreads the command's payload into the handler. A retry after a failed call goes silently only to a target that needs no question (**proven_on_this_machine**, or a remembered yes); any other retry is refused with the wire reason `fallback_needs_consent`, naming the model the retry would have sent, the peer's display name and its kind in the question's words ("qwen on peer Linux (on that node's own hardware)", `unknown` when the menu names none), and nothing is sent there. The guard is on for every press whose `extract_consent` is not `true` (`fallbacks_need_consent = extract_consent is not True`), the presses whose first target needed no question included: a press that went unasked to a model on this machine still refuses a retry that would leave it. A second press while an extraction runs is refused with the wire reason `extraction_in_progress` (an error answer plus a `knowledge_extraction_failed` event, both with that `reason`, as with `vote_in_progress`). A chain that names no provider (`NoKnowledgeProvider`) is not asked about. "Leaves this machine" is what this gate decides, from what the node can prove about where the model runs; it is not ADR-050's "a model the node does not own", which is about ownership: a `host_scope` `lan` model can be owned and still leave this machine, and this gate asks for it unless the owner remembered a yes for that target (ADR-050 Open Question 1, Mike's call, 2026-11-14, in his Claude Code session; see **send_without_asking**). Distinct from **model download consent**, which is about fetching a model, not sending a conversation. `extract_consent`: Mike's call, 2026-11-14, in the group chat; `provider_alias` is the field the code already used; `consent_required`, `fallback_needs_consent` and `extraction_in_progress`: proposed, reviewers agreed 2026-11-14.
- **Defined in:** [`knowledge_service.py`](../dpc-client/core/dpc_client_core/knowledge_service.py), [`knowledge_routing.py`](../dpc-client/core/dpc_client_core/knowledge_routing.py), [`extractionConsent.ts`](../dpc-client/ui/src/lib/extractionConsent.ts)

### extraction target

- **Term note:** (`extraction_target`, `monitor.extraction_target`, `_resolve_extraction_target`)
- **Met in:** code
- **Meaning:** Where one knowledge extraction sends the conversation, in three things that share the name. The record: what `knowledge_routing.extraction_target` builds from the facts of **provider_facts** (`provider_alias`, `provider_type`, `provider_kind`, `host_scope`, `compute_host`, `url`, `model`); **proven_on_this_machine** reads it, and the six fields other than `model` are what **consent_required** names and echoes and what a **send_without_asking** entry stores. The pin: that record set on `monitor.extraction_target` for one press, which the monitor's `_infer_inference_settings` and the self-reflection check then obey instead of the conversation's provenance, and cleared when the press ends (a second press during a running one does not touch it). The resolver: `KnowledgeService._resolve_extraction_target`, which asks the monitor's own chain once, before anything is touched, and returns the record, or None when the chain names no provider. Proposed, reviewers agreed 2026-11-14.
- **Defined in:** [`knowledge_routing.py`](../dpc-client/core/dpc_client_core/knowledge_routing.py), [`knowledge_service.py`](../dpc-client/core/dpc_client_core/knowledge_service.py), [`conversation_monitor.py`](../dpc-client/core/dpc_client_core/conversation_monitor.py)

### send_without_asking

- **Term note:** (`knowledge_extraction`, `remember`, `remembered_entry`, `may_send_unasked`, `remembered_targets`)
- **Met in:** code, rules, screen, wire
- **Meaning:** The owner's "Don't ask again for <name>" on the **consent_required** question, offered for every target that asks (a machine on the network, a cloud vendor, a peer). Stored in `privacy_rules.json` as the top-level section `knowledge_extraction`, whose list `send_without_asking` holds one entry per remembered target: the six fields of **consent_required**, `url` among them. What a yes is bound to depends on the target, and the echo check of the question's yes (`same_target`) uses the same identity. A local alias: all six, the full stored `url` (not the host the question prints), so the same alias at another URL, host scope, kind or type asks again. A peer: the alias and the peer (`compute_host`) alone; the entry still stores the kind it was asked with, for the editor's line, but does not compare it, because a peer's kind is the menu's word of the moment (`self_hosted` after a HELLO, `unknown` after a restart). So a yes for `qwen` on a peer does not cover another alias there, nor a retry that names no alias: the fallback retry to a peer sends only the conversation's last model, and is refused (the safe direction). A yes is bound to the alias and its address (the peer, for a peer), not to the model, so it survives a model swap under the same alias. The editor's line shows the kind the target was asked with: a memory, not a current reading (a peer read as `unknown` after its restart still shows its old kind). The list is one list in two homes: `firewall.remembered_extraction_targets()` reads it from the loaded rules at each press, and `monitor.remembered_targets` is the copy the service hands the monitor for that press alone, so the retry guard never reads the firewall. The request field `remember` (a boolean beside the `extract_consent` echo) writes the entry, and only when it is `true`, the echo names the target resolved now, and that target asks at all; `extract_consent` `true` (Telegram) never writes one. The write goes through the editor's own save (`save_firewall_rules`), as file transfer's "always allow" does, so it is validated, hot-reloaded and announced to the UI with `firewall_rules_updated`; a save that changed this section and nothing else (a remembered yes, or the editor's Remove of one) does not tell peers of a provider change nor make the agents re-read their settings, which neither reads. The editor's Save reads the rules again and sends the list on disk minus the entries removed in that edit session, so a yes remembered while the editor was open survives; a payload without the section keeps the disk section. An editor Save that lands between the service's read of the rules and its write of a yes is undone by that write: the yes survives and the editor's change is lost (accepted: a narrow window). Block deletes every entry on the blocked peer, and Unblock does not restore them. A remembered target passes the gate and the retry guard (`knowledge_routing.may_send_unasked`); **proven_on_this_machine** is the other half of that test and is never written here. In code since `5b17856e`; the names were proposed and the reviewers agreed, 2026-11-14. The box for every target: Mike's call, 2026-11-14, in his Claude Code session; the peer rule, alias + peer: Mike's call, 2026-11-15, in the same session.
- **Defined in:** [`knowledge_routing.py`](../dpc-client/core/dpc_client_core/knowledge_routing.py), [`firewall.py`](../dpc-client/core/dpc_client_core/firewall.py), [`extractionConsent.ts`](../dpc-client/ui/src/lib/extractionConsent.ts)

### cancel_knowledge_extraction

- **Met in:** code, wire
- **Meaning:** The local API command (payload `conversation_id`) the Extract Knowledge button sends while that conversation is extracting, where the button reads "Cancel" (the token-warning banner's button does the same). It asks an extraction to stop while the model is still working, before the vote: each press runs its model call as a task held per conversation, and the command cancels that task and waits up to 5 s for the press to release its pin; a `success` answer says the stop was requested. The boundary is the model task: a cancel that arrives once the call has returned is refused with **extraction_not_running** and the proposal is announced, while one that lands before it returns counts even if the call goes on to return a proposal. A model call that ignores the cancel keeps the button at "Cancel" until it returns, and even then proposes nothing: the press reads the cancel again after the call. Pressed while the consent question is open, it is a no to that question. The pressed `end_conversation_session` then answers `status: "cancelled"` and broadcasts **knowledge_extraction_cancelled**; the pin, the consent state (**extraction target**, `fallbacks_need_consent`, `remembered_targets`) and the monitor's `_extracting` are released, so the next press runs a new extraction, and nothing is proposed. The buffers rebuilt from the history before the model call are kept: a cancel loses no messages. A cancel stops this node's wait and proposes nothing; it does not recall what the target already received: a cloud alias or a peer has the conversation, its cost is paid, and a local engine may finish the answer unread. Once the proposal is announced (`knowledge_commit_proposed`) there is no withdraw, and the command refuses with `vote_in_progress` or **extraction_not_running**; a refusal is an answer only, with no event. Telegram's `/extract_knowledge` has no cancel, and an extraction it started and the UI cancelled is reported there as failed. Modelled on `cancel_agent_task`. Mike's call, 2026-11-15, in his Claude Code session.
- **Defined in:** [`knowledge_service.py`](../dpc-client/core/dpc_client_core/knowledge_service.py), [`+page.svelte`](../dpc-client/ui/src/routes/+page.svelte)

### knowledge_extraction_cancelled

- **Met in:** code, wire
- **Meaning:** The event `end_conversation_session` broadcasts when its extraction was stopped by **cancel_knowledge_extraction** (payload `conversation_id`, `message`), after the pin is released. Not a failure: unlike `knowledge_extraction_failed` it shows no failure toast and closes no open vote dialog; the UI only turns the button back to "Extract Knowledge". Beside `knowledge_extraction_failed` and `knowledge_extraction_fallback`. Mike's call, 2026-11-15, in his Claude Code session.
- **Defined in:** [`knowledge_service.py`](../dpc-client/core/dpc_client_core/knowledge_service.py), [`KnowledgeEventsPanel.svelte`](../dpc-client/ui/src/lib/panels/KnowledgeEventsPanel.svelte)

### extraction_not_running

- **Met in:** code, wire
- **Meaning:** The reason **cancel_knowledge_extraction** refuses with when this conversation has no extraction to cancel: none was pressed, it already finished, or its proposal is announced and no vote is open (an open vote answers `vote_in_progress` instead). An error answer only, with no event; the UI clears a stale "Cancel" on it without a message. Beside `vote_in_progress` and `extraction_in_progress` (**consent_required**). Mike's call, 2026-11-15, in his Claude Code session.
- **Defined in:** [`knowledge_service.py`](../dpc-client/core/dpc_client_core/knowledge_service.py), [`extractionConsent.ts`](../dpc-client/ui/src/lib/extractionConsent.ts)

### shared_proposal_not_editable

- **Term note:** (`EDIT_REFUSED_REASON`, `EDIT_REFUSED_MESSAGE`)
- **Met in:** code, wire
- **Meaning:** The reason `vote_knowledge_commit` refuses a vote with when the vote carries edited entries or summary and the proposal has a participant other than this node. An edit changes this node's copy only, and the text goes into the commit hash, so another participant would sign a different commit. The vote is not cast; the answer tells the person to approve, reject, or ask for changes with a comment. Text equal to the proposal's own is not an edit, and a vote on a session no longer voting answers `already_decided` first. The vote dialog shows Edit only on a solo proposal: AI, agent and Telegram chats, whose roster is the owner alone. Editing a shared proposal as a proposal for a new vote waits on REQUEST-CHANGES-ON-A-KNOWLEDGE-VOTE-LEADS-NOWHERE-OUTSIDE-AGENT-CHATS. Mike's call, 2026-11-16 (variant B; the name and text agreed by Ark, Zcode and CC).
- **Defined in:** [`knowledge_service.py`](../dpc-client/core/dpc_client_core/knowledge_service.py), [`KnowledgeCommitDialog.svelte`](../dpc-client/ui/src/lib/components/KnowledgeCommitDialog.svelte)

### proven_on_this_machine

- **Met in:** code
- **Meaning:** `knowledge_routing.proven_on_this_machine(target)`: true for an **extraction target** with no peer, **provider_kind** `self_hosted` and **host_scope** `this_machine`, the one target Extract Knowledge sends to with no question and no remembered yes, and the one a **send_without_asking** entry is never written for. It is what the node can prove, not what the owner allowed: a target the owner remembered passes `may_send_unasked` without being proven. Proposed; the name, team consensus in review.
- **Defined in:** [`knowledge_routing.py`](../dpc-client/core/dpc_client_core/knowledge_routing.py)

### tokens_paid_by

- **Met in:** code, rules
- **Meaning:** In an agent's runtime budget: who pays *per token* for its calls — `nobody` (a self-hosted model; power and card time still cost), `this_node` (this node's API key), `peer` (a vendor model the serving node gives as a gift) or `unknown`. On a peer route the payer follows ADR-041's «the payer is the caller»: a **tariff** above zero in the peer's menu row makes it `this_node`; no tariff (the gift) or a declared zero leaves the model's own cost with whoever runs it. The budget reads the last quote; the **usage row** is the record of what was charged.
- **Defined in:** [`dpc_agent/provider_facts.py`](../dpc-client/core/dpc_client_core/dpc_agent/provider_facts.py)

### agent_lifetime_tokens

- **Met in:** code, rules
- **Meaning:** In an agent's runtime budget: the tokens that agent has used across all its chats and tasks since its state began (`state/state.json`). Not this chat's and not the node's: the **node ledger** counts the node.
- **Defined in:** [`dpc_agent/context.py`](../dpc-client/core/dpc_client_core/dpc_agent/context.py)

### own / burn

- **Met in:** code, screen
- **Meaning:** Two readings of the same `route=local` rows, kept apart because they answer two questions. `own_rows` is `route=local` minus `caller_kind=peer` — this node's own consumption on its own key, which the usage tab titles "Own calls (peers excluded)". `burn_rows`, named `owner_rows` until 2026-09-15 and renamed because one letter is not a distinction, is *every* `route=local` row, the calls served to peers included: this node's dollars whoever asked, which is what a burn reader wants in place of the vendor's own usage log. `served_rows` is the difference between the two, and a `gateway` caller is on neither side of it — the gateway door is local, so its client is this node's own user.
- **Defined in:** [`node_ledger.py`](../dpc-client/core/dpc_client_core/node_ledger.py)

### engine time / round trip

- **Met in:** code, screen
- **Meaning:** The same `duration_s` read from the two sides of one call, named apart because it is not one quantity: **engine time** is this node's own provider call with the engine's warm-up inside it, and **round trip** is this node's wait for a peer's answer, with the wire and the host's queue inside that. The usage tab prints the first over the Served and Own lists and the second over Consumed (`durationLabel`). Measured 2026-09-14 over 28 calls on the two-node LAN/VPN pair: guest minus host had a median of about 1.0 s, 27 of the 28 fell inside 0.92–1.28 s, and the one outlier of 7.3 s is placed outside the host process by the host's own log. Two numbers, and no row carries both.
- **Defined in:** [`inferenceUsage.ts` `durationLabel`](../dpc-client/ui/src/lib/components/inferenceUsage.ts)

### counts_source

- **Met in:** code, rules
- **Meaning:** Where a **usage row**'s token counts came from, as the writer of that row knows it: `engine` — the numbers were received, from the engine this node called or from a host's answer on the wire, and this node counted nothing; `ours` — this node counted the text itself. A guest's `engine` is therefore not an assertion that an engine counted: a host that counted the text itself writes `ours` on its own row and sends the same numbers, which the guest records as `engine` (open card `TWO-NODES-WRITE-THE-SAME-TOKEN-COUNT-UNDER-TWO-DIFFERENT-WORDS`). It says where the numbers came from and never what is inside them — that is **output_includes_thinking** — and an exact total can hold an estimated split, which is what **thinking_source** is for.
- **Defined in:** [decisions/041 D3](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d3--one-usage-row-per-model-call-on-the-node-that-ran-it-carrying-who-called)

### output_includes_thinking

- **Met in:** code, rules, wire
- **Meaning:** `includes` / `excludes` / `unknown`: whether a row's output count already holds the reasoning count, as declared by the node that produced the count rather than assumed from a convention. The word is what sets the billable output, and ADR-041 states the formula: `includes` → `tariff_out × completion_tokens`; `excludes` → `tariff_out × (completion_tokens + thinking_tokens)`, reasoning being billable output at `tariff_out` with no rate of its own; `unknown` → not billed, analytics only — the state of every row written up to that decision. A row written before the column reads as `unknown`. Under `includes` the reasoning is inside the output and never larger than it; a count that overflowed is clamped to the completion and marked `estimated`, so the record of a call that was made is kept rather than refused.
- **Defined in:** [decisions/041 D3, amendment 2026-09-13](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d3--one-usage-row-per-model-call-on-the-node-that-ran-it-carrying-who-called), [specs/dptp_v1.md §3.4](../specs/dptp_v1.md#34-remote-ai-inference)

### on_reasoning

- **Met in:** code
- **Meaning:** The callback the gateway passes to `LLMManager.query_messages` and, through it, to a provider entry point that names the parameter: `on_chunk` for the reasoning text, `(text, conversation_id)`. No other caller passes it, so an agent and a peer's chunk sender receive the answer alone. What it carries leaves on the OpenAI form as `reasoning_content`. Proposed name, open to the owner of this vocabulary.
- **Defined in:** [decisions/041 D4, amendment 2026-11-04](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d4--shipping-order-and-the-colleague-is-served-at-step-4)

### thinking_source

- **Met in:** code, wire
- **Meaning:** Where a row's `thinking_tokens` came from: `engine`, the host's vendor reported the reasoning split, or `estimated`, the host derived it from the reasoning text, which is what a build that reports no split sends. Null is silence — a node that said nothing, an older row, or a call with no reasoning — and is never a claim that an engine counted. Provenance, not arithmetic: the counts beside it can be exact while the split inside them is a guess, and a **tariff** is charged on the prompt and output counts either way.
- **Defined in:** [specs/dptp_v1.md §3.4](../specs/dptp_v1.md#34-remote-ai-inference)

### peer_proved

- **Met in:** code
- **Meaning:** On a **usage row**, whether the node at the other end of the call had its key proved on the connection the call travelled over — the line ADR-041 D2 draws for serving peer inference at all. `True` means one of two checks ran: inbound, the certificate's CN is the claimed `node_id`, its public key hashes to that `node_id`, and a signature over the **HELLO preimage** (a nonce this node chose, its own `node_id`, the claimed one) verifies under it; outbound, the same CN and key hash over a TLS session that has already made the far end prove it holds the private half. `False` is a tier that runs neither, where the name is the Hub's assertion, this node's own intention or an envelope field. Null means there is no far end to prove — an agent or a gateway client on this machine is not reached over a tier — and is what a row written before the column reads as. `peer_connection_type` beside it names the tier the answer came from.
- **Defined in:** [decisions/041 D2](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d2--authorised-on-a-connection-whose-peer-key-has-been-proved)

### `LINK_PING`, `LINK_PONG`

- **Met in:** code, wire
- **Meaning:** DPTP commands on a direct TLS link: a node sends LINK_PING to a peer that advertised **`link_ping`** once a third of **`link_silence_timeout`** passed with no frame from it; the peer answers LINK_PONG at once, echoing the payload (`sent_at`). Answered and consumed by the connection's reader, never routed. Not the DHT's `PING`/`PONG`, which is UDP on another socket. DPTP 1.13. Names agreed by the team, Mike's go 2026-10-30.
- **Defined in:** [specs/dptp_v1.md §3.1](../specs/dptp_v1.md#31-connection-establishment)

### `link_ping`

- **Met in:** code, wire
- **Meaning:** Optional key of the direct-TLS `HELLO` payload and `HELLO_ACK`, value `1`: the sender answers LINK_PING. Only a peer that sent it is pinged and can be ended for silence; an older peer is never pinged and never cut. Outside the **HELLO preimage**. DPTP 1.13.
- **Defined in:** [specs/dptp_v1.md §3.1](../specs/dptp_v1.md#31-connection-establishment)

### `link_silence_timeout`

- **Met in:** code, rules
- **Meaning:** `[connection]` key in `config.ini`, default 90 s: how long a direct TLS link to a **`link_ping`** peer may go without a frame before it is ended — one WARNING naming the threshold, one INFO line with both ends, who dialled, the last frame and whether the peer's address is this node's own and which source says so (an interface, the default route, the STUN address as current or earlier), then an abort into the usual loss cleanup and the dialler's redial. Any frame counts, by 64 KiB slice while it arrives; the LINK_PING interval is a third of it.
- **Defined in:** [docs/CONFIGURATION.md](CONFIGURATION.md)

### `max_proved_connections_per_ip`

- **Met in:** code, rules
- **Meaning:** `[connection]` key in `config.ini`, default 8: the most inbound **proved connection**s one remote address may hold at once. A new node id from that address past it is closed before HELLO_ACK, silently and not as a failed HELLO; a node replacing its own connection is never refused. Separate from `max_pending_hellos_per_ip`, which counts connections still waiting for their HELLO (two limits of 8). Mike's go, 2026-10-30.
- **Defined in:** [docs/CONFIGURATION.md](CONFIGURATION.md)

### `blocked_nodes`

- **Met in:** code, rules
- **Meaning:** Top-level list of whole **node_id**s in `privacy_rules.json`, shaped like `allow_nodes`: nodes this node refuses outright. A listed node gets no session either way (closed before HELLO_ACK like the `max_proved_connections_per_ip` refusal; no dial, no Hub WebRTC answer, no relay session, no gossip delivery, no file), and its group posts are kept but relayed nowhere and wake nobody. In the DHT a packet naming it as sender is dropped unanswered and nothing is handed out under its id; the DHT message id is a claim nothing proves (unsigned), so that stops a client that keeps its id and not one that lies, until the RPCs are signed (a separate card). It wins over every allow anywhere in the file. Not `free_nodes`, which only picks, among nodes already allowed, who is served at a tariff of 0; and not the `not_allowed` **refusal code**, which refuses one inference request and leaves the connection standing. A block names a key, so it does not survive the peer's reinstall: a new key is a new node id. Written and cleared only by **Block / Unblock**; an editor Save keeps the list on disk and drops any entry that is a blocked id, so a snapshot from before the Block puts nothing back. Mike's call, 2026-10-31.
- **Defined in:** [docs/CONFIGURATION.md — Firewall rules](CONFIGURATION.md#firewall-rules-privacy_rulesjson)

### Block / Unblock

- **Met in:** screen
- **Meaning:** What a person does to a peer to keep it out, and to let it back. Block writes the id into **`blocked_nodes`**, removes it from every chat group whose roster holds it, deletes every other rule in `privacy_rules.json` that names it, ends its session and its file transfers, and drops its DHT routing entry and stored value; the deletions are not undone. Unblock removes the id from the list and nothing else: it dials nobody and re-adds no group and no rule. Not Disconnect, which only ends the current session.
- **Defined in:** [docs/GROUP_CHAT.md — Group Management](GROUP_CHAT.md#group-management)

### `block_peer` / `unblock_peer`

- **Met in:** code
- **Meaning:** The WebSocket commands behind **Block / Unblock**, beside `connect_to_peer` / `disconnect_from_peer`. `block_peer {node_id}` answers `removed_from_groups`, `removed_rules` (the dotted path of each removed rule or list) and `was_connected`; it refuses this node's own id, a malformed id and one already blocked. `unblock_peer {node_id}` refuses an id that is not blocked. Both broadcast `firewall_rules_updated`.
- **Defined in:** [docs/GROUP_CHAT.md — WebSocket Commands](GROUP_CHAT.md#websocket-commands)

### proved connection

- **Met in:** code, rules, screen
- **Meaning:** A connection on which the peer's key was proved — today direct TLS only (`p2p_manager.PROVED_CONNECTION_TYPES`), the same line **peer_proved** draws on a usage row. It proves that the peer holds the key of its own node id and nothing about whether this node's user knows the peer: an inbound direct connection is accepted from any DPC node that can reach the port. Two doors stand on it: peer inference is served only over one, and `file_transfer.proved_connection` decides for a sender no node or group entry names — allow, ask or deny, and since 2026-10-25 (Mike's call) ask by default: the first file from such a peer is put to the user like any stranger's (**awaiting_permission**), and allow, with its limits, is the owner's opt-in. The screen calls it "Peers on a Direct Connection". Hub WebRTC, relay, UDP and gossip connections are not proved.
- **Defined in:** [docs/CONFIGURATION.md — Firewall rules](CONFIGURATION.md#firewall-rules-privacy_rulesjson), [`p2p_manager.py`](../dpc-client/core/dpc_client_core/p2p_manager.py)

### already_received

- **Met in:** code, wire
- **Meaning:** A FILE_CANCEL `reason`: the receiver already has this group file under the same identity (`group_id` + `message_id`, else `group_id` + hash) — received, receiving, or sent by itself — so the offer is refused out loud and the relaying sender frees its upload. Not a failure and not shown. The identity is unsigned, so a member can make the others refuse a copy by claiming its `message_id` first; the harm is a missing file, never a forged one. Mike's word, 2026-10-25.
- **Defined in:** [specs/dptp_v1.md §3.11](../specs/dptp_v1.md#311-file-transfer-messages-v0110), [`file_transfer_manager.py`](../dpc-client/core/dpc_client_core/managers/file_transfer_manager.py)

### no_answer

- **Met in:** code, screen, wire
- **Meaning:** A FILE_CANCEL `reason`: the offer expired unanswered. A receiver sends it when its user did not answer the question or the accept button within 120 s (`OFFER_ANSWER_DEADLINE_S`) and drops the download; a sender sends it when it releases an upload that had neither FILE_ACCEPT nor FILE_CANCEL for 180 s (`UNANSWERED_UPLOAD_RELEASE_S`), so a receiver that never sends it cannot hold an upload slot for good. Neither a refusal (**user_rejected**) nor a failure: the sender is told the receiver did not answer, in the tone of information. The two numbers are constants, not settings. Mike's word, 2026-10-25.
- **Defined in:** [specs/dptp_v1.md §3.11](../specs/dptp_v1.md#311-file-transfer-messages-v0110), [`file_transfer_manager.py`](../dpc-client/core/dpc_client_core/managers/file_transfer_manager.py)

### not_offered / upload_limit

- **Met in:** code, screen
- **Meaning:** Two keys in the answer of a group file send (`send_group_file`, `send_group_image`, `send_group_voice_message`), not wire. `not_offered` lists the connected members that were given no offer, each with a `reason` — `max_concurrent_transfers` for the cap, `send_error` with a `detail` for anything else; `upload_limit` is there when the cap was the cause: the `limit`, the `open_uploads` and the `sentence` a one-to-one send at the cap raises too. The screen shows a toast and a system line in the group chat. Mike's call, 2026-10-25.
- **Defined in:** [GROUP_CHAT.md — File Transfer / Voice / Screenshots](GROUP_CHAT.md#file-transfer--voice--screenshots--voice-transcription), [`file_transfer_manager.py`](../dpc-client/core/dpc_client_core/managers/file_transfer_manager.py)

### saved_locally / offline_members

- **Met in:** code, screen
- **Meaning:** Two keys in the answer of `send_group_file`, not wire. `saved_locally` says the sender's copy in the group's `files/` and its history record exist before any member is offered the file; `offline_members` lists the other member nodes that were not connected and so were not offered it. The screen reads "Saved in the group" when nobody was offered the file. Mike's call, 2026-11-19.
- **Defined in:** [GROUP_CHAT.md — File Transfer / Voice / Screenshots](GROUP_CHAT.md#file-transfer--voice--screenshots--voice-transcription), [`service.py`](../dpc-client/core/dpc_client_core/service.py)

### FILE_OFFER `text`

- **Met in:** wire
- **Meaning:** The optional words written with a file, carried on its FILE_OFFER and copied unchanged by every relay, so a file and its text are one message on every node; a member's record of the file uses it instead of "Received file: …". Absent or not a non-empty string means no text; an older peer ignores it. Mike's call, 2026-11-19.
- **Defined in:** [specs/dptp_v1.md §3.11](../specs/dptp_v1.md#file_offer), [GROUP_CHAT.md — File Transfer / Voice / Screenshots](GROUP_CHAT.md#file-transfer--voice--screenshots--voice-transcription)

### composer / pending file

- **Met in:** code, screen
- **Meaning:** The composer is the message input with what waits to be sent with it; a pending file (`pendingFile`) is a picked file shown there as a removable chip, as a pasted screenshot is, one attachment per message, the newer replacing the older. Send sends the file alone or with the text. Mike's call, 2026-11-19.
- **Defined in:** [GROUP_CHAT.md — File Transfer / Voice / Screenshots](GROUP_CHAT.md#file-transfer--voice--screenshots--voice-transcription), [`pendingAttachment.ts`](../dpc-client/ui/src/lib/utils/pendingAttachment.ts)

### FILE_REQUEST / FILE_REQUEST_DECLINED

- **Met in:** wire
- **Meaning:** A node that holds a record but not its file asks one connected peer at a time (the author first, then members) for it by `message_id`, name, size and hash; a holder answers with an ordinary FILE_OFFER carrying the request's `request_id`, anyone else with FILE_REQUEST_DECLINED. The first step of the pull that [ADR-047](decisions/047-multi-source-file-transfer-and-peer-file-search.md) plans. Mike's call, 2026-11-19.
- **Defined in:** [specs/dptp_v1.md §3.11](../specs/dptp_v1.md#file_request)

### not_held / busy / not_the_requested_file

- **Met in:** wire, screen
- **Meaning:** Reasons around a FILE_REQUEST. `not_held`: the one answer every peer that will not serve gives, whether it lacks the file or the requester may not have it, so a refusal tells nothing about what a node holds (word from [ADR-047](decisions/047-multi-source-file-transfer-and-peer-file-search.md)). `busy`: the holder's upload cap or a per-requester or per-file limit, with `retry_after_sec`. `not_the_requested_file`: the FILE_CANCEL a requester sends when an answering offer's name, size or hash is not the record's. Mike's call, 2026-11-19.
- **Defined in:** [specs/dptp_v1.md §3.11](../specs/dptp_v1.md#file_request)

### request_attachment / file_request_status

- **Met in:** code, screen
- **Meaning:** The local API command behind the Request file button, and the UI event that reports its states (`requesting`, `receiving`, `received`, `not_held`, `no_answer`, `busy`, `failed`). Mike's call, 2026-11-19.
- **Defined in:** [specs/dptp_v1.md §3.11](../specs/dptp_v1.md#file_request), [`file_request.py`](../dpc-client/core/dpc_client_core/managers/file_request.py)

### available / content_verified

- **Met in:** code, screen
- **Meaning:** Two attachment fields. `available: false` is set when history is served and the attachment's file is not on this node; only then does the chat show "Not on this node" and a Request file button. `content_verified: false` marks a requested file accepted on name and size because its record carried no hash. Mike's call, 2026-11-19.
- **Defined in:** [specs/dptp_v1.md §3.11](../specs/dptp_v1.md#file_request), [`attachmentRequest.ts`](../dpc-client/ui/src/lib/utils/attachmentRequest.ts)

### bytes_per_second

- **Met in:** code, screen
- **Meaning:** A `file_transfer_progress` field, both directions: the transfer's current speed in bytes a second, an integer, measured over the last 5 s of chunks (`SPEED_WINDOW_S`); 0 until a second of chunks has been seen. The event goes out on the first chunk, then at most once a second (`PROGRESS_INTERVAL_S`), then on the last. The Active Transfers row shows it beside the percent as `1.1 MB/s`. Local API, not wire. Mike's name, 2026-10-29.
- **Defined in:** [`transfer_speed.py`](../dpc-client/core/dpc_client_core/managers/transfer_speed.py)

### seconds_left

- **Met in:** code, screen
- **Meaning:** A `file_transfer_progress` field beside **bytes_per_second**: the estimated seconds to completion at that speed, an integer rounded up, or null while the speed is not known or is 0, and then the row shows neither figure. The row reads it as `~2 h left`. Local API, not wire. Mike's name, 2026-10-29.
- **Defined in:** [`transfer_speed.py`](../dpc-client/core/dpc_client_core/managers/transfer_speed.py)

### cost_reference

- **Met in:** code, screen
- **Meaning:** A summary-side sibling of `cost`: the per-currency total of a group's rows whose `cost_basis` is `list_price_reference` — a price quoted for a call a subscription or free key covered, so no money moved. Kept out of `cost` (which sums `charged` and `unknown` amounts) so the Usage screen does not show a spend that never happened; shown beside `cost` as a secondary, muted figure ("list price … · not charged"). Added 2026-09-28 (THE-USAGE-SCREEN-SUMS-A-LIST-PRICE-REFERENCE-AS-IF-IT-WERE-SPENT).
- **Defined in:** [decisions/041, amendment 2026-09-28](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d3--one-usage-row-per-model-call-on-the-node-that-ran-it-carrying-who-called), [`node_ledger.py`](../dpc-client/core/dpc_client_core/node_ledger.py)

### cost_amount

- **Met in:** code, rules
- **Meaning:** What a call cost the node that ran it, as four columns that are one field: `cost_amount` (a number or `null`) in `cost_currency` (ISO 4217) on a `cost_basis` — `charged` (debited), `list_price_reference` (a price quoted for a call a subscription covered) or `unknown` (priced, but whether it was debited could not be read) — with `cost_unpriced_reason` saying why when nobody priced the call. Priced once, at the moment of the call, by the provider's own report or else by the provider type. A non-zero amount needs its currency; `0` in no currency is a local card's free call; `null` is **unpriced**, and is what every guest's row carries, because the guest ran nothing and prices nothing. Replaced `cost_usd` on 2026-09-28; a row written before that is read with its `cost_usd` as USD, `charged`, except the zero a local card was written with, which reads as free.
- **Defined in:** [decisions/041 D3, amendment 2026-09-28](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d3--one-usage-row-per-model-call-on-the-node-that-ran-it-carrying-who-called), [`node_ledger.py`](../dpc-client/core/dpc_client_core/node_ledger.py)

### billing

- **Met in:** code, rules, wire
- **Meaning:** Which pool a call's cost belongs to — `subscription` or `pay_per_use` — as `dpc_agent/pricing.py` classifies the alias and model; a label on the usage row, not an amount.
- **Defined in:** [decisions/041 D3](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d3--one-usage-row-per-model-call-on-the-node-that-ran-it-carrying-who-called)

### served_effort

- **Met in:** code, rules, wire
- **Meaning:** The usage-row column and `REMOTE_INFERENCE_RESPONSE` field naming the **reasoning effort** the host actually ran a call at — the rung its provider reports having sent, where the provider reports one, and otherwise the guest's word clamped to the host's cap or the alias's configured word. Null means no word describes the call — the alias has no **effort channel**, or its host could not read its own configured word — and is never the same as `off`. Distinct from the word the caller asked for and from the alias's configured word.
- **Defined in:** [specs/dptp_v1.md §3.4](../specs/dptp_v1.md#34-remote-ai-inference)

### effort channel

- **Met in:** code, rules, wire
- **Meaning:** Whether a provider class can forward a **reasoning effort** word to its engine at all, declared by the class itself (`AIProvider.reasoning_words_served`, empty and fail-closed by default) and read by both doors from that one declaration. llama.cpp (`chat_template_kwargs`), DeepSeek (`reasoning_effort`) and Ollama (`think`) forward the ladder; Z.AI forwards only `off`; the other eight — Anthropic, the OpenAI-compatible provider, Gemini, GigaChat, GitHub Models, DpcAgent, RemotePeer and LocalWhisper — forward none, three of them naming `reasoning_effort` in a signature and saying in the code that they do not send it. What `served_effort` reports on, and why an alias with no channel offers an empty `reasoning_words` menu row and refuses every word, `off` included.
- **Defined in:** [decisions/041 D4, amendment 2026-09-14](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d4--shipping-order-and-the-colleague-is-served-at-step-4), [specs/dptp_v1.md §3.4](../specs/dptp_v1.md#34-remote-ai-inference)

### rung

- **Met in:** code, rules, screen, wire
- **Meaning:** One step of an alias's own reasoning ladder, as against the shared-scale word a caller asks in (**reasoning effort**). `_rung_of` answers what a word names on one alias: a provider whose ladder is its model's own answers for itself, the rest stand on the shared scale, and a word that reaches no rung is refused by name with the words the alias does accept. The distinction is why `served_effort` reports a rung and not the word that asked for it — an entry point may run a rung the configuration does not name.
- **Defined in:** [specs/dptp_v1.md §3.4](../specs/dptp_v1.md#34-remote-ai-inference)

### tariff

- **Met in:** code, rules, screen, wire
- **Meaning:** The rate an owner declares for a shared alias — per 1M input and per 1M output tokens, in that alias's currency (**compute.currency**), dated — applied once at the moment of a call and written into both nodes' usage rows as the applied values. `null` means no tariff was declared (a gift), `0` means declared free, `>0` means paid. Distinct from `cost_amount` (what the call cost the host) and from `charge`, which is reserved for the act of settlement.
- **Defined in:** [decisions/041 D3, amendment 2026-09-10](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d3--one-usage-row-per-model-call-on-the-node-that-ran-it-carrying-who-called)

### unpriced

- **Met in:** code, rules, screen
- **Meaning:** A **usage row** whose `cost_amount` is null, with no currency and no basis: a call nobody priced, `cost_unpriced_reason` saying why where the writer knew. The ledger's readers count such a row rather than summing it as zero, and `spent_today` adds nothing for it, so an unpriced row can never fill a ceiling. Not free — a declared `0.0` is a price somebody computed — and not **untariffed**, which is about what a guest was charged rather than what the call cost the node that ran it. Every `route=peer` row is unpriced by construction: the guest ran nothing and prices nothing.
- **Defined in:** [decisions/041 D3](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d3--one-usage-row-per-model-call-on-the-node-that-ran-it-carrying-who-called), [`node_ledger.py`](../dpc-client/core/dpc_client_core/node_ledger.py)

### untariffed

- **Met in:** code, rules
- **Meaning:** A served call for which the host declared no **tariff** — a gift. Counted, never summed as a zero, because a missing tariff is not a declared free one; the usage tab marks a group `gift` when every row in it is untariffed. The sister state of **unpriced** on the other side of the same row: unpriced is what the call cost the node that ran it, untariffed is what the guest was charged for it.
- **Defined in:** [decisions/041 D3, amendment 2026-09-10](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d3--one-usage-row-per-model-call-on-the-node-that-ran-it-carrying-who-called), [`node_ledger.py`](../dpc-client/core/dpc_client_core/node_ledger.py)

### unpriceable

- **Met in:** code, rules, screen
- **Meaning:** A **tariff** was declared and the amount could not be computed from the row's own counts. The ledger counts the row (`tariff_unpriceable`) instead of adding a zero, and the usage tab shows it as an `N unpriceable` badge with the "counted, not summed" note that stands on it. Distinct from **untariffed**, where there was no rate to apply, and from **unrated**, which refuses a call before it runs rather than describing one that did.
- **Defined in:** [decisions/041 D3](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d3--one-usage-row-per-model-call-on-the-node-that-ran-it-carrying-who-called), [`node_ledger.py`](../dpc-client/core/dpc_client_core/node_ledger.py)

### unrated

- **Met in:** code, rules, wire
- **Meaning:** A vendor alias whose model `dpc_agent/pricing.py` has no rate for. What such a call spends cannot be counted against its ceiling in `compute.vendor_quotas` — the meter is absent rather than slow — so both doors refuse it rather than serve it against a ceiling that would read `0.00` for ever: the word `unrated` on the wire, and 503 with no `Retry-After` at the guest. The repair is a rate on the host and never waiting, which is why it was split off `insufficient_quota` on 2026-09-15. Not an **unpriced** row: that is the record of a call that ran.
- **Defined in:** [specs/dptp_v1.md §3.4](../specs/dptp_v1.md#34-remote-ai-inference), [decisions/041 D5](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d5--api-backed-models-are-shareable-and-the-quota-is-a-financial-control)

### served_by

- **Met in:** code, rules
- **Meaning:** The node that ran a call this node only consumed: the host's `node_id` on a `route=peer` usage row, written beside the tariff group because the two answer one question together — what the call was charged and by whom. Absent, never null, on a row this node ran itself, where there is no other node to name; an `alias` is unique only under its host, so the reader keys a consumed row `remote:<served_by>:<alias>`. A `route=peer` row written before the column names no host and keys `remote:?:<alias>` (`node_ledger.UNKNOWN_HOST`), shown as "host not recorded": a node id is `dpc-node-<32 hex>`, so `?` is a name no host can have and the bucket collides with none. Keyed by the bare alias, as such a row was until 2026-09-15, it carried the very string a local alias of that name uses, and a node that both serves and consumes one alias name folded the two into one line.
- **Defined in:** [decisions/041 D3, amendment 2026-09-14](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d3--one-usage-row-per-model-call-on-the-node-that-ran-it-carrying-who-called)

### `remote:?:<alias>`

- **Met in:** code, screen
- **Meaning:** The consumed key of a **usage row** written before **served_by** existed. A `route=peer` row is keyed `remote:<served_by>:<alias>`, and a row that names no host takes `node_ledger.UNKNOWN_HOST`, `?`, in the host's place: a node id is `dpc-node-<32 hex>`, so `?` is a name no host can have and the bucket it keys collides with none. The usage tab reads it as a `host not recorded` badge rather than naming a host it does not know. Keyed by the bare alias, as such a row was until 2026-09-15, it carried the very string a local alias of that name uses.
- **Defined in:** [`node_ledger.py` `consumed_key`](../dpc-client/core/dpc_client_core/node_ledger.py)

### free_nodes / free_groups

- **Met in:** code, rules
- **Meaning:** The subset of the allowed nodes and groups that a shared alias is served to at a tariff of 0 — free for these, the tariff for everyone else allowed. `allow_nodes` / `allow_groups` remain the door; the pair mirrors the transcription block's `allow` / `send_to`.
- **Defined in:** [decisions/041 D3, amendment 2026-09-10](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d3--one-usage-row-per-model-call-on-the-node-that-ran-it-carrying-who-called)

### compute.currency

- **Met in:** code, rules, wire
- **Meaning:** The default ISO 4217 unit a node declares its tariffs in — every local alias, and a vendor alias whose provider names none. Since 2026-09-28 an alias may carry its own (`compute.tariff_currency`), and a vendor alias defaults to the currency its provider bills in. A usage row freezes the resolved unit into itself (`tariff_currency`), so rows outlive a changed configuration; the protocol picks no currency, and parity between two nodes' units is their own agreement.
- **Defined in:** [decisions/041 D3, amendment 2026-09-10](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d3--one-usage-row-per-model-call-on-the-node-that-ran-it-carrying-who-called)

### serving lists

- **Term note:** (`serving_local`, `serving_vendor`, `vendor_quotas`)
- **Met in:** code, rules, screen
- **Meaning:** What a node serves to peers and to its own gateway: local aliases, bounded by the card, and vendor aliases, bounded by a per-day ceiling per caller in `vendor_quotas`, in the currency the alias's provider bills in; an alias in neither list is not served, and neither is one whose provider has left providers.json: it is dropped from the lists with a WARNING and shown on the tab (`serving_absent`) while the rest keep serving, unless the registry holds no provider at all, which refuses the lists (amendment 2026-11-11, Mike's call). Every alias in either list is served over P2P — `serving_local` since 2026-09-18, `serving_vendor` since 2026-09-28 (amendments; the vendor list to the peers `allow_nodes` / `allow_groups` admit, the owner's choice, with the vendor's terms the owner's to check) — and `serving_alias`, one of `serving_local` and never a vendor alias, is what a peer request naming no provider is served from; absent, the first `serving_local` entry is (ADR-044 point 9, 2026-10-18). Alone, with no `serving_local`, `serving_alias` is read as `serving_local = [alias]`. Standing on a list is necessary and not sufficient: a vendor alias no rate table can price is refused rather than served for free, because a ceiling summed from rows that all cost 0.00 could never bind; and a transcription-only alias is left off the chat menu and refused at the door with `model_not_found` saying it transcribes, on this node's own lists as on a peer's rows.
- **Defined in:** [decisions/041 D5, amendment 2026-09-10](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d5--api-backed-models-are-shareable-and-the-quota-is-a-financial-control)

### vendor_request_quotas

- **Term note:** (`vendor_request_quotas`, `vendor_token_quotas`, `vendor_owner_reserve`)
- **Met in:** code, rules, wire
- **Meaning:** The guest ceiling on a subscription-billed vendor alias, counted in the vendor's own units rather than money — `vendor_quotas`'s ceiling never trips there, since its rows are `list_price_reference`. `vendor_request_quotas.<alias>` (`per_session`, `per_week`) bounds requests per guest inside the vendor's own fixed window (`resets_at` minus the window's length); `vendor_token_quotas.<alias>.per_day` bounds tokens per guest per UTC day; `vendor_owner_reserve.<alias>` (default `0.25`) keeps a fraction of the vendor's window for the owner against all guests together. Counted per vendor account (an alias and its `-noreason` twin share one key), not only the named alias. A subscription alias with no `vendor_request_quotas` entry is refused to a guest (strict); the vendor's own `daily_capacity.exhausted` refuses every caller alike, guest or owner. Every refusal carries `retry_after_sec` where the clearing time is known (`insufficient_quota` on both doors). Keyed by alias like `vendor_quotas`, so a rename carries them too.
- **Defined in:** [decisions/041 D5, amendment 2026-09-29](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d5--api-backed-models-are-shareable-and-the-quota-is-a-financial-control), [guest_vendor_quota.py](../dpc-client/core/dpc_client_core/guest_vendor_quota.py)

### quota block

- **Met in:** code, screen
- **Meaning:** The normalized shape `NeuralDeepProvider._quota_from_limits` (and `get_balance()`'s `quota` field) builds from a vendor's raw `/v1/limits` payload: `tier`, `billing_mode`, `can_request`, `blockers`, `retry_after_sec`, `windows` (named request/volume windows — session, week, per-minute — each `used`/`limit`/`remaining`/`resets_at`), `parallel_limit`, `observed_at`, `blocked_models`, plus two entries kept out of `windows` because neither is a request-count window: `daily_capacity` (a spend ceiling — `pct_used` 0–100 already on that scale, never re-multiplied; `exhausted`; `resets_at`) — no longer display-only since ADR-041 D5's 2026-09-29 amendment, where `daily_capacity.exhausted` gates every caller, guest and owner alike, in `guest_vendor_quota_refusal` — and `night` (whether the night capacity multiplier is active right now — every other number in the payload is already effective with it applied, so `capacity_factor` is informational only and must never be applied again). Provider-neutral by design, so another vendor's own reader can fill the same shape; missing fields are omitted or None rather than a crash. Distinct from the wallet balance (`balance_infos`), which answers "how much money", where the quota block answers "how many requests, of which kinds, until when".
- **Defined in:** [decisions/041, amendment 2026-09-28](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d5--api-backed-models-are-shareable-and-the-quota-is-a-financial-control), [`neuraldeep_provider.py`](../dpc-client/core/dpc_client_core/providers/neuraldeep_provider.py)

### vendor account

- **Met in:** code, screen
- **Meaning:** One wallet, grouped across every alias that spends from it: `service.get_provider_balances`'s `accounts` list (`{account, provider_type, label, aliases, result}`) beside its per-alias `balances` map. DeepSeek and NeuralDeep each bill one wallet per key regardless of how many aliases point at it, so the account id (a provider type, its base_url, and a truncated hash of the key — never the key itself) groups those aliases and only the first is actually queried; an alias with no identifiable key is its own singleton account rather than being merged by guess.
- **Defined in:** [decisions/041, amendment 2026-09-28](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d5--api-backed-models-are-shareable-and-the-quota-is-a-financial-control), [`service.py` `get_provider_balances`](../dpc-client/core/dpc_client_core/service.py)

### menu row / menu

- **Met in:** code, rules, screen, wire
- **Meaning:** One `providers` entry of a PROVIDERS_RESPONSE, resolved by the host for one recipient: `alias`, `model`, the `supports_*` flags and **serves_images_with_tools**, `reasoning_words`, `reasoning_default`, the **tariff** and the `settings` block. The **menu** is the set of rows a host would send that peer (`service.menu_for_peer`), which is not the set a door offers: a transcription-only alias stands on the menu behind its own permission and is refused at the chat door with `model_not_found` saying it transcribes (`608cd4fa`). A guest reads a row fail-closed — an absent key is the host's silence, never a default — and the row, not the guest's own table, is the dictionary a **rung** is checked against.
- **Defined in:** [specs/dptp_v1.md §3.5](../specs/dptp_v1.md#35-provider-discovery), [`service.py` `menu_for_peer`](../dpc-client/core/dpc_client_core/service.py)

### serves_images_with_tools

- **Met in:** code, rules, wire
- **Meaning:** The **menu row** field saying the host serves a request whose `messages` hold `image` blocks beside `tools` on that alias — the pictures in their turns, the tools called. It names the other kind of row flag: a `supports_*` flag (`supports_vision`, `supports_tools`) says what the provider can do, a `serves_*` flag says what the node's route will serve for a request of one shape, and the two are not interchangeable. Set from `entry_point_for(provider, tools=True, streaming=False, images=True)`, the predicate the host's gate and `query_messages` ask — never from the two `supports_*` flags multiplied. Fail-closed where `supports_vision` is not: a guest reads it absent as no, because a host that predates it may drop the pictures without a word; a guest refuses the combination with `tools_unsupported` before the round trip, and the host's own refusal stays the gate. Name accepted by Mike, 2026-09-17.
- **Defined in:** [specs/dptp_v1.md §3.5](../specs/dptp_v1.md#35-provider-discovery), [decisions/041 D4, amendment 2026-09-17](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d4--shipping-order-and-the-colleague-is-served-at-step-4)

### unit

- **Met in:** code, rules, wire
- **Meaning:** The scale a **tariff**'s `in` and `out` rates are quoted in, on the provider row that carries them. `per_1m_tokens` is the only value DPTP v1.7 defines, and a receiver that does not know the word must not price the row rather than guess a scale.
- **Defined in:** [specs/dptp_v1.md §3.5](../specs/dptp_v1.md#35-provider-discovery)

### max_output_tokens

- **Met in:** code, rules, screen, wire
- **Meaning:** The ceiling on one answer that a call on an alias will actually run at, carried under that one name whatever the provider's own field is called — `num_predict` on Ollama, `max_tokens` on DeepSeek and llama.cpp. The host's own dial, read from the host's configuration and stated so a guest sees what it is choosing, including the settings it cannot change; it is not a ceiling a caller sets. Fail-closed: absent means "not stated", never "no ceiling applies".
- **Defined in:** [specs/dptp_v1.md §3.5](../specs/dptp_v1.md#35-provider-discovery)

### variant

- **Met in:** code, screen, wire
- **Meaning:** In a **menu row**'s `settings`, the build behind `model` where the host can read one — a GGUF file name, say. Absent is legal and reads "not stated by the host", never a default: the guest's panel prints the line either way, because a dial the host did not state is itself worth reading. ADR-041 D7's older word for the same field is *quantization*, which names one property of a build rather than the build, and is being reworded. Not a music job's `variants`, the number of takes (**music job `variants`**).
- **Defined in:** [specs/dptp_v1.md §3.5](../specs/dptp_v1.md#35-provider-discovery)

### refusal code

- **Met in:** code, rules, wire
- **Meaning:** The machine-readable word beside the prose `error` on an error `REMOTE_INFERENCE_RESPONSE`, naming at which of the host's gates the call was refused so that a refusal keeps its reason across the hop. The eleven: `identity_unproved` (the connection's key was not proved — see **peer_proved**), `not_allowed` (the host's firewall does not let this peer ask for inference, or not for what it asked), `model_not_found` (the host serves no alias to this peer, or not the one named), `onward_sharing_refused` (the alias is itself somebody else's model, and what is shared is not shared onward), `invalid_value` (a gate that names the guest's own request — an effort word the alias has no **rung** for, or a `request_id` already **in flight** from this peer), `tools_unsupported` (tools sent to an alias with no native tool-calling path, or beside images on a path that cannot carry both — image blocks in `messages` to an alias that cannot see, or the flat `images` field, whose vision entry point takes no tools; see **serves_images_with_tools**), `insufficient_quota` (this guest's daily ceiling on a vendor alias, spent — 429 at the guest, come back tomorrow), `unrated` (a vendor alias `dpc_agent/pricing.py` has no rate for — 503, repaired by adding a rate), `misconfigured` (the host cannot read its own serving lists, so the class of the alias is unknown — 503, repaired by its owner), `provider_unavailable` (the host's local engine cannot take a peer now — on any local alias the card held by the other local engine, a request of the host's waiting for the card, or the engine stopping; on a Strata alias also not running and not to be started for this peer now, or stopped after an engine failure; not the owner's parked conversations since 2026-10-20, because a peer's call parks only when `guest_conversation_cache` is on and the cache has room, else it is sent with checkpointing off (ADR-044 amendment of 2026-11-13); 503, retryable later but not by a short wait, ADR-044 points 4 and 9; a refusal of the **card queue** is a card-held refusal, `CardQueueRefusal` being a `CardHeldError`, so it adds no code) and `provider_loading` (the host's local engine is loading its model, including a start a peer's request has just begun; the guest may re-ask shortly — 503, and a guest agent does re-ask every 10 s for up to 120 s; its own word because a refusal whose instruction differs gets its own code, Mike's call 2026-10-20, DPTP v1.9). `insufficient_quota`, `unrated` and `misconfigured` were one word until 2026-09-15 and were split while v1.7 is unreleased, because a code is read as an instruction and only the ceiling refills by itself. It rides beside `error` and never replaces it: the code is what a program reads and the text what a person reads. Absent means a failure mid-call rather than a gate, and is never itself a reason; a receiver reads an unknown word exactly as an absent one, because a newer host may name a cause this one has no word for, and no receiver refuses a message over its code.
- **Defined in:** [specs/dptp_v1.md §3.4](../specs/dptp_v1.md#34-remote-ai-inference)

### in flight

- **Met in:** code, wire
- **Meaning:** A `request_id` a host is serving for one peer right now. The guest mints the id that keys the host's **usage row** and routes its chunks, so only the host can see a reuse; the pair (peer, id) is held unique for a non-empty id only (`if request_id:`) and released in the `finally`, which is to say when the call has ended, answered or failed alike. A reuse while the first call is still in flight is refused `invalid_value` before the queue and before any row, because two calls under one id would cross their chunk streams and collapse two rows into one — the card is `THE-GUEST-CHOOSES-THE-REQUEST-ID-THAT-JOINS-ITS-ROW-TO-THE-HOSTS-AND-THE-HOST-NEVER-CHECKS-IT`.
- **Defined in:** [specs/dptp_v1.md §3.4](../specs/dptp_v1.md#34-remote-ai-inference)

### endpoint_not_served

- **Met in:** code, rules
- **Meaning:** The gateway's own 404 code for a route it deliberately does not serve — `/v1/embeddings`, `/v1/completions` and Anthropic's legacy `/v1/complete` — answered with a sentence naming the route and the chat routes that are served, rather than aiohttp's bare 404, which a client cannot tell from a wrong port or a door that is down. A door-local word, in the family of `model_not_found` and `card_busy`: no DPTP command learns it, because nothing crosses the wire.
- **Defined in:** [decisions/041 D1, amendment 2026-09-14](decisions/041-openai-compatible-gateway-as-a-dpc-surface.md#d1--the-openai-surface-is-a-component-of-the-dpc-client-on-the-consumers-loopback)

### is_connected

- **Met in:** code
- **Meaning:** On a peer row of the node status, that a live P2P connection to that peer exists right now. Every such row is built from `p2p_manager.peers`, which holds the connections that exist, so the field says what the list already meant rather than leaving each reader to infer it. Distinct from known — the caller picker offers every node the application holds a name for, connected first — and from allowed, which is the **firewall / privacy rules** answer and no part of this field.
- **Defined in:** [`service.py` `get_status`](../dpc-client/core/dpc_client_core/service.py)

### message signing / preimage

- **Met in:** code, rules, wire
- **Meaning:** A message signature covers a canonical preimage of eleven fields in a fixed order, tagged `dptp-msg-v3`, the eleventh being **mention_depth**; a record signed over `dptp-msg-v1` or `dptp-msg-v2` (ten fields each) still verifies under its own field set. Signed by the author at send time, never re-signed by a receiver.
- **Defined in:** [specs/dptp_v1.md §4.1](../specs/dptp_v1.md#41-message-signing), [decisions/036](decisions/036-message-authenticity-signed-at-origin.md#decision)

### processed message ids

- **Term note:** (`ProcessedMessageIds`, `service._processed_message_ids`)
- **Met in:** code
- **Meaning:** The one window of wire message ids this node has already handled, shared by every handler that drops a repeat (group text, 1:1 text, knowledge, session, voice transcription, file completion). Insertion-ordered and bounded in `add`, its only evicting call: past `max_size` the oldest half goes, and a key already present keeps its place, so it is the first sighting that ages. Until 2026-11-13 it was a plain set trimmed in hash order at three of its twelve users and unbounded at the other nine. A dedup by id, not by content: `conversation_monitor.message_ids` is the per-conversation one.
- **Defined in:** [`processed_message_ids.py`](../dpc-client/core/dpc_client_core/processed_message_ids.py)

### mention_depth

- **Met in:** code, rules, wire
- **Meaning:** The number of cross-node agent mentions behind an agent's post, signed from `dptp-msg-v3` on. An agent post from another node wakes an agent here only at 0, and the reply of the agent it wakes carries one more; a hop inside one node passes the number on unchanged. Absent on a v3 agent post is 0 under the signature; an agent post signed over v1 or v2, or not `verified`, wakes nobody. An external agent's bridge is handed the number on its `cc_group_mention` event and carries it to its next post in that group itself; `--mention-depth` overrides.
- **Defined in:** [specs/dptp_v1.md §4.3](../specs/dptp_v1.md#43-agent-mentions-across-nodes)

### verdict (verified / unverified / legacy)

- **Met in:** code, rules, wire
- **Meaning:** What a receiver concludes about an arriving record: hash and signature check against a cached certificate; the same except the certificate is not yet cached; or no signature this receiver can recompute. The verdict is the receiver's, and absence of a signature is never acceptance.
- **Defined in:** [specs/dptp_v1.md §4.2](../specs/dptp_v1.md#42-verdicts-on-receipt)

### deferred

- **Met in:** code
- **Meaning:** The import status of an AI chat whose history the one-time import from the old UI store could not write because persistence is off for that chat: the chat is not settled, stays in the old store and is asked again at the next start, and its record carries `history_deferred` meanwhile so it stays listed. The same word on a record and in an answer; not the held vote of a group's knowledge commit (`knowledge_vote_deferred`, see **consensus / devil's advocate**).
- **Defined in:** [`ai_chats.py`](../dpc-client/core/dpc_client_core/ai_chats.py)

### voice-memory

- **Met in:** code, rules
- **Meaning:** The one optional install extra (`pyproject.toml`) that carries the machine-learning stack: local Whisper voice transcription (torch, transformers, accelerate, librosa, soundfile, sentencepiece) and agent memory search (sentence-transformers with bge-m3, faiss-cpu). Off the default install; absent, the client starts and those two features say what is missing. Add it with `uv sync --extra voice-memory --inexact` in `dpc-client/core` (needs `uv`); `graph-ner` includes it. The backend says whether it is there in the `get_status` payload as `voice_memory_installed` (false means missing; an older backend omits the field), and the UI toggles for voice and memory show the command when it is false.
- **Defined in:** [QUICK_START.md — Optional features](../QUICK_START.md#optional-features), [pyproject.toml](../dpc-client/core/pyproject.toml)

### CLI

- **Met in:** code, rules
- **Meaning:** The backend's command-line client, `dpc_client_core/cli.py`, run as `python -m dpc_client_core.cli`: a client of a backend that is already running on the same machine (token from `.ws_token`, local API on 9999), not a second backend. It connects to a peer, reports which transport the link uses, and reads or rotates the gateway key.
- **Defined in:** [QUICK_START.md — Backend only](../QUICK_START.md#backend-only-a-peers-models-for-your-own-tools)

## Processes the backend starts

Its own short section: these words are neither the agent's nor the wire's, they belong to
ADR-046, and a reader looking for "what is running and who started it" should find them
together. None is on the wire, and none is on a screen yet, so the tokens are `rules`
(and `code` where the word already exists in the source); the registry core is built
(`4ea0560f`), the sampler (`3d380706`), the local API command `list_processes` (and
`get_process_command`, which reads a running process's full command line from the OS on
request and stores nothing) and the agent tools `list_my_processes` and `kill_my_process` too, and the screen, the Agent map. The
agent's **turn** and the chats **waiting** for it are shown on the Agent map and as a state
line under each agent in the sidebar; the open chat shows no line of its own (Mike's call,
2026-10-27).

### process registry

- **Met in:** code, rules
- **Meaning:** The backend's one list of the processes it starts, kept and persisted by the backend itself: per entry the `pid` and start time, the parent (`parent_pid`, `parent_start_time`), who started it (the **spawn origin**) and with what (the **launcher**), start and end, how it ended (**exit reason**, `exit_code`, `signal`) and the **resource peaks**. Because every entry names its parent, the list is a tree rooted at the backend, and the graph on screen — backend, agent, conversation, tool call, process, its children — is a view over it, never a second store. The same on Windows, Linux and macOS; only the kill-on-close job under it is Windows-only, so on Linux and macOS the guarantee is weaker: with no kernel mechanism, after a backend crash the children live until the next start's sweep. Kept in two places, written only by the persistent registry (the in-memory default and the tests never touch the home): the state file `process_registry.json` holds the open entries and the newest 200 finished ones (what the sweep and "what runs now" read), and every finished entry is also appended as one JSON line to `process_registry/YYYY-MM-DD.jsonl`, the UTC date of its end (what the graph reads); a daily file rotates past 10 MiB as `YYYY-MM-DD.N.jsonl`, files older than 7 days are removed, and all history is held to 50 MiB, oldest first (Mike's order, 2026-11-03). A subsystem started inside an agent's tool call keeps that call's **spawn origin**; its owner and parent are the backend. Not the registry of agents (`AgentRegistry`) nor of tools. Accepted by Mike, 2026-10-25; the job is built (`2ebdb0a3`), the registry core is built (`4ea0560f`) and amended after review the same day.
- **Defined in:** [decisions/046 — Decision, point 3](decisions/046-the-backend-owns-every-process-it-starts.md#decision), [New words](decisions/046-the-backend-owns-every-process-it-starts.md#new-words)

### exit reason

- **Met in:** code, rules
- **Meaning:** One word, closed, the same in the log, the API and the UI, for how a registry entry ended. Eight: `exited` (on its own, any code, 0 included), `timeout`, `memory_ceiling` (our watcher at `ceiling_mb`), `backend_stop` (the backend's orderly stop), `cancelled_by_agent` (`kill_my_process`), `orphan_swept` (found alive at start and killed), `crashed` (the registry sent no stop; a number or a signal only confirms it) and `stopped` (a stop DPC asked for while the backend runs). `stopped` alone carries `stop_kind`, one of `ui_stop`, `swap` (a local engine stopped because another start needed the card: the idle swap, the card queue, a borrow and its way back, with no person's press; 2026-11-05), `failed_start` (a start that did not finish, or was refused at the version check or the smoke), `engine_died` (the engine died after its start had succeeded and DPC stopped the launcher; Strata's only producer today), `browser_closed` and `idle_unload`; the last two have no producer yet and stay on a named decision (`browser_closed` on browser registration, `idle_unload` on the owner's choice of who unloads an idle model), and `engine_switch` is not in the list: a switch of engines is `swap`, since the idle swap stops a holder with no press, and a person's Stop engine stays `ui_stop`. A value stays in a closed list only if something produces it or a named decision will create its producer. Precedence: a stop announced after the backend's orderly stop began is `backend_stop` whoever sent the signal; one announced before it keeps its word. A kill nobody in DPC sent reads differently by platform: on POSIX a foreign SIGKILL reads `crashed` with `signal` `SIGKILL` (stored by name, not number), on Windows a foreign `taskkill /F` leaves exit code 1 and reads `exited`, indistinguishable from an ordinary exit. `exit_code` and `signal` are kept on every entry; `exit_code: null` means this backend did not see the end, which is how an entry found gone at the next start reads (`backend_stop` with null). A path that ends a process without one of these words is a defect. The field is named `stop_kind` so as not to collide with the `stop_reason` the gateway writes on the wire (the Anthropic Messages API field), nor the browser tool's scroll-loop `stop_reason`; a registry file written under the first spelling is read as `stop_kind`. Mike's call, 2026-10-25, the rename and `engine_died` the same day on the reviews.
- **Defined in:** [decisions/046 — Decision, point 4](decisions/046-the-backend-owns-every-process-it-starts.md#decision), [Open Questions](decisions/046-the-backend-owns-every-process-it-starts.md#open-questions)

### shell-gate refusal

- **Term note:** (`SHELL_GATE_REFUSALS`, `is_a_refusal_result`)
- **Met in:** code, rules
- **Meaning:** A `run_shell` result saying the command was not run because the tool's gate refused it: an approval answer (nobody could be asked, it timed out, a person said no), a safety-guardrail rule, or a `cwd` check. Each text names what was refused, why, and that it was not run. Their texts do not start with the warning mark, so the loop reads `run_shell` results against `loop.SHELL_GATE_REFUSALS` as well, and `is_a_refusal_result` counts either kind as an error (`is_error`).
- **Defined in:** [ADR-046, amendment of 2026-11-10](decisions/046-the-backend-owns-every-process-it-starts.md)

### spawn origin

- **Met in:** code, rules
- **Meaning:** Who made a process start, as a fact written at its birth: `agent_id`, `conversation_id`, `tool`, `round` and `turn_id` of the tool call, set by the dispatcher in a context variable (`SPAWN_ORIGIN`) and read by every spawn site, so a site added later is attributed without being touched. Not the board's **origin** (who raised an entry and when); the two never meet in one file. A spawn no tool made has none and is `launcher_kind: subsystem`.
- **Defined in:** [decisions/046 — Decision, point 3](decisions/046-the-backend-owns-every-process-it-starts.md#decision)

### turn_id

- **Met in:** code
- **Meaning:** The loop's id of one **turn** (`_new_turn_id`: UTC time and 8 hex characters), on the **spawn origin** of every tool call the turn makes and on the registry entry of each process it starts (`ProcessEntry.turn_id`, returned by `list_processes`); it also names the turn's folder of stored tool output, `state/tool_results/<turn_id>/`. The user's Kill ends every open tool entry with that agent, conversation and `turn_id` — any round, any tool — and closes the agent's browser; an entry of an earlier turn, or with no `turn_id`, stays. Mike's go, 2026-10-30.
- **Defined in:** [decisions/046 — Decision, point 3](decisions/046-the-backend-owns-every-process-it-starts.md#decision)

### launcher / launcher_kind

- **Met in:** code, rules
- **Meaning:** `launcher` is the label `run_supervised` logs in `spawned pid N for <launcher>`: a tool's name (`run_shell`, `read_document`), a tool's name with a mode (`git_tool(sandbox)`), or, for a spawn no tool made, a subsystem (`strata`, `llamacpp_server`, `browser`, `device_probe`, `git_helper`, `acestep_server` and `yue2_server` — the music engines, ADR-051 D30, D35). `launcher_kind` says which, `tool` or `subsystem`, written at birth; `tool` with no **spawn origin** is a lost attribution and a defect, never read as "DPC started it itself". Not the Strata launcher, the program ADR-044's supervisor starts, which the Providers screen also calls the launcher.
- **Defined in:** [decisions/046 — Decision, point 3](decisions/046-the-backend-owns-every-process-it-starts.md#decision)

### conversation_id / chat

- **Met in:** code, rules, screen
- **Meaning:** One thing under two names: the field is `conversation_id`, the code's word, and "chat" is the screen's. The **spawn origin** and the registry use the code's. Its readable name is `conversation_title` (on `ToolContext`, the approval cards, the **turn** and the **waiting** items); `chat_name` is the older name of the same thing inside the context block a run is given, and stays there.
- **Defined in:** [decisions/046 — Decision, point 3](decisions/046-the-backend-owns-every-process-it-starts.md#decision)

### turn

- **Met in:** code, rules, screen
- **Meaning:** An agent's current run in one conversation, from the moment it holds the agent's **run gate** to the moment it lets go; null when the agent is idle. On the agent's row as `turn`: `conversation_id`, `conversation_title`, `started_at`, `round`, `provider_alias`, `tool`, `wait_reason`. The event `agent_turn_updated` carries `{agent_id, turn, waiting, ts}` whole at every change, and `turn: null` is the idle state, with no separate clear event. A turn holds many **round**s. The screen shows a turn with no event from its agent for 120 s as unconfirmed rather than working.
- **Defined in:** [decisions/046 — Decision, point 8](decisions/046-the-backend-owns-every-process-it-starts.md#decision)

### round

- **Met in:** code, rules
- **Meaning:** One model call inside a **turn**, with the tool calls it asked for; the loop's counter (`round_idx`), the one the progress line, the **spawn origin** and the turn's `round` carry.
- **Defined in:** [decisions/046 — Decision, point 8](decisions/046-the-backend-owns-every-process-it-starts.md#decision)

### turn_end

- **Met in:** code, rules, screen
- **Meaning:** Why a **turn** ended without an ordinary answer, on the assistant record it leaves in a 1:1 agent chat's history: `stopped` (the person pressed Stop), `killed` (a second press ended the model call in flight; outranks `stopped`), `failed` (the model call or the loop raised, and a local engine's refusal: the **card queue**'s, the card held with the swap off, an engine draining; no model failed there and the text is the refusal's own, without the "LLM error" label and the provider's wrap), `refused` (the host refused a peer call), `guard` (a guard ended it with no answer: round or context limit with no answer written after it, the window-full refusal, the daily quota, a **written call** still written when its retries were spent, a model that returned no text when an empty round's retries were spent, a reply still at the **output limit** when its continues were spent, and a finalising answer cut there). No field is an ordinary answer, including one that opens with a warning sign and one the model wrote after a guard stopped the loop (that fact stays in the turn's trace, `guard_answered`, in memory only). Set by the loop in the branch that exits, carried by the agent manager, never read off the text (`TURN_END_VALUES` in `conversation_monitor.py`). Outside `content_hash`, `chain_hash` and the signed preimage. A marked record is replayed into the next prompt (`failed` as its first line, at most 200 characters) and kept out of knowledge extraction and sleep. Group records do not carry it: in a group a `failed` turn posts `FAILED_TURN_NOTICE` (`dpc_agent/context.py`) in place of its error, which goes to the owner as an error toast, and a post from a turn with any `turn_end` routes no @mention on the sending node (Mike's go, 2026-10-30). Named by Ark, Mike's go 2026-10-27.
- **Defined in:** code: `conversation_monitor.py`, `dpc_agent/loop.py`

### task_type

- **Met in:** code
- **Meaning:** On a prompt record in a 1:1 or `telegram-` agent chat's history: the type of the scheduled task (`check_back`, `chat`) whose run wrote it. Such a record is named `Scheduled <task_type>` and carries this node's id, and the screen draws it under that name, not as the owner's own message. Absent on anything a person, CC or a peer sent. Not `source`, which names the transport a record came by (`telegram`, `discord`): the Telegram forward of a CC-chain answer skips a record that carries it, as it skips CC's. Outside `chain_hash`. Mike's go, 2026-10-30.
- **Defined in:** code: `conversation_monitor.py`, `managers/agent_manager.py`

### speed_summary

- **Met in:** code, screen
- **Meaning:** The finished **turn**'s header strip on an agent's message, a sibling of `tool_calls` (never inside it): `rounds`, the upper median over the **round**s of `prefill_tok_s` / `decode_tok_s` / `total_tok_s`, the last round's `context_used` / `context_window` / `context_reserve`, `cached_tokens` and `prompt_tokens` summed over the rounds that stated both (the header divides them: a token-weighted cache hit rate), `round`, `model`, `median: true`. Made once by the loop from the rounds' strips (`speed_summary_of`), a stopped or killed turn included, and stored on the record in a 1:1 agent chat and on this node's own agent posts in a group; the screen shows the stored one, and a record without it shows no strip. Local and unsigned: outside `content_hash`, `tool_calls_digest` and `chain_hash`, never sent to a peer or exported in a history sync, dropped from a peer's record on merge, not replayed into a prompt. Mike's order, 2026-11-12.
- **Defined in:** code: `dpc_agent/loop.py`, `conversation_monitor.py`

### written call

- **Term note:** (`_written_tool_calls`, `_keep_unexecuted`, `MAX_WRITTEN_CALL_RETRIES`, trace `written_call_retries`, `written_calls_beside`)
- **Met in:** code, rules
- **Meaning:** A tool call a model wrote into its text instead of making one: DSML, a lone `{name, arguments}`, a ```` ```tool_call ```` fence, or a `<tool_call>` block that starts a line outside code and ends the answer, by its own closer with only further blocks behind it; a block with words after it is a quote. Not an answer, so the loop asks once more with the reason: once since the last tool ran, three times in a **turn**, as a round that counts, and once in a finalising call. The text of the call is kept beside the turn's tool output from the first attempt and the reason says where; words in front of it stand above what the retry writes, or in that round's text if the retry makes the call. Still written, the **turn** ends with a not-executed note and `turn_end` `guard`; a tool of that name that ran earlier in the turn is not called never executed, and the note says to look at its result. After a guard stop up to three model calls can run (the finalising call, one with `tool_choice="none"` if it answered with calls, one more if the answer is a written call), none counted as a round, each with its usage row; ContextLimitGuard skips them. A written call handed over as one chunk (the tools path) reaches neither the screen nor `streaming_raw`; a reply streamed in pieces (the text path, a peer's relay) shows it live, and `_answer_extras` takes the tag form out of what is stored. It is no text for ResearchLimitGuard. One written beside native calls that ran (`tool_calls_native`; a call the adapter read out of the text is the call that ran) is taken out of the replayed turn and the agent is told. Strata's launcher names what it refused in `strata_rejected_tool_calls` (`rejected_tool_calls` on the reply); that is logged and goes into the reason, and the check on the text decides, because the launcher also counts a mid-sentence quote. A note the loop adds carries `_runtime_note`, so a turn's image description stays on the person's turn. Proposed 2026-11-04, Mike's word awaited.
- **Defined in:** code: `dpc_agent/loop.py`, `providers/llamacpp_server_provider.py`

### output limit

- **Term note:** (`_output_limit_of`, `_reached_the_window`, `_take_out_cut_calls`, `MAX_OUTPUT_LIMIT_CONTINUES_SINCE_A_TOOL`, `MAX_OUTPUT_LIMIT_CONTINUES_PER_TURN`, `MAX_OUTPUT_LIMIT_NO_TEXT_SINCE_A_TOOL`, `MAX_OUTPUT_LIMIT_NO_TEXT_PER_TURN`, `unreadable_arguments`, trace `output_limit_continues`)
- **Met in:** code, rules, screen
- **Meaning:** Where a model call stops because its reply reached the tokens it may write: a usage's `finish_reason` `length`, also read as `max_tokens` and `MAX_TOKENS`. Not the context window, which the same word can also mean: a reply short of the ceiling, or with none in the usage, whose prompt and reply reach 95% of the window (`_reached_the_window`, `CONTEXT_LIMIT_RATIO`) ends the turn with the window named and no further call. Not the context cut of a tool result. In an agent's **turn** such a reply is told so and may go on, twice since the last tool ran and six times in the turn, each one a **round** that counts: the words stay, the next reply joins them character by character, the answer ends with `⚠️ Continued`, and past the bound with `⚠️ Cut off` and `turn_end` `guard`. A reply cut with reasoning and no text has a smaller budget of its own, once since the last tool and twice in the turn, a policy number. A native call whose arguments did not parse never runs (`unreadable_arguments`, on any provider), nor a last call with none in a cut round. A vendor's own word for it (`max_tokens`) is mapped to `length`; a provider whose usage carries no such word keeps a cut text as the answer (the peer-provider class, which records no usage; a `remote:` alias gets the host's word, never its ceiling). Mike's call, 2026-11-04; the names proposed 2026-11-05, his word awaited.
- **Defined in:** code: `dpc_agent/loop.py`, `providers/base.py`

### cut call

- **Term note:** (`_without_the_cut_call`, `_keep_unexecuted`, `Completion.cut_note`, `gateway/unexecuted`)
- **Met in:** code, rules
- **Meaning:** A tool call the **output limit** ended inside its arguments, so it is not whole. The gateway door does not hand it to the client: it takes out any call marked `unreadable_arguments` and, under an output-limit `finish_reason`, a last call with no arguments (an earlier call was finished before the next began and stays), keeps what the call had written in `<DPC home>/gateway/unexecuted/<request id>.txt` (the newest 20 stay), and appends one note to the reply text saying the call was not executed, where what it wrote is, and to make it again with less in it. `finish_reason` stays `length` (`max_tokens` in the Messages form). The agent loop does the same for an agent in `_take_out_cut_calls`. Mike's call, 2026-11-05.
- **Defined in:** code: `gateway.py`, `dpc_agent/loop.py`

### turn command

- **Met in:** code
- **Meaning:** A local API command whose reply is the agent's **turn** it starts — `send_image` in an agent chat today: the UI does not time it out from the send but waits while that chat has a turn or a waiting entry in the agent's turn state (`agent_turn_updated`, `list_agents`), gives up 240 s after the send if no turn appeared and 60 s after the turn it saw ended, and `ai_response_with_image` settles it even when the socket that carried it closed. Not a command that controls a turn (Stop is a button, recorded as `turn_end: stopped`), and not a **`slow_command`** — that one takes a handler off the backend's read loop, this one is the UI's wait. Mike's go, 2026-10-30.
- **Defined in:** code: `dpc-client/ui/src/lib/utils/turnCommandWait.ts`

### run gate

- **Term note:** (`RunGate`)
- **Met in:** code, rules
- **Meaning:** Two locks that decide when a run starts: the agent's own (one run at a time per agent, whichever door: a message, or a queued task), and the room's (one agent at a time answering in a group). A lock does not show its queue, so the gate keeps a registry of its waiters beside it: an entry before the acquire, removed in a `finally` after it, cancelled or timed out alike. `dpc_agent/run_gate.py`.
- **Defined in:** [decisions/046 — Decision, point 8](decisions/046-the-backend-owns-every-process-it-starts.md#decision)

### waiting

- **Met in:** code, rules, screen
- **Meaning:** The list of chats waiting for an agent, in arrival order (the position is the order; there is no position field). Each item: `conversation_id`, `conversation_title`, `waiting_since`, `wait_reason`, `held_by`. Not the task scheduler's `TaskQueue`: a queued task joins this list only when it reaches the **run gate**.
- **Defined in:** [decisions/046 — Decision, point 8](decisions/046-the-backend-owns-every-process-it-starts.md#decision)

### wait_reason

- **Met in:** code, rules
- **Meaning:** Why a turn or a waiting chat is not moving: two closed lists, one per place. On a **turn** (or null): `model_call` (waiting for the model), `approval` (waiting for a person's approval of a tool), `card_queue` (the turn waiting in the **card queue**, from its first real wait in line, after which it reads `model_call` again; a start that passes straight through never shows it) and `slot_queue` (the turn waiting in the **slot queue** behind another conversation's turn, from its first real wait, as `card_queue`). On a **waiting** item: `other_chat` (the agent is in another chat's turn) and `room_busy` (the room's lock is held by another agent's turn). `held_by` on a waiting item names the turn that holds the lock, one level and no further: its `conversation_id` and `wait_reason`, and for `room_busy` its `agent_id`, so "busy in X, which waits for an approval" is said without another value.
- **Defined in:** [decisions/046 — Decision, point 8](decisions/046-the-backend-owns-every-process-it-starts.md#decision), [decisions/044 — Amendment, the card queue](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-02--the-card-queue), [decisions/044 — Amendment, the slot queue](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-17--the-slot-queue-a-conversation-keeps-the-slot-until-its-turn-ends)

### burst

- **Met in:** code, screen
- **Meaning:** The messages one person sends in a row to an agent's Telegram bot, gathered per chat and handed to the agent as one run under the header `[N messages sent in a row]`, not one run each. A burst closes when the chat has been quiet for the agent's pause, or, for a stream that never goes quiet, three pauses after its first message; a photo is its own run. The pause is set per agent in Edit Telegram Configuration ("Join messages sent within (seconds)"; command parameter `burst_quiet_seconds`, agent row field `telegram_burst_quiet_seconds`): 20 s when the row has no value, 0 to 300. With 0 nothing waits for a quiet chat and each message starts its run at once, but a message that arrives while the agent is busy still waits at its **run gate** and is drained with whatever else has arrived when the gate frees. A Stop or Kill on the agent drops the bursts not yet handed over in every chat of its bot, and the bot says so in each chat in one line; `/stop` in a chat stops the turn that answers it and drops that chat's burst; `/newsession` drops it before the fresh session starts; a run already started is stopped as a run, not dropped (Mike's call, 2026-10-30). Not the **bridge** (that is an external agent's helper script). Mike's names, 2026-10-30.
- **Defined in:** [`agent_telegram_bridge.py`](../dpc-client/core/dpc_client_core/managers/agent_telegram_bridge.py)

### resource peaks

- **Met in:** code, rules
- **Meaning:** What an entry keeps of a process's use, sampled every 2 seconds and never stored as a series: `peak_rss_mb`, `peak_commit_mb`, `peak_vram_mib` (per process, read by PDH; the card's own figures are `vram_free_mib` / `vram_total_mib`, read by nvidia-smi), `cpu_seconds`, `io_read_bytes`, `io_write_bytes`, and `last_output_at`, when it last wrote. Commit stands beside RSS because a death by commit limit happens with RAM to spare. The units are two families on purpose and are not to be unified: host memory in MB (`shell_memory_limit_mb`, `ceiling_mb`) and VRAM in MiB (`vram_free_mib`, `--min-free-vram-mib`). The registry's MB and the MiB of the system part of `list_processes` (**free commit**) are one unit, 2^20 bytes (`_MB` in `process_registry.py`, `>> 20` in `commit_memory.py`): two names because they follow the names of different APIs, not two sizes.
- **Defined in:** [decisions/046 — Decision, point 3](decisions/046-the-backend-owns-every-process-it-starts.md#decision), [New words](decisions/046-the-backend-owns-every-process-it-starts.md#new-words)

### commit_mb

- **Met in:** code, rules
- **Meaning:** On a live entry of `list_processes`, flat beside `peak_commit_mb`: the tree's commit (`private` summed over the process tree, the sampler's walk) measured at the moment of the answer, never stored in the state file nor in the daily history. Null on a finished entry and off Windows, where there is no cheap commit reading. MB, as the peaks.
- **Defined in:** [decisions/046 — Decision, point 8](decisions/046-the-backend-owns-every-process-it-starts.md#decision)

### free commit

- **Term note:** (`free_commit`)
- **Met in:** code, rules, screen
- **Meaning:** `ullAvailPageFile` of `ullTotalPageFile` read by `GlobalMemoryStatusEx` (`managers/commit_memory.py`), in MiB: what is left before the Windows commit limit (RAM plus page file), where Strata can die with physical RAM to spare (ADR-044). The reference by Mike's call, 2026-10-19, because two commit metrics disagreed in the gate runs and this one is the figure that predicted the death of the 15360 MiB gate run; free physical memory (`ullAvailPhys` of `ullTotalPhys`) comes from the same call and is printed after it, secondary. The only source on Windows. In `list_processes` the key has the same fields on every OS: on Linux and macOS it carries physical memory only (`psutil.virtual_memory()`), `source` names that reading (`psutil`) and the two commit fields are null, decided with the names on 2026-10-26. `local_engine_status` answers with the same reading since 2026-11-03, because the **resident budget** suggestion needs physical memory off Windows; the two commit fields stay null there.
- **Defined in:** [decisions/044](decisions/044-strata-as-a-second-supervised-local-engine.md), [decisions/046 — Decision, point 8](decisions/046-the-backend-owns-every-process-it-starts.md#decision)

### resident budget

- **Term note:** (`resident_budget_gib`, `--resident-budget-gib`)
- **Met in:** code, rules, screen
- **Meaning:** The GiB of experts the **engine** keeps in host RAM, the rest read from the model files. Set on a `strata` alias it overrides the base config, and it must be more than 0 (`budget_error`). Add Provider suggests one when the size's `ram_needed_gb` exceeds free memory now, a hint at overflow and not at every start: the lower of free physical and free commit, physical alone where commit is not measured, less 9 GiB, floored, capped at the arena (`experts_gb`, decimal GB, taken to GiB and floored), and none when under 8 GiB is left after the 9. It is marked not measured and never replaces a typed value or a budget the base config names. It is not the withdrawn `resident:` of the **pin** row, and not a process's residency.
- **Defined in:** [decisions/044 — Amendment, a size short of RAM](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-03--a-size-short-of-ram)

### `vram_free_mib`, `vram_total_mib`

- **Met in:** code, rules, screen
- **Meaning:** The card's free and physical VRAM in MiB in the answer of `list_processes`, flat beside `free_commit`, integers or null when there is no NVIDIA signal; first GPU only. Both are the card's figures, read by nvidia-smi: `read_vram_mib` in `managers/commit_memory.py`, beside the other system readings of this answer, through the same two helpers (`_free_vram_mib`, `_total_vram_mib` in `managers/llama_server_supervisor.py`) that llama-server admission and Strata's release check use, cached 30 s so the open Agent map's 5-second poll starts no probe of its own each time; the total is read once. They are not `peak_vram_mib` of a process entry, which the registry sampler reads per process through PDH (`\GPU Process Memory`): one figure for the card, one for a process, and the two are not summed or compared. `vram_free_mib` is the word of the Strata status the Providers editor shows; `vram_total_mib` was added beside it with Mike's word, 2026-10-27. MiB, the VRAM family of **resource peaks**.
- **Defined in:** [decisions/046 — Decision, point 8](decisions/046-the-backend-owns-every-process-it-starts.md#decision)

### memory limit per command

- **Term note:** (`shell_memory_limit_mb`, `shell_memory_limit_source`)
- **Met in:** code, rules, screen
- **Meaning:** The limit `[system] shell_memory_limit_mb` sets on the tree of one agent command, as the Agent map's D-PC backend card shows and edits it under the label "RAM limit per command": host memory (the larger of resident and committed) summed over one agent command and everything it started, for as long as the command's root process lives, never VRAM; what the command left running after that is not counted (label renamed from "per process" to match the sum, Mike's call, 2026-11-04). `shell_memory_limit_mb` in the answer of `list_processes`, flat beside `free_commit`, is the limit the next command will be held to, an integer in MiB (the host-memory family of **resource peaks**), with 0 meaning no limit. `shell_memory_limit_source` beside it is one of `env` (either environment variable, `DPC_SHELL_MEMORY_LIMIT_MB` first), `config` (the key in config.ini) or `default` (the key is absent, or its value is not a number, and the built-in 8192 runs); a non-numeric environment value still reports `env`, because the variable shadows config.ini. Both are read by `read_memory_limit` in `dpc_agent/tools/process.py`, the same reader the commands use, so the screen cannot show another number than the one enforced. In the card only a typed 0 means no limit; an emptied field sends the default 8192 back (Zcode, 2026-11-02). The command `set_shell_memory_limit` (param `mb`, a whole number of MiB, 0 = no limit) writes the key through Settings, refuses a negative or non-integer value and any change while an environment variable decides (it names that variable), and answers with the effective value and source; the next command reads it with no restart. The card warns below 1,024 MiB that every agent command which grows past the limit is stopped. Names agreed in the team chat, 2026-11-02.
- **Defined in:** [`process.py`](../dpc-client/core/dpc_client_core/dpc_agent/tools/process.py)

### command job

- **Term note:** (`CommandJob`)
- **Met in:** code
- **Meaning:** The Job Object of one command started by `run_supervised` on Windows, nested in the backend's job. While the command's root process lives it names the command's processes for the memory ceiling's sum and kills them at the ceiling or the timeout; once the root has exited nothing automatic kills through it, and a person's Kill still does while the call waits. After the call has returned nothing reaches what the command left, which stays until it ends or the backend's job takes it. Its handle closes when the command is done; it is not kill-on-close and sets no limit. Not the backend's job.
- **Defined in:** [decisions/046](decisions/046-the-backend-owns-every-process-it-starts.md)

### pin

- **Met in:** code, rules, screen
- **Meaning:** Two senses, never mixed. The **download pin** is a release DPC fetches and trusts by name: a tag and, per asset, a sha256 held in code (`LLAMA_CPP_TAG` and its rows in `llama_server_fetcher.py`), with `.dpc-pin.json` marking what a fetch installed and `install_pin` the call that does it. Strata's download pin, in `strata_fetcher.py`, is the release tag `STRATA_RELEASE_TAG`, the engine version `STRATA_ENGINE_VERSION`, one **Control** row per platform in `PLATFORM_ASSETS` (archive name, sha256, size), one row per engine package in `_ENGINE_PACKAGES` (sizes and the build commit that package's engine carries in its `build_id`; per package since v0.2.19, whose cuda12 engines are built from another commit than the rest) and the held signing key `STRATA_UPDATE_PUBLIC_KEY`; `.dpc-pin.json` marks the install and names its engine folder and that engine's identity (version, build_id, variant, backend, manifest sha256). An alias with no `strata_install_dir` runs that install and refuses an engine reporting another version; an alias with one set runs the owner's checkout, where a mismatch only warns. It is moved by one reviewed commit after a live start smoke on a node, never by an alias and never to upstream's «latest». The Strata pin is shown as "Strata v0.2.20 (engine 0.1.66)" and rides the status as `pin: {release_tag, engine_version}`. A pin folder is `<pins folder>/<tag>/<platform>/` whose `.dpc-pin.json` names that same tag and platform; any other folder there (`b10684`, from before markers, or a hand-made `prism-…` build) is not DPC's to run or remove. The **retained pin** (`retained_pin`) is the other pin whose marker carries the newest `start_passed_at`, written at its first start that passed; it runs while the pin is not downloaded yet, and once in place of a pin whose first start failed for the build's fault (`rolled_back_to`) — DPC's own rule, not Strata's `--rollback-engine`. The folder a child actually runs from is its `running_pin`; in `local_engine_status` `pins` both it and `rolled_back_to` are per engine (the latest rollback, one live child), beside `installed_pins`. Older pins are removed only after the current pin has passed a start, and only while a retained pin exists. The other sense, **residency**, is withdrawn: ADR-040 gave up calling an Ollama `keep_alive` of `-1` "pinned" because it protects against the TTL and not against eviction, and `resident: pinned | on-demand` is a D4-β proposal for llama-server, not a field.
- **Defined in:** [decisions/044 — Amendments, 2026-10-31](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-10-31--the-strata-pin-lives-in-code-b2-step-1) and [2026-11-01](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-01--dpcs-own-strata-install-b2-step-2), [the retained pin](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-01--the-retained-pin-and-the-rollback-b2-step-3), [decisions/040 — D3](decisions/040-local-inference-second-path-as-a-dpc-owned-llama-server.md#d3--stage-2-route-b--a-dpc-owned-llama-server-behind-llamacpp_server)

### Control

- **Term note:** (Strata)
- **Met in:** code, rules
- **Meaning:** Strata's own pinned package `strata-control-<ver>-<platform>.zip` (`.tar.gz` off Windows), an asset of a Strata release (`STRATA_RELEASE_TAG` names the release, not this package; Control and the engine are versioned 0.1.x inside it). DPC fetches it by its sha256 row to `~/.dpc/bin/strata/<tag>/<platform>/` (the Control root, `install_root`) with its own Python, the **launcher** and Strata's updater; the engine is installed into that root's `components/` folder by the updater. Not the **launcher** (`serve/server.py`, the program DPC starts), not the **engine** (`strata.exe` and `strata-vision.exe`), and not a control vector: `control_vector` on an alias is a steering file given to the engine as `--control-vector-scaled`, and has nothing to do with this package.
- **Defined in:** [decisions/044 — Amendment, 2026-10-31](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-10-31--the-strata-pin-lives-in-code-b2-step-1), [2026-11-01](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-01--dpcs-own-strata-install-b2-step-2)

### engine

- **Met in:** code, rules, screen
- **Meaning:** The program a local provider answers with: `llama-server` for `llamacpp_server`, `strata.exe` / `strata-vision.exe` for `strata`. Both run on one card, and one engine holds it at a time. Not the **launcher** (Strata's `serve/server.py`, which DPC starts and which starts the engine) and not **Control**.
- **Defined in:** [decisions/044](decisions/044-strata-as-a-second-supervised-local-engine.md), [decisions/040](decisions/040-local-inference-second-path-as-a-dpc-owned-llama-server.md)

### context window

- **Met in:** code, rules, screen, wire
- **Meaning:** What one conversation may occupy on an alias, in tokens. `LLMManager.alias_context_window` answers: the alias's own `context_window`, then the **engine** behind it (`server_window`: a running child's report, else the start the alias would make), then, for an alias with no engine, a table of model names. When none of them knows it stays unknown and no number stands in for it: an agent's **turn** is refused by the sentence in `window_unknown`, a monitor holds 0, the screen says "window not known", a `PROVIDERS_RESPONSE` row carries null and `model_max_tokens` stays off the wire. Not `--max-context` or `n_ctx`, which are the server's, and not a `token_limit` in `history.json`, which is what was true when it was written. Mike's order, 2026-11-02. One exception: the sleep synthesis on a local engine reads the engine's reported `n_ctx`, not the alias's `context_window`, because a synthesis is not a parked conversation; the alias's value still governs parking and live turns. Mike's call, 2026-11-09.
- **Defined in:** [decisions/044 — Amendment, 2026-11-03](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-03--where-an-aliass-window-comes-from), [2026-11-09](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-09--sleep-synthesis-reads-the-engines-window)

### engine backend

- **Term note:** (`strata_engine_backend`)
- **Met in:** code, rules, screen
- **Meaning:** The `strata` alias key whose value is the word Strata's updater takes as `--backend`: a family from Strata's `BACKEND_MATRIX` (cuda, hip, sycl, metal, cpu — Strata's set, not ours), and for AMD `hip-<build>`; empty means Strata picks by the card. Offered on a platform only where the pinned feed has a package of it (Strata v0.2.20 has no `cpu` package), normalised as Strata does (trimmed, lower case); CUDA 12 or 13 is not a choice, Strata picks it from the driver. It reaches the updater as `--backend`, and an installed engine of another backend (by Strata's rule: `hip-<build>` family and build, any other word the family) is treated as a missing pin for that alias. Not the DPC backend (the Python service) and not the **menu row**'s **variant**; the `variant` in the engine's marker (`cuda13`) is the build inside a family in Strata's own words. Agreed in the team chat, 2026-11-01.
- **Defined in:** [`strata_fetcher.py` `resolve_engine_backend`](../dpc-client/core/dpc_client_core/managers/strata_fetcher.py)

### guest_conversation_cache

- **Met in:** code, rules
- **Meaning:** The `strata` alias field, true or false and on when absent, that lets a call served to a peer go out with checkpointing on when the conversation cache has room for its snapshot and one slot more (`StrataSupervisor.guest_checkpoint`); off, every such call goes out with checkpointing off (`strata_checkpoint: false`) and parks nothing. Its **guest** is the peer a call is served to, the guest of the host-and-guest pair; it is not the guest ceiling of ADR-041 D5 (**vendor_request_quotas**, `guest_vendor_quota.py`), which bounds a guest's use of a vendor alias, and it sets no quota. A gateway client is not a guest here: its calls are this node's own and keep checkpointing on. Voted 2026-11-13 (CC, Zcode, Johnny; Ark pending).
- **Defined in:** [decisions/044 — Amendment, 2026-11-13](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-13--a-peers-call-parks-when-the-cache-has-room)

### KV snapshot

- **Term note:** (conversation KV snapshot; `slot_save_path`)
- **Met in:** code, rules, screen
- **Meaning:** A file in which a local engine keeps one conversation's engine state on disk (its KV cache, context checkpoints, draft state and token identity) so that the conversation can come back after the engine stopped without a cold read. DPC saves the owner's live conversation at a stop and restores it on that conversation's first request into an empty engine, in the folder the alias's `slot_save_path` names ("KV snapshots folder"), with an index, a TTL and a cap of DPC's; empty means off. It is not the conversation: the history stays in `history.json`. Not a **session archive** (`*_session.json`), which keeps a closed session's messages under `~/.dpc/conversations/` when a conversation is reset; a reset deletes that conversation's KV snapshots. Not the "snapshot" of Strata's own docs, a conversation its conversation cache parks in host RAM (the peer's snapshot of **guest_conversation_cache**, `GUEST_SNAPSHOT_*`), which a stop drops; Strata's docs call the disk file a "session file", a word DPC does not use for it. Not the browser page snapshot of the agent's tools (`browser_snapshot`, `snapshot_summarize_provider`). Mike's word, 2026-11-13.
- **Defined in:** [decisions/044 — Amendment, 2026-11-13, KV snapshots](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-13--kv-snapshots-the-live-conversation-is-saved-to-disk-at-a-stop-and-restored-into-an-empty-engine)

### model files folder

- **Term note:** (`model_files_dir`)
- **Met in:** code, rules, screen
- **Meaning:** The folder where DPC keeps the local engines' model files: llama.cpp GGUFs as `<folder>/<owner>--<repo>/<file>.gguf`, and later Strata's data folder `<folder>/strata/` with `run-configs/` and `settings.json`. Config key `[local_engines] model_files_dir`, default `~/.dpc/models` (empty means the default; `~` is expanded; it must be absolute, not a filesystem root or a system folder, and writable, checked and created when it is chosen); shown in the Providers editor as "Model files folder"; reported by `local_engine_status` as `model_files_dir`, the resolved path. In the UI "model" means a **provider alias**; a "model file" is a GGUF or a Strata pack. Not the owner's hand-made Strata data folder; choosing or changing this folder moves and deletes nothing in any folder. The command `list_model_files` lists its GGUFs (the folder and one level of subfolders, `strata` skipped; a split model is one row) and Strata's `run_configs`; a row's `is_mmproj` comes from the header's `general.architecture == "clip"`, not from the file name, and is false when the header cannot be read. The command `list_strata_models` lists the Strata models that can be installed: it is Strata's own `model_table`, read by the pinned Control's Python and passed through as it is (Strata's field names, GB units and verdict words), and its `on_this_pc` is advice, never a gate. Strata's `--gguf-dir` takes in place only the exact catalog files (checked by sha256); any other GGUF is not accepted by it and stays on the manual alias plus base config path. Agreed in the team chat, 2026-11-02.
- **Defined in:** [`settings.py` `get_model_files_dir` / `set_model_files_dir`](../dpc-client/core/dpc_client_core/settings.py), [`llama_server_supervisor.py` `model_files_in`](../dpc-client/core/dpc_client_core/managers/llama_server_supervisor.py), [`strata_models_probe.py`](../dpc-client/core/dpc_client_core/managers/strata_models_probe.py)

### local engine swap

- **Term note:** (`swap_when_idle`, `swap_wait_s`, `caller_idle_s`)
- **Met in:** code, rules, screen
- **Meaning:** The owner's start of a local alias stopping the other local engine that holds the card, instead of failing with the card-held refusal. Three keys in `[local_engines]`: `swap_when_idle` (default off until the card queue is live-checked; the card-held refusal names the switch), `swap_wait_s` (whole seconds, default 300; 0 means do not wait for a busy holder) and `caller_idle_s` (whole seconds, default 120; 0 means no **caller_window**). The holder is stopped by the Stop engine command's path once it is no longer busy, within the request's one deadline, and the process registry records that stop as `stopped` / `swap`, not `ui_stop` (2026-11-05); requests wait for that in the **card queue**. A busy holder (see **card hold**) is not marked and keeps serving what stood there when the first place was taken; on a deadline before the mark it was never touched, and the refusal names who stands on its alias. After a mark, which falls only on a holder that is not busy, a call at the slot is refused with "'<holder>' is stopping to free the card for '<new>'", and a new request takes a place. A peer's request swaps only on the owner's own terms (the switch on, the **card queue** empty, no owner's request waiting in the **lease line**, the holder not busy and not already stopping, and idle for at least 60 s, `PEER_SWAP_IDLE_S`, since the owner's last request on its alias; Mike's call, 2026-11-24). An inference peer's start swaps with no wait and takes no place (ADR-044, 2026-11-06). A peer's transcription through the **lease line** is different: it takes a place there and waits for the stop, within the guest's 120 s (ADR-052 L13). A swap away from Strata drops its parked conversations, which the notice and `parked_dropped` say unless the launcher's own count, read just before the stop, is zero. Set in the Providers editor under "Model files folder" ("Swap local models when the card holder is idle", "Wait for a busy holder, s"), by the command `set_local_engine_swap` (its third argument `caller_idle_s` has no field in the editor yet, and left out the stored value stays); reported by `local_engine_status` as `swap_when_idle`, `swap_wait_s`, `caller_idle_s` and `last_swap`. A swap changes the model holding the card; a change of conversation on the same model is a **switch**. Not the **engine**'s own idle unload (Strata's `--idle-unload`) and not the hand-made Stop engine, which it reuses. Agreed in the team chat, 2026-11-02.
- **Defined in:** [decisions/044 — Amendment, 2026-11-02](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-02--the-idle-swap), [decisions/044 — Amendment, 2026-11-06, a peer may swap](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-06--a-peer-may-swap-an-idle-card-when-the-owner-allows-swaps), [`llama_server_supervisor.py` `_free_the_card`](../dpc-client/core/dpc_client_core/managers/llama_server_supervisor.py)

### conversation_spill_mib

- **Term note:** (SSD tier)
- **Met in:** code, rules, screen
- **Meaning:** The `strata` alias field, a whole number of MiB, 0 when absent: 0 keeps Strata's **SSD tier** off; N > 0 turns it on with that cap, in `<slot_save_path>/conversation-spill`, and only while the **KV snapshot** folder is set and its disk is there. The SSD tier is Strata's own word (v0.2.16): a parked conversation the conversation cache evicts from RAM is written to disk and read back while the engine runs; a stop removes it, so it is not a **KV snapshot**. A restart key; a spill flag in `extra_args` overrides the field and is warned about. Mike's call and name, 2026-11-23.
- **Defined in:** [decisions/044 — Amendment, 2026-11-23, SSD tier](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-23--stratas-ssd-tier-is-opt-in-inside-the-kv-snapshots-folder-conversation_spill_mib)

### switch

- **Term note:** (`kv_at_a_switch`)
- **Met in:** code, rules
- **Meaning:** On one local engine, the slot passing from one conversation to another: another agent, another conversation, or a call with none. At a switch on `llama-server` the outgoing conversation's KV snapshot is saved and the incoming one's restored (slice 1, amendment of 2026-11-17); Strata at a switch is a later slice. Not a **local engine swap**, which changes the model holding the card. Mike's words, 2026-11-17: a switch is the same model but different conversations.
- **Defined in:** [decisions/044 — Amendment, 2026-11-17](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-17--a-kv-snapshot-at-an-agent-switch-llama-server-slice-1), [`llama_server_supervisor.py` `kv_at_a_switch`](../dpc-client/core/dpc_client_core/managers/llama_server_supervisor.py)

### prefix_hashes, prefix_chars, call_chars

- **Met in:** code
- **Meaning:** Fields of a **KV snapshot**'s index entry, written at the save: `prefix_hashes`, the short hashes of the messages of the saved turn base, one per message; `prefix_chars`, their running character counts; `call_chars`, the character count of every message the saved call sent, tool rounds after the base included. Hashes and counts only, never message text. They let a request whose turn base differs find how many leading whole messages it shares with the snapshot (**common_part**). Older entries without them keep the exact-match rule. Mike's call, 2026-11-17.
- **Defined in:** [decisions/044 — Amendment, 2026-11-17, a snapshot whose turn base differs](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-17--a-snapshot-whose-turn-base-differs-is-restored-when-its-common-messages-pay), [`kv_snapshots.py` `new_entry`](../dpc-client/core/dpc_client_core/managers/kv_snapshots.py)

### common_part

- **Term note:** (`kv_snapshots.common_part`, `_kv_common_part`)
- **Met in:** code, rules
- **Meaning:** For a **KV snapshot** whose turn base differs from the request's: the leading whole messages the two share and an estimate of their tokens (the snapshot's saved tokens times the share of the saved call's characters those messages hold). On `llama-server` the snapshot is restored when re-reading that part cold would take longer than restoring the file, at this host's measured rates; with no whole message in common it is deleted as before. The engine resumes at its last context checkpoint at or below the common part, so the reuse can fall up to `checkpoint_min_step` tokens short. Strata keeps the exact-match rule. Mike's call, 2026-11-17.
- **Defined in:** [decisions/044 — Amendment, 2026-11-17, a snapshot whose turn base differs](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-17--a-snapshot-whose-turn-base-differs-is-restored-when-its-common-messages-pay), [`llama_server_supervisor.py` `_kv_common_part`](../dpc-client/core/dpc_client_core/managers/llama_server_supervisor.py)

### accepted_request

- **Met in:** code, rules
- **Meaning:** The kind of **card hold** that makes a card holder busy: a request on an alias, from its acceptance to its answer, its wait in the **card queue** included. Acceptance also fixes its one deadline, reading `swap_wait_s` once. Acceptance is per door (Mike's call, variant A, 2026-11-02): an agent's **turn** is accepted on entry into `DpcAgent.process`, the hold taken around `run_llm_loop`, so it lies inside the turn and is not a second kind of busy, and waiting at the **run gate** holds nothing; an owner's chat is accepted in `InferenceOrchestrator._execute_local_inference`; a gateway call on a local alias from `Gateway.complete`'s acceptance on, its wait in the **card queue** and on `P2PCoordinator._peer_inference_lock` included. A peer's call is none: its **caller_window** covers it between calls. A turn waiting for a person's approval counts. Not the **card lease** (ADR-052), which is the standing of a client that is not a supervised child, and not the run gate's `hold`, which stands on a gate where this stands on an alias. Mike's call, 2026-11-02.
- **Defined in:** [decisions/044 — Amendment, the card queue](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-02--the-card-queue)

### card hold

- **Term note:** (`_CardHold`, `_CARD_HOLD`, `_CARD_HOLDS`, `card_held_for`, `card_hold_follows`, `current_card_hold`, `carrying_card_hold`)
- **Met in:** code, rules
- **Meaning:** The record that something stands on an alias, so that the card holder on it is busy between its calls too. `card_held_for(alias, kind, *, agent_id=None, conversation_id=None)` enters it and ends it however the body ends; `_CARD_HOLDS` keeps the holds by alias, written on the owner loop, so one survives a settings save replacing the supervisor; `_CARD_HOLD` is the hold of the request this code runs for. It creates no claim, and a dead child holds nothing. One alias at a time: `card_hold_follows` moves it when a switch lands between rounds. `current_card_hold` and `carrying_card_hold` carry it into a tool thread, which inherits no context, so a request asking for another excluding alias is refused at once instead of waiting on itself, unless its description may take a **card-borrow**. The busy test `_busy_for_the_card` has four parts: a child starting, a call in flight, a hold on the alias that stands before the line (`_stands_before_the_line`: older than the first place of the **card queue**, or `admitted`, which it becomes by passing through the line or as a call of an open window), a caller in its **caller_window**. The one kind is **accepted_request**; a window is a test of the clock and not a hold, so `card_held_for` refuses that word. Not the run gate's `hold`, which stands on a gate. Mike's call, 2026-11-02.
- **Defined in:** [decisions/044 — Amendment, the card queue](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-02--the-card-queue)

### stretch

- **Term note:** (`one synchronous stretch`)
- **Met in:** code
- **Meaning:** In the card queue, code between two `await`s on the owner's loop: nothing else on that loop runs inside it, so two steps in one stretch are seen together.
- **Defined in:** code: `managers/llama_server_supervisor.py`

### caller_window

- **Term note:** (`caller_idle_s`, `note_caller_call`, `_last_caller_call_at`)
- **Met in:** code, rules
- **Meaning:** What keeps a card holder busy between the calls of a run DPC cannot see the end of: a peer's or a gateway client's run of calls counts as busy for `[local_engines] caller_idle_s` seconds (default 120, 0 = no window; read by `get_caller_idle_s()`) after its last served call on that local alias, recorded by `note_caller_call` in `_last_caller_call_at`, and calls inside the window renew it. It was decided as a second kind of **card hold** and is built as a test of the clock at `_busy_for_the_card`, because a hold is entered and left around a body and a window has no body to leave. It stands on the alias, creates no claim, and a dead child holds nothing; each note and each look drop every ended window. Served means the call that leaves a **usage row**: a refused or failed call neither opens a window nor renews one, so a window ends `caller_idle_s` after the caller's last successful call whatever failed since, and the first call after that is new to the line; a vendor's alias has no card. A busy refusal names it as "caller '<id>'". A gap longer than the window still cuts that caller's loop, a trade-off taken on purpose. It changes nothing while `swap_when_idle` is off. A call inside the window is not new to the **card queue**: it is served while a place waits. Not `[connection] remote_inference_timeout` (default 1200 s), which is a door limit. The caller is the one `caller_kind` names `peer` or `gateway`. Mike's call, 2026-11-02.
- **Defined in:** [decisions/044 — Amendment, the card queue](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-02--the-card-queue)

### card queue

- **Term note:** (`_CARD_QUEUE`, `_CardPlace`, `_QueuedRequest`, `CardQueueRefusal`, `leave_card_queue`, `take_card_queue_place`, `waiting_for_the_card_at_most`, status `card_queue`)
- **Met in:** code, rules
- **Meaning:** The line of requests waiting for the card, nothing being forced: one queue at the level of `ensure_running`, entered before `_start_lock`, with one place per alias, in the order taken, which stays with the alias's next oldest live request and is gone only when empty; only the first place acts. A request accepted after a place was taken waits behind it, on the holder's own alias too; a later round of a running request and a call of an open **caller_window** are not new and are served. A request has one deadline, on its waiting and not on work: `swap_wait_s` fixed at acceptance and covering the wait and the stop; before a mark it leaves with a refusal naming the holder, why it is busy and the setting, and after one it leaves saying the holder is already stopping, the stop runs to its end and the next in line takes the card. A request that came with no time left (`swap_wait_s` 0, or an old request that lost its alias) is refused at once by a busy holder, by a holder someone else is stopping and behind a place, and a holder that is not busy asks it for no wait: what then stands between it and the claim, the stop it began above all, it waits for up to 60 s (`_STOP_WITH_NO_WAIT_LEFT_S`, a constant), and a refusal at that limit names the limit and not the setting. A person's group image waits at most 30 s (`GROUP_IMAGE_CARD_WAIT_S`, a constant, or `swap_wait_s` where that is less; Mike's call, 2026-11-03), because its description goes into the message: the message then goes with its warning and no description, while a load past the claim and the description itself are work, and it waits for them. A holder stopping for another reason is waited for; a request on a supervisor that will not reopen is refused at once. Its refusal, `CardQueueRefusal`, is a `CardHeldError`, the card-held refusal in code, so a door that reads one reads the other; an agent turn it ends reads `turn_end` `failed`. A peer never takes a place and a new one is refused `provider_unavailable` while one waits (a **refusal code**, no `retry_after_sec`); a gateway client queues before the semaphore, and is refused 503 `provider_unavailable`. A person's Stop takes a turn out (`leave_card_queue`, the turn ends `stopped`), Stop engine takes the alias's place away (`take_card_queue_place`), and a holder's death advances it. It lives only while `swap_when_idle` is true. `local_engine_status` reports it for the owner alone as `card_queue` (`card_queue()`): one row per request in position order with no position field, as **waiting** does, each `alias`, `agent_id`, `conversation_id`, `queued_at` (when it took its place, not its acceptance) and `give_up_at`, wall-clock seconds, with every holder named beside it by `card_holders`; the Providers editor's Local engines block shows them. A **background call** takes no place; a **card-borrow** takes one for each of its two swaps. Not `P2PCoordinator._peer_inference_lock`, the line of guest calls on a loaded alias; not the **waiting** list, which is the **run gate**'s; and its cancel is not `stop_waiting`. Mike's call, 2026-11-02.
- **Defined in:** [decisions/044 — Amendment, the card queue](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-02--the-card-queue)

### slot queue

- **Term note:** (`_SLOT_QUEUE`, `SlotQueueRefusal`, `TakenOutOfTheSlotQueue`, `in_the_slot_queue`, `wait_reason` `slot_queue`, status `slot_queue` / `slot_queue()`)
- **Met in:** code, rules
- **Meaning:** DPC's wait in front of one local alias's slot: who sends calls into the engine next. A conversation keeps the slot until its turn ends or leaves the alias, and the next one waits; when the slot passes, the outgoing conversation is saved at the **switch** before the incoming call is sent. Held by an agent's turn (its `accepted_request` hold), or for one call by the owner's chat, a gateway call, a peer's call or a call with no hold; the turn's own calls pass (a description, the pre-analysis, a tool thread carrying its hold). The owner's own work waits first in, first out, peers behind every waiting owner request; the owner's chat, and a peer at its deadline (the smaller of `swap_wait_s` and `remote_inference_timeout`, from its arrival at the peer door) less the save's allowance, go in between two calls of the turn, saving it first; a save that does not fit the peer's deadline is logged as "evicted unsaved". A named **background call** is skipped by name rather than wait. A call first waits for the card (**card queue**), then for the slot. The same on llama-server and Strata aliases (Mike's call, 2026-11-20); Strata saves nothing at the switch, its park keeps the outgoing conversation. Not the **card queue**, which decides which alias holds the card; not `llama-server`'s own ordering of requests; not the **run gate**. Mike's word, 2026-11-17.
- **Defined in:** [decisions/044 — Amendment, the slot queue](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-17--the-slot-queue-a-conversation-keeps-the-slot-until-its-turn-ends)

### successor

- **Term note:** (`supersedes`, `superseded_by`, `where_calls_go`, `waiting_in_the_card_queue`)
- **Met in:** code, rules
- **Meaning:** The supervisor a providers save builds in place of one it retires for the same engine. The new one names the old (`supersedes`) and the old names the new (`superseded_by`); a request still waiting for the card on the old one (`waiting_in_the_card_queue`) is not refused but goes on under the successor with its place in the **card queue**, and the provider it came through points there from then on (`where_calls_go`). A changed engine type has no successor in this sense and keeps the draining refusal.
- **Defined in:** [ADR-044, amendment 2026-11-11](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-11--a-providers-save-does-not-refuse-a-call-that-has-not-started)

### card-borrow

- **Term note:** (`a_card_borrow`, `_CARD_BORROW`, `_CardBorrow`, `way_back`, `description_key`, `turn_description`, `keep_turn_description`)
- **Met in:** code, rules
- **Meaning:** A request's description giving up the request's own alias for the vision alias that excludes it, and getting it back, instead of the from-inside refusal of a **card hold**. It is what `_pre_analyze_image_for_agent` and `describe_image` may do, marked by `a_card_borrow()`; `read_document`'s look, compaction, a **background call** and a call served to a peer never do. The first swap goes through the **card queue** on the request's one deadline, and only with `swap_when_idle` on, only on a holder with `DROPS_PARKED_ON_STOP` false (a llama-server child; a Strata holder keeps the refusal, which says so), with no other child on the card, no place already in line (that place may be waiting for this very hold) and time left; a holder busy for someone else is waited for and never forced. From the stop on the request stands on the vision alias too, and `way_back` is a place of its own for its own alias, right behind the acting place, so what came later stands behind it; `card_queue` reports it as a row with `give_up_at` null until its next round takes that place and stops the vision alias. That round starts its own bound, a fresh `swap_wait_s`; the refusal at the end, naming the holder and who stands on it, is the one way a borrow can cost a turn (Mike's call, 2026-11-04). Its own stop has the limit of 60 s (`_STOP_WITH_NO_WAIT_LEFT_S`). A person's Stop ends the wait at once, and the switch does not. So one description is two swaps, each said once. A description, or the failure a retry would only repeat, is kept once per turn on the hold, by the images' bytes and what was asked, and the request's end drops it. A failed description leaves the turn working, told that it has not seen the image; a person's Stop that takes the request out of the line ends the turn `stopped` with no further model call. A borrow cut short leaves the vision alias's child up until Stop engine or the next local start, which reads it as any idle holder. Not a card-borrow: a description whose request stands on no alias on the card (a cloud agent, or a turn whose alias has no child now). It queues as a normal request, except that it never stops a holder with `DROPS_PARKED_ON_STOP` true: it meets the Strata-holder refusal, the turn goes on with a failed description and what is parked stays (`_description_may_not_stop`; Mike's call, 2026-11-04, agreed by the team until the session-files measurement). That covers only calls marked as a description: an ordinary request's swap and a person's group image still swap out an idle Strata holder. Mike's call, variant B, 2026-11-02; the direction confirmed 2026-11-03; the first swap's wait for a busy alias and the way back's bound, 2026-11-04.
- **Defined in:** [decisions/044 — Amendment, the card queue, rule 9](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-02--the-card-queue)

### background call

- **Term note:** (`a_background_call`, `_BACKGROUND_CALLS`, `BackgroundCallSkipped`, status `last_background_skip`; for a **card lease** `acquire(background=True)`, `LeaseSkipped`)
- **Met in:** code, rules
- **Meaning:** A model call no **card hold** stands behind, which never swaps and never queues: with `swap_when_idle` on it runs only on a child already on the card with nobody in the **card queue**, and otherwise it is skipped by name. Nothing tries a skip again; each name carries what follows it. Two exist, closed in `_BACKGROUND_CALLS`: `self_reflection_confirmation`, not a background job of its own but a step inside a person's extraction, marked because it has no hold and could otherwise stop a holder for a 10 s classification (a skip classifies the conversation as general, and that is a result, not a gap); and `skill_reflection` (a skip ends that reflection, and the next long task with a used skill, `REFLECTION_ROUNDS_THRESHOLD`, reflects). A skip leaves one row per name in `last_background_skip` in `local_engine_status` (`alias`, `reason`, `what_follows`, `at`), in memory until the backend restarts as `last_swap` is, and the Local engines block shows it with its date. `BackgroundCallSkipped` is a `CardQueueRefusal` for its "never retried"; the call never stood in the line. With the swap off, and on an alias with no card to be on, it goes on as any call does. Not a request's auxiliary call, which has a hold (`describe_image`, compaction: rule 9; the description may take a **card-borrow**), and not sleep (rule 11). Not knowledge extraction, which starts at a person's word (End Session, `/extract_knowledge`, Extract Knowledge) and is a request that queues, the owner's call, 2026-11-03; nor the group image of `_describe_image_for_agents`, also a request (the AI's own vote on a proposal, once a request too, is gone: only people vote, Mike's call, 2026-11-16). The reason is `DROPS_PARKED_ON_STOP` on Strata. **The term has two carriers.** The one above is a model call on a supervised alias, skipped by name, and it leaves a `last_background_skip` row. The second is a **card lease** asked with `background=True` (ADR-052 L4): when the card is not free right now it is refused with `LeaseSkipped`, having waited for nothing and asked no holder to yield, and it is logged and leaves no status row. What follows a lease's skip is the client's: the Whisper preload stays lazy, a background transcription would run on the CPU twin when L12 passes it and be skipped otherwise (the twin is built, S3d; the door that sends a background transcription into L12 is not, so no such call exists yet), the embedder's steady-state query runs on its CPU model. The music engines (yue2, acestep) have no CPU twin and never ask with `background`: a job waits in the **lease line**. The rule is Mike's call, 2026-11-02; the names were reviewed by Ark, Zcode and Johnny, 2026-11-03, and `self_reflection_confirmation` is Ark's.
- **Defined in:** [decisions/044 — Amendment, the card queue](decisions/044-strata-as-a-second-supervised-local-engine.md#amendment-2026-11-02--the-card-queue)

### card lease

- **Term note:** (`CardLease`, `_CARD_LEASES`, `LeaseRefusal`)
- **Met in:** code, rules
- **Meaning:** The standing on the one card of a GPU client that is not a supervised llama.cpp child (Whisper, a music engine, the embedder, GLiNER): registered once under a holder name, then granted, used and released many times, so that it is a visible card holder beside the supervisors and is asked to let go when idle or waited for and named when busy. Not the **card hold**, which marks a request standing on a supervised alias, and not the run gate's `hold`. Every refusal of a lease is a `CardQueueRefusal`. Designed in ADR-052 (2026-11); built for Whisper and the music engines (S1 to S4, with follow-ups to 9), pending for the embedder and GLiNER (S6, S7).
- **Defined in:** [decisions/052 — Decision](decisions/052-the-card-lease-lets-every-gpu-client-in-dpc-hold-wait-for-and-yield-the-one-card.md#decision)

### lease line

- **Term note:** (`_LEASE_LINE`, `_LeaseWaiter`)
- **Met in:** code, rules
- **Meaning:** The waiters for the card that came through a **card lease**, in the order they came; a waiter holds nothing. It is ordered by the same clock as the **card queue**'s places, so no waiter overtakes another in either line (ADR-052 L5a), and its rows are read with the card queue's. Not a second queue of the engines: the one order is across both lines.
- **Defined in:** [decisions/052 — Decision](decisions/052-the-card-lease-lets-every-gpu-client-in-dpc-hold-wait-for-and-yield-the-one-card.md#decision)

### card ask

- **Term note:** (`card_ask`, `expected_seconds`, `speed_figures`; cold and warm figures)
- **Met in:** code, rules
- **Meaning:** What a door to local Whisper passes with a transcription for the card: its `purpose` ("dictation", "music Transcribe", "agent tool", "peer <node>"), its `wait_s` by ADR-052 L4 (`min(caller's remaining - margin, bound)`, the margin `max(10 s, estimate)`) and `expected_s`, the provider's own estimate: the load when the model is not loaded plus the audio divided by the **cold** ratio (the first call after a load) or the **warm** one (every later call), per device, the last measured, else the ADR's start figures; unknown, never 0, when a part is missing. Not the **card hold**, which stands on a supervised alias, and not the lease itself: since S3b the Whisper **card lease** takes the ask (`CardLease.use`). Built in S3a, 2026-11-23.
- **Defined in:** [decisions/052 — Review notes, S3a build](decisions/052-the-card-lease-lets-every-gpu-client-in-dpc-hold-wait-for-and-yield-the-one-card.md#review-notes)

### self-wait rule

- **Met in:** code, rules
- **Meaning:** A tool in a turn that holds a live **card hold** on a local alias must not wait on a card job queued behind that hold, because the job's holder would be this very turn. It answers at once with the job's real state, and says "queued, starts after this turn" only when the turn holds a live card hold on a local alias and the job is `queued`; a job in `review`, `failed` or `cancelled` is reported as that. The hold is read as a local alias, not as `current_card_hold()` being non-empty. A `queued` or `running` job whose `card_wait` holders name the turn's own local alias (a running job whose engine ended and waits for the card again) is answered at once too, a job in any other state never: "waits for the card behind this turn; check back after it ends". On a cloud alias there is no self-wait and the rule does not apply. The same refusal-instead-of-waiting-on-itself as the **card hold** term describes. Built in `music_wait` (ADR-051 D51), not observed live.
- **Defined in:** [decisions/052 — 3.5, the self-wait rule](decisions/052-the-card-lease-lets-every-gpu-client-in-dpc-hold-wait-for-and-yield-the-one-card.md#35-an-agents-own-turn-that-needs-a-transcription-ark-82-2-cc-84-mike-11)

## Groups

### group

- **Met in:** code, rules, screen, wire
- **Meaning:** A conversation fanned out to N participants under a `group_id`; each member keeps its own copy of metadata and history, reconciled on connect.
- **Defined in:** [GROUP_CHAT.md — Architecture](GROUP_CHAT.md#architecture)

### group `chat_context`

- **Met in:** code, rules
- **Meaning:** The block of a group's metadata (name, id, topic, participants, then the fields that are set) given to every agent woken in that group, on every wake path, and to external agents in the `cc_group_mention` event. One builder, `CoreService._group_chat_context`; the local API command **`get_group_chat_context`** returns it, and the CC bridge prints it as a **`[CHAT_CONTEXT]`** line, after each mention and on **`--chat-context`**. Names kept on Mike's word, 2026-10-27.
- **Defined in:** [GROUP_CHAT.md — Group Metadata Injection](GROUP_CHAT.md#group-metadata-injection)

### creator

- **Met in:** code, rules, screen, wire
- **Meaning:** The node named in a group's `created_by`: fixed when the group is first seen and never changed from the wire; ownership passes only by an explicit signed transfer, and the creator cannot leave without one or deleting the group (Mike's call, 2026-10-23). The pin is implemented (ADR-038 part 1, 2026-11-13): a record naming another creator is refused at any version, `members` is applied only from the creator, and the creator's node refuses to leave or to remove itself; a copy held with an empty `created_by` has no creator, so its roster is frozen. The transfer is not implemented, so a lost creator freezes the roster too.
- **Defined in:** [decisions/038](decisions/038-group-roster-as-signed-state.md#decision)

### roster

- **Met in:** code, rules, screen, wire
- **Meaning:** The group's `members` (human nodes) plus `agents` and `agent_names` per node, carried in `metadata.json` and converged by `GROUP_SYNC` on version with a content-hash tie-break.
- **Defined in:** [GROUP_CHAT.md — Data Model](GROUP_CHAT.md#data-model), [decisions/038](decisions/038-group-roster-as-signed-state.md#decision)

### per-reader role derivation

- **Met in:** code, rules
- **Meaning:** In a group with several agents the same message is `assistant` to its author and `user` to everyone else, so roles are derived per reader when the payload is built, not stored.
- **Defined in:** [GROUP_CHAT.md — Per-Reader Role Derivation](GROUP_CHAT.md#per-reader-role-derivation-adr-031)

### history sync

- **Met in:** code, rules, screen, wire
- **Meaning:** On connect, nodes exchange `{count, hash}` per group and per author, and ship only what differs; disk is the source of truth and `message_id` deduplicates.
- **Defined in:** [GROUP_CHAT.md — Cross-Node History Sync](GROUP_CHAT.md#cross-node-history-sync)

### live-history boundary

- **Met in:** code, rules, wire
- **Meaning:** A node's own statement that it keeps no live history of a conversation older than a moment. It moves forward when that node resets, approves a New Session or honours a session marker. Records that arrive older than it go to the archive, not the live history. It needs no proof because it reaches no one else's records. Absent means "accept everything". Two nodes compare history only inside the later of their two boundaries. Not the **session marker**.
- **Defined in:** [decisions/038 — amendment 2026-09-23](decisions/038-group-roster-as-signed-state.md#amendment-2026-09-23--the-marker-and-the-live-history-boundary)

### session marker

- **Met in:** code, rules, wire
- **Meaning:** `session_started_at` on a group's record: the group agreed to start over. It is honoured only when every member of the receiver's pre-sync roster signed it, and what predates it is archived rather than deleted. It belongs to the group; the **live-history boundary** belongs to one node.
- **Defined in:** [decisions/038 — Q3](decisions/038-group-roster-as-signed-state.md#open-questions)

### feed

- **Met in:** —
- **Meaning:** Not used. `VISION.md:126` rules out feeds, and ADR-037 keeps the word out of code, spec and ADRs because it carries subscription and a social graph. One carve-out: the word appears as the name of Strata's own file `strata-feed.json` and its updater's `--feed` option (upstream artefact names, mentioned in `managers/strata_fetcher.py`): DPC hands `--feed` the pinned release folder and Strata picks its feed file there. Never a DPC concept or identifier. A member's signed messages form an **author attribution chain**; what a node shows live is bounded by its **live-history boundary**.
- **Defined in:** [decisions/037 — Naming](decisions/037-author-attribution-chains.md#decision)

### New Session

- **Met in:** code, rules, screen, wire
- **Meaning:** A vote to archive a conversation's live history and start it empty; in a group every member must be online and vote yes, silence times out as rejection, and a member gone for good blocks the reset until removed.
- **Defined in:** [GROUP_CHAT.md — Session Reset](GROUP_CHAT.md#session-reset)
