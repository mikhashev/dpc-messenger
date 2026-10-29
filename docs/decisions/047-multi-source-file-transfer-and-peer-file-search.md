---
adr: 047
title: "Move large files as signed, content-addressed chunks that a node pulls from several holders at once, over the DPC connections it already has, and let members of a group ask each other who holds a file"
status: proposed
date: 2026-10-29
axis: network, collective
deciders: [Mike]
consulted: []
informed: []
depends_on: [ADR-036]
related: [ADR-032, ADR-038, ADR-041, ADR-046]
supersedes: []
session: "DPC Project group chat, 2026-10-29 — epic FILE-TRANSFER-CANNOT-CARRY-A-MULTI-GIGABYTE-FILE-BETWEEN-TWO-NODES"
---

# ADR-047 — Multi-source file transfer and peer file search

> **Proposed, 2026-10-29. Draft by CC for review; nothing here is decided.** Mike asked for
> torrent-like transfer between DPC nodes — many sources, large binary blocks, in parallel —
> and for search across peers for files and the nodes that hold them, like a torrent
> tracker. His reason: a team has many people and agents, and many files are the same on
> every node, so pulling each one again from a single sender wastes the copies the team
> already has. Every new name below is a **proposal awaiting Mike** and is listed in
> **Names for Mike**; none has a glossary row.
>
> Claims are marked `Observed` (read at HEAD `e5590742` or in a log, location given),
> `Inferred`, or `Not verified`. Line numbers are at `e5590742`; another session is editing
> the file-transfer code today, so they will drift.
>
> **Axis `network, collective`.** `network` because the epic and all its children carry it;
> `collective` because the decision is about what group members share with each other and
> who may learn what another member holds.

## Context and Problem Statement

### What a transfer is today

`Observed`, code at `e5590742`, `dpc-client/core/dpc_client_core/`:

- **One sender, one receiver, one stream.** `send_file` builds one `FILE_OFFER`
  (`managers/file_transfer_manager.py:840-868`); on `FILE_ACCEPT`, `handle_file_accept`
  reads the file in order and sends one `FILE_CHUNK` per chunk, awaiting each send and then
  sleeping `chunk_delay` (`:977-1022`, the default 1 ms at `:547`). Each chunk is
  base64 inside JSON (`:989-1000`); a 64 KiB chunk is about 88 KB framed
  (`dpc_protocol/protocol.py:653`).
- **On the chat connection.** Chunks go through `send_message_to_peer`
  (`p2p_manager.py:1664-1670`), the one connection per peer that carries everything else. A
  second direct connection from the same node replaces the first (`p2p_manager.py:669-676`).
  On WebRTC there is one ordered channel, `dpc-data` (`webrtc_peer.py:266`), and `send` does
  not look at the channel's buffer (`webrtc_peer.py:357-366`). The reader starts one task
  per arriving message (`p2p_manager.py:1313-1322`), so nothing on the receiving side slows
  a fast sender down except TCP itself.
- **One chunk size from config for every file.** `chunk_size` is read once from
  `[file_transfer]` (`:546`, 64 KiB) and used for hashing, the offer and the loop
  (`:669`, `:787`, `:816`, `:846`, `:988`). The receiver already takes `chunk_size` from the
  offer, not from its own config (`message_handlers/file_offer_handler.py:75`, `:246`,
  `:254`; `file_transfer_manager.py:1417`, `:1451`).
- **Integrity.** The offer carries a SHA-256 of the whole file and a CRC32 per chunk
  (`:840-848`), both computed in one read before the offer leaves (`:152-200`, `:786-796`).
  A CRC is an error check, not a hash a hostile holder cannot forge. The receiver writes each
  chunk at its offset into `<name>.<tag>.tmp` (`:1229-1259`, `:1445-1452`, landed in
  `cda6364f`), re-hashes the whole file at the end, and on a mismatch drops it all
  (`:1564-1582`).
- **No resume.** Any end of a download deletes the temporary file (`:1267-1287`); the tag
  is a hash of the `transfer_id` (`:1237`), which a new transfer does not share. The module
  says so (`:16-17`), and so does the spec (`specs/dptp_v1.md:1506`).
- **A group file is relayed, once per member, whole.** A member that finished a group file
  offers it to every connected member except itself, the hop and the author
  (`:1781-1802`), as a new single-source transfer. Each relay counts against
  `max_concurrent_transfers` (3, `:550`, `:774-777`); a relay refused by that cap is logged
  and not retried (`:1808-1830`).
- **What a node holds is known only in memory.** `_group_files_held` keys group files by
  `group_id` + `message_id`, else by hash (`:221-235`, `:538-541`, `:893-938`), is never
  written to disk, and is consulted only to refuse a copy the node already has. That
  refusal is placed after the firewall on purpose, "so a peer it refuses must not learn from
  the answer whether this node holds a file" (`file_offer_handler.py:130-146`) — the project
  already treats holding a file as private.
- **Who may send a file** is the receiver's decision: node entry, then the first group in
  `node_groups` order with an entry, then `file_transfer.proved_connection` (default `ask`),
  else the user is asked (`file_transfer_manager.py:2013-2103`, `firewall.py:130-143`,
  CLAUDE.md "Who may send this node a file"). Nothing governs who may *fetch* from a node,
  because nothing fetches.

### The nearest precedent for search

`Observed`. `SkillCoordinator.search_peer_skills` sends `SKILL_SEARCH` (tags, query) to one
connected peer and waits 10 s for `SKILLS_CATALOG` (`skill_coordinator.py:22`, `:42-81`).
The answering side lists its shareable skills with no firewall check
(`skill_coordinator.py:195-229`). Announcing skills to the DHT is behind the
`auto_announce_to_dht` permission, off by default (`skill_coordinator.py:362-399`,
`firewall.py:1405-1428`). An unknown command is logged and dropped
(`message_router.py:75`; `specs/dptp_v1.md:2769`), so an old peer simply does not answer.

The DHT stores one record per node, keyed by `node_id`, unsigned
(`dht/manager.py:502-549`, `:652-774`). **It does not bootstrap on this node:** this
Windows node's `dpc-client.log` for 2026-10-29 has three "DHT cannot bootstrap: 2 of 2
seed(s) answered and are this node itself" lines and no successful bootstrap (`Observed`;
the Linux and Old PC nodes are per the search card, `Not verified` by me). The board's star
topology — host firewalls block inbound except on one node (`docs/GLOSSARY.md`, **star
topology**) — also means not every pair of members can connect.

### Measured numbers

| What | Number | Source |
|---|---|---|
| LAN, direct TLS, Linux → Windows, `stage.dump.2026-10-28` | 1521 chunks in 130 s = 11.7 chunks/s, **about 0.75 MB/s** | card THE-SENDER-USES-ONE-64-KB-CHUNK…, Windows log 2026-10-28 20:42:50–20:45:00 |
| Same file over the VPN, same day | about 17 chunks/s, **about 1.1 MB/s** | same card; and THE-SENDERS-CHUNK-LOOP-IGNORES-A-CANCEL… (15:21–15:29) |
| Where the LAN time goes | gaps between 10-chunk log lines p50 0.65 s, max 6.8 s; the receiver handles 10 chunks in 7 ms; **about 65 ms per chunk on the sender**, cause not found | card THE-SENDER-USES-ONE-64-KB-CHUNK… |
| 8 329 503 582 B file at 64 KiB | **127 099 chunks** | card, and recomputed |
| 47 039 860 096 B model shard, 2026-10-20 | 89 s of hashing (≈ 530 MB/s for SHA-256 + CRCs in one read), 717 772 CRCs, a refusal 26 s after the offer | epic card, main node log 2026-10-20 01:58–02:00 |
| CRC list in that offer | 717 772 × 8 hex ≈ 7.9 MB of JSON with quotes and commas (`Inferred` from the count) | THE-CHUNK-CRC-LIST… card says about 7 MB |

`Not measured`: throughput with any chunk size other than 64 KiB; throughput from more than
one source (impossible today); chat latency while a transfer runs; SHA-256 speed per chunk
on the three nodes.

### The question

Whether to stretch today's single-source stream, to build a pull protocol of our own over
our connections, or to adopt an existing swarm system — and, for the one chosen, how a
receiver knows a chunk is right when it came from a node that is not the author, who may
fetch what from whom, and what a search may reveal.

## Decision Drivers

- **Identity and firewall stay ours.** Peers are `node_id`s proved by certificate
  (`specs/dptp_v1.md` §4, ADR-041 D2); a file reaches a node only through its rules.
- **Holding a file is private.** An answer must not tell a non-member that a node holds a
  file, nor confirm a hash to someone who could not have received it.
- **Works on the topology we have:** one connection per pair, often only in one direction,
  through several tiers; no new listening port.
- **Chat stays usable during a large transfer.**
- **Old peers keep working** with today's `FILE_*` flow, unchanged.
- **Windows, Linux, macOS** with no native dependency we do not already ship.
- **Measured, not assumed.** The 65 ms per chunk is not explained; the first phase must
  produce the number every later phase is compared with.

## Considered Options

- **A — Keep the single-source `FILE_*` flow; choose a larger chunk per file; later a
  binary chunk frame.** No new roles, no search.
- **B — Our own content-addressed chunk protocol over the existing DPC connections.** A file
  is named by its hash; its author signs a description that binds a list of per-chunk
  SHA-256 digests; a receiver pulls chunks from several holders in parallel as binary
  frames, verifies each, keeps what it has on disk and resumes; holders are found by asking
  connected group members, the DHT later.
- **C — Adopt an existing system:** BitTorrent through libtorrent, WebTorrent, or IPFS
  (kubo with bitswap).

### Pros and Cons of the Options

#### Option A

- Good: phase 0 of it is a sender-only change — the receiver already uses the offer's
  `chunk_size`. A larger chunk shrinks the CRC list and the message count by the same factor
  (1 MiB: 7 944 chunks for the 8.3 GB file, 44 861 for the 47 GB shard).
- Good: no new trust question; the connection authenticates the sender.
- Bad: one source. A file five members hold still comes from the one that offers it, and in
  the star it reaches the rest only after the hub node has it whole and relays it, three
  uploads at a time.
- Bad: no resume across a restart without more work, and a hash mismatch still drops all of
  a file that carried only CRCs.
- Bad: a larger chunk in one JSON frame blocks chat behind it: a 4 MiB chunk is about 5.6 MB
  of base64, about 5 s on the 1.1 MB/s VPN link (`Inferred` from the two numbers).

#### Option B

- Good: uses every copy the team has; resume and per-chunk repair come with the chunk list.
- Good: rides every tier the connection already rides, so the star topology and the
  one-connection-per-pair rule are not obstacles; no new port, no new dependency (SHA-256
  and RSA-PSS are in the stdlib and `cryptography`, which we ship).
- Good: the firewall, the group roster and the verdict rules of ADR-036 apply as they are.
- Neutral: the design is known in kind (BitTorrent v2 per-file SHA-256 trees, IPFS want/have
  lists); what is ours is the binding to `node_id` and to the group.
- Bad: the largest of the three to build and to test, and a DPTP version bump.
- Bad: a holder must read and send for others, which is resource sharing (VISION C8, C10)
  and needs a switch and limits.

#### Option C

- Good: mature swarming, endgame and choking logic; BitTorrent v2 already binds a file to a
  SHA-256 Merkle root (BEP 52, recalled, not re-read for this record).
- Bad — **identity**: a BitTorrent peer is an IP address and a random peer id; an IPFS peer
  is a libp2p key. Either is a second identity beside `node_id`, or a second use of
  `node.key`, which the open card ONE-RSA-KEY-SIGNS-THE-NODES-MESSAGES… already counts as a
  risk.
- Bad — **firewall and privacy**: anyone who learns an infohash or a CID and reaches the
  port may fetch; the public DHT, trackers, PEX and local discovery announce what a node
  wants and holds unless every one is switched off; IPFS bitswap sends want-lists to
  connected peers by design. Our firewall would sit outside the engine, not in it.
- Bad — **topology**: each runs its own listener and NAT traversal (uTP, UPnP, libp2p
  relays). On the stand, where inbound is blocked except on one node, it would not connect
  without tunnelling its streams through our connections — at which point we keep only its
  scheduler.
- Bad — **dependency and platforms**: libtorrent is a C++ library behind Python bindings;
  kubo is a Go daemon we would supervise as a child (ADR-046); WebTorrent is JavaScript and
  would move file I/O into the webview. Wheels and binaries for all three platforms exist
  for some versions (`Not verified` here), and each is one more thing to pin and update.

## Decision

**Option B, entered through Option A's phase 0.** Recommended for Mike's call, not decided.

### Rationale

A gives a fast first number and nothing for the team case. C brings a better scheduler at
the price of everything that makes a DPC transfer a DPC transfer: identity, firewall,
privacy and the connection tiers would all live outside the engine. B keeps those and
borrows C's ideas. Phase 0 (A's larger chunk) costs one sender change and tells whether the
65 ms per chunk is a per-message cost — the number that sizes everything after it.

### Design of the recommended option

The names in `code font` that are new are proposals (see **Names for Mike**).

**1. Content identity.** A file is named by the SHA-256 of its bytes — the offer's existing
`hash`, already the group-file fallback key (`group_file_keys`). It is divided into chunks
of a fixed `chunk_size` (proposed **1 MiB** on this path; below). Each chunk has a SHA-256;
**`chunks_hash`** is the SHA-256 of the 32-byte digests of every chunk, concatenated in
order. Both are computed in the one read that already serves the hash and the CRCs
(`hash_file_in_one_pass`); the cost is one more SHA-256 over the same bytes (`Inferred`,
measured in phase 1).

- *Why SHA-256:* already the hash of the offer, the history and the signatures; no new
  dependency; accelerated on current x86 and ARM CPUs (recalled). BLAKE3 is faster but is a
  native package we do not ship, and would put two hashes on every file.
- *Why a flat list and not a Merkle tree:* at 1 MiB the list is 254 KB for the 8.3 GB file
  and 1.4 MB for the 47 GB shard — one frame, fetched once from any holder and checked
  against `chunks_hash`. A tree pays when the list is too large to fetch first; a terabyte
  is 32 MiB of list, still under `MAX_FRAME_BYTES`. A tree can replace the list later under
  a new preimage version.
- *Why 1 MiB:* one chunk is one frame. On the 1.1 MB/s VPN link a 1 MiB frame holds the
  connection about 1 s; 4 MiB would hold it about 4 s. 1 MiB is also the request unit, the
  verification unit and the repair unit. Files under 1 MiB are one chunk. Phase 0 measures
  whether the size is right.

**2. The author binds the description to its key.** The author signs a **file preimage**
in the §4.1 encoding (length-prefixed UTF-8, fixed order), tagged **`dptp-file-v1`**:

| # | Field |
|---|---|
| 1 | `dptp-file-v1` |
| 2 | `conversation_id` — the `group_id`; for a 1:1 file, the recipient's `node_id` |
| 3 | `message_id` — the author's id for the file, as on a group file today |
| 4 | `sender_node_id` — the author |
| 5 | `size_bytes`, decimal |
| 6 | `chunk_size`, decimal |
| 7 | `hash`, hex |
| 8 | `chunks_hash`, hex |
| 9 | `filename` |
| 10 | `mime_type` |

`file_signature` is RSA-PSS over the hex SHA-256 of that preimage, the same call as §4.1
(`CommitSigner.sign_commit`). The tag is field 1 for the reason the HELLO preimage has one
(`specs/dptp_v1.md` §3.1, v1.11): `node.key` signs messages, votes, gossip, commits and
HELLOs, and the tag keeps a file signature from being read as any of them. It travels as
optional fields of `FILE_OFFER` (`chunks_hash`, `file_preimage_version`, `file_signature`)
and in every search answer. A receiver verifies it against the author's certificate under
the verdict rules of §4.2: **verified** → chunks may come from any holder; **unverified**
(certificate not cached) or **absent** (an old author) → single source only, from the node
that offered it, as today. A relaying member never signs for the author. The existing
message preimage stays as it is — its signature covers no attachment field
(`file_transfer_manager.py:1694`) — and binding through a `dptp-msg-v4` field was
considered and not taken: it would tie the file to message-version churn and to history
sync, which a receiver of the file may not have yet.

*What this buys:* a holder cannot feed a bad chunk unnoticed (its SHA-256 is in the signed
list), and the receiver knows which holder sent it and drops that holder for this file. The
whole-file `hash` is still checked at the end; on a mismatch the chunks are re-verified one
by one and only the bad ones fetched again, instead of today's drop of everything.

**3. Who holds what, on disk.** Each node keeps a **`held_files`** index — the persisted
successor of `_group_files_held`: per file, `hash`, `chunks_hash`, `chunk_size`,
`size_bytes`, `filename`, `mime_type`, the conversation (`group_id` or the peer),
`message_id`, `sender_node_id`, `file_signature`, the local path, and whether the file is
complete. Written when a file is sent or finalised, removed when the file is gone from disk
(checked on read). The path is local and never leaves the node.

**4. Who may fetch what, from whom.** A holder serves a chunk to a requester only if all of
these hold, checked on every request:

- the file is in its `held_files` and complete;
- for a **group file**, the requester is in that group's `members` on this node now (the
  roster of ADR-038), and the requester's tier is one the group already uses for files;
- for a **1:1 file**, the requester is the other party of that conversation — so this path
  gives a 1:1 file resume, never extra sources;
- the owner's switch `file_transfer.serve_chunks` is on.

Every other case — not held, not a member, wrong conversation, switch off — gets one and the
same answer, `not_held`, so the answer is no oracle for "does this node hold hash H". A
requester that names a hash it could not have been offered learns nothing more than a
requester asking for a random hash.

Proposed default for `serve_chunks`: **on**. A member already receives every group file by
push today (the relay), so serving chunks of a group file to a member widens nothing; it
replaces a push with a pull. Whether a bandwidth share like this must be opt-in under C8 is
**Q3**.

**5. Finding holders and searching.** One request, **`FILE_SEARCH`**, answered by
**`FILES_CATALOG`**, shaped after `SKILL_SEARCH` / `SKILLS_CATALOG`:

- `FILE_SEARCH`: `request_id`, `group_id` (required), and either `hash` (who holds this
  file?) or `query` (a substring of the file name, optionally `mime_type`), and `limit`.
- `FILES_CATALOG`: `request_id` and entries of `hash`, `chunks_hash`, `filename`,
  `size_bytes`, `mime_type`, `group_id`, `message_id`, `sender_node_id`, `file_signature`,
  and `holders` — the node ids this node knows to hold it. **Never a path.**
- A `hash` query is answered under `serve_chunks` (rule 4: a member may learn that a member
  holds a file of their group). A `query` by name is answered only under the opt-in
  **`file_search.enabled`**, off by default, because it lists what a node holds. Answers
  include only files of groups the requester is a member of. 1:1 files are never listed.
- **How it travels, in this order:** (i) to every connected member of the group, at once,
  10 s timeout as `SKILL_SEARCH`; (ii) the `holders` a catalog names are asked in turn,
  directly if connected — so in the star the hub node's answer reaches members behind it;
  (iii) the DHT, once it bootstraps, with records keyed by `hash` and signed by the holder,
  readable only by members — a separate decision, not part of this one (**Q6**).
- A search reveals the searcher's interest to the members it asks; it is never sent to a
  non-member.

**6. Pulling chunks.** The receiver asks each holder for chunks with
**`FILE_CHUNK_REQUEST`** (`request_id`, `hash`, `chunk_index` list, or `chunk_hashes` to ask
for the list itself). The holder answers each chunk with a **`FILE_DATA`** frame, or with one
`FILE_DATA` carrying `reason` (`not_held`, or `busy` with `retry_after_sec`) and no bytes.

Scheduling, receiver-driven: a shared queue of missing indices in file order; each holder
gets up to **`chunks_in_flight`** requests outstanding (proposed 8 — 8 MiB), topped up as
chunks land, so a fast holder takes more of the file and a slow one less; a request
unanswered in 30 s (today's `RECEIVE_STALL_TIMEOUT_S`) goes back to the queue; when the
queue is empty, the remaining outstanding indices are asked again of idle holders and the
first answer wins (the endgame); at most **`max_sources`** holders per download (proposed 4).
Only complete holders serve in this decision; a node part-way through a download serving
the chunks it has is **Q7**.

Holder side: at most `max_concurrent_transfers` requesters served at once, the existing
cap; requests beyond it get `busy`.

**7. The binary frame, on the connection that exists.** A `FILE_DATA` frame is an ordinary
DPTP frame (`hash`, `chunk_index`, `data_bytes`) followed by exactly one raw frame: the same
10-byte length header, then `data_bytes` raw bytes, at most `chunk_size`. The reader reads
the raw frame right after the JSON one and refuses a length above the chunk size of that
file. On WebRTC the same pair is a text message and a bytes message on the ordered
channel. No base64: the third of extra bytes is gone.

- **Why the same connection and not a second one.** A second direct connection from the
  same node replaces the first today (`p2p_manager.py:669-676`), in the star a second
  connection can only be dialled the way the first was, and WebRTC and DTLS would each need
  their own variant. One connection with priority is one design for every tier. A second
  connection for data stays open as a later optimisation if phase 2 measures chat suffering.
- **Priority.** Each peer connection gets a two-lane send queue: every other message first,
  `FILE_DATA` only when the first lane is empty. Chat then waits at most for the one chunk
  already being written.
- **Backpressure.** On TLS, `write_message` already awaits `drain()`
  (`protocol.py:723-725`). On WebRTC the sender waits until the channel's `bufferedAmount`
  falls below a threshold before the next frame (aiortc's attribute, recalled, `Not
  verified`). On the receiver, `FILE_DATA` is written to disk in the read loop, not in a
  task per message (`p2p_manager.py:1322`), so a slow disk slows the reader and TCP slows
  the holder; the request window bounds the rest.
- **Tiers.** Served on `direct_tls`, `webrtc` and `udp_dtls`. Not on a volunteer relay —
  another person's bandwidth, C10 — and not on gossip. Over those a file goes single-source
  as today, or not at all (**Q5**).

**8. Resume.** A download is keyed by `chunks_hash`, not by `transfer_id`. Its temporary
file is named from it, and beside it a **`<name>.<tag>.received`** sidecar holds the bitmap
of verified chunks (1 bit per chunk: 1 KB for the 8.3 GB file at 1 MiB). A chunk is written,
flushed, then marked; the bitmap is rewritten at most once a second. A cancel by the person
deletes both; a disconnect, a restart or a crash keeps them, and the download resumes from
the bitmap when a holder is reachable. A partial older than a limit is removed at start
(**Q8**). Single-source transfers from new senders gain resume too, through the same pull.

**9. Agents.** One agent tool, **`search_peer_files`**, and the local API command of the
same name (after `search_peer_skills`, `service.py:8819`): a search in one group, answering
hashes, names, sizes, types, the group and the holders by name, never a path. It is
registered with `default_enabled=False` (CLAUDE.md: network egress), and
`_seed_missing_tools_into_rules` then adds it switched off to every profile. The answer
reaches the agent's model, which may be a cloud one: file names of the team then leave the
machine, a second reason it is off. A tool that also *starts* a download into the group's
folder touches user-visible state; it is not proposed here (**Q9**). `search_files` is
already the name of the agent's local sandbox search (`dpc_agent/tools/core.py:2552`), which
is why the new tool carries `peer`.

**10. Compatibility.** DPTP 1.12, additive, not a hard cut. An old peer ignores
`FILE_SEARCH` and `FILE_CHUNK_REQUEST` (unknown commands are dropped) and therefore never
receives a `FILE_DATA` frame, which only answers a request — no capability handshake is
needed. An old receiver ignores the new optional offer fields and gets today's stream. A
new receiver that wants to pull answers `FILE_ACCEPT` with **`pull`: true**, and the new
sender then waits for requests instead of streaming; an old sender never sends
`chunks_hash`, so a new receiver never asks it to. The group relay stays for old members and
is skipped for members that pull. The spec's FILE_* text is amended where it says there is
no resume.

**11. Limits and abuse.**

- `FILE_SEARCH`: per requester at most one in flight and 10 a minute; `limit` at most 50
  entries; `query` at most 128 characters (the bound `offer_identity_field` uses).
- `FILE_CHUNK_REQUEST`: at most `chunks_in_flight` outstanding per requester per file; more
  is `busy`. Indices out of range are refused; the hash list is served at most once per
  requester per file per hour.
- A `FILE_DATA` longer than its chunk, or for an index not requested, closes the download
  from that holder; a chunk that fails its SHA-256 drops the holder for the file and is
  logged with its node id.
- Every refusal reads `not_held` unless it is `busy`; no timing shortcut for "held but
  refused" (the check order is the same for both, then the answer).

### Phased plan, on the board's cards

| Phase | What | Wire | Cards |
|---|---|---|---|
| 0 | Sender picks `chunk_size` per file (this record: 64 KiB up to 100 MB, 1 MiB above; the card proposes 4 MB above 1 GB, Q2); nothing else | none (the receiver already reads the offer's size) | THE-SENDER-USES-ONE-64-KB-CHUNK…; THE-CHUNK-CRC-LIST… shrinks 16× |
| 1 | `chunks_hash`, `dptp-file-v1` signature, `held_files`, resume keyed by content, per-chunk repair on a final mismatch | optional `FILE_OFFER` fields | A-FILE-TRANSFER-CANNOT-RESUME-AFTER-A-RESTART; the hash-mismatch gap of A-RECEIVED-FILE-IS-HELD-WHOLE… |
| 2 | `FILE_CHUNK_REQUEST` / `FILE_DATA` raw frames, `pull`, two-lane send queue, one holder | DPTP 1.12 | FILE-CHUNKS-TRAVEL-AS-64-KB-BASE64-JSON…; THE-CHUNK-CRC-LIST… (list fetched, not in the offer) |
| 3 | Several holders, scheduler, endgame, `max_sources` | same | A-FILE-HELD-BY-SEVERAL-NODES-STILL-COMES-FROM-ONE-SOURCE… |
| 4 | `FILE_SEARCH` / `FILES_CATALOG`, `file_search.enabled`, `search_peer_files` tool and command | same | A-NODE-CANNOT-SEARCH-ITS-PEERS-FOR-A-FILE… |
| later | Holder records in the DHT | DHT | blocked on the DHT bootstrapping |

A-RECEIVER-CAN-REFUSE-A-FILE-ONLY-AFTER-THE-SENDER-HASHED-ALL-OF-IT is compatible and runs
beside phase 1: its early offer (name, size, type) is answered first, and the hashing that
produces the signed description happens after a yes. Phase 4 can move before phase 3 if
search is wanted first: a `hash` search is what phase 3 needs, a name search is not.

## Consequences

- **Positive:** a file the team has comes from all of it; a restart costs at most a window
  of chunks; a corrupted chunk costs a chunk; group files stop being pushed N times by the
  hub node; agents can find what the team already holds.
- **Negative:** a holder spends disk reads and upload for others; a node's index of what it
  holds becomes a file on disk, readable by anything that reads `~/.dpc`; two transfer
  paths live side by side for as long as old peers exist; one more hash pass per file sent.
- **Neutral:** the DHT stays out of this decision; the firewall's "who may send me a file"
  is untouched; a pull is still subject to it (the receiver asked).

## Confirmation

Each phase is accepted by measurements on the three-node stand, the numbers written to its
card:

- [ ] **Phase 0:** the same 8.3 GB file over the same LAN link with 1 MiB chunks, MB/s
  against the 0.75 MB/s baseline; the sender's per-chunk time from its log; a node on the
  build before phase 0 receives a 1 MiB-chunk file and its hash matches; chat round trip
  (send to display on the peer) during the transfer, p50 and max.
- [ ] **Phase 1:** a 1 GB transfer interrupted at about half by restarting the receiver,
  then the sender: it resumes, and the bytes sent twice are at most `chunks_in_flight`
  chunks; a chunk corrupted on disk before finalisation is found by its SHA-256 and fetched
  again alone; a file signature made over `dptp-file-v1` does not verify as a message or a
  HELLO preimage (test).
- [ ] **Phase 2:** the 8.3 GB file from one holder, MB/s against phase 0; chat round trip
  p95 during it; an old peer on the same link receives an ordinary offer and completes.
- [ ] **Phase 3:** the same file held by three nodes and pulled by a fourth (or by one of
  the three after deleting it), aggregate MB/s and per-holder share; one holder made to
  serve a wrong chunk is dropped and the file completes from the others; a holder that
  disappears mid-file costs no more than its window.
- [ ] **Phase 4:** a member's search returns the holders of a group file; the same search
  from a non-member, and a `hash` query for a file of another group, return what a query for
  a random hash returns; `search_peer_files` appears switched off in a fresh
  `privacy_rules.json`; no answer on the wire carries a path (test over every field).
- [ ] Every phase: the full client suite on Windows, and CI on ubuntu for the ref; macOS is
  not covered by CI and is named as such.

## Names for Mike

Proposals; none accepted, none in the glossary. "Hits" is `rg --no-ignore -c` over the
repository and the gitignored layer on 2026-10-29, this record excluded; 0 means the name
is new.

| Proposed name | What it means | Built from | Hits |
|---|---|---|---|
| **`chunks_hash`** (offer, catalog, preimage field) | SHA-256 of the concatenated 32-byte SHA-256 digests of every chunk, in order | `chunk_hashes` (the CRC list), `hash`, `content_hash` | 0 |
| **file preimage**, tag **`dptp-file-v1`**, fields **`file_preimage_version`**, **`file_signature`** | What the author signs to bind a file's description to its key | HELLO preimage, `dptp-hello-v1`, `hello_preimage_version`, `hello_signature`; `dptp-msg-v3` | 0 each |
| **`held_files`** (an index on disk; file name to be decided) | This node's list of files it holds and their descriptions | `holds_group_file`, `_group_files_held` | 0 |
| **`FILE_SEARCH`** / **`FILES_CATALOG`** (DPTP commands) | Ask members of a group which files they hold, by hash or by name; the answer | `SKILL_SEARCH` / `SKILLS_CATALOG` | 0 |
| **`FILE_CHUNK_REQUEST`** (DPTP command) | Ask one holder for chunks, or for the chunk-hash list | `FILE_CHUNK_RETRY`, `SKILL_REQUEST` | 0 |
| **`FILE_DATA`** (DPTP command) with **`data_bytes`** | One chunk, as a JSON frame followed by one raw frame; or a refusal with `reason` | `SKILL_DATA`, `FILE_CHUNK`, `size_bytes` | 0 each |
| **`not_held`**, **`busy`** (values of `FILE_DATA.reason`) | Not served, for any reason but load; served later | `records_not_held` (`knowledge_service.py:1019`); `retry_after_sec` (DPTP v1.8) | `not_held` 0 as a whole word (inside `records_not_held` and a test name); `busy` ordinary English |
| **`holders`** (catalog field; screen word "holders") | The nodes known to hold a file | `holds_group_file` | 144 for "holders": in the sense of "holds a path open" in `file_transfer_manager.py:535`, and English elsewhere — a near-collision to weigh |
| **`pull`** (optional `FILE_ACCEPT` field) | The receiver will request chunks; do not stream | — | 1 as `"pull"`, 135 as a word: git and Ollama `pull`, and the gossip "anti-entropy pull" (`specs/dptp_v1.md:2890`) |
| **`file_transfer.serve_chunks`** (privacy rule, default on) | Members of a group may pull its files' chunks from this node | `file_transfer.*` keys; the relay | 0 |
| **`file_search.enabled`** (privacy rule, default off) | This node answers name searches from members | `transcription.enabled`, `compute.enabled` | 1 (`file_search` inside a test name on the board) |
| **`chunks_in_flight`**, **`max_sources`** (`[file_transfer]` keys) | Outstanding requests per holder; holders per download | `max_concurrent_transfers`, `chunk_size` | 0 each |
| **`.received`** sidecar (`<name>.<tag>.received`) | Bitmap of the verified chunks of a partial download | `<name>.<tag>.tmp`, `chunks_received` | 4 as `.received`: a test attribute (`self.received`), not a file name |
| **`search_peer_files`** (agent tool and local API command) | Search one group's members for files | `search_peer_skills`, `search_files` | 0 |
| **"sources"** (screen word in the transfer row, "from 3 sources") | How many holders a download is pulling from now | the epic's "many sources" | not counted: an ordinary word on screen |

## Open Questions

1. **Option B with A's phase 0 first** — or A alone for now, with B later? (Mike)
2. **Chunk size on the new path: 1 MiB** for every file, or scaled by size as phase 0's
   proposal (64 KiB / 1 MiB / 4 MiB) at the cost of a longer chat stall per frame on slow
   links? (Mike, after phase 0's numbers)
3. **`serve_chunks` on by default** for group files, because the relay already pushes them
   to every member — or off, as C8 asks of shared compute? (Mike)
4. **Which tiers may serve chunks:** `direct_tls` only (the proved line of ADR-041 D2), or
   also `webrtc` and `udp_dtls`, as group files travel today? (Mike)
5. **Relayed connections:** single-source as today, or refuse large files over a volunteer
   relay altogether? (Mike)
6. **The DHT:** holder records in it are a later decision; does it wait for the DHT to
   bootstrap on the owner's nodes, which no card schedules now? (Mike)
7. **Partial holders:** may a node part-way through a download serve the chunks it has (the
   swarm effect for a fresh file), or only complete holders, as proposed? (Mike)
8. **Partials left behind:** how long a resumable partial lives before it is removed at
   start, and whether the person sees a list of them. (Mike)
9. **Agents fetching:** is search enough, or should an agent tool also start a download
   into the group's folder (user-visible state, so `default_enabled=False` as well)? (Mike)
10. **Names** — every row of **Names for Mike**, in particular `holders` (near-collision in
    `file_transfer_manager.py`) and `pull` (the gossip sense). (Mike)
11. **1:1 files:** resume from the sender only, as proposed, or also extra sources when the
    two parties share a group that holds the same hash? (Mike)

## Authors

- **Mike** — the request and the reason; Decision pending
- **CC** — Analysis, Draft (2026-10-29)

## References

- Board: epic FILE-TRANSFER-CANNOT-CARRY-A-MULTI-GIGABYTE-FILE-BETWEEN-TWO-NODES and its
  children named above; THE-SENDERS-CHUNK-LOOP-IGNORES-A-CANCEL… (`172c7aac`);
  THE-ACTIVE-TRANSFERS-PANEL-SHOWS-NO-SPEED…; ONE-RSA-KEY-SIGNS-THE-NODES-MESSAGES…
- `specs/dptp_v1.md` §2 (frame caps), §3.1 (HELLO preimage), §3.11 (FILE_*), §3.17
  (SKILL_SEARCH), §4.1–4.2 (signing, verdicts), §7 (extensions)
- [ADR-036](036-message-authenticity-signed-at-origin.md) — signed at the author;
  [ADR-038](038-group-roster-as-signed-state.md) — the roster that decides membership;
  [ADR-041](041-openai-compatible-gateway-as-a-dpc-surface.md) — the proved-connection line;
  [ADR-032](032-group-attachment-local-first-and-voice-modality.md) — group files deferred
  for a size and chunking design; [ADR-046](046-the-backend-owns-every-process-it-starts.md)
  — what adopting a daemon (Option C) would mean
- `cda6364f` — receive to disk at offsets; `172c7aac` — a cancel ends the chunk loop
