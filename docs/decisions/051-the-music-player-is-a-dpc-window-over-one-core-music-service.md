---
adr: 051
title: "The music player is a DPC window over one core music service, with one door for the UI and one for agents, and engines reached directly"
status: accepted
date: 2026-11-19
axis: collective, knowledge
deciders: [Mike]
consulted: [CC, Zcode, Forge, Ark, Warren, Johnny]
informed: []
depends_on: [ADR-040, ADR-044, ADR-046]
related: [ADR-032, ADR-041, ADR-047, ADR-052]
supersedes: []
session: "AI Studio group chat (group-60aff6110f02), 2026-11-19 03:17-05:33 — epic THE-AI-MUSIC-PLAYER-RUNS-ONLY-AS-A-SIMULATED-BROWSER-PROTOTYPE-AND-DPC-HAS-NO-PLACE-TO-HOST-IT; Mike asked for this ADR at 05:31; reviews by Zcode, Forge and Ark at 06:03-06:33"
---

# ADR-051 — The music player is a DPC window over one core music service

> **Proposed 2026-11-19, accepted. Built and on `origin/dev` as of 2026-11-22 (checked at
> `1fa73a26`): Phase 0 and the listener part of Phase 1, and the first creative slice (own text
> to takes, review, hide/delete, MP3 export, provenance, comments, waveform). Not built: the
> `music_*` agent tools, QC, region edits, audio into the gateway, SoundCloud/Yandex, the
> voice clone, and the card lease for the engines (ADR-052 S4). See Implementation Status.**
> Mike asked for this record at
> 05:31 in the AI Studio group chat, "following the template". Only the points under
> **Decided** are Mike's. Everything under **Not decided** and **Open Questions** is open
> or is a proposal, and needs his yes. This file is self-contained, so it can move to
> `ai-studio` if Mike prefers.
>
> **Revised 2026-11-19 after three reviews** in the same chat, requested by Mike at 05:56:
> Zcode (06:03, "accepted with small fixes"), Forge (06:04, checks plus measurements) and
> Ark (06:33, "three factual fixes and four missing Scope items"). Each point taken from a
> review is attributed to its reviewer. Reviewer positions on Mike's open questions are
> recorded under Open Questions; none of them is a decision.
>
> **Revised 2026-11-22, whole document against the code at `1fa73a26` (`origin/dev`) and the
> decisions up to then** (Mike's request, 2026-11-22: "bring ADR-051 up to date"). Every
> status row now says what the code does, with commits, and every commit named there is on
> `origin/dev`. What changed in substance: the first creative slice is built, so several
> open items are answered by the code (N7, N11 in part, N12, N13, `Where takes live`); D30-D36
> are marked built; D37-D39 point at SPEC decisions 32, 33 and 39, which had no entry here;
> the relations to ADR-052 (the card lease: Transcribe in S3, the music engines in S4, the
> idle-swap rule of Mike's #70, the "Stop X" button) are written where this record talks
> about the card; stale text was removed (the axis paragraph, the "not on the board" and "not
> started" rows, Q8's number). What the code, SPEC, EVOLUTION and this record still disagree
> on is listed under **Open Questions, Q9**. `accepted` is kept: the record's own lifecycle
> says `implemented` means the decision is built, and the tools, QC, region edits and the
> card lease are not.
>
> **Words.** `docs/GLOSSARY.md` rows are taken as the meaning. **Engine** here means a *music
> engine*, the program that makes the audio (acestep.cpp, yue2.cpp); the glossary's
> **engine** is the program a local LLM provider answers with (`llama-server`, `strata`).
> The two share the one card (**card hold**, **card lease**, **local engine swap** are the
> glossary words for how it changes hands). The glossary has no rows yet for **track**,
> **take**, **version** or **watched folder** (Consequences).
>
> **How claims are marked.** `Observed` means read by CC on `dpc-messenger` branch `dev`
> at `2d333000` (HEAD, 2026-11-19 13:07 local), or in a log or a config file with the
> place given. `2d333000` sits one commit above `95acf009`, which the reviewers read, and
> that commit changes only `docs/decisions/044-…md`, so every code line the reviewers
> cited at `95acf009` is the same line at `2d333000`. **The working tree is not clean**:
> on top of `2d333000` there is this ADR (untracked) and a regenerated `ROADMAP.md`
> (modified, the axis table now lists ADR-051). Every `path:line` in this file is at
> `2d333000` unless it says otherwise; line numbers will drift. They have: by `1fa73a26`
> `service.py` had grown by about 500 lines. The names are the stable reference; the
> references in **Scope** were re-read at `1fa73a26` and say so, and the `Observed` lines in
> Context and in the risk section are history of 2026-11-19, kept as written. `Inferred` is reasoning
> not run. `Reported` is stated by an agent in the thread and not re-checked here.
>
> **Axis `collective, knowledge`** (Q7: Mike, 2026-11-19 07:39, "more from personal to
> collective"; the front matter says so). `knowledge` because the player is a place where a
> person and an agent work on one track: a comment becomes a job, the job returns takes, and
> the person keeps one. The epic carries `knowledge, network`; `network` is dropped here
> because the card is shared through ADR-052 (axis `network`), not through this record.

## Context and Problem Statement

*This section is the situation of 2026-11-19, when the record was written; it is kept as
written. What was missing then and is built now is in Implementation Status.*

On 2026-11-06 the AI Studio group discussed a music studio: an editor where a person and
an agent work on the same track (SPEC §1 calls this "the 2026-11-06 vision"). The research
that day also found the GPU problem. Strata held 30.9–32.2 GiB of the RTX PRO 4500's
32.6 GiB, and the figure varies between readings (`Reported`, Ark: two readings within a
minute on 2026-11-06, 32 212 and 30 901 MiB in use). A generation job that DPC did not
start cannot ask Strata to give the card back (card
EXTERNAL-GPU-JOBS-CANNOT-JOIN-THE-CARD-QUEUE-SO-AN-IDLE-ENGINE-KEEPS-THE-CARD-THEY-NEED,
filed 2026-11-06).

By 2026-11-18 the idea had narrowed to a **player-reviewer**. A person listens, marks
*where* a track is wrong with a timestamped point or region comment, and sends it to an
agent. The agent returns takes. The person compares each take with the original at the
seam (A/B) and accepts one as a new version. The original file is never overwritten. On
2026-11-19 Mike put plain listening first: "so I can just listen to music, and generate
or edit when I want to" (03:17). He then set the modes, the sources and the engines.

What exists today is a spec, `ai-studio/experiments/music-player/SPEC.md`, and a simulated
browser prototype, `prototype/index.html`, where every engine is a labelled mock. Neither
is committed in ai-studio: `experiments/music-player/` is untracked at `f341db1` (*it is
committed since, in ai-studio, which has no remote*). None of
the runtime pieces exist in DPC (epic THE-AI-MUSIC-PLAYER-RUNS-ONLY-AS-A-SIMULATED-BROWSER-PROTOTYPE-AND-DPC-HAS-NO-PLACE-TO-HOST-IT):

- `Observed`: no agent tool posts a file into its own chat. The agent tool names in
  `dpc_agent/tools/*.py` include `read_file`, `write_file` and `send_user_message`
  (`messaging.py:165`), and none of them attaches a file to the chat.
- `Observed`: no chat attachment plays a music file. The attachment type is
  `'file' | 'image' | 'voice'` (`ui/src/lib/types.ts:17`, `mime_type?` at `:23`).
  `ChatMessageList.svelte` draws `image` at `:419`, `voice` with `voice_metadata` at
  `:422`, and anything else as a plain file row (`:445-464`), which prints `mime_type`
  (`:452-453`).
- `Observed`: the agent never learns that a non-image file is in its history. The history
  replay adds attachment markers only from `image_attachment_markers(record)`
  (`dpc_agent/context.py:877`), and that function skips every attachment whose
  `type != "image"` (`:895-913`, the skip at `:912`).
- `Observed`: nothing turns a job into takes, there is no player component, and no audio
  judge can run. The `llama-server` provider sends `image_url` parts only
  (`providers/llamacpp_server_provider.py:1761-1790`). `input_audio` occurs **0 times in
  the `dpc_client_core` package**. Its only occurrence under `dpc-client/core` outside
  `.venv` is a test that asserts the gateway **refuses** it
  (`core/tests/test_the_gateway_carries_the_effort_and_the_image_through_both_doors.py:602-607`).
  Forge counted 95 files with the word across `dpc-client`, nearly all in `.venv`, so a
  plain search over the client overstates what exists.
- `Observed`: there is no `music_*` agent tool, no `~/.dpc/music/` and no
  `~/.dpc/models/music/` (CC's listing, 2026-11-19 13:40 local; Forge reports the same).
  `~/.dpc/models/` holds only LLM and OCR model folders and `strata/`.

It is a research project. The question this record answers is where the player lives,
how it reaches files, engines and the GPU, and how agents drive it, so that the
child cards can be built in order without going back over these questions.

## Decision Drivers

- **Listening first.** The player has to work as a plain player before anything else.
  Review and generation are opt-in actions on the track that is playing.
- **One card, two tenants.** Every music engine needs the GPU that Strata holds. The
  existing swap (ADR-044, amendment of 2026-11-02) is how the card changes hands. ADR-052
  (the card lease, proposed) extends that to the clients that are not supervised
  `llama-server` children: Whisper and these music engines.
- **One rule, one place.** The person (in the UI) and an agent (through tools) make the
  same requests. The capability check, the hash check and never-overwrite must not be
  written twice.
- **No new dependency surface without need.** DPC has no MCP client and no ComfyUI
  dependency in its own paths, and Mike wants it kept that way for the player.
- **The person decides.** A take never replaces a version unless the person asked for it.
- **Honest capability.** An engine that is not installed shows `unsupported` with its
  reason. It never shows a simulated result as if it were real (SPEC §7).

## Prior art outside our engine

Required by the template (Mike's call, 2026-11-17).

- **(a) The project's own reading.** Searched 2026-11-19 with `rg --no-ignore -i -w
  "music|song|playlist|lyrics|melody"` over `dpc-messenger/ideas/dpc-research/articles`
  (47 files). The only hits are author names ("Song", "Melody") in reference lists. There
  is no article on music generation, players or audio libraries. In the ai-studio project,
  `ai-studio/model-candidates.md:162-165, 262-263, 294-295` lists ACE-Step 1.5 (MIT,
  under 4 GB for the base model, about 20 GB for XL) and MusicGen Stereo (CC BY-NC) with
  their sources. **Take:** ACE-Step's licence and size, which are why it is the first
  text2music engine.
- **(b) The topic, not tied to our engine.** These are from general knowledge and
  **were not re-read for this draft**, except where marked:
  - **Winamp / foobar2000 media library: watched folders plus a manual rescan.** This is
    Mike's own reference ("classic, like Winamp", 04:00). **Take:** watch, with rescan as
    the fallback.
  - **beets, MusicBrainz Picard and AcoustID/Chromaprint: identify a track by its sound,
    not by its bytes.** **Not now:** SPEC flow 22 keys a track on SHA-256 of the file and
    defers Chromaprint. Two costs follow: a tag edit changes the bytes (Q4), and the same
    song in two encodes is two entries (D5).
  - **Navidrome / Jellyfin / the Subsonic API: a local server streams bytes over HTTP with
    range requests and a token.** **Relevant to** how core serves bytes (N8). It is the
    usual shape when a webview cannot read the file itself. DPC already has a small server
    of this kind without range requests or a token (`file_server.py`, see N8).
  - **SoundCloud's timestamped comments and precomputed waveform.** The review UI copies
    the comment pattern (SPEC §1: "SoundCloud-style"). Forge proposes the waveform pattern
    too: peaks computed once (by ffmpeg) into a JSON file and drawn as a static wave
    (N13).
  - **wavesurfer.js** (the waveform, regions and spectrogram in the prototype). Licence
    **BSD-3-Clause**: `Observed` in the npm registry metadata for 7.12.12 on 2026-11-19,
    and reported the same by Ark from npm, jsDelivr and cdnjs. **Take:** it would be the
    only new UI dependency (Scope), unless N13 chooses precomputed peaks. The prototype
    decodes at `sampleRate: 22050`, because the default of 8000 cuts the spectrogram at
    4 kHz (`ai-studio/experiments/music-player/prototype/index.html:881`, untracked in
    ai-studio); a DPC component must carry the same setting.
  - **LRCLIB** (`https://lrclib.net/docs.md`, server code MIT). `Observed` by the SPEC on
    2026-11-19 with live probes: the ±2 s duration match, about 45 requests per minute
    per IP for clients that do not identify themselves, and no licence for the lyrics
    text. **Take:** the lookup step, run from core, with a local cache after the first
    fetch so a repeat works offline and the rate limit is respected (Zcode).
  - **ACE-Step 1.5 API server and YuE2-Studio** (README copies read for the SPEC on
    2026-11-19, not run). **Take:** the engines' own HTTP interfaces instead of a
    ComfyUI graph or MCP (Decided D12-D13). *2026-11-21: the engines DPC actually runs are the
    C++ ports, acestep.cpp and yue2.cpp, each with its own server (D30, D35); YuE2-Studio and
    the Python ACE-Step API server are not used.* `Reported` (Zcode): YuE2-Studio documents a
    `--headless` mode (tray, auto-stop after about 10 minutes idle).
  - **yamusic-rs** (GPL-3.0-only). It was used for visual tone only, and no code was
    taken (SPEC §13).

## Decision

**The AI music player is part of DPC. It is a floating window over one core music
service. The UI reaches that service through `local_api` commands and agents reach it
through `music_*` tools. The service reaches each engine directly, with no ComfyUI and no
MCP in between.**

### Decided (Mike's calls, 2026-11-19, AI Studio group chat)

Times are from the group chat. The sources are SPEC §12, the epic and its notes. Zcode
checked D6, D7, D11, D14, D16, D17 and D19 against his own chat windows; the rest of the
timestamps lie outside them and are worth one read by Mike before acceptance.

- **D1. The surface.** The player is part of DPC. A **"Music Player" button next to the
  AGENTS MAP button** in the Chats panel header (`Observed`: the label is at
  `ui/src/lib/components/Sidebar.svelte:992`) opens a **floating window** like the Agent
  map. **Minimising it gives a mini-player that keeps playing** while the person moves
  between chats (SPEC §11 item 2 and §12 decision 1). Precedent (`Observed`):
  `AgentMap.svelte` is a window the person can drag by its header, resize by its corner
  and minimise to its header bar. Its box and minimised flag are kept in localStorage
  through `utils/windowGeometry.ts` (header comment `:11-13`; `toggleMinimised` at
  `:282`).
- **D2. Listening first** (03:17). The player has a library, playlists and a queue.
  Listen is the default view. Generate and edit are actions on the current track.
- **D3. Three modes** (03:52-04:06): **listener**, **creative** and **musician**.
  - Listener: local folders plus SoundCloud and Yandex Music.
  - Creative: similar tracks, or a whole playlist, from references or a description.
    It runs through an agent with the `music_*` tools or in the player, and the person
    decides when and for how long the GPU is used. Overnight queues are fine.
  - Musician: all three sub-modes are offered. These are hum to an AI-sung song, the
    person's own voice plus an arrangement, and an instrument part completed into a song.
- **D4. Watched local folders with a content hash, "like Winamp"** (04:00, 04:09). The
  folders the person adds are watched, so new, removed and changed files are picked up
  without a button, and a manual rescan stays as the fallback. Every file gets a content
  hash. The hash keeps comments, lyrics, versions and playlist positions attached across
  renames and moves, and it catches exact duplicates. What it does **not** survive is an
  edit of the file's own tags: that is Q4.
- **D5. SoundCloud and Yandex Music** (04:00, 04:41). Mike has accounts on both. Tracks
  are **streamed** to listen, with a **save (download)** action. A track is
  **downloaded** when it is used as a creative reference or edited. Tracks are
  **deduplicated by content hash**, which merges **exact byte copies only**.
  *Correction from review (Zcode, 2026-11-19):* the first draft said the same song from a
  folder, SoundCloud and Yandex gives one library entry. In practice each source delivers
  its own encode, so the hashes differ and the entries stay separate. Merging "one song
  from several sources" needs an acoustic fingerprint (Chromaprint), which is deferred.
  Importing his playlists from those services is **still open** (Mike: "needs thinking").
- **D6. The lyrics chain, both sources.** File tags (ID3 USLT/SYLT, Vorbis `LYRICS`) or
  a `.lrc` sidecar come first, then **LRCLIB**, then DPC **transcribe**, then manual
  editing, which is always possible (SPEC §12 decisions 8 and 13). Nothing replaces
  existing lyrics without an explicit choice. Where an edit is saved (tags or a sidecar)
  interacts with track identity: Q4.
  *Implementation note (2026-11-20):* the automatic chain reads only the local LRCLIB
  cache; a request to lrclib.net goes out only on the explicit "Find on LRCLIB" lookup
  (review of `cb9af8a1`: no egress on a track change).
  *Built 2026-11-20 (`cb9af8a1`, `74c2de84`, `a94c7943`, `3672ee23`, `7fd6a407`); the
  Whisper run releases the model it loaded (`provider.transcribe(..., release_if_loaded_here=True)`,
  `music/commands.py`, with the idle self-unload of `b796af4a`). Where Transcribe runs is
  ADR-052's: in S3 it becomes a lease client (rule L12 chooses the GPU, the CPU twin or the
  line; Mike #62; a person's click waits without a bound), so the player's Transcribe waits
  and names the holder instead of loading beside a running engine. Not built yet (S3 is
  pending). ID3 SYLT is not read (backlog card
  A-SYNCED-ID3-LYRICS-FRAME-IS-NOT-READ-SO-A-FILE-WITH-ONLY-SYLT-LYRICS-SHOWS-NONE); the
  Transcribe quality card is open.*
- **D7. "Generate similar" means both** "same style" (a new song) **and** "cover" (the
  same melody and lyrics with a new arrangement), as two separate actions (SPEC §12
  decision 10).
- **D8. Creative references** are everything the person provides (library tracks, links,
  uploaded files, recordings, text) **plus** what the agent finds on request, over several
  rounds (04:00).
- **D9. Recording** by **microphone and file upload**, **in the player and in the chat**
  (04:06, 04:30). A recording is one file under `~/.dpc/music`, shown in both places.
  Whether "the chat" includes groups of one node plus agents is Q3.
- **D10. A clone of the person's own voice is wanted** (04:04-04:05). No engine is chosen
  yet (N5).
- **D11. One core music service with two front doors.** `local_api` commands serve the
  player UI, and a **`music_*` agent tool family**, built like `comfyui_*`, serves agents
  (Mike to CC directly; SPEC §6). `Observed`: the seven `comfyui_*` entries are
  `ToolEntry`s at `dpc_agent/tools/comfyui.py:1069-1328` (Ark: entries begin at `:1070,
  1126, 1156, 1191, 1216, 1253, 1291`), each `default_enabled=False`. Both doors call the
  same functions. The rule that **an agent accepts a take only when the person asks for
  it** is in the caller's list of decisions, but SPEC §6 and `docs/dpc-integration.html`
  still mark it **[P]**, so it stays open (Q1).
- **D12. No ComfyUI in the player's path** (05:00-05:01): "ComfyUI is an external tool we
  already integrate with, but I don't want to tie the music player to ComfyUI." The
  service reaches each engine directly, through an adapter behind one runner interface:
  - the **ACE-Step 1.5 API server**, for text2music, cover, repaint, and lego/complete
    (*2026-11-21: the engine actually chosen is acestep.cpp, run and supervised by DPC; D30*);
  - **YuE2-Studio** over its HTTP API (`/synth`, `/job`, `/health` on yue-server) or its
    CLI (*2026-11-21: the engine actually reached is yue2.cpp's own `yue-server`, run and
    supervised by DPC, the default engine; D35. YuE2-Studio is not used and no MCP is
    needed*);
  - **Music Flamingo** through DPC's own `llama-server` (ADR-040).

  ComfyUI stays an external tool that DPC integrates with separately, through the
  `comfyui_*` tools.
- **D13. No MCP** (04:48): "agents have tools and CLI via shell; for the music player, see
  what is simpler." DPC gets no MCP client. `Observed`: there is no `mcp` dependency in
  `dpc-client/core/pyproject.toml`, and no agent tool has a name starting with `mcp_`.
- **D14. Weights** live in a **`music/<engine>/` folder inside the model files folder**
  (04:41, 05:08), that is **`get_model_files_dir() / "music" / <engine>`**. It is the
  single physical store, next to the other models. *Correction from review (Forge, Ark,
  2026-11-19):* the first draft wrote the literal `~/.dpc/models/music/<engine>/`, which
  is only the default. `Observed`: the root is `[local_engines] model_files_dir`
  (`settings.py:145`; empty means `<DPC home>/models`). `get_model_files_dir()`
  (`:838-856`) substitutes the default and ignores a non-absolute value with a warning.
  The person changes it from the Providers editor ("Model files folder") through
  `set_model_files_dir` (`local_api.py:137`, `service.py:11198`, `settings.py:858`). On
  this node the key is absent from `~/.dpc/config.ini`, so the default applies. Adapters
  must resolve the folder through the getter, never the literal. Apps point at it through
  their own path setting or a Windows junction; which of the two is a proposal.
  A consequence (the folder also shows in the provider model list) is under Consequences
  and N15.
- **D15. No DAW in the MVP.** The later stage is **stem lanes** in the area where the
  waveform and spectrogram are now (SPEC §12 decision 15; §1 "Later stages").
- **D16. Web tracks have no rights gate.** Every generate flow works on a web track.
  Rights are the user's responsibility. What stays off is technical: a web file cannot be
  edited in place (SPEC §7 "Rights").
- **D17. It is a research project, so non-commercial weights are acceptable**: YuE2
  (CC BY-NC), SheetSage2 (CC BY-NC-4.0) and the NVIDIA Flamingo family. They block
  anything published or sold.
- **D18. Strata is switched through the existing swap.** For processes DPC did not start,
  that is the mechanism the GPU card adds (the epic's note of 2026-11-19 on
  EXTERNAL-GPU-JOBS-CANNOT-JOIN-THE-CARD-QUEUE-SO-AN-IDLE-ENGINE-KEEPS-THE-CARD-THEY-NEED).
  What the registry needs before this can be checked is in N11.
  *Status 2026-11-22.* **Built:** DPC starts the two music engines itself (D30, D35), the
  registry knows them (`acestep_server`, `yue2_server` in `SUBSYSTEMS` and `ENGINES`, so each
  has a VRAM row), only one music engine is on the card at a time, and a job whose card is
  held by Strata or `llama-server` waits in `queued` with `card_wait` naming the holders, and
  the job card offers **"Stop X"**, which calls the existing `stop_local_engine` by the
  person's press; nothing is stopped automatically (SPEC decision 32, D37). **Not built, and
  now ADR-052's:** the music engines are not card holders, so `llama-server` and Strata do
  not see them (they do not wait for a running song, which Mike asked for at #20), and a
  music job does not yet swap an idle server out. ADR-052 S4 makes the music engines lease
  clients (acquired before the engine's `_the_lock`), S2 makes the supervisors wait for a
  busy lease and unload an idle one (built at `1e57932f`); Mike #70 amends SPEC decision 32: with the queue empty
  and the server idle, music unloads the server and takes the card by the owner-start rule,
  gated by `swap_when_idle` (D37). Two smaller items of the same plan touch this record's
  code: `stop_local_engine` on a lease alias (S2 item 8) is what lets the "Stop X" button stop
  an idle lease or hear what a busy one is doing, and the `mp.job/1` `card_wait.holders`
  schema (exactly `{alias, engine}` per holder, `music/job_schemas.py:479-488`, kept by
  `set_waiting_for_card`) cannot tell a lease apart, so widening it and the "Stop X" label is
  an S4 item. The job's `card_wait` shows only while the job is `queued`; S4 widens it to
  `running`.
- **D19. No new `audio` attachment type** (05:33, decided by Mike's objection). Mike asked
  why a new type was needed when `file` already exists. The existing `file` attachment
  carries `mime_type` (`types.ts:17` union, `mime_type?` at `:23`), so **a `file` whose
  `mime_type` is `audio/*` renders as a player** (a VoicePlayer-style card plus "Open in
  player"). Audio files already in chats will play too, once a duration source exists
  (N12). The B1 card (AN-AGENT-CANNOT-POST-A-FILE-INTO-ITS-OWN-CHAT-AND-THE-CHAT-HAS-NO-AUDIO-ATTACHMENT-TYPE,
  `backlog.md:388`) records the change in a dated note of 2026-11-19. **Card names are
  deliberately not renamed** (Ark): B1's title still says "audio attachment type", and it
  is cited by `[[…]]` links at `backlog.md:251, 264, 351, 380, 404, 407` (six, `Observed`;
  Ark counted five). A rename would edit other entries' links and could break the graph,
  so the correction lives in dated notes in each body. Phase 0 in
  `docs/dpc-integration.html` will be corrected to this decision.
  *Built 2026-11-20/21: an audio `file` plays in the chat (`ChatMessageList.svelte`, the
  `knownDurationSeconds` of the attachment, `dd446110`), core reads the duration with ffprobe
  (N12), and the agent's history names every non-image attachment
  (`file_attachment_markers`, `dpc_agent/context.py`, `3837b3a5`). The union is unchanged. B1's own part,
  an agent posting a file into its chat, is not built and no longer a Phase 0 need (Q1).*

### Decided (Mike's calls, 2026-11-20, AI Studio group chat, on the external review v2 and the review of its written decisions)

Times are UTC from the group chat, where the group discussed Golova's critical review v2
of the spec and the integration pages. Only what Mike said is listed here; the group's
consensus proposals from the same discussion are in SPEC (marked "[P] (review v2,
consensus 2026-11-20)") and are not decisions.

- **D20. Region edits keep the source length** (05:03). For `remove_region` and the
  region replace (`regenerate_region`), the received take's length must equal the
  source's; otherwise the lyric timings break (SPEC §13). Proposed by Ark, backed by CC;
  Mike: "I am for this rule too". So `remove_region` removes the sound, not the time.
  `extend_ending` stays as it is (SPEC §4, §6, §12 decision 23).
- **D21. A generated take is registered once** (05:04). The review's double registration
  ("join Generated" versus `open_music_take_as_track`) is resolved when takes move into
  core: one entry into the library, one event producer, a test against a repeat. Until
  then the docs keep the note as the rule to implement (SPEC §3 flows 10 and 21, §12
  decision 24).
- **D22. Checked live, no separate stem spike** (05:04). Instead of a separate one-phrase
  stem spike, everything is checked live in the already-working player as features are
  added (SPEC §12 decision 25).
- **D23. QC ranking is off by default** (05:05) until the QC judge is chosen (SPEC §12
  decision 12, listed below under "Also open from SPEC §12"). Today nothing can execute
  the rubric (SPEC §8.1, §12 decision 26). A consequence noted by CC, not said by Mike:
  the agent's "ranks the takes by QC" in Q1's flow waits for the same switch.
- **D24. `remove_region` near the end of the track may cut with shift** (05:53). The
  question was the silent tail that decision D20 leaves when a removal lies near the end
  (SPEC §12 decision 27). Mike chose option (b): as an explicit exception, on the
  person's word, `remove_region` near the end of the track may cut with shift and re-time
  the lyrics; the default stays D20 (keep the length). Mike added: "the tail can really be
  quiet for 1-5 seconds", so a natural quiet tail of up to about 5 s is normal and the
  listen-to-the-tail gate does not flag it by itself; it flags a cut-off last phrase or a
  longer tail (SPEC §8). How the exception is asked for and how the lyrics are re-timed
  is not specified yet.
- **D25. The pair report and the external-player rule are adopted** (05:53). Forge's
  table (requested vs received length, seam window, loudness of control vs edit, planned
  vs sung words) and the rule "on a live complaint about sound, first play the same file
  in an external player and say where it reproduced" are accepted (SPEC §8). The ASR
  caveat stays: the planned-vs-sung row has false hits on instrumental passages, where ASR
  invents text, and does not measure stress.
- **D26. "Checked live" also replaces the repaint spike** (05:53). Mike answered "yes, I
  think we will check it" to whether D22 also drops the one-evening repaint spike of N2.
  The repaint engine is checked live in the working player as B4 lands (SPEC §12
  decisions 2 and 28). Consequence raised by Ark, not a decision: the known-good /
  known-bad control pair for the seam metrics was parked in that spike and now has no
  owner. **Not decided, a condition (Forge proposed, CC backed; Mike did not rule on it):**
  if stem separation turns out to damage the vocal during the live checks, the repair
  path does not become a second edit option; it returns to Mike's decision.
- **D27. Seam metrics are advisory pointers, never gates** (08:07). The question was
  whether the `> 1.5 dB` seam check stays a gate for any edit (SPEC §12 decision 29).
  Mike chose **advisory**: the seam metrics (`seam_start_jump_db`, `seam_end_jump_db`,
  `seam_click_score`) are pointers ("where to listen") everywhere and never gate or reject
  a take. The deterministic gates, as worded in the option Mike picked (CC and Forge; true peak added from the pair report), are only the unarguable ones: length (requested /
  engine window / received), clipping, true peak, silence outside the edit zone, and
  silence instead of the edit (SPEC §8). Measurements behind it (Ark, Forge, not
  decisions): the absolute jump fires on untouched audio and cannot separate a known-bad
  splice from a good short crossfade.

- **D28. N8 is resolved: a local server with `Range`, a token and an `Origin` allowlist,
  keyed by `track_id`; implementation deferred** (09:01). Mike took the reviewers' option
  (i) of N8 ("ok, let it be so, but we'll do it later; for now everything added to
  dpc/music plays and that is enough for the checks"). So the byte-serving endpoint is
  decided and **not built**: until it is, the window plays only files under `~/.dpc`, and
  **the checks use files inside `~/.dpc/music`** (a file in a watched folder elsewhere
  shows "not playable yet"). Options (ii) `blob:` and (iii) copy-in are not chosen. The
  CSP `media-src` change and the first-step experiment of N8 belong to the implementation
  (SPEC §12 decision 30).
  *Implementation note (2026-11-20): built on `dev` in `9bb7599d`, on `origin/dev` (checked
  2026-11-22).* Asked for after the Linux node measured the `blob:` path as a live stream
  (`loadedmetadata duration=0.00`, `seekable` only what had played). The Linux window then
  needed four follow-ups (`89123e1d`, `05549c35`, `012d8c0b`, `36a4777a`: no-Range
  whole-file stream, the 9998 headers, preload, wait for a duration above zero before play);
  see the N8 entry for the shape.

- **D29. Song creation is tracked as evolution; the process shows a lineage graph** (14:34,
  14:52). Mike's direction: creating a song is like evolution; each take records what
  changed from the version before it and why versions were rejected, and the process shows
  the branches as a graph. On the fitness side (14:46): "fitness is my ear, yes, but there
  will also be models like Flamingo (and probably others) + tools for the agent, the
  spectrogram, other things", so those signals are recorded as advisory with their source
  and none selects (SPEC §12 decision 29); a person makes every selection. Mike asked for
  it to be added to the ADR and the other docs (14:52). **The specifics are proposals
  (CC with the group's review), not decisions:** the genotype / substrate / phenotype
  record, edge kinds, failure classes, selection events, the measurement rules, the
  generated `lineage.json` / `lineage.html`, and the lineage fields on
  `register_generated` (D21); see `experiments/music-player/docs/EVOLUTION.md` and
  `docs/PROVENANCE.md` in ai-studio, and SPEC §12 decision 36. **Open for Mike:** blind
  A/B from slice 1; a mandatory reason + failure class on Discard (a `rejected` status
  instead of removing the entry, which would change SPEC decision 35); the style battery;
  graph per song; agent notes; the true-peak export policy; the `N_delta` warning.
  *Update 2026-11-21: Mike answered two of these in #159, see D32 (a reason on Discard is
  mandatory) and D33 (blind A/B comes later). The `rejected`-status part, the style battery,
  graph per song, agent notes, the true-peak policy and the `N_delta` warning remain open.*
  *Update 2026-11-22 (EVOLUTION v3): **true peak is decided**, Mike, AI Studio group session B
  #15, 2026-11-21 09:29Z: a limiter on the lossy export only (D36). **The genome definition is
  decided** by the group 4:1 on 2026-11-21 (the engine's inheritable unit; technique, not
  taste) and built at `abcaaa44`. **The public build recipe is answered**, Mike, session B
  #17, 09:31Z. Still open: the style battery, graph per song, agent notes, the `N_delta`
  warning, and hiding a rejected take's library entry (D32).*
  *Built as of 2026-11-22 (on `origin/dev`):* the provenance record `mp.provenance/1` inside
  every generated file, with the narrow ids (`06c8b20f`, `ac25991d`, `abcaaa44`; see the
  status table), the take-level `rejected` record with its reason (D32), the level-matched
  plain A/B (D33) and timecoded comments. **Not built:** `lineage.json` / `lineage.html`
  (nothing draws a graph; `register_generated` takes a `lineage` dict and stores it), the
  `parents` edges and `N_delta`, selection events beyond Keep / Discard, and any reader of
  `genome_sha256` (EVOLUTION v3 §14, items 10 and 14). EVOLUTION v3 §14 also holds
  questions that were not put to Mike (items 10-15), among them the genome close-out and
  whether a timecoded comment is a `note` selection event.*

- **D30. DPC supervises the acestep.cpp server** (2026-11-21, #159 (a), 19:33; SPEC §12
  decision 31). Asked by CC: "does DPC itself manage the acestep.cpp server?" Mike: "да, мы
  же обсуждали уже это" (yes, we already discussed this). The engine is acestep.cpp (Mike's
  choice 2026-11-20, built from source, recipe `experiments/music-player/engine/acestep-cpp/BUILD.md`
  in ai-studio). DPC starts, stops and watches the server, takes models from
  `~/.dpc/models/music/ace-step/` (D14) and binds it to loopback. This replaces the earlier
  "Mike starts ACE-Step himself, DPC only connects" (2026-11-20 09:56). The settings keys,
  port and restart policy are CC's proposal, not Mike's. Backlog:
  DPC-DOES-NOT-START-OR-WATCH-THE-ACESTEP-CPP-SERVER-SO-NO-MUSIC-JOB-CAN-REACH-AN-ENGINE.
  *Built 2026-11-21 (`6b34b44c`, on `origin/dev`):* `music/acestep.py` over the shared
  supervisor `music/engine_base.py`; the `[music] acestep_*` keys (binary, models dir, port
  9995, `acestep_idle_stop_s` 300, device) and the commands `get_music_engine_status`,
  `start_music_engine`, `stop_music_engine`. The process goes through the registry as the
  subsystem `acestep_server` (ADR-046 point 5). Windows only so far: Linux and macOS have no
  build recipe and report `not_installed`. In ADR-052 the engine becomes a lease client in
  S4 (holder `music:acestep`).

- **D31. Slice 1 engine settings and output format** (SPEC §12 decision 34; defaults accepted
  by Mike 2026-11-20 09:56, engine line rewritten 2026-11-21). acestep.cpp turbo, Q8, 8 steps.
  The engine writes **wav24** (it has no FLAC) and DPC converts it to FLAC for the library.
  **The wav24 + FLAC plan is the group's consensus after Mike chose acestep.cpp, not a
  recorded word of Mike's.** DPC records `synth_batch_size` and the batch index per take: a
  measurement of 2026-11-21 showed the batch size changes the audio for the same seed
  (PROVENANCE section 2). It replaces the earlier "ACE-Step API, `audio_format: flac`".
  *Built (`6b34b44c`): one `/synth` per job with `synth_batch_size` = the variants, wav24
  converted to 24-bit FLAC, a sidecar, the library entry; Mike accepted the live run (#191).*

- **D32. Discard requires a reason** (2026-11-21, #159 (b); SPEC §12 decision 35). Asked by
  CC: "is a reason mandatory on Discard?" Mike: "да, как написать текстом так и выбрать из
  списка" (yes, both writing it as text and picking it from a list). So Discard, and also
  None of these, needs a reason: free text, a pick from a list, or both. **The list** (Mike,
  #169, 2026-11-21): "ну я думаю его можно будет формировать как словарь по мере моего
  ручного заполнения" (it can be built as a dictionary as I fill it in by hand). So the list
  is a dictionary that grows from the reasons the person types; it starts empty and is not
  written in advance. CC's proposal [P]: it lives in the job store, is deduplicated
  case-insensitively and ordered by use. An optional failure class (EVOLUTION section 6) can
  be attached to an entry; a class is never required to save a reason. Whether Discard keeps a `rejected` record
  instead of removing the library entry is **not** decided (EVOLUTION section 14).
  *Update 2026-11-22: built (`9ccd950a`, `2208a803`, on `origin/dev`): the take-level
  `rejected` record is in the job store (`music/jobs.py` `discard_take`: the take becomes
  `rejected` with its reason and the record stays; `reject_all_takes` for "None of these";
  `list_music_discard_reasons` / `set_music_discard_reason_class` for the dictionary), and
  the library entry of a discarded take is **hidden**, not removed (`_music_hide_for_discard`
  in `music/commands.py`; the file stays in `generated/<job>/`). Whether the entry should
  instead show as `rejected` is still open (EVOLUTION v3 §14, open item 1).*

- **D33. Blind A/B is not in slice 1** (2026-11-21, #159 (c); SPEC §12 decision 37). CC
  proposed "later; slice 1 gets a plain A/B with level matching". Mike: "ок позже" (ok,
  later). Slice 1 has the plain, level-matched A/B; blind A/B comes in a later slice.
  *Built (`2208a803`, `0063d4be`, `c146b811`): the review plays every take at the quietest
  take's integrated loudness (ffmpeg `ebur128`, `music/loudness.py`, display gain only, the
  files are never changed), A/B/C/D keys keep the position, and a waveform shows in the
  review. It is one audio element, so a switch has a short gap; SPEC decision 7 (two elements
  or decoded buffers) stays open. There is no seam A/B yet: region edits are not built.*

- **D34. The library needs hide and delete** (2026-11-21, #159; SPEC §12 decision 38). **The
  requirement is Mike's:** raw engine outputs duplicated tracks in the library; he wrote
  "это плохо что есть такое, так же нужна возможность как то удалять или скрывать потом
  файлы сами (уже в UI я вижу очень много файлов лишних)" (it is bad that this exists; a way
  to delete or hide files myself is also needed; the UI already shows many extra files).
  **The specifics are CC's proposal, not decisions:** Hide is reversible and leaves the file
  untouched; Remove from library applies only to entries whose file is missing; Delete file
  applies only to files under `~/.dpc/music/generated/`; the person's own folders are never
  deleted from, only hidden. Backlog card
  THE-MUSIC-LIBRARY-HAS-NO-WAY-TO-HIDE-OR-DELETE-A-TRACK-SO-RAW-ENGINE-OUTPUTS-AND-DUPLICATES-CLUTTER-IT.
  *Built 2026-11-21 (`8e134ec6`, `76a081c4`, `8d716430`, on `origin/dev`): the commands
  `hide_music_track`, `unhide_music_track`, `remove_missing_music_tracks`,
  `delete_generated_music_file`; ownership is decided by the path under
  `~/.dpc/music/generated/`, not by the folder id.*

- **D35. YuE2 via yue2.cpp is the default music engine** (2026-11-21, #198; SPEC §12 decision
  40). **The decision is Mike's:** after the first listening pairs ("заметно лучше чем acestep",
  noticeably better than acestep) he wrote "Делать YuE2 — вообще сделать его движком по
  умолчанию" (do YuE2; in fact make it the default engine). **The specifics are CC's proposal
  [P], not decisions:** acestep.cpp stays as the second engine, picked per job
  (`params.engine`); one music engine on the card at a time (an idle one is stopped, a busy one
  waited for); batch 1 per variant until the ggml CUDA flash-attention mask fix, each variant
  with seed and lm_seed `seed + i`; the duration is a cap (default 180 s, 30-360) with a
  short-cap warning; backbone `stock` or `rs-tw`; voice adapters for local listening only, none
  by default, split by DPC into music and sound halves (music 0.45 / sound 0.35).
  **Backbone default resolved (Mike, 2026-11-21, CC window):** "ну сток по умолчанию это
  правильно" (stock by default is right): `stock` is the default, RS-TW is picked per job in
  the Create form.
  **Voice adapters in renders: Mike said yes** (#198 (в), 2026-11-21, "да", adding "not
  everything is listened to yet"); that settles that they may be used in renders. Keeping
  them to local listening, none by default and nothing published with them, is CC's proposal
  after the consent question of #195 (each adapter was trained on one real reader's voice,
  the readers' consent is not stated in the README) and is not Mike's word.
  *Known limit (SPEC decision 40): an instrumental request to YuE2 comes back sung. Mike,
  session B #6: LoRAs are added as needed, with ideas from Ruach.* Backlog:
  AN-INSTRUMENTAL-JOB-ON-YUE2-COMES-BACK-SUNG-BECAUSE-THE-MODEL-INVENTS-LYRICS-WHEN-NONE-ARE-GIVEN
  (open; what exactly to build stays undecided). Own adapters: DPC-CANNOT-TRAIN-ITS-OWN-YUE2-ADAPTERS-SO-VOICES-STYLES-AND-INSTRUMENTS-COME-ONLY-FROM-OTHERS
  (Mike, 2026-11-21: "later, our own voices, styles and instruments the way Ruach Studio
  does"; open, LOW).
  *Built (`ade158b7`, on `origin/dev`): `music/yue2.py` supervises `yue-server` (`[music]
  yue2_*` keys, port 9994, `--max-batch 1`), `music/engines.py` keeps one music engine on the
  card, `params.engine` defaults to `yue2`, the Create form has engine, backbone and voice
  selects and the short-cap warning. The process is the subsystem `yue2_server`. In ADR-052
  it is the holder `music:yue2` (S4). Linux and macOS have no build recipe.*

- **D36. MP3 export without duplicates or folder chaos** (2026-11-22, #210; SPEC §12 decision
  41). **The ask is Mike's:** "думаю надо сделать, не хватает экспорта или сохранения в mp3 —
  подумай как избежать дублирования и не превратить папку и подпапки в хаос организационный"
  (it should be done; MP3 export is missing; avoid duplicates and do not turn the folder and its
  subfolders into chaos). **The design is CC's proposal [P], not a decision**
  (`core/dpc_client_core/music/export.py`): one export root outside every watched folder
  (`<DPC home>/exports/music`, or `[music] export_dir`); single tracks flat as
  `<Artist> - <Title>.mp3`, playlists one level as `<Playlist>/NN - <Artist> - <Title>.mp3`;
  an index `.dpc-exports.json` keyed by (source content hash, format, preset), so a repeat
  export answers the existing file and a playlist links tracks already exported instead of
  encoding them again; the library scan skips indexed files and any file tagged
  `TXXX:DPC_EXPORT_OF`; presets `mp3-v0` (default) and `mp3-320`; a -1 dBTP limiter on the
  export only, verified on the decoded MP3 (the policy is decided: Mike, session B #15, 2026-11-21 09:29Z, limiter on lossy export only);
  provenance in ID3v2.4 per PROVENANCE §3, with `cmd_as_sent` reduced to file names (rule 3).
  *Built `98001dc7`, on `origin/dev`: the commands `export_music_track`,
  `export_music_playlist`, `get_music_export_info`; an "Export MP3" action and an "mp3" chip
  in the window. Tests and real ffmpeg runs on Windows only; not yet tried in the app, and
  Linux and macOS not run. A tree of exported copies under the track is a LOW card
  (A-TRACKS-EXPORTED-COPIES-SHOW-ONLY-AS-A-CHIP-AND-A-ONE-TIME-NOTICE-NOT-AS-A-TREE-OF-VERSIONS).*
  *Update 2026-11-25, **decided by Mike**, AI Studio group #171: the lossy export keeps the -1 dBTP
  limit by default, with a switch in the export controls to turn it off for one export
  (`limit_true_peak`; the unlimited file is a separate export, named `... (no limit).<ext>` and
  tagged `DPC_EXPORT_TP_LIMIT=off`), and an Opus 160 kbps preset is added for sending to messengers.
  Presets: `mp3-v0`, `mp3-320`, `opus-160`. CC's choice [P] inside that: `opus-160` is libopus VBR in an
  Ogg container with the `.opus` extension, 48 kHz, tagged with Vorbis comments carrying the same
  provenance, export marker and lyrics as the MP3 (`LYRICS` for `USLT`); the tag packet is written by
  hand, as the ID3 tag is. A single track's copies list names the playlist-folder files of the same song
  and Delete says they stay. Tests with a real ffmpeg (libopus) ran on Windows only; Linux and macOS not
  run, and Opus playback in the macOS webview is not tried (the export is for other players).*

- **D37. The GPU card in slice 1: the job waits, the person presses "Stop X"; amended by Mike
  #70** (SPEC §12 decision 32; Mike's call 2026-11-20 09:56, defaults as proposed by CC;
  amendment: Mike #70, 2026-11-21, ADR-052 "Decided by Mike", decision 4). Before a job
  starts, the runner reads who holds the card (`card_holders()`); if a DPC engine (Strata or
  `llama-server`) holds it, the job stays `queued` with `card_wait` ("waiting for the card: X
  holds it") and the job card offers "Stop X" (the person's action, the existing
  `stop_local_engine`). Nothing is stopped automatically. **Built** (the wait in `6b34b44c`,
  `music/runner.py` `_wait_for_card`; the button in `2208a803`; the runner reads the card once per job, not before each
  variant as the SPEC text says, and it treats a failed read as a free card, which ADR-052's
  Ruach rule forbids and S4 changes). **Amendment (#70):** with nothing waiting and the
  server idle, music unloads the server and takes the card, by the owner-start rule and gated
  by `swap_when_idle`. **Half built:** the lease side is ADR-052 S2 (`1e57932f`, a lease with
  `waits_for_engines` swaps an idle supervisor out; follow-up in the commit after `373d7b7e`), and
  the music engines become lease clients only in S4, so no job reaches it yet. It acts only with
  `[local_engines] swap_when_idle = true`; the default is `false` (`settings.py`), and with it off
  the job waits and the person presses "Stop X". SPEC decision 32 carries the amendment since 2026-11-22
  (Q9).

- **D38. Song from the person's own text** (SPEC §12 decision 33; Mike's call 2026-11-20
  09:56, defaults as proposed by CC). A job type `generate_from_text` with `track_id` and
  `base_version_id` null and no source fields; the lineage relation is "song from own text"
  with `source_track_id` null (`mp.job/1` gains `params_from_text`). **Built** with the first
  slice (`f97a454e`, `6b34b44c`, `ade158b7`, `2208a803`): the Create panel proposes it, the
  runner returns takes, and each take enters the library once (D21, `a4f2c785`). Every other
  job type (`edit`, `generate_similar`, `generate_from_lyrics`, `cover`, `transcribe`, `qc`,
  `generate_from_melody`, `voice_clone`) is reported `planned` or `unsupported` by
  `get_music_capabilities`.

- **D39. A Russian stress dictionary the person downloads** (SPEC §12 decision 39; Mike #182,
  2026-11-20 20:31Z, 2026-11-21 by the SPEC's Bangkok date). CC asked "Zaliznyak, which the
  user downloads (CC BY-NC), or a free dictionary?" Mike: "I think take it, and others can be
  added later? (are the stress dictionaries pluggable?)". So the large source is **Zaliznyak's
  paradigm, downloaded by the person, never bundled in the repository** (CC BY-NC), like
  the engine weights. That dictionaries are pluggable, as an ordered list of sources, is CC's
  answer [P], not Mike's word. **Not built:** nothing in `music/` prepares lyrics (no
  syllable-meter check, no U+0301 audit, no stress resolver); the only trace is the count of
  stress marks in the provenance record. The dictionary file sits in
  `~/.dpc/models/lyrics/stress/ru/forms.sqlite` and nothing reads it. Mike put it after the
  Whisper card (2026-11-21, CC window). Backlog:
  A-VOCAL-JOB-SENDS-THE-AUTHORS-LYRICS-TO-THE-ENGINE-UNCHECKED-SO-SYLLABLE-METER-AND-RUSSIAN-STRESS-ARE-LEFT-TO-CHANCE.

  *Numbering: ADR D30-D39 mirror SPEC §12 decisions 31, 34, 35, 37, 38, 40, 41, 32, 33, 39 in
  that order; every SPEC decision from 30 to 41 now has an entry here (SPEC 36 is D29).*

### Decided (Mike's calls, 2026-11-24, AI Studio group chat, the agent tools)

**Built, not observed live** (each decision names its commits: parts 0-3 at `8c73b1ba`,
`8c5b51c4`, `e3b0ea6c`, `29383864`, and the follow-up at `64b793ac`). When the section was
written, orbit and grep found no `music_*` tool and no `chat_post_file` at `a8a46197`. Sources: CC's plan #44 (12:31Z), Mike's
answer "yes to everything, by the recommendations" relayed in #46 (13:17Z; questions A-I),
Zcode's review #51 (14:01Z; seven corrections, all accepted), and Mike's later calls in
#53-#57, which settled D (revised), I, J, K and L, one message each: #53 is D (a take by
`take_id`, D44), #54 is I (`check_back` through `schedule_task`, D49), #55 is J (the approval,
D47), #56 is K (Stop cancels the queued jobs, D48), #57 is L (bridges count as the owner,
D43). His earlier "yes to everything, by the recommendations" for A-I is group #46, as CC
recorded it. The decisions below that cite "#53-#57" as a range rest on these single
messages. Where a
cap value is not chosen yet it says "value set at build, recorded here": no figure below is
invented. The letters A-L are the plan's questions; each is placed under one decision id.

| Letter | Subject | Decision |
|---|---|---|
| A | turn classes `other_node` and `stranger` refused | D43 |
| B, L | owner sources `ui`, `external_agent`, `telegram_owner` | D43 |
| C | a take goes into the chat only when the person asks | D50 |
| D (revised) | a take by `take_id`, no directory access | D44 |
| E | an agent cancels only its own jobs | D46 |
| F | no LRCLIB for agents | D50 |
| G | posting into groups later | D50 |
| H | names, and a GLOSSARY row per name | D42 |
| I | `check_back` through `schedule_task` | D49 |
| J | `music_propose_job` asks the owner's approval | D47 |
| K | Stop cancels the turn's queued jobs | D48 |

- **D40. Q1's "`chat_post_file` is not needed" is reversed** (Mike #46, on CC's plan #44
  section 3; Zcode #51 asked for the cancellation to be written down). Q1's call of
  2026-11-19 07:59 said results go to a folder and `chat_post_file` is not needed by the music
  flows. The plan brings it back for music: the agent posts a take into a 1:1 agent chat as an
  `audio/*` file attachment (D19: no new attachment type), but **only when the person asks**
  (D50). The call of 07:32 that names the tool `chat_post_file` stands (Q5). B1 (an agent
  posting a file into its chat) is again a need of the music flows. `_record_answer` must carry
  attachments and write an answer that has no text (Zcode #51 correction 4): today an answer
  that is only a file is lost (`agent_manager.py`, `_record_answer`). **Built at `e3b0ea6c`
  (part 3: `_record_answer` takes attachments and writes an answer with no text), not observed
  live.**
- **D41. `chat_post_file(path, caption?)` and its limits** (Mike #46; Zcode #51 corrections 4
  and 5). The limits:
  - **At most 4 files per turn.** The plan marks the figure [P]; it stands as accepted by #46.
  - **The file-transfer size cap**, `file_transfer_cap_mb(rules)` (100 MB by default), checked
    before the copy. The file is copied, never moved.
  - **1:1 agent chats only** (`agent_*`). Groups, Telegram and Discord turns are refused with
    a reason (posting into groups: D50). *Since D50 G was built, a group turn posts into that
    group; Telegram and Discord are still refused.*
  - **The path is resolved like `read_file`**, through `_resolve_file_path`
    (`tools/core.py`, around line 247), never through a bare `validate_extended_path`: the
    first checks the "Extended path read" switch and the audience gate of the turn, the second
    skips both.
  - It runs on the owner's turns and on a call the owner approved; since ADR-053 a request of
    someone else shows the owner's card instead of being refused (see D43's amendment).
  - **A music take is posted by `take_id` instead of `path`** (added at `64b793ac`, for the
    interface's "Open in player"): `chat_post_file(take_id, caption?)`. Core resolves the id
    through `get_music_take` into the take's own file (D44), so neither `_resolve_file_path`
    nor the DPC-home refusal applies to a path the agent never named; every other limit above
    does. The attachment then carries `music: {track_id, take_id, job_id}`, where `track_id` is
    the music library entry id the player opens (the take's `library_track_id`, not its source
    `track_id`; null while the take is not in the library). `path` and `take_id` are given one
    at a time; both, or neither, is refused. An unknown `take_id` is refused (`not_found`) and
    nothing is copied.
  - **Follow-up 12 (Zcode #68 P2, P3):** a take is posted only when its job is done (`review`
    or `accepted`), the take is not rejected and its library entry, when it has one, is not
    hidden (`get_music_take` now says `library_hidden`); otherwise it is refused with the
    state. A file that runs or opens something when clicked is never posted, by one constant
    (`REFUSED_EXTENSIONS`: .exe .bat .cmd .com .lnk .ps1 .vbs .scr .msi .sh .app .dmg; replaced in
    follow-up 13, below). A DPC
    home that cannot be resolved refuses (fail closed). The files a turn copied anew are removed
    again when its answer record is not written (the loop raised, or the record failed); a file
    whose bytes were already in the chat is never removed. **Built at `e2cef2da`, not observed live.**
  - **Follow-up 13 (Zcode's review of follow-up 12, P3):** the deny-list became an allow-list,
    checked by the file's first bytes as well as its suffix: audio (flac, wav, mp3, ogg, m4a),
    image (png, jpg, webp, gif), pdf, and plain text (txt, md, json, csv); anything else is
    refused with the list in the message, and so is a program renamed to an allowed name, or a
    file whose content is another allowed group than its name says. The copy is read again after
    it is made, since the source may change after the check. A copy that was cancelled or failed
    part way (the hashing and the ffprobe measure come after the bytes are copied) leaves no half
    file in the chat's `files/`. Not observed live.

  **Built at `e3b0ea6c` (part 3), `take_id` at `64b793ac`; not observed live.**
- **D42. The `music_*` family: ten names, a timeout and a wait budget with each** (letter H,
  Mike #46; Zcode #51 "names: no objection"; Q5 asks for the timeout and the wait budget to be
  declared with the name). The names are `music_capabilities`, `music_propose_job`,
  `music_check`, `music_wait`, `music_cancel_job`, `music_list_takes`, `music_list_library`,
  `music_get_lyrics`, `music_playlist_add` and `chat_post_file`. `music_` stays reserved for
  domain actions; `music_check` and `music_wait` mirror `comfyui_check` and `comfyui_wait`.
  Every tool is off by default (`default_enabled=False`, as the ComfyUI tools). Each name gets
  a GLOSSARY row sourced from this record.

  | Tool | What it does | Timeout and wait budget |
  |---|---|---|
  | `music_capabilities` | wraps `get_music_capabilities` | 30 s; no wait budget |
  | `music_propose_job` | wraps `propose_music_job`; one job per call (D45); asks approval (D47) | 100 s: the schedule approval's 60 s plus the calls; no wait budget |
  | `music_check` | wraps `get_music_job`: state and the `card_wait` holders | 30 s; no wait budget |
  | `music_wait` | polls `get_music_job`; checks Stop between polls; answers at once by D51 | poll every 2 s, `timeout` 600 s by default and 900 s at most, `timeout_sec` 915 s (the longest wait plus 15 s; the per-call default of 120 s, `registry.py`, would cut it). A turn has 900 s of `music_wait` in all; once spent, `music_wait` is refused for the rest of the turn |
  | `music_cancel_job` | wraps `cancel_music_job`; own jobs only (D46) | 30 s; no wait budget |
  | `music_list_takes` | wraps `list_music_takes`; a take by `take_id`, with duration and loudness (D44) | 30 s; no wait budget |
  | `music_list_library`, `music_get_lyrics` | read-only | 30 s each; no wait budget |
  | `music_playlist_add` | wraps `update_music_playlist` | 30 s; no wait budget |
  | `chat_post_file` | D41 | 120 s (one copy and two hashes of at most the size cap); no wait budget |

  Each service call a tool makes has 20 s (`SERVICE_CALL_TIMEOUT_S`). The `check_back` delay a
  tool suggests is the job's ETA plus 25 s (Strata's cold start after it), or 180 s when the
  ETA is not known (acestep.cpp), plus the same 25 s (D49).

  Agents get no Keep, Discard or reject (Q1), no engine start or stop (the runner starts the
  engines), no watched folders, no delete, no register_track and no export (out of scope of
  the first cut). **Built at `29383864` (part 2, `dpc_agent/tools/music.py`) and `e3b0ea6c`
  (`chat_post_file`), with the timeouts above; not observed live.**
- **D43. Only the owner's turns may use the family; the rule is declarative and written once**
  (*amended by ADR-053, see the last paragraph of this decision: a request of someone else is no
  longer refused, it asks the owner's card; `owner_only` is carried by no tool now*)
  (letters A, B and L: Mike #46 and #53-#57; Zcode #51 correction 6 and its note on A).
  - **Refused:** turn classes `other_node` and `stranger`: a peer, or a group that has a
    foreign node in it. A group of only the person's own nodes is owner class
    (`dpc_agent/agent.py`, around lines 92-113). There is no per-tool switch.
  - **Owner sources:** `ui`, `external_agent` and `telegram_owner`. The bridges count as the
    owner (the rule of 2026-11-07, "Bridges count as the owner"), so a message from the CC or
    Zcode bridge opens `music_*` and `chat_post_file` for the agent.
  - **Where it is checked.** An `owner_only` field on the tool's `ToolEntry`, checked in
    `registry.execute` and when the offered tool list is built (`_get_allowed_tools` reads no
    turn class today). Each tool checks the class again as a second line. The ComfyUI tools
    take the same field (a card is filed for them). With a refused turn no job file and no file
    copy exist, and no `music_*` schema is offered.

  **Built at `8c73b1ba` (part 0: `ToolEntry.owner_only`, `registry.execute`, the offered list;
  ComfyUI is owner_only), each tool's second line at `29383864` and `e3b0ea6c`; not observed
  live.** *Follow-up 12:* `list_all_tools`, what the Agent Permissions panel renders, carries
  each tool's `owner_only`, so the badge can read the registry (`784e1298`); the sandbox check
  under every file tool compares path components, so `<agent root>2` or `<agent root>-backup`
  is no longer inside the agent's folder (`16573047`, Zcode #68 P2). Not observed live.

  **Amended by ADR-053 (Mike, 2026-11-25; built on `dev`, not observed live).** "Refused" on a
  turn that is not the owner's became "asks the owner when someone else asked": `music_*`,
  the ComfyUI tools and `chat_post_file` carry `owner_approval_for_others` instead of
  `owner_only`, are offered on every turn, and each call asked for by someone who is not the
  owner shows the owner a card (who asked, the chat, the tool, what exactly, and for a GPU
  tool the cost line). The owner asking in a mixed group runs them with no card.
  `music_propose_job` keeps its own card, which now names the asker and states the cost. The
  second line in each tool (`music._not_owner`, `chat_files.turn_refusal`) accepts the owner or
  a call the owner approved, per call. On a foreign request the ComfyUI tools take only paths
  the file tools' gate opens and only the local server. `chat_post_file` refused every group
  then; posting into groups is decision G (D50), built since (see D50). `list_all_tools` carries
  `owner_approval_for_others`, and the panel's badge reads "asks you for others".
- **D44. The agent gets a take by `take_id`, resolved by core, and no directory access**
  (letter D, **revised**: first decided in #46 as "the agent reads `~/.dpc/music/generated/`
  without a `sandbox_extensions` entry"; revised by Mike in #53-#57 on Zcode #51). A read path
  outside the sandbox would be a third way round it that the glossary's **sandbox** row does
  not know, and it would need a `sandbox_extensions` path (Q1). *Correction (Zcode #68): the
  reason first written here, that `generated/` holds every job's request JSON with its prompts
  and lyrics, is wrong as far as the request is concerned: the request JSON lives in
  `music/jobs/<job>/job.json` and its `payload/` folder (`music/jobs.py`); `generated/` holds
  the audio and its sidecars. Decision D stands on the two grounds above.*
  *Second correction (follow-up 13, Zcode): the sidecars are not free of lyrics either. The
  sidecar JSON beside each take (`<name>.json`, `dpc.take-sidecar/1`, `music/export.py`) and the
  take's own tags (a FLAC Vorbis comment `DPC_PROVENANCE`, a WAV `LIST/INFO` item `DPCP`,
  `music/provenance.py`) carry the provenance record, whose `lyrics` field is the lyrics as sent and
  `lyrics_prep` what the engine changed them to, with the style the song was asked for.
  So `generated/` does hold prompts and lyrics, in those two places (not the job's request
  file or its payload), and a take posted by `chat_post_file` carries its lyrics in its tags.
  It is the person's own song, so this changes no rule, but D44 must not be read as saying a
  take's files hold no lyrics.* Core resolves `take_id` to the take's file; this
  also gives "only takes in `review`" a meaning (today `review` is a job state, not a take
  state). The tools need no `sandbox_extensions` entry for `~/.dpc/music`; Q1's remark that the
  agent needs write access there is dropped for them. **Built at `8c5b51c4` (part 1:
  `get_music_take`) and `29383864` (the tools read takes by id); `chat_post_file` posts a take
  by id at `64b793ac` (D41). Not observed live.**
- **D45. Job limits** (Zcode #51 correction 2, accepted in #53-#57). Every new
  `idempotency_key` makes a new job and the key is global, so an agent in a 200-round loop
  could hold the card for hours. So:
  - **one job per call** of `music_propose_job`;
  - **a cap per turn** and **a cap per requester in the queue**: 1 job per turn
    (`[music] turn_jobs_max`) and 2 queued or running jobs per agent (`[music]
    agent_queue_max`), the defaults set at build;
  - **the default idempotency key derives from the turn id**, so a retry in the same turn
    returns the same job.

  **Built at `8c5b51c4` (part 1, the store's limits) and `29383864` (part 2: a repeat or a
  reached cap is answered before the person is asked); not observed live.** *Follow-up 12
  (Zcode #68 P2):* everything core would refuse (variants out of range, an empty style, an
  unknown engine) and a job core would make `unsupported` are answered before the person is
  asked, by core's own checks with no job made (`check_music_job`, `MusicJobStore.check_job`).
  An `unsupported` job that does exist (the runner refused a queued job) counts in the turn cap,
  in the tool and in the store; the queue cap still counts `queued` and `running` only, since an
  `unsupported` job holds no place in the queue. **Built at `98ceed3a`, not observed live.**
- **D46. `requested_by` on `mp.job/1`, and an agent cancels only its own jobs** (letter E, Mike
  #46). `requested_by {kind, agent_id, conversation_id, turn_id}` is added to the job schema;
  the schema is closed to undeclared keys (`job_schemas.py`), so a store written before the
  field must still open. `cancel_music_job` called by an agent for a job it did not propose is
  refused. The person's own cancel in the UI is unchanged. **Built at `8c5b51c4` (part 1;
  `turn_id` is recorded beside `conversation_id`); not observed live.** *Follow-up 12 (Zcode
  #68 P3):* an agent's cancel compares the agent **and** the conversation, so the same agent's
  turn in another chat does not cancel the job (`not_your_job`). **Built at `98ceed3a`, not
  observed live.**
- **D47. `music_propose_job` asks the owner's approval, as `schedule_task` does** (letter J,
  Mike #53-#57; Zcode #51 correction 7). A GPU job needs the person's consent, and the plan,
  copying `comfyui_submit`, would have inherited "no question". The approval machinery of
  `schedule_task` (`tools/core.py`) is reused, not rebuilt. A return to a job that already
  runs asks nothing (D49). **Built at `29383864` (part 2). The card says what it asks at
  `64b793ac`: for `task_type` `music_job` the Telegram card and the interface's event read "<agent>
  asks to run a music job on the GPU" with Approve / Refuse; `about` is "Run a music job on the
  GPU: <type>, "<style>", <n> takes, instrumental or with lyrics, <engine>, about <ETA> s on the
  GPU"; `schedule_approval_request` carries `heading`, `approve_label`, `refuse_label` and
  `music {type, style, variants, instrumental, engine, eta_seconds}`. Not observed live.**
  *Follow-up 12:* `music` also carries `duration_s`, `has_lyrics` and, only when the job has
  lyrics, `lyrics_excerpt` (their first 120 characters); `about` says the length. The card is
  put up only for a request core would accept (D45). A Stop on the turn while the card is up
  withdraws it (`superseded`, "The turn was stopped; no music job was made."), so a "yes" after
  the Stop makes no job. The approval log lines name what is asked (`music_job approval
  requested`, not `check_back`). **Built at `98ceed3a`, not observed live.**
- **D48. Stop cancels the turn's queued jobs** (letter K, Mike #53-#57; Zcode #51: Stop today
  takes only the requests that hold the chat, and a waiting job holds nothing, so Mike's Stop
  moves Strata off the card and the music engine starts). When the person stops a turn, the
  jobs proposed in that turn that are still `queued` are cancelled. A job already running is
  not stopped by this, and cancelling a running job does not stop the engine, which stays on
  the card until its idle stop (`yue2_idle_stop_s`, 300 s; ADR-052 section 3.5).
  **Built at `8c5b51c4` (part 1: cancel by turn) and `29383864` (part 2: the loop cancels on
  Stop or Kill); not observed live.**
- **D49. `check_back` is the return path, through `schedule_task`** (letter I, Mike #46 and
  #53-#57; Zcode #51 correction 3). The agent learns that a job is done by scheduling a
  `check_back`. For agents that have music tools `schedule_task` must be enabled (it is off
  by default, `tools/core.py`), and a return to an already running job asks no approval (the
  default asks the person within 60 s; the chain is at most three). A wake from core is
  later. The delay is computed from the job's ETA plus Strata's cold start of about 23 s after
  the job (ADR-052 section 3.5). **Built at `29383864` (part 2: a `check_back` whose task data
  names this agent's own queued or running job asks nothing) and `64b793ac` (`_get_allowed_tools`
  adds `schedule_task` wherever a music tool is left after the owner_only filter, so an owner
  turn is offered it and an `other_node` turn is not); not observed live.**
  - **Music-return-only (follow-up 12; Zcode #67 P1; decided by Mike, AI Studio group #83,
    2026-11-25: keep it, a switched-off `schedule_task` must not work around the switch).** A music tool does not open the whole `schedule_task`: Mike enabled
    music tools, not `chat` tasks or reminders. When `schedule_task` is in a turn's tools only
    because a music tool is (the firewall does not grant it), the turn's `schedule_task` is
    music-return-only (`ToolContext.schedule_task_music_return_only`, set by
    `DpcAgent.process`): it takes only task_type `check_back` whose task data names
    `music_job_id` of a job this agent proposed that is still `queued` or `running`. Any other
    task type, a foreign or finished job, or no job id is refused: "schedule_task is not enabled
    for this agent; only a return to your own music job is". The tool's name and schema stay.
  - **The approval-free return is bound to the job alone (Zcode #68 P2).** In both modes core
    sets its delay (`check_back_delay_s(job)`: the job's ETA plus 25 s, or 180 s plus 25 s when
    unknown) and composes its wake text (`music_return_text`: the job id and "call music_check /
    music_list_takes"); a delay, text or other task data the agent gave is dropped, and the
    answer says so. A `check_back` with the agent's own text and delay goes through the ordinary
    approval, and only where the firewall grants `schedule_task`.

  **Music-return-only and the bound return built at `94a6710b`, not observed live.**
- **D50. Three narrowings of the first cut** (Mike #46). **C:** the agent posts a take into
  the chat only when the person asks. **F:** no LRCLIB lookups by agents (they send data off
  the machine). **G:** posting into groups comes later; it is separate work, because in
  groups `_record_answer` is not called (`send_group_agent_message` takes no attachment).
  Licence is not a reason to hold it back (Mike, AI Studio group #88, 2026-11-25): the owner
  makes the group and chooses who joins, and what members do with a take is theirs to answer
  for. **Built at `29383864` (F: `music_get_lyrics` reads stored and local
  lyrics only) and `e3b0ea6c` (G: `chat_post_file` refused a group turn then); C is said in the
  tool's description. Not observed live.**

  **G built (Mike #166, 2026-11-25; on `dev`, not observed live).** `chat_post_file` in a group
  posts into the group the turn is in. It does not ride on the answer (no `_record_answer` in
  groups): the file goes through the person's group file path, `_send_group_file`, which the
  person's `send_group_file` and the new `send_group_agent_file` share (`service.py`). That is
  the copy in the group's `files/`, the sender's record, one signed file description
  (`dptp-file-v1`, ADR-047 phase 1) and the chunked offer to every connected member, so members
  receive it as any group file. Only the author differs: the record is the agent's, as its text
  posts are: `sender_type` "agent", the name checked as `send_group_agent_message` checks it
  (`agent_post_name`, now shared), `agent_owner` this node, all inside the record's signature.
  The words posted with the file are the record's text and wake no agent; the agent's answer,
  posted after the turn, routes its own mentions. `send_group_agent_file` is not a local API
  command.
  - **Who may (ADR-053).** The owner asking posts, under the profile's `chat_post_file` switch
    and its `group_allowed` setting (`_firewall_allowed_tools`: unset means allowed for every
    tool but `run_shell`). A foreign asker gets the owner's card; in a group its "what exactly"
    names the file, its size in MB and bytes, its type, and the group by name and id
    (`chat_files._group_post_card`). What the tool would refuse anyway (a closed store, a type
    off the allow-list, a file over the cap, a take that is not postable) is refused before any
    card.
  - **The room (ADR-048).** A path goes through `_resolve_file_path`, whose audience gate is the
    room's: in a mixed group the agent's private memory, its mixed stores and the extended
    paths not marked for the group are refused, whoever asked. A take by `take_id` is the
    owner's generated music and is posted as before (D41, D44).
  - **Limits.** The follow-up 13 allow-list by content, the 4 files per turn (counted on
    `ToolContext.posted_group_files`; a second post of one file in a turn is refused) and the
    size cap: the group file-transfer cap `file_transfer_cap_mb(rules, SENDER_FILE_DEFAULT_MAX_MB)`
    held to D41's cap, so 100 MB when the rules name none, else `file_transfer.max_size_mb`.
    The refusal names the group file-transfer limit; `_send_group_file` refuses an oversized
    file too. The source is first copied to a temporary folder under the DPC temp dir and that
    copy's content is checked again, so a file changed after the check is never offered; the
    temporary copy is removed after the send.
  - **A take's music block** goes on this node's record (the sender sees "Open in player", its
    library holds the track). It never crosses: not on the offer, and `_exported_attachments`
    drops `music` from every attachment a history export sends, since a peer's library has no
    such entry. On a peer the take plays inline as a plain audio attachment (`audio/*`).
  - **How a peer sees it now.** The file arrives as any group file of this node: its live record
    names the node by its display name (the receiver's note is built from `peer_metadata`), not
    the agent, until the author's signed record arrives with the next group history sync and
    takes the note's place (`_drop_local_file_note`). Carrying the agent's name on the offer is
    a wire change (DPTP §4.4) and is not made here.
  - **The UI.** The live bubble on this node is drawn as the agent's
    (`groupFileAgentFields` in `musicAttachment.ts`, spread by `MessageRouterPanel`).

  Pinned by `tests/test_chat_post_file_posts_into_the_group_it_answers_in_as_the_agent.py`.
- **D51. `music_wait` and the self-wait rule** (Zcode #51 correction 1, accepted in #46 and
  #53-#57; the rule itself is written in ADR-052 section 3.5). An agent turn on a local alias
  holds the card for all its tool rounds (`card_held_for`). `music_wait` answers "queued,
  starts after this turn" **only when** the turn holds a live card hold on a local alias
  **and** the job is `queued`. Otherwise it reports the job's real state: a job in `review` or
  `failed` is never reported as queued. The test is not `current_card_hold()` being non-empty:
  that returns a hold also in a cloud-alias turn (`alias=None`) and in a tool thread that
  outlived its turn. `card_wait` is empty right after `propose` (the runner writes it), so the
  state is read from the job. Beside it: `music_wait` checks Stop between polls (Stop does not
  interrupt a running tool, it is read before a call); no elapsed seconds in the output (they
  reset the count of five identical calls, `guards.py`), a wait budget per turn instead (value
  set at build: 900 s of `music_wait` per turn); `music_wait` and `music_check` go into
  `guards._OUTPUT_KEYED_TOOLS`.
  - **Extended at `64b793ac` (part 2's finding):** a job can wait on this turn in another state.
    A `running` job whose engine child ended waits for the card again (ADR-052 S4), and its
    `card_wait.holders` (named by alias, `holders_in_the_way`) can name this turn's own local
    alias. So when the turn holds a live hold on a local alias and the job's `card_wait`
    holders include that alias, `music_wait` answers at once "waits for the card behind this
    turn; check back after it ends". *Corrected in follow-up 12 to match the code (Zcode #68):*
    that holds for a job in `queued` or `running` only (`_waits_behind_this_turn`); a job in any
    other state is reported as it is, whatever its `card_wait` says. A `card_wait` naming
    another alias is waited for as before. A hold that is not live (its turn has ended) makes no
    self-wait. Both gates, and `live`, are pinned by tests at `98ceed3a`.

  **Built at `29383864` (part 2), extended at `64b793ac`; not observed live.**

*Numbering: D40-D51 are the agent-tool decisions of 2026-11-24. They have no SPEC §12 number
yet; SPEC §12 and the GLOSSARY rows (the names of D42, `chat_post_file`, `requested_by`, the
self-wait rule, `owner_only`) take this section as their source.*

### Not decided

Each item needs Mike's yes or a measurement. Where an item is built in practice, a dated
implementation note under it says so (N1, N6, N7, N8, N11, N12, N13); the rest are not
built.

- **N1. The phase plan 0 → 1 → 2 → 3** (CC's proposal, `docs/dpc-integration.html`).
  Mike asked what Phase 0 contains. He has **not** said go (Q2).
  - Phase 0, foundation with no GPU: B1, the microphone sending audio, the agent context
    markers for audio (Scope, `context.py`), a metadata source (N12), and the core service
    (library, watched folders and hash, playlists, byte serving, `local_api` commands).
    Whether the group-file fix joins it is Q2.
  - Phase 1, listener: the library UI, the window and mini-player, the lyrics chain, and
    SoundCloud/Yandex in core.
  - Phase 2: risk spikes first (the GPU card option and audio input for Flamingo), then
    creative work (the B2 runner with ACE-Step text2music and stage-1 QC, the tools and
    generate dialogs, YuE2, stage-2 QC, review). **Commitment from review (Ark):** phase 2
    counts the card swaps in one full run. A musician run can need ACE-Step, Whisper (the
    WER pass) and Flamingo, and each change of tenant is a swap under ADR-044 with its own
    cost (see Consequences).
  - Phase 3, musician: the repaint spike (since D26 no separate spike: checked live), B4,
    melody to song, lego/complete, and the clone.

  The epic's older child order ("Order B") is also CC's recommendation, not Mike's.

  *Implementation note (2026-11-22):* Phase 0 (library, byte serving, chat audio, the agent's
  context markers, a metadata source) and the listener part of Phase 1 (window, mini-player,
  lyrics chain with Transcribe) are built and on `origin/dev`. SoundCloud/Yandex is postponed
  (Q6). **Phase 2 has started in a different order from the plan above** (Mike #192, 2026-11-21:
  engine, then the window): the runner, acestep.cpp and YuE2 as supervised engines, the
  Create panel, the level-matched A/B and Keep / Discard are built (slice 1, D31-D35, D38).
  Not built from Phase 2: the tools and the generate dialogs for agents, stage-1 QC and the
  judge, cover, and the card swaps counted over one full run. Phase 3 is not started. The
  card is no longer a precondition for the engines; the lease (ADR-052) is what lets the
  other GPU clients, Whisper first, wait for music and the reverse.
- **N2. The repaint engine.** The proposal is ACE-Step 1.5 API repaint, with Resonant as
  the fallback (SPEC §12 decision 2). It was to be settled by a one-evening spike; since
  D26 (2026-11-20 05:53) it is checked live in the working player as B4 lands, with no
  separate spike.
- **N3. lego / complete** (own voice plus arrangement, instrument part to song). These
  are ACE-Step 1.5 base-model tasks per its README and are untested by us.
- **N4. Music Flamingo on raw humming** is untested. It does not transcribe notes and it
  confuses relative major and minor (SPEC §13).
- **N5. The voice-clone engine** is not chosen (research card
  NO-SINGING-VOICE-CLONE-IS-CHOSEN-SO-A-SONG-CANNOT-BE-SUNG-IN-THE-MUSICIANS-OWN-VOICE).
- **N6. The folder watcher**: the Python `watchdog` package (unverified, and not a core
  dependency today) or polling of size and mtime.
  *Implementation note (2026-11-20): implemented as polling, so no longer undecided in
  practice.* Every `[music] poll_interval_s` (default 60 s, written to a fresh
  `config.ini` by `b712712a`) each watched folder is walked and compared by path, size and
  mtime; there are no OS file events and no `watchdog` dependency
  (`music/library.py`, docstring and the poll loop).
- **N7. The tag reader**: which library to use and its licence. `Observed`: there is no
  mutagen, tinytag or eyed3 in `dpc-client/core/pyproject.toml`. It may be the same
  library as N12.
  *Implementation note (2026-11-22): answered in practice without a library.* Tags are read
  through `ffprobe` (`music/identity.py`, `lyrics.py`), and the FLAC Vorbis comments and the
  WAV `LIST/INFO` chunk that carry the provenance record are read and written by a small
  pure-Python module (`music/audio_tags.py`, audio copied byte for byte). The cost is
  known: ffprobe does not surface ID3 SYLT, so a file whose only lyrics are the synced frame
  shows none (the SYLT card). Whether to add a tag library for that is open.
- **N8. How core serves bytes to the webview.** *Resolved 2026-11-20 by Mike, D28: option
  (i), a local server with `Range`, a token and an `Origin` allowlist, keyed by `track_id`.
  Implementation deferred; checks use files inside `~/.dpc/music`.*
  - *Implementation note (2026-11-20): built in `9bb7599d` (on `dev`, pushed), tests
    only; the Linux window check is open (Confirmation).* What was built:
    - **A dedicated listener, not the 9998 file server**: `core/dpc_client_core/music/stream.py`,
      aiohttp (the stack the gateway already uses), bound to `127.0.0.1` only (any other
      host is refused at construction), port `[music] stream_port`, default `9996`. The
      9998 server was not grown: it is a single-threaded `TCPServer`, so one held media
      connection would block voice playback, and it reads whole files and sends CORS `*`.
    - **One route**, `GET`/`HEAD /music/track/{track_id}?t=<token>`. The id is resolved by
      `MusicLibrary.get_track_path`; there is no path parameter and any query key but `t`
      is 400. Unknown, missing, fileless, non-regular files and files without one of the
      library's audio extensions are 404. So any library entry plays wherever its file is
      (a watched folder in `~/Music`, a generated take), and nothing else does.
    - **Range**: `bytes=a-b`, `a-`, `-n` answer 206 with `Content-Range`; a start past the
      end is 416 with `bytes */size`; no `Range`, a malformed one or **several ranges answer
      200 with the whole file**. Read in 256 KiB chunks; `Cache-Control: no-store`.
    - **Token**: `secrets.token_urlsafe(32)`, made per core process, held in memory,
      compared in constant time (403 when missing or wrong), handed to the UI only by the
      new `local_api` command `get_music_stream_info` (`{base_url, token}`). Not the
      `.ws_token`, which opens every command and would then sit in media URLs.
    - **Host and `Origin`**: the `Host` header must be the listener's own
      (`127.0.0.1:<port>` or `localhost:<port>`, against DNS rebinding); an `Origin`, when
      sent, must be `tauri://localhost`, `http(s)://tauri.localhost` or the dev server
      (`http://localhost:1420`, `http://127.0.0.1:1420`), else 403; an allowed one is echoed
      with `Vary: Origin`, never `*`. A media request without `Origin` rests on the token.
    - **CSP**: `media-src` gains exactly `http://127.0.0.1:9996`
      (`ui/src-tauri/tauri.conf.json:27`). The port is fixed because the CSP is static; a
      taken port leaves the stream off, logged, and the command answers
      `stream_unavailable`. Another port needs the CSP changed and the app rebuilt.
    - **UI** (`trackPlaybackUrl`, `ui/src/lib/utils/musicPlayer.ts`): Linux plays every
      track from the stream; Windows and macOS keep `asset:` for files under `~/.dpc` and
      use the stream for files outside it, which `asset:` cannot reach. Without stream info,
      Linux falls back to `blob:` for `~/.dpc` files and a file outside `~/.dpc` stays "not
      playable yet". The `[music] source ... via=http|asset|blob` log line names the source
      and any `fallback=` reason.
  - `Observed`: the Tauri asset scope is only `$HOME/.dpc/**`
    (`ui/src-tauri/tauri.conf.json:28-33`), so a file in `~/Music` cannot go through
    `convertFileSrc`. Indexing folders in place, with core serving the bytes, is CC's
    proposal and was not part of Mike's folder decision (SPEC §12 decision 16).
  - `Observed`: the CSP is `default-src 'self' … http://localhost:* ws://localhost:* …;
    media-src 'self' mediastream: blob: https://asset.localhost asset:;`
    (`tauri.conf.json:27`). `media-src` is present, so it overrides `default-src` for
    `<audio>`. `http://localhost:*` is allowed for other fetches but **not for media**.
  - `Observed`: **a local file server already exists** (Ark). `file_server.py` runs on
    `127.0.0.1:9998` (`:89`; started at `service.py:453`). It serves only paths under
    `~/.dpc/conversations/<peer>/files/` (`:34-43`), reads the whole file into memory
    (`:63-64`), sends no `Accept-Ranges` and handles no `Range` (`:67-72`), allows CORS
    `*` (`:70`), sets a one-year cache (`:71`) and has no authentication. `VoicePlayer`
    already uses it in the Tauri-on-Linux path (`VoicePlayer.svelte:100-108`, with the
    comment that `asset://` does not play audio in the Linux WebView) and in the browser
    path (`:134-140`).
  - **Range is a requirement, not an optimisation.** Without `Range`/206, seeking in a long
    track does not work, and seeking is the player's main action (Ark).
  - The options:
    - (i) a local HTTP endpoint with `Range` and a token: grow `file_server.py` or add one.
      By the CSP line this also needs a `media-src` change, unless the experiment below
      shows otherwise;
    - (ii) bytes over `local_api`, turned into a `blob:` URL in the webview (Forge).
      `blob:` is already in `media-src`, so no CSP change. `Inferred`, untested: it rests
      on the CSP line and standard `URL.createObjectURL` behaviour. A blob holds the whole
      file in webview memory (`Inferred`);
    - (iii) copy each track into `~/.dpc/music/` (Considered Options, Local files A).
  - **First step (Ark): one experiment, before any option is chosen.** Does a
    `http://localhost:9998/files/…` URL play, and seek, in the Tauri window on Windows and
    on Linux? The Linux VoicePlayer path already depends on it while the CSP line says it
    should be blocked, so the run either confirms the CSP or shows that Tauri does not
    apply it as read. One run informs N8 and N9 without code.
  - *Implementation note (2026-11-20, earlier; superseded by D28 at 09:01): N8 stays open; Mike is thinking.* Until the endpoint is built,
    the window plays only files under `~/.dpc`: `convertFileSrc` on Windows, and on Linux
    a `blob:` read through the fs plugin, because `asset:` does not play audio in
    WebKitGTK; any other path shows "not playable yet" (`trackPlaybackUrl`,
    `ui/src/lib/utils/musicPlayer.ts`). On 2026-11-20 CC, Zcode, Forge and Ark all
    recommended option (i): a local HTTP endpoint with `Range`/206, a token in the URL
    plus an `Origin` allowlist, keyed by `track_id`, with a CSP `media-src` change.
    Widening the Tauri asset scope was raised as a fourth path; it does not play audio on
    Linux WebKitGTK. Forge's cost argument against (ii) `blob:`: the whole file sits in
    webview memory for every playing track. None of this was a decision when written; Mike then chose (i), D28,
    built in `9bb7599d` (note above).
  - *Implementation note (2026-11-22):* the Linux WebKitGTK window needed follow-ups. Linux
    plays the track without `Range` by default (`stream_ranges_linux`, false), with the
    headers of the 9998 server (`89123e1d`, `05549c35`), preloads the track (`012d8c0b`) and
    waits for a duration above zero before play so seeking works (`36a4777a`; the Linux node's
    log is in the backlog cards, not re-read here). Whether the no-`Range` special case is
    still needed is a LOW card, on Mike's yes
    (THE-LINUX-TRACK-STREAM-KEEPS-A-NO-RANGE-SPECIAL-CASE-THAT-THE-DURATION-BEFORE-PLAY-FIX-MAY-HAVE-MADE-UNNECESSARY).
- **N9. Web delivery**: core proxies (fetches and caches) the stream, or open web
  `https:` is added to `media-src`. `Observed`: `media-src` allows only
  `https://asset.localhost` among https origins (`tauri.conf.json:27`); there is no open
  web `https:` and no `http://localhost:*`. A proxy also gives the agent its copy for
  transcribe and QC, and its bytes then reach the webview by whichever N8 option wins.
  For SoundCloud/Yandex, the diagram's tool (`yt-dlp`, not in `core/pyproject.toml`) is
  CC's proposal.
- **N10. Playlist import** from SoundCloud and Yandex (D5).
- **N11. The GPU card option.** The card lists (a) a supervised external worker, (b) a
  claim/lease API in `local_api`, and (c) detection through `nvidia-smi`. Its own note of
  2026-11-19 says (c) alone cannot free the card, so (a) or (b) is required.
  - `Observed` (Forge, Ark): `process_registry.py:218` has `SUBSYSTEMS = ("strata",
    "llamacpp_server", "browser", "device_probe", "git_helper")`, and registering a
    `subsystem` launcher outside that tuple raises `ValueError` (`:1145`). VRAM is
    sampled only for `ENGINES = ("strata", "llamacpp_server")` (`:277`). The card claim
    map is `_CARD_CLAIMS: Dict["LlamaServerSupervisor", str]`
    (`managers/llama_server_supervisor.py:657`), typed for that supervisor only.
  - So (a) needs **a new subsystem name and `ENGINES` growth** before a music engine gets
    a VRAM reading. Registered as `launcher_kind="tool"` it would have no VRAM row, and the
    swap and the "OOM → controlled refusal" path could not see it. (b) needs a claim that
    is not a `LlamaServerSupervisor`. Naming the process category is a required part of
    whichever option is chosen; without it the GPU confirmation cannot be checked.
  - `Reported` (Zcode): YuE2-Studio documents `--headless` (tray, auto-stop after about
    10 minutes idle). That makes (a) viable for YuE2: DPC starts it headless through the
    registry (ADR-046) instead of the person starting a desktop app.
  - *Implementation note (2026-11-22): the half of (a) that names the process is built, and (b)
    became ADR-052.* The music engines DPC supervises are registered as
    `launcher_kind="subsystem"` under their own names, `acestep_server` and `yue2_server`,
    which `SUBSYSTEMS` (`process_registry.py:220`) and `ENGINES` (`:281`) now list, so each has
    a VRAM row (`6b34b44c`, `ade158b7`; the unknown-subsystem `ValueError` is at `:1148`).
    `_CARD_CLAIMS` (`llama_server_supervisor.py:664`) is still typed for the supervisor.
    What the card option still has to do is ADR-052: a **card lease** for the engines (S4)
    and for Whisper (S3), so that the supervisors see them and wait (S2). ADR-052 depends
    on this record, is `proposed`, and S1 (the lease registry and the lease line) is built
    (`53a2eaf5`, follow-up `4d0fa885`). The probe is still `nvidia-smi`; foreign GPU use
    (ComfyUI, an external music app) is S8 / S10. S2 (the supervisors wait for a busy lease and
    unload an idle one) is built at `1e57932f`; S3 and S4 are pending.
- **N12. A metadata source** (Ark). `Observed`: `VoicePlayer` takes a required `duration:
  number` prop (`VoicePlayer.svelte:41`), and a `file` attachment carries no duration.
  `ffprobe` is called nowhere in `dpc_client_core`; `ffmpeg` is called only by
  `comfyui_convert` (`dpc_agent/tools/comfyui.py:932`). D19's "a `file` with `audio/*`
  plays" therefore needs core to read duration, sample rate and channels, either through
  `ffprobe` or through a pure-Python reader (licence to check; possibly the N7 library).
  `Reported`: ffmpeg 8.1.2 (winget) is on this node (Forge's measurement, relayed by Ark);
  on how many other nodes ffmpeg is present is unknown. The same choice decides what the
  payload hash of Q4 (a) costs.
  *Implementation note (2026-11-22): resolved by `ffprobe`.* Core reads duration, sample rate
  and channels with `ffprobe` through `run_probe` and the registry (`music/identity.py`
  `probe_metadata`; for a chat attachment, `measure_recording_seconds` in `service.py`), and an audio
  `file` in a chat shows its `duration_seconds` (`knownDurationSeconds`). **Without ffprobe**
  the title is the file name, a FLAC still gives its duration from STREAMINFO, the audio hash
  falls back to the whole file (`hash_kind: "file"`, a retag then looks like new audio), and
  the waveform, loudness and MP3 export, which run through ffmpeg, have nothing to run on
  (`Inferred`, not run without it). The per-node ffmpeg presence check of Scope stays a real
  requirement.
- **N13. Waveform rendering.** (A) `wavesurfer.js` 7.12.12 bundled by Vite (BSD-3-Clause,
  Prior art), with `sampleRate: 22050` for the spectrogram; (B) peaks precomputed by
  ffmpeg into JSON, cached beside the file hash and drawn as a static wave (Forge's
  preference: no DSP library in the webview, cheap on a large library). Recorded so the
  choice is not made silently by installing a package.
  *Implementation note (2026-11-22): built as (B), with no recorded decision of Mike's.*
  Core decodes the track with ffmpeg to mono 8 kHz PCM and reduces it to peaks, cached by
  content hash (`music/peaks.py`, `get_music_track_peaks`, `0063d4be`); the window draws them
  (`MusicWaveform.svelte`, on the now-playing card and in the A/B review, `c146b811`).
  `wavesurfer.js` is not a dependency (`ui/package.json`). There is no spectrogram, so the
  `sampleRate: 22050` setting of the prototype has nothing to carry.
- **N14. Audio through the gateway (ADR-041).** If Music Flamingo becomes a DPC engine,
  audio either passes the gateway's doors (today a test asserts that `input_audio` is
  refused there, `tests/test_the_gateway_carries_the_effort_and_the_image_through_both_doors.py:599-608`),
  or the record states that the QC judge calls the provider directly and bypasses the
  gateway (Ark).
- **N15. The `music/` folder in the provider model list** (Forge). `list_model_files`
  (`service.py:11232-11253`) walks the model files folder through `model_files_in`
  (`managers/llama_server_supervisor.py:1964-2021`) and lists GGUF files with
  `is_mmproj` and `shard_count`. `Observed`: the walk reads `.gguf` files directly in the
  folder and in each subfolder one level down, "no deeper", skipping only `strata`
  (docstring `:1965-1973`, the loop `:2013-2020`). So the outcome depends on depth:
  - a GGUF placed directly in `music/` (one level) **is listed** as a provider model,
    which is Forge's case;
  - a GGUF at `music/<engine>/` (two levels, the D14 layout) **is not listed**
    (`Inferred` from the code; not run).

  This cuts both ways, because Music Flamingo is meant to run through DPC's own
  `llama-server` (D12, ADR-040) and a provider alias is normally chosen from that list.
  Open: exclude `music/` from the walk and give Flamingo its own path to an alias, accept
  a mixed list (and place Flamingo where the walk sees it), or teach the walk about
  `music/<engine>/`.
- **Also open from SPEC §12**, not in the caller's list: where takes live (5), the HTTP
  service versus `local_api` commands only (6), two media elements versus WebAudio
  buffers for A/B (7), tags versus sidecar precedence (8, 11), QC thresholds and which
  judge to install first (12; until then ranking is off, D23), a version tree for sibling takes (14), the Listen-view
  defaults (17), and the build order 1 → 2 → 3 of the modes (19).
  *2026-11-22, as the code answers them (none of these is a decision of Mike's):* (5) takes
  live in `~/.dpc/music/generated/<job_id>/` (D31, D21), and the library is where the person
  finds them; (6) `local_api` commands for everything and one loopback listener for bytes
  (N8), no HTTP service for jobs; (7) one audio element, a short gap on a switch
  (`MusicReview.svelte`), so still open for seamless A/B; (8, 11) sidecar `.lrc`, then tags
  (D6 chain), and tags in watched folders are never written (Q4); (12) no judge is
  installed; (14) a take is registered once and carries `parents: []` only (PROVENANCE §7);
  (17) not re-checked here; (19) the listener and the first creative slice came
  first, musician mode is not started.

### Rationale

- **A window in DPC, not a separate app.** The files, the agents, the chats and the GPU
  arbitration already live in DPC's backend. A separate app would need its own way to all
  four. The Agent map already proves the window pattern, including the minimised state
  that becomes the mini-player.
- **One service, two doors.** A person and an agent ask for the same things. Putting both
  doors on one set of functions means the capability check, the hash check and
  never-overwrite are written once. This is how `comfyui_*` already works for images. It
  is also why Ark wants the accept rule enforced in the tool, not only in the UI (Q1).
- **Engines reached directly.** Each engine already ships its own interface: the ACE-Step
  API server is asynchronous submit and poll, and YuE2-Studio has `/synth` and `/job`
  (the C++ servers DPC runs, `ace-server` and `yue-server`, are the same shape: a local HTTP
  server DPC starts, polls and stops).
  Going through ComfyUI would add a second process on the card and tie the player to
  graphs. ComfyUI 0.37 has no repaint node (repaint spike card, `Observed` there on
  2026-11-19).
- **No MCP.** Agents already have tools and a shell. An MCP client in DPC would be a new
  trust boundary for one engine that also has HTTP and a CLI.
- **`file` plus `audio/*`, not a new type.** The metadata is already there, so old
  messages gain playback once a duration source exists (N12), and the attachment union
  does not grow.

## Considered Options

- **Surface:** (A) a separate desktop or web app; (B) a window inside DPC. **Chosen: B
  (D1).** A was rejected because it would duplicate file access, agent wiring and GPU
  arbitration that DPC already has. The standalone prototype page stays as the
  development harness.
- **Engine path:** (A) through ComfyUI (0.37 has ACE-Step 1.5 and MiniMax Music3 core
  nodes); (B) each engine's own API or CLI. **Chosen: B (D12).** A was rejected because,
  in Mike's words, he does not want the player tied to ComfyUI. It also has no repaint
  node, and MiniMax Music3 is ComfyUI-only in our stack.
- **How agents reach engines:** (A) an MCP client in DPC, which YuE2-Studio offers at
  `127.0.0.1:8791/mcp`; (B) `music_*` tools over the core service, with engines reached
  by HTTP or CLI. **Chosen: B (D13).** A was rejected because agents already have tools
  and a shell, and an MCP client would be a new trust boundary.
- **Attachment:** (A) a new `audio` attachment type; (B) `file` with `mime_type: audio/*`
  rendered as a player. **Chosen: B (D19).** A was rejected by Mike: `file` already
  carries the MIME type.
- **Local files:** (A) copy every track into `~/.dpc/music/files/` (the asset scope
  already covers it); (B) widen the Tauri asset scope per folder; (C) index in place and
  serve the bytes from core. D4 decides that folders are watched. **C is CC's proposal**
  (N8). A costs disk space, and B is a security change per folder. C itself has three
  shapes (N8 i-iii), and one experiment comes first.

## Consequences

- **Positive:**
  - One place for the rules. A person and an agent see the same state, and a take an
    agent posts appears in the window.
  - Old audio files in chats become playable once `audio/*` renders as a player and a
    duration source exists (N12).
  - No new protocol client and no ComfyUI dependency in the player's path.
  - Weights live in one store, with no 6-19 GB duplicates.
- **Negative:**
  - Every engine is a GPU process beside Strata. Until ADR-052 S4 is built (N11; S2, the supervisors' side, is built at `1e57932f`),
    Strata is stopped and restarted by hand ("Stop X" on the job card), a job waits in
    `queued` with a reason, and `llama-server` and Strata do not wait for a running song.
  - **The queue clock is long.** `Observed`: `swap_wait_s = 1800` on this node
    (`~/.dpc/config.ini:181`; the default is 300, `settings.py:147`). A queued job can
    legitimately wait up to 30 minutes for the card, so the GPU acceptance runs on a
    30-minute clock and must not be read as hung (Forge).
  - **Tool timeouts must match that clock.** `Observed`: `comfyui_wait` has
    `timeout_sec=610` (`dpc_agent/tools/comfyui.py:1186`). If `music_wait` copied that
    scale, the agent would see a tool failure on a job that is correctly waiting, and
    "queued" would look like "failed" (Ark). The wait budgets and the tool timeouts are
    declared in the same commit as the tool names (Q5).
  - **Swaps per run.** One musician run may need ACE-Step, Whisper for the WER pass and
    Flamingo, and each is a swap under ADR-044. Whisper adds its own CUDA residency
    question (card THE-WHISPER-ALIAS-IS-THE-BACKENDS-LAST-STANDING-CUDA-RESIDENT,
    `backlog.md:7212`, already linked from the QC card at `backlog.md:313`). How many
    swaps a full run costs decides whether the musician mode is usable; phase 2 counts
    them (N1). *2026-11-22: Whisper now unloads itself after a transcription
    (`[local_transcription] idle_unload_s`, default 300, `b796af4a`, `69ae799c`), and the
    player's Transcribe releases a model it loaded; the queue and the GPU/CPU choice are
    ADR-052 S3. Whisper beside a busy music engine is the one shared cell ADR-052 allows
    when it provably fits (L11).*
  - Watching and hashing a large library is I/O-heavy (SPEC §13). A payload hash (Q4 a)
    adds one ffmpeg spawn per file.
  - Web streaming and core byte serving add an outbound path and a local serving path
    that each need a security review (N8, N9). The existing 9998 server has no token and
    allows CORS `*`; widening it to library folders would carry that over unless changed.
  - **Disk.** `Reported` (Forge): ACE-Step about 6.3 GB, MiniMax Music3 about 11.9 GB
    (Forge, from the HF trees), the Music Flamingo GGUF about 8 GB (CC's figure, Q8_0 size
    confirmed by Forge), YuE2 20-40 GB (CC and Zcode from its README, not checked by
    Forge), the clone unknown: 40-70+ GB in all. Free space on 2026-11-19: C: 543.5 GB of
    1862, D: 169.8 GB (Forge). The ADR does not choose a disk; the model files folder
    (D14) decides it.
  - The non-commercial weights (D17) block any product use.
- **Neutral:**
  - **The provider model list.** Because the weights live under the model files folder
    (D14), `list_model_files` walks the same tree. A GGUF directly in `music/` is listed as
    a provider model; one at `music/<engine>/` is not, which may also hide Music Flamingo
    from the alias it needs (N15). Exclude `music/`, accept a mixed list, or extend the
    walk: open.
  - New words need GLOSSARY rows in the same commit as the code: the button label, the
    tool family, **track**, **take**, **version** and **watched folder** (Ark), plus the
    tool names of Q5. *2026-11-22: not done.* The library, the jobs and the review shipped
    without rows (`docs/GLOSSARY.md` has none for these words, **genome** or the `mp.*`
    records); this is a gap against the commit rule, listed in Q9. The glossary's **engine**
    already means the local LLM provider's program, so the rows have to say "music engine".
  - ROADMAP's axis table is generated (`tools/backlog/build.py --roadmap`) from the front
    matter, so it lists this record under `collective` and `knowledge`.
  - **ADR number.** A parked draft,
    `ideas/parked/adr-051-draft-group-extraction-target-2026-11-15.md` (2026-11-15, older
    than this record), also declares `adr: 051`. `build.py` reads front matter only from
    `docs/decisions/[0-9]*.md` (`tools/backlog/build.py:432-438`), so the collision is
    invisible to `--check` (Ark). Renumbering is Mike's call (Q8); 052, the number
    recommended there, is now ADR-052.

### Risk: a file sent to a group of agents is dropped silently

*Status 2026-11-22: fixed on Mike's verb, 2026-11-19 (`3837b3a5`, `3f2de3c7`, `dd446110`);
the text below is the finding as it stood at `2d333000`, and its line numbers are of that
commit. The second gate (ADR-032 Part B, Q3) is still as described, and the third gate is
closed by `file_attachment_markers`.*

This is a precondition for the musician flow in group chats. `Observed` in code at
`2d333000` and in the log:

- **The code.** `send_group_file` (`dpc-client/core/dpc_client_core/service.py:7882-7944`)
  only fans a file out P2P to other member nodes:
  - `connected_peers = …get_connected_peers()` (`:7904`);
  - the loop over `group.members` (`:7918`) skips this node (`:7919`) and any
    non-connected member (`:7921`);
  - it returns `{"status": "success", "transfer_ids": transfer_ids, …}` (`:7941`).

  It saves nothing locally, writes no history and broadcasts no event. The sender's record
  of a group file is written only in `message_handlers/file_complete_handler.py:132-157`,
  on the first `FILE_COMPLETE` from a peer.
- **The effect.** In a group whose only node is the sender, with only agents as other
  members, the loop sends nothing. No history is written, no local copy is kept, and the
  call still returns `success` with an empty `transfer_ids`. The UI then shows "Sending..."
  (`ChatPanel.svelte:840-852`).
- **Observed live.** `~/.dpc/logs/dpc-client.log.1:75624-75625` (the log rotated at
  12:42 local, so the lines are in the `.1` file), 2026-11-19 12:34:49,326 local (05:34 on
  the chat's clock): `send_group_file` for group `group-60aff6110f02` with Mike's
  `C:\Users\mikha\Music\Тысячи умов   один огонь.mp3` (4 918 633 bytes), and the response
  sent in the same millisecond. *Correction from review (Ark):* the first draft cited
  `dpc-client.log`, where these lines no longer are.
  - `~/.dpc/conversations/group-60aff6110f02-ai-studio/metadata.json` lists one member
    node, with ten agents attached to it.
  - Its `history.json` held 110 messages with **zero** attachments, and its `files/`
    held only `screenshots/` (read at 12:38 local).
- **The contrast.** `send_group_image` (`service.py:7948`) writes the image to the
  conversation folder, then `add_message` and `save_history` (`:8105-8112`), then
  broadcasts `group_file_received` (`:8114`), all **before** the fan-out. That is why
  screenshots work in such a group and files do not.
- **This is not a new defect, and it now has its own card.** It is the deferred phase 3 of
  an accepted decision: ADR-032 Part A (local-first publishing for group voice, image and
  file). Voice (`e05f5a1`) and images (`4fc62643`) were done. Files were deferred until
  large-file size research (`tasks/adr-032-group-attachment-local-first/007` and `008`:
  "Deferred"). The board carries the general case as
  GROUP-IMAGE-AND-FILE-ECHO-STILL-WAITS-ON-THE-TRANSFER (LOW, open, 2026-08-11,
  `backlog.md:8393`: a peer is offline). The agents-only case is now its own entry,
  A-FILE-SENT-TO-A-GROUP-WITH-ONLY-AGENTS-VANISHES-AND-THE-UI-IS-TOLD-SUCCESS (MEDIUM,
  open, `backlog.md:121`, filed by CC on Mike's request at 05:51), which records its
  relation to the older entry: there is never a peer, so the loss is permanent and the UI
  is told success. The music player raises its weight: an audio file sent to an
  agents-only group has to land before an agent can register it.
- **A second gate on the same path.** ADR-032 Part B routes the microphone in a group with
  one node or fewer to **dictation, by design** (`ChatPanel.svelte:987-993`, "≤1 node
  (agents are node-less) → dictation"). In agent chats it always dictates (`:996-998`).
  The microphone card covers `local_ai`, `ai_*` and `agent_*` chats only. So D9's
  "recording in the chat", in a group like AI Studio, needs both the group-file fix and an
  exception to Part B (Q3). Both walls were hit live on 2026-11-19: the mp3 at 12:34:49
  and the microphone dictating in this group.
- **A third gate after the file lands.** Even when the file is in the history, the agent's
  context names only image attachments (`dpc_agent/context.py:877, 895-913`), so an agent
  does not see an audio path until that changes (Scope).

## Confirmation

Taken from the child cards' acceptance lines and the reviews. Each one is checked in the
real app, not in the prototype.

*Status 2026-11-22.* No box below is ticked: the boxes mean "seen working in the app", and
most of what is built is covered by tests (23 committed core test files with "music" in the
name under `dpc-client/core/tests`, more for provenance, export and lyrics, and the UI
suites) and by Mike's live tries of
2026-11-21 (the acestep.cpp run, #191; the YuE2 listening set, #197-#198; his first Create
test and the first MP3 export). Built and waiting for a try in the app on both platforms:
byte serving, library, watched folder, track identity, chat playback, agent context,
microphone, lyrics, runner (as `generate_from_text`), the A/B (level-matched, no seam), MP3
export. **Not built, so not testable:** the tools, QC, the audio input, the region edit,
cover, and the lease items of the GPU box. The agents-only group precondition was fixed and
seen working by Mike (2026-11-19, Implementation Status).

- [ ] **Byte serving first step (N8, N9).** A `http://localhost:9998/files/…` audio URL is
  tried in the Tauri window on Windows and on Linux, and the result (plays or not, seeks
  or not) is written down when the N8 endpoint (option (i), D28; deferred) is built. The endpoint serves
  `Range` and seeking works on a long track. *2026-11-20: the endpoint is built
  (`9bb7599d`, port 9996, N8 note); what remains is the window check on Linux: a long
  track shows its full duration at once and a seek forward plays.*
- [ ] **Window (B3).** In DPC, open a local track, leave a region comment, send it, watch
  the job reach review, A/B a take at the seam and accept it as v2. Reopening the track
  brings back the comments, lyrics and versions. The Music Player button opens the window,
  and minimising it keeps playback going across chats.
- [ ] **Library.** A playlist of local files and one web URL plays through with next,
  previous, shuffle and repeat on Windows and Linux. It survives a restart. Switching chats
  does not stop playback. Generate and Comment act on whatever is playing.
- [ ] **Watched folder (D4).** A file added to, renamed in or removed from a watched folder
  shows up without a button. A renamed file keeps its comments and playlist position
  (same hash). An exact byte copy shows once. A re-encode of the same song from another
  source is a separate entry, and nothing claims otherwise (D5).
- [ ] **Track identity (Q4).** Whatever Mike chooses is tested on a tag edit: after a
  title change, the track keeps its comments, versions and playlist positions, or the
  person is asked to relink it. A file in a watched folder outside `~/.dpc/music` is not
  rewritten unless Mike decides otherwise.
- [ ] **Chat playback (B1, as corrected by D19).** An agent posts a WAV it made. It plays
  in its chat on Windows and on Linux, as a `file` attachment with `mime_type: audio/*`,
  shows its duration from core's metadata source (N12), and survives closing and
  reopening the chat. A denied path is refused with the reason. No `audio` value is added
  to the attachment union.
- [ ] **Agent context.** After an audio `file` lands in an agent chat or a group, the
  agent's next turn names its path (the `context.py` change), and the agent can read it
  only where the firewall grants access (Scope).
- [ ] **Runner (B2).** (Built for `generate_from_text`; `generate_similar` is `planned`.)
  One `generate_similar` job with 2 variants returns 2 `new_track`
  takes. Each has `mp.take/1` and `mp.qa/1`, and its files are written once and hashed.
  A repeat with the same `idempotency_key` returns the same job.
- [ ] **Tools.** With the tools enabled, an agent registers a track, proposes a
  `generate_from_lyrics` job, waits, lists the takes with QC, posts the best take into the
  chat and adds it to a playlist. The same take then appears in the window. With the tools
  disabled, no `music_*` name is offered. If Mike makes Q1 a decision,
  `music_accept_take` without the person's request refuses in the tool itself. A job
  queued behind the card for longer than 610 s is still reported as `queued`, not as a
  tool timeout.
- [ ] **Microphone.** In an agent chat, 20 s of humming sent as audio shows a card that
  plays, the agent's next turn lists the path, and `music_register_track` returns a track
  id. Dictation still works when chosen.
- [ ] **Lyrics.** Auto on a tagged MP3, on a FLAC with a sidecar, and on a file with
  neither (with LRCLIB off) each stops at the right step with the right source badge.
  Existing lyrics are never replaced. A second lookup of the same track is served from the
  local LRCLIB cache with the network off.
- [ ] **QC.** Before any threshold goes into config, a control pair of seams (one
  known-bad, one known-good) is measured and the threshold is set between them (Forge).
  Two takes carry `mp.qc/1` and are ordered by overall with the caveat shown. A take with
  a clipped sample is flagged whatever the judge says. Loudness and true-peak differences
  across codecs (a lossy original against a WAV take) are shown, not gated.
- [ ] **Audio input.** A Music Flamingo alias captions a WAV through the provider, reports
  audio and not vision, and refuses an image call with a reason. The gateway either
  carries the audio part or the record says the judge does not go through it (N14).
- [ ] **Region edit (B4).** A region comment returns two takes whose seam numbers are
  shown as advisory pointers (D27), not gates, and accepting one creates v2 without
  touching v1.
- [ ] **YuE2.** A cover job on a track with lyrics returns `new_track` takes titled "Cover
  of". Without lyrics it is refused with `missing_lyrics`.
- [ ] **No ComfyUI, no MCP (D12, D13).** No module under the music service imports the
  ComfyUI client or an MCP package, and `pyproject.toml` gains no `mcp` dependency.
- [ ] **GPU (D18, N11, D37).** A music engine is registered under a named process category
  and has a VRAM row in the registry (**built**: `acestep_server`, `yue2_server`). A music job
  on a card held by an idle Strata leads to `stopped` / `swap` in the process registry, the
  job runs, and Strata comes back on the next demand (**not built**: ADR-052 S4, Mike #70, on
  S2's lease side built at `1e57932f`; gated by `swap_when_idle`, default `false`, which a live
  check needs `true`; today the job waits and the person presses "Stop X"). The
  run is timed against `swap_wait_s` (1800 s on this node). A transcription during a running
  song waits and names the holder (ADR-052's Done-when, S1 + S3 + S4).
- [ ] **Agents-only group (precondition).** An audio file sent to a group whose only node
  is the sender lands in its history and `files/`, and an agent can read its path. A send
  that reaches nobody does not report bare `success`.

## Scope

New code and files touched. Paths are relative to `dpc-client/`. *Re-read at `1fa73a26`
(2026-11-22); each item says what is built.*

- `core/dpc_client_core/music/` (**built**; the name came from `docs/dpc-integration.html`):
  the core service (library and identity, folders and polling, playlists, jobs and takes,
  runner, engine supervisors, lyrics with a local LRCLIB cache, comments, peaks, loudness,
  export, provenance, the track stream) and the two engine adapters. Adapters resolve
  weights through `get_model_files_dir()` (D14).
- `core/dpc_client_core/music/commands.py` (**built**): the `local_api` commands as
  `async def`s on `CoreService`, dispatched by `getattr` (`local_api.py:711`). Also
  `send_group_file` (`service.py:8399`, fixed) for the group-file card, and
  `list_model_files` (`service.py:12032`) if N15 excludes `music/` (**open**).
- `core/dpc_client_core/dpc_agent/context.py` (**built**, Ark): `history_body` (`:889`)
  names image attachments through `image_attachment_markers` (`:922`) and every other file
  or voice attachment through `file_attachment_markers` (`:1007`, `3837b3a5`), so the agent
  learns where an audio file is kept.
- **Agent file access to `~/.dpc/music`** (Ark; **open**, belongs to the tools card).
  `Observed`: an agent's paths outside its own sandbox are granted per profile through
  `sandbox_extensions` `read_only` / `read_write` in the firewall (`firewall.py:1224-1226`;
  checked by `is_extended_path_allowed`, `:1387`; extended read is gated per agent,
  `get_extended_read_enabled`, `:1327`). `Inferred`: the `music_*` tools read through the
  core service, but an agent that opens a take with `read_file` or `transcribe` needs
  `~/.dpc/music` (and any watched folder it should read) granted. Which paths are granted,
  and to which agents, is a security choice to state in the tools card.
- `core/dpc_client_core/dpc_agent/tools/music.py` (**not built**; no music module exists
  under `dpc_agent/tools`): the `music_*` `ToolEntry`s, `default_enabled=False`, autoloaded
  like `comfyui.py`. Each declares a `timeout_sec` and a wait budget consistent with
  `swap_wait_s`, in the same commit as its name (Consequences). With ADR-052 the wait is a
  lease wait: a job whose engine waits for the card is `queued` or, after S4, `running`.
- The metadata reader (N12, **built** as `ffprobe`, `music/identity.py`), and the ffmpeg
  presence check per node (**open**).
- `core/dpc_client_core/providers/llamacpp_server_provider.py` (`:1762-1784` build
  `image_url` parts only, `supports_vision` at `:1198`): audio content parts for Flamingo
  (**not built**). **Open:** the gateway (ADR-041) either carries them or the judge bypasses
  it (N14).
- `core/dpc_client_core/managers/llama_server_supervisor.py` (`_CARD_CLAIMS` at `:664`,
  typed for `LlamaServerSupervisor`), `managers/card_lease.py` and `local_api.py`: the card
  option (N11). **ADR-052:** S1 (lease registry, lease line) is built; S2 (supervisors wait
  for a busy lease, unload an idle one, `stop_local_engine` on a lease alias) is built at
  `1e57932f`, with a follow-up in the commit after `373d7b7e`; S4 (the
  music engines hold the lease; `music/engine_base.py`, `runner.py`, `jobs.py`, `commands.py`,
  `acestep.py`, `yue2.py`, `ui/src/lib/utils/musicCreate.ts`; both gates of a waiting job)
  is pending.
- `core/dpc_client_core/process_registry.py` (`SUBSYSTEMS` at `:220`, `ENGINES` at `:281`,
  the unknown-subsystem `ValueError` at `:1148`) and `dpc_agent/tools/process.py`
  (`run_supervised` at `:983`): any engine DPC starts goes through the registry (ADR-046
  point 5), under a named category with a VRAM probe (**built** for `acestep_server` and
  `yue2_server`; the library's probes run as `device_probe`).
- `core/dpc_client_core/file_server.py` (`:22-79`): not grown; N8 is a separate listener,
  `core/dpc_client_core/music/stream.py` (`9bb7599d`).
- `ui/src/lib/components/music/` (**built**; the first draft named
  `components/MusicPlayer.svelte`): `MusicPlayer.svelte`, `MusicCreate.svelte`,
  `MusicReview.svelte`, `MusicReasonBox.svelte`, `MusicLyrics.svelte`, `MusicComments.svelte`,
  `CommentMarkers.svelte`, `MusicWaveform.svelte`; `services/music.ts`, `services/musicJobs.ts`,
  `utils/musicPlayer.ts`, `utils/musicCreate.ts`. `Sidebar.svelte` (the button, label
  "MUSIC PLAYER" at `:1002`, beside AGENTS MAP at `:995`) and `utils/windowGeometry.ts`
  (reused).
- `ui/src/lib/components/ChatMessageList.svelte` (`:517`): the `audio/*` file row as a
  player (**built**, `dd446110`). `types.ts` is unchanged (D19; the union is at `:19`,
  `mime_type?` at `:25`). `VoicePlayer.svelte` (`duration` at `:46`, `:63`) receives its
  duration from N12.
- `ui/src/lib/panels/ChatPanel.svelte` (`:1201-1204`): sending a recording as audio
  (**built** for 1:1 agent chats); groups of one node plus agents only if Q3 says yes (then
  ADR-032 Part B is amended; **open**).
- `ui/package.json`: **no new UI dependency** (`Observed`: no `wavesurfer`; N13 went to
  precomputed peaks).
- `ui/src-tauri/tauri.conf.json` (`:27`): `media-src` gained `http://127.0.0.1:9996` for the
  stream; no scope change.
- `~/.dpc/music/` (library, folders, playlists, comments, peaks, jobs, `generated/<job_id>/`)
  and `get_model_files_dir() / "music" / <engine>` (D14); `<DPC home>/exports/music` for MP3
  exports (D36).
- `docs/GLOSSARY.md` (**not done**): track, take, version, watched folder, the button label,
  the tool names and the music words of ADR-052's holders (`music:yue2`, `music:acestep`),
  in the same commit as the code that introduces each.

## Implementation Status

Accepted by Mike on 2026-11-19 (08:21). **Status as of 2026-11-22**, read from the code on
`origin/dev` at `1fa73a26` (every commit named below is an ancestor of it). Built: the group-file
fix, the core music library and its stream, the window and mini-player, the lyrics chain with
Transcribe, audio into a 1:1 agent chat and into the chat player, and the first creative slice:
the job store, the two engine supervisors (YuE2 default, acestep.cpp second), the Create panel,
live jobs, the level-matched A/B with Keep / Discard and its reason dictionary, hide / remove /
delete, timecoded comments, the waveform, MP3 export, and the provenance record inside every
generated file. **Not built:** the `music_*` agent tools, QC, region edits and the seam work
(built 2026-11-25, see the B4 row), cover, `generate_similar`, audio into the gateway, SoundCloud/Yandex, the voice clone, stress
and meter preparation of lyrics (built 2026-11-25, see its row), the lineage graph, and the card lease for the engines
(ADR-052 S4). The cards are in `dpc-messenger/backlog.md` (gitignored); a card's heading there
can still say open after part of it is built, so the Status column describes the code. Cards
were filed 2026-11-19 unless a date is given. Card names are kept as filed (D19). **Tried in the
app by Mike:** the acestep.cpp run (#191), the YuE2 pairs and voices (#197-#198), the first Create
and the first export. **Not yet tried in the app:** anything marked "not tried" below, and
everything on Linux and macOS except playback.

| Task (card id) | Status | Commit |
|---|---|---|
| Epic: THE-AI-MUSIC-PLAYER-RUNS-ONLY-AS-A-SIMULATED-BROWSER-PROTOTYPE-AND-DPC-HAS-NO-PLACE-TO-HOST-IT | In progress (the card heading says in-progress) | — |
| B1: AN-AGENT-CANNOT-POST-A-FILE-INTO-ITS-OWN-CHAT-AND-THE-CHAT-HAS-NO-AUDIO-ATTACHMENT-TYPE (title kept; dated correction per D19 in the body) | Half built: an audio `file` plays in the chat and the agent's context names it (D19 note). Not built: an agent posting a file into its own chat (`chat_post_file`); Mike's 2026-11-19 07:59 call (Q1) makes it unneeded for the music flows | `dd446110`, `3837b3a5` |
| B2: NO-MUSIC-JOB-RUNNER-EXISTS-SO-A-COMMENT-OR-A-GENERATE-REQUEST-CANNOT-BECOME-A-TAKE | Slice 1 built, on `origin/dev`: the job store and capabilities report (SPEC §5 states, payloads written before send, takes with seed, `synth_batch_size` and index), job and take commands on `local_api`, the runner for `generate_from_text` (D38), the Create panel with live jobs and "Waiting for the GPU: X holds it" with "Stop X" (D37), the level-matched A/B with Keep / Discard / None of these and the reason dictionary (D32, D33), loudness by ffmpeg `ebur128`, the runner refusing instead of hanging (`f1d85057`). Stage-1 QA gates are in the next-but-one row. Left: the other job types, a seam A/B. Open review card (Zcode, #206): THE-MUSIC-JOB-PATH-TURNS-SEVERAL-RECOVERABLE-EDGE-CASES-INTO-AN-INTERNAL-ERROR | `f97a454e`, `8d716430`, `6b34b44c`, `9ccd950a`, `2208a803`, `f1d85057`, `1eca804e` |
| Engine supervision (2026-11-21): DPC-DOES-NOT-START-OR-WATCH-THE-ACESTEP-CPP-SERVER-SO-NO-MUSIC-JOB-CAN-REACH-AN-ENGINE | Built, on `origin/dev` (slice 1 step 2): `core/dpc_client_core/music/acestep.py` supervises `ace-server` on 127.0.0.1 (`[music] acestep_*` settings, `get_music_engine_status` / `start_music_engine` / `stop_music_engine`), `music/runner.py` runs `generate_from_text` to review: one `/synth` per job with `synth_batch_size` = variants, wav24 to 24-bit FLAC, sidecar, library entry. Live run on the Windows card (2 x 30 s in 16.9 s, peak 9344 MiB), **accepted by Mike (#191)**. Open: Linux and macOS have no build recipe (the engine reports `not_installed`); llama-server and Strata do not see the engine as a card holder (ADR-052 S2 and S4). Known differences from SPEC (open for Mike, not decided): `ACESTEP_OFFLOAD_TO_CPU` and `thinking off` have no acestep.cpp equivalent; the card is checked once per job, not per variant; `variant` is 1-based in names, the batch index is recorded separately (0-based) | `6b34b44c` |
| YuE2 default engine (2026-11-21, D35, Mike #198): no card yet | Built, on `origin/dev`: `music/yue2.py` supervises `yue-server` on 127.0.0.1:9994 (`[music] yue2_*` settings; `--max-batch 1`; backbone `stock` / `rs-tw`), `music/engine_base.py` is the supervisor both engines share, `music/engines.py` keeps one music engine on the card (idle one stopped as `swap`, busy one waited for or `engine_busy`), `music/runner.py` runs yue2 jobs one `/synth` per variant at batch 1 (seed and lm_seed `seed + i`, payload per variant before send, replay JSON kept as a payload, sidecar with commit, backbone sha256, adapters with source and split sha256s). Voice adapters are split by key prefix into music (AR) and sound (NAR) files under `<models>/yue2/loras/.split/`. `params.engine` defaults to `yue2`; `ace-step-1.5:text2music` and engine-less records still load as acestep. Create panel: engine, backbone and voice selects, short-cap warning. Live smoke (scratch DPC home, real `yue-server`, card free): 2 variants at a 120 s cap in 39.7 s wall, peak 5258 MiB. Mike listened to the pairs and the voices (#197-#198, 2026-11-21); the adapters are heard through the trial, not yet through the DPC window. Open: an instrumental job comes back sung (card AN-INSTRUMENTAL-JOB-ON-YUE2-COMES-BACK-SUNG-...); Linux/macOS build recipe | `ade158b7`, `ac25991d` |
| Library hide/delete (2026-11-21): THE-MUSIC-LIBRARY-HAS-NO-WAY-TO-HIDE-OR-DELETE-A-TRACK-SO-RAW-ENGINE-OUTPUTS-AND-DUPLICATES-CLUTTER-IT | Built, on `origin/dev` (D34): hide, unhide, remove missing, delete a generated file; ownership is decided by path under `~/.dpc/music/generated/`, and the person's own folders are only hidden. A Discard hides the take's library entry (D32). Awaiting Mike's try in the app | `8e134ec6`, `76a081c4`, `2ae20b01`, `8d716430` |
| MP3 export (2026-11-21, D36, Mike #210): A-TRACK-CAN-ONLY-BE-KEPT-AS-WAV-OR-FLAC-SO-THERE-IS-NO-MP3-TO-SHARE-OR-PLAY-ELSEWHERE | Built, on `origin/dev` (design [P]): `music/export.py` (one root, identity index, portable names, playlists as one level of hard links, -1 dBTP limiter checked on the decoded MP3, ID3v2.4 with `DPC_PROVENANCE`); the library scan skips exports; `export_music_track` / `export_music_playlist` / `get_music_export_info`; Export MP3 in the window with an "mp3" chip. Seen on one track by Mike (card note); tests and real ffmpeg runs on Windows only; Linux and macOS not run. Open: a tree of exported copies (LOW card) | `98001dc7` |
| Provenance inside the file (2026-11-21, Mike "да сделай"): A-GENERATED-FILE-CARRIES-NO-PROVENANCE-RECORD-SO-A-TRACK-CANNOT-BE-REPRODUCED-FROM-THE-FILE-ALONE | Built, on `origin/dev`: `music/provenance.py` builds `mp.provenance/1` per engine (genotype with the `audio_codes` or the YuE2 score and semantic tokens, substrate with weights and binaries by sha256 and the hidden genes batch size and index, phenotype, `genome_sha256` / `intent_sha256` / `genome_basis` / `render_sha256`, output chain, privacy guard, 64 KB cap with hash-reference fallback); `music/audio_tags.py` writes it once into the FLAC (`DPC_PROVENANCE`, `LYRICS`, `UNSYNCEDLYRICS`) or the WAV (`LIST/INFO` item `DPCP`, [P]) with the audio copied byte for byte and the PCM md5 checked; the runner embeds it at creation and the sidecar is the same record; the library reads it back on a scan; MP3 export carries it on; `tools/retag_music_provenance.py` re-tagged the 55 trial and smoke files on this machine. Ids are narrow (`genome_sha256` = the engine's inheritable unit with the engine id inside, `genome_basis` names it; `intent_sha256` = the text bucket; `render_sha256` = genome + substrate); records without `genome_basis` are legacy wide and summarised as `legacy_*`. Not tried in the app; Linux and macOS not run; the GPU probe is `nvidia-smi` only. Open: EVOLUTION v3 §14 items 10 and 14 (close-out of the genome, a first reader of the ids) | `06c8b20f`, `ac25991d`, `abcaaa44` |
| Comments (2026-11-21, no card of its own; SPEC §4 `mp.comment/1`): the timecoded notes of the review | Built, on `origin/dev`: four commands (`list_music_comments`, `add_music_comment`, `update_music_comment`, `delete_music_comment`), a file per track, markers on the slider, a `C` shortcut and a review list (`music/comments.py`, `MusicComments.svelte`, `CommentMarkers.svelte`). The action is `note` only; `version_id` is the constant original, as the core has no versions. The command is reachable by agents, so a track holds at most 500 comments (`MAX_COMMENTS_PER_TRACK`, error `too_many_comments`; added 2026-11-25 after Zcode's review, #206), and a merge of two entries takes the same lock as a comment write. A comment does not yet become a job (region edits are not built); the capabilities report still says comments are `planned` (Q9) | `f62f998b`, `f587ec06` |
| Waveform (2026-11-21, N13): peaks computed by ffmpeg | Built, on `origin/dev`: `get_music_track_peaks`, a cache by content hash, a waveform on the now-playing card and in the A/B review, an empty placeholder when no track is selected. No spectrogram | `0063d4be`, `c146b811`, `1eca804e`, `e2fbee26` |
| Library: THE-MUSIC-PLAYER-HAS-NO-LIBRARY-OR-PLAYLIST-SO-PLAIN-LISTENING-IS-NOT-ITS-DEFAULT-MODE | Core built, on `origin/dev` (`20dcc3f1`, 2026-11-20): `core/dpc_client_core/music/` (watched folders indexed in place and polled, N6; track identity by `track_id` plus an audio-payload hash, Q4 layers 1-3 and 5; playlists; `local_api` commands; `[music] poll_interval_s` in a fresh `config.ini`). Queue, shuffle and repeat live in the window (B3 row). Open card: the window re-reads the whole library every 10 s (THE-MUSIC-WINDOW-RE-READS-THE-WHOLE-LIBRARY-EVERY-TEN-SECONDS-AND-AUDIO-STUTTERS-ON-LINUX-DURING-PLAYBACK) | `1cdce551`, `aa61925b`, `6f797e02`, `22d73fd0`, `b712712a` |
| B3: THE-MUSIC-PLAYER-HAS-NO-DPC-COMPONENT-SO-COMMENTS-LYRICS-TAKES-AND-AB-REVIEW-EXIST-ONLY-IN-THE-PROTOTYPE | Mostly built, on `origin/dev`: the Music Player button beside AGENTS MAP, a floating window that minimises to a mini-player with its own width and place (`ui/src/lib/components/music/MusicPlayer.svelte`), comments, waveform, takes and the plain A/B (rows above), lyrics. Since `9bb7599d` any library track plays through the N8 stream. **Not built:** a region comment that becomes an edit job, a seam A/B, accepting a take as a new version `v2` (a take is Kept as a new track, not a version of the source), the version tree | `32900e82`, `8bebfb6a`, `ec79687d`, `2457aa7c`, `2208a803` |
| Byte serving (N8, D28; 2026-11-20, no card) | Built, on `origin/dev`: a loopback track stream with `Range`, a per-process token and an `Origin` allowlist, keyed by `track_id` (`core/dpc_client_core/music/stream.py`, `get_music_stream_info`, CSP `media-src http://127.0.0.1:9996`); Linux plays every track from it (no-`Range` whole-file by default, preload, wait for a duration before play), Windows and macOS the files outside `~/.dpc`. Tests plus the Linux node's log; the Linux special case is a LOW card | `9bb7599d`, `ff48a730`, `89123e1d`, `05549c35`, `012d8c0b`, `36a4777a` |
| Lyrics: A-TRACK-HAS-NO-LYRICS-CHAIN-SO-TAGS-LRC-LRCLIB-AND-TRANSCRIBE-ARE-NOT-TRIED-IN-ORDER | Built, on `origin/dev`: sidecar `.lrc`, then tags (through ffprobe: USLT yes, SYLT no), then the local LRCLIB cache, with lrclib.net only on an explicit lookup (D6 note); manual lyrics; Transcribe only when the person asks, with `provider.transcribe(..., release_if_loaded_here=True)` and provider and model recorded (`music/lyrics.py`, `MusicLyrics.svelte`). Transcribe is a card-lease client in ADR-052 S3 (pending) | `cb9af8a1`, `74c2de84`, `a94c7943` (chain); `3672ee23`, `7fd6a407` (Transcribe) |
| Transcribe quality (2026-11-20): TRANSCRIBED-LYRICS-CARRY-WHISPER-HALLUCINATIONS-AND-MERGE-LONG-SEGMENTS-INTO-ONE-LINE | Built, not heard live (CC's proposal, not a decision): `music/lyrics_filter.py` drops known credit phrases, sound tags, silence and loops and splits a segment over 7 s or 80 characters at word times (proportionally when there are none); the music call asks Whisper for word timestamps (`with_detail`); the stored lyrics carry `transcription.filter`, the unfiltered result stays in `<track_id>.transcribe-raw.json`. Not built: vocal separation (Demucs), the quiet-window check by ffmpeg astats | — |
| Stress and meter (2026-11-21, D39): A-VOCAL-JOB-SENDS-THE-AUTHORS-LYRICS-TO-THE-ENGINE-UNCHECKED-SO-SYLLABLE-METER-AND-RUSSIAN-STRESS-ARE-LEFT-TO-CHANCE | Built on `dev`, not pushed, not tried by Mike in the app (CC, 2026-11-25; the rules are CC's proposal, no ADR or SPEC text fixes the numbers): `music/lyrics_prep.py` counts syllables per line (Cyrillic vowel letters, a heuristic for Latin words), checks the meter per section (a line more than `max(2, ceil(0.25 * typical))` syllables from the median of the sung lines of its section is flagged; sections under 3 sung lines are not checked), audits the author's U+0301 marks (six kinds) and resolves the stress of Russian words from the Zaliznyak `forms.sqlite` under `<model files folder>/lyrics/stress/ru/` (one stress gets the mark, a homograph is listed and never guessed, an unknown word is counted); without the file it says `dictionary missing` and still checks meter and marks. The read-only command `prepare_music_lyrics` and the Create panel's "Prepare lyrics" (per-line syllables, meter flags, per-word status, a click on an ambiguous word picks its reading, "Use marked lyrics" with Undo) are built; the check is kept as `params.lyrics_prep` only while it describes the text and engine sent. The engine gets marks only when the person uses the marked lyrics, and only YuE2 is offered them (D39; the effect on the sung result is not established). Not built: the per-song and the person's own dictionary of the D39 chain, a second installed dictionary, a blocking pre-run gate, numbers to words, English stress, forced alignment of stress. Commits `72952177`, `6cfebf93` | `72952177`, `6cfebf93` |
| Own adapters (2026-11-21): DPC-CANNOT-TRAIN-ITS-OWN-YUE2-ADAPTERS-SO-VOICES-STYLES-AND-INSTRUMENTS-COME-ONLY-FROM-OTHERS | Not built (LOW) | — |
| Lineage graph (2026-11-21, D29): NO-LINEAGE-GRAPH-EXISTS-SO-HOW-A-SONG-EVOLVED-FROM-ITS-TAKES-AND-REJECTIONS-CANNOT-BE-SEEN | Built on `dev`, not pushed, not tried by Mike in the app (CC, 2026-11-25; the graph's shape is CC's proposal [P], EVOLUTION.md section 14 is still open): `music/lineage.py` builds `mp.lineage/1` read-only from the job and take store (and the library entry's provenance ids): nodes are the intent of a generate job, each take (engine, params summary, stage-1 QA verdict, keep / discard / pending with the reason and failure class) and a source node for an imported file an edit started from; edges are `generate` and `edit` (`op`, `region`, the takes of one edit job are siblings), and an edge into a discarded take carries the reason; events are the person's Keep (with the takes it was picked over, the A/B pick) and Discard. `validate_lineage` is the schema. The read-only command `get_music_lineage` answers it for a track or a job, and `write_music_lineage_files` writes `lineage.json` and a self-contained static `lineage.html` beside the job's generated folder on request. The UI has the Evolution card (`MusicEvolution.svelte`: an SVG branch graph, QA badge, kept / discarded / pending and the reason on rejected edges, a click plays the take); it is **not mounted in the player shell yet** (one line in `MusicPlayer.svelte`). Not built, waiting on Mike's rulings: the `prefer` event of a plain A/B without a Keep, `delta` / `N_delta` and the clone / mutate `edge_kind` on an edge, the graph joining jobs into one song beyond the edit chain, the library entry kept as `rejected`, agent notes as nodes. Commits `43deabab`, `fd9da8ce` | `43deabab`, `fd9da8ce` |
| Button title (2026-11-20): THE-MUSIC-PLAYER-BUTTON-CUTS-A-LONG-TRACK-TITLE-INSTEAD-OF-SCROLLING-IT | Open | — |
| Mini-player place (2026-11-20): THE-MINI-PLAYER-FIT-TO-A-SMALLER-SCREEN-MOVES-ITS-SAVED-PLACE-FOR-GOOD | Open | — |
| Whisper switch (2026-11-20): THE-VOICE-WHISPER-SWITCH-CAN-UNLOAD-THE-MODEL-UNDER-A-RUNNING-TRANSCRIPTION | Closed 2026-11-21 (`backlog_closed.md`): the unload is counter-aware and a providers reload hands the count over | `b796af4a`, `3b4ae1d3` |
| Whisper on the card queue (2026-11-21): WHISPER-RUNS-OUTSIDE-THE-CARD-QUEUE-STAYS-RESIDENT-AND-USES-ONE-MODEL-FOR-DICTATION-AND-SUNG-LYRICS | Step 1 built (safe unload, idle self-unload after `[local_transcription] idle_unload_s`, default 300; per-call language and task). Open: the queue and the GPU/CPU choice (ADR-052 S3), a model per use. The player's Transcribe depends on it | `b796af4a`, `69ae799c`, `b9725990` |
| Tools: DPC-AGENTS-HAVE-NO-MUSIC-TOOLS-SO-THEY-CANNOT-DRIVE-THE-PLAYER-THE-WAY-THEY-DRIVE-COMFYUI | Not built (no `dpc_agent/tools/music.py`); the service layer the tools would wrap is the `music/` package | — |
| Microphone: IN-AN-AGENT-CHAT-THE-MICROPHONE-ONLY-DICTATES-SO-A-HUMMED-MELODY-NEVER-REACHES-THE-AGENT-AS-AUDIO | Built for 1:1 agent chats, on `origin/dev` (`20dcc3f1`, 2026-11-20): a recording (Dictate or Send as audio) or a paperclip file reaches the agent as an audio `file` attachment with its path in context; length measured, picked file capped, a refusal or an unconfirmed send (180 s) gives the draft back. Groups keep ADR-032 Part B dictation (Q3) | `b5c31fcd`, `3f2c6600`, `cc1c1e87`, `d218c432` |
| Audio input: THE-LLAMA-SERVER-PROVIDER-SENDS-ONLY-IMAGES-SO-AN-AUDIO-MODEL-LIKE-MUSIC-FLAMINGO-CANNOT-BE-A-DPC-ENGINE | Built (2026-11-25, CC), not run against a model: `generate_with_audio(prompt, bytes or path)` on the llama-server provider sends one user message with an `input_audio` part `{"type": "input_audio", "input_audio": {"data": <raw base64>, "format": "wav"|"mp3"|"flac"}}` and the prompt, through the same card lease and supervisor start as vision. The shape was read in llama.cpp at the pinned tag b12110: `tools/server/server-common.cpp` `oaicompat_content_load_media` takes `input_audio.data` (or `.url`) as raw base64 or a data URI and ignores `format`; `tools/mtmd/mtmd-helper.cpp` decodes WAV, MP3 and FLAC by header (miniaudio); the request cap is cpp-httplib's default 100 MiB, so DPC refuses audio over 64 MiB. `supports_audio` reads `/props` `modalities.audio`; before a child is up the alias field `mmproj_kind` (`vision` default, `audio`, `vision+audio`) decides, so an audio projector is not reported as vision; once a child is up `/props` decides both ways (before, a `vision: false` child with an mmproj still read as vision). An image sent to an audio alias is refused where images enter (`llm_manager.query`, the gateway, the tools path). Left: the live probe and the alias (how-to below), a describe-audio agent tool (a naming question for Mike) | — |
| Stage-1 QA (2026-11-25, Mike, AI Studio group #166): A-MUSIC-TAKE-HAS-NO-STAGE-1-QA-RECORD-SO-NO-GATE-FOR-LENGTH-CLIPPING-SILENCE-OR-SEAMS-EVER-RUN | Built (design [P], not heard on a real engine take yet): `music/qa.py` measures every take the runner makes in one ffmpeg pass over the staged FLAC (`astats` for clipping, `silencedetect` for silence; integrated loudness and true peak come from the loudness record, or from ebur128 in the same pass when there is none) and stores two fields on the take: `qa`, a valid `mp.qa/1` (`job_schemas.check_qa`: every field, no extras; null when a required number could not be measured, never filled with an invented one), and `qa_report`, because `mp.qa/1` has no room for a verdict: four gates, each `pass` / `fail` / `not_measured`, and a verdict (`failed` if any gate failed, `passed` only if all four passed, else `not_measured`). Gates and starting numbers (all echoed in the report and the capabilities report): **length** (acestep: within max(2 s, 5 %) of the request; YuE2, where `duration_s` is a cap and the model ends the song itself: not over the cap by more than 2 s and not under 10 s); **clipping** (a sample at 0.999 of full scale or more; the count is the samples at the file's peak, a lower bound); **true peak** (above -1.0 dBTP fails); **silence** (below -60 dBFS for 0.5 s or more: leading over 3 s fails, a dropout of 1 s or more inside fails, the whole take silent fails, a tail over 5 s fails; **a quiet tail of 1-5 s passes**, D27's range from Mike's 2026-11-20 answer). Seams and lyrics WER are advisory entries `not_measured` and can never gate (D27; the schema refuses a `pass` or `fail` there). QA flags a take and never rejects it: SPEC §8 lets the length gate reject, stage 1 does not (the person's Keep / Discard stays final, D23: no ranking, no judge). The runner measures off the event loop and a QA failure gives a report with verdict `not_measured` and the reason, never a failed job. `get_music_take` and `list_music_takes` carry `qa`, `qa_report` and `qa_summary`; `music_check` and `music_list_takes` print "QA passed", "QA failed: <gates>" or "QA not measured"; the review panel shows that as a badge per take with the failed gates' details. Left: takes made before this have no report (shown as not measured; no backfill command), seam metrics and loudness delta wait for edit takes, lyrics WER waits for the judge, and the thresholds are guesses to tune on real takes | — |
| QC: A-TAKE-ARRIVES-WITH-NO-QUALITY-CHECK-SO-TAKES-CANNOT-BE-RANKED-BEFORE-THE-PERSONS-AB | Built (2026-11-25, CC), no judge installed: `music/qc.py` assembles the take's `mp.qc/1` (field `qc`, `job_schemas.check_qc`) when its stage-1 report is stored (`MusicJobStore.add_take`): `deterministic` is the take's `qa`, `stage1` the gate summary (a clipped take is flagged whatever a judge says), `lyrics_wer` advisory and read only from a stored unedited transcription of the same file (QC never starts Whisper), the judge `absent`. `judge_music_take {take_id}` (on demand only) sends the take to the alias in `[music] qc_judge_alias` with the flow's SPEC §8.1 rubric and parses one JSON object into the six scores, issues, reasons and confidence; an answer that does not parse, a refused call or an alias that cannot hear is a `failed` judge with the reason, and stage 1 stays. `get_music_job_qc {job_id, refresh?}` answers the records, the ranking state and the display order: ranking by `scores.overall` is on only with a judge alias and `[music] qc_ranking` true (default false; SPEC decisions 12, 26). QC never accepts, rejects or blocks Keep / Discard. `generate_from_text` is read as the `lyrics` flow (no row of its own in §8.1; CC's reading). The capabilities report names it (2026-11-25): `features.qc` supported (stage 1 on every take, the judge only on `judge_music_take`, ranking off by default), `qa.field_status.lyrics_wer` measured in `mp.qc/1` only (`take.qa` keeps null), `job_types.qc` still planned. Left: the live judge, the take card's QC block and the issue click (UI), a `qc` child job (SPEC §4; QC is folded into the take as the prototype does) | — |
| Spike: NOBODY-HAS-RUN-ACE-STEP-REPAINT-ON-AN-OWN-TRACK-SO-THE-REGION-EDIT-ENGINE-IS-UNCHOSEN | Superseded by D26 (checked live while B4 is built) | — |
| B4: REGION-EDIT-JOBS-HAVE-NO-ENGINE-SO-REGENERATE-REGION-AND-FIX-SEAM-ANSWER-UNSUPPORTED | Built on `dev`, not pushed, not run on the real engine and not tried by Mike in the app (CC, 2026-11-25; the start values below are CC's, SPEC leaves them open). acestep.cpp serves repaint through `ace-server` (engine commit 973517b: `tools/ace-server.cpp` `handle_synth` takes multipart parts `request` and `audio`; `src/pipeline-synth.cpp` `run_repaint`; `task_type: "repaint"`, `repainting_start` / `repainting_end` in seconds on a 25 Hz latent grid; `output_format: "wav32"` skips the server's peak normalisation). `music/region_edit.py`: the params and the three **edit zones**, the engine window, the splice (Forge's option A: the control outside the splice bounds, a crossfade at each edge moved to a near-zero crossing and shaped by measured coherence, equal-gain at \|corr\| >= 0.85 else equal-power, 75 ms; the take has the control's length by construction), `remove_region` (silence with fades inside the region, length kept; **cut with shift** on the person's word within 10 s of the end, lyrics re-timed), the advisory seam numbers (jump dB, click score, step over p99; null with a reason, never inf). The runner (`music/runner.py` "Region edits") runs `regenerate_region` and `fix_seam` as one multipart `/synth` with 2 takes on acestep.cpp's card lease and `remove_region` with no engine or card; each take is an `edit` take and a library entry (lineage `region_edit`), with `mp.provenance/1` naming the parent track, its take when it is one, the region and the zones (`build_edit_record`, the parent's licence inherited), and stage-1 QA in length mode `same` with the edit zone (a removal's silence expected, a repaint's silence a hole) and the seam fields filled. Capabilities: the repaint pair follows acestep.cpp, the removal needs ffmpeg only, `extend_ending` stays planned. Doors: `propose_music_job` / `check_music_job` type `edit` (web track `listen_only_source`, unhashed `track_not_ready`, the region comment made by the door), `music_propose_job` type `edit` (the card names the region), the comment form's "Propose edit", the review's region line and "Play region". Owed: the live check on an own track (decision 28), the seam control pair (no owner), `loudness_delta_lu` (still null) | `f776a6b8` |
| YuE2 cover: DPC-CANNOT-CALL-AN-MCP-SERVER-SO-YUE2-STUDIO-COVER-AND-LYRICS-TO-SONG-HAVE-NO-WAY-IN (scope now HTTP/CLI, D13; the engine is yue2.cpp, not YuE2-Studio, so no MCP) | Not built: `cover` is `planned`; DPC does not run the yue2.cpp score step (SheetSage2) | — |
| Clone: NO-SINGING-VOICE-CLONE-IS-CHOSEN-SO-A-SONG-CANNOT-BE-SUNG-IN-THE-MUSICIANS-OWN-VOICE | Research | — |
| Streaming: THE-MUSIC-PLAYER-CANNOT-PLAY-OR-SAVE-SOUNDCLOUD-AND-YANDEX-MUSIC-TRACKS-SO-LISTENER-MODE-IS-LOCAL-ONLY | Postponed (Mike, 2026-11-20: later, no rush); the `yt-dlp` path is not started | — |
| Group file, agents-only (bug, MEDIUM): A-FILE-SENT-TO-A-GROUP-WITH-ONLY-AGENTS-VANISHES-AND-THE-UI-IS-TOLD-SUCCESS | Fixed on Mike's verb (07:58), observed live by Mike after a restart and closed 2026-11-19; follow-up: one message for a file and its text, audio plays inline | `3837b3a5`, `3f2de3c7`, `dd446110` |
| Group file (dependency, 2026-08-11): GROUP-IMAGE-AND-FILE-ECHO-STILL-WAITS-ON-THE-TRANSFER (ADR-032 phase 3) | Files half fixed by `3837b3a5` | `3837b3a5` |
| GPU (dependency, 2026-11-06): EXTERNAL-GPU-JOBS-CANNOT-JOIN-THE-CARD-QUEUE-SO-AN-IDLE-ENGINE-KEEPS-THE-CARD-THEY-NEED | Answered by ADR-052 (the card lease, `proposed`): S1 built; S2 built at `1e57932f`; S3, S4 pending. Mike's #20 and #70 are the decisions it rests on; the engines no longer wait for a decision on the option (N11) | `53a2eaf5`, `4d0fa885` (S1); `1e57932f` (S2) |
| Whisper residency (related, 2026-08-20): THE-WHISPER-ALIAS-IS-THE-BACKENDS-LAST-STANDING-CUDA-RESIDENT | Open (HIGH); the idle self-unload of `b796af4a` is the first half, the lease is S3. Bears on swaps per run | — |
| Prototype and spec (ai-studio `experiments/music-player/`) | Exists; the prototype is simulated; the SPEC, EVOLUTION v3 and PROVENANCE are the product record. ai-studio has no remote | — |
| Linux track stream (2026-11-21): THE-LINUX-TRACK-STREAM-KEEPS-A-NO-RANGE-SPECIAL-CASE-THAT-THE-DURATION-BEFORE-PLAY-FIX-MAY-HAVE-MADE-UNNECESSARY | Open (LOW); a revert is proposed, not approved | — |
| Linux track switch (2026-11-21): SWITCHING-TRACKS-QUICKLY-ON-LINUX-GIVES-MEDIA-ERROR-4-BECAUSE-THE-PLAYER-REUSES-THE-AUDIO-ELEMENT-WITHOUT-A-RESET | Open | — |
| Synced lyrics (2026-11-21): A-SYNCED-ID3-LYRICS-FRAME-IS-NOT-READ-SO-A-FILE-WITH-ONLY-SYLT-LYRICS-SHOWS-NONE | Open (LOW) | — |
| Exports tree (2026-11-21): A-TRACKS-EXPORTED-COPIES-SHOW-ONLY-AS-A-CHIP-AND-A-ONE-TIME-NOTICE-NOT-AS-A-TREE-OF-VERSIONS | Open (LOW), Mike's wish | — |


### How to add the Music Flamingo alias (for the live probe, 2026-11-25)

Not done by the build: nothing was downloaded and nothing ran on the card. The model is NVIDIA's
`music-flamingo-hf` under an NVIDIA **non-commercial** licence: research use in this project
only, never for anything published or sold. Stock llama.cpp reads it at the pinned b12110
(projector type `musicflamingo` in `tools/mtmd/clip-impl.h`).

1. Download from `mradermacher/music-flamingo-hf-GGUF` the `Q8_0` model file (about 7.5 GiB) and
   the projector (`mmproj`, about 0.7 GiB) into the model files folder.
2. Add a `llamacpp_server` alias in `providers.json` with the two paths and `"mmproj_kind": "audio"`,
   for example `{"alias": "music_flamingo", "type": "llamacpp_server", "gguf_path": "<model files
   folder>/music-flamingo-hf.Q8_0.gguf", "mmproj": "<model files folder>/music-flamingo-hf.mmproj-Q8_0.gguf",
   "mmproj_kind": "audio", "n_ctx": 16384}` (file names as the repository spells them).
3. Probe once: `/props` should answer `modalities.audio: true`, `vision: false`; a caption request
   for one WAV through `generate_with_audio` should answer text. Record the prompt tokens per minute of
   audio (the window `n_ctx` must hold the take) and the wall time on the card.
4. To use it as the QC judge set `[music] qc_judge_alias = music_flamingo`; leave `[music] qc_ranking`
   off until Mike decides (SPEC decision 12).

## Open Questions

Each item is Mike's to decide. Reviewer positions are arguments, not decisions.

- **Q1. The accept rule.** Is "an agent accepts a take only on the person's request"
  (`music_accept_take` refusing with `needs_person`) Mike's decision? SPEC §6 marks it
  [P].
  - Zcode: yes, make it a decision; it follows the driver "The person decides".
  - Forge: yes; publication already works this way.
  - Ark: yes, and enforce it **in the tool** (`music_accept_take` refuses), not only in
    the UI, so the rule is written once.
  - CC recommends: yes, enforced in the tool.
  - **Decided by Mike (2026-11-19 07:55), simpler than all of the above:** no approval
    machinery and no accept tool for agents. Mike's flow: he attaches audio in a chat (group
    or private), tags an agent that has the music tools and writes "make one in the same
    style"; the agent proposes the job, waits, ranks the takes by QC and posts the result with
    `chat_post_file`. Generated results are **new tracks** (Generated list) and need no
    accept. Only an edit of an existing track produces a **version**, and fixing a version is
    the person's own action (a button in the player or in the chat card). The `music_*`
    family therefore has no `music_accept_take`/`music_reject_take` for agents.
  - **Decided by Mike (2026-11-19 07:59): results go to a folder, not into the chat.** The
    agent writes finished tracks under `~/.dpc/music/` (proposed: `generated/<job>/`), which
    is part of the library and shows in the player at once; in the chat it reports in plain
    text ("done, 3 variants in Generated", optionally an Open-in-player link). `chat_post_file`
    is therefore not needed by the music flows and B1 stops being a Phase 0 prerequisite; the
    generic mechanism stays a separate card for other file types. The agent needs write access
    to `~/.dpc/music` through its firewall profile's `sandbox_extensions`.
  - *Amended 2026-11-24 (Mike #46, #53-#57; Zcode #51): `chat_post_file` is needed after
    all, for the music flows: the agent posts a take into a 1:1 chat when the person asks
    (D40, D41, D50). The agent gets a take by `take_id` and no directory access, so the
    `sandbox_extensions` entry above is not needed by the tools (D44). Built at `29383864` and
    `e3b0ea6c`, not observed live.*
- **Q2. Phase 0.** Go or no-go on the Phase 0 contents (N1), and whether the group-file
  fix (A-FILE-SENT-TO-A-GROUP-WITH-ONLY-AGENTS-VANISHES-AND-THE-UI-IS-TOLD-SUCCESS, the
  agents-only half of ADR-032 phase 3) joins Phase 0.
  - Zcode: go, and include the fix: it is small, mirrors `send_group_image`, and is a
    data-loss bug regardless of the player.
  - Forge: include it; without it the musician mode does not work in this chat at all.
  - Ark: did not vote on Q2; lists it among Mike's verbs.
  - CC recommends: include the fix. — @Mike
  - *2026-11-22: settled in practice, no single recorded "go".* The group-file fix went in on
    Mike's verb (2026-11-19 07:58, `3837b3a5`); Phase 0 and the listener part of Phase 1 were
    built after the acceptance (08:21); the creative slice was ordered step by step by Mike
    (engine first, #192: YuE2, then the window). What was never put to him is the phase plan
    as such (N1).
- **Q3. "In the chat" (D9).** Does it include group chats with one node plus agents, like
  AI Studio? If yes, ADR-032 Part B needs an amendment: the dictation branch for groups
  with one node or fewer (`ChatPanel.svelte:987-993`) gets a "send as audio" choice, and
  the microphone card's scope widens beyond `agent_*` chats. Both walls were hit live on
  2026-11-19: the mp3 at 12:34:49 and the microphone.
  - Zcode, Forge, Ark: all three yes. Ark: without it D9 holds nowhere except a private
    agent chat. Forge: otherwise D9 is false in the one group "we live in".
  - CC recommends: yes, with the ADR-032 amendment written as its own record or
    amendment. — @Mike
  - *2026-11-22: unchanged and still open.* A group of one node plus agents still routes the
    microphone to dictation (`ChatPanel.svelte:1201-1204`); the file half works (the
    agents-only group fix), the microphone half does not.
- **Q4. Track identity.** SHA-256 over the whole file changes when a tag is edited.
  `Reported` (Forge, measured on the "Куртка" file, 4 321 302 bytes): a `-c copy` retag
  of title and artist changed the whole-file hash (`1B2DBB4C` → `DC4D3B6F`), while the
  audio payload extracted with `-map 0:a -c copy -f mp3 -id3v2_version 0` hashed the same
  before and after (`fe96b6cf`, 4 321 152 bytes); a whole-file hash took 0.086 s. Ark
  adds that the file already carries generator-written ID3v2.4 tags, and that Mike's
  library lives outside `~/.dpc` (the log path is `C:\Users\mikha\Music\…`). Zcode: this
  cannot wait, because saving lyrics to tags (D6) would detach comments on every save.
  The options:
  - (a) **audio-payload hash** (Forge): stable across a retag; needs a recipe per
    container format plus a whole-file fallback; costs one ffmpeg spawn per file (N12);
    does not survive a transcode (that is Chromaprint, deferred);
  - (b) **hash at first discovery plus an explicit relink event**, and **never rewrite
    tags in watched folders outside `~/.dpc/music`**, only a sidecar `.lrc` (Ark). This is
    a sovereignty point: tag edits there change the person's originals;
  - (c) **a stable `track_id` plus a list of known hashes**, with a fuzzy rematch (same
    folder, duration ±2 s, similar size) confirmed by the person (Zcode).
  - CC recommends: (a) plus (b)'s never-rewrite-tags rule.
  - **Decided by Mike (2026-11-19 07:43, "ПО #127 ок", on CC's industry survey):** a layered
    identity. Survey: most players key on path plus tag IDs (MusicBrainz MBIDs or an
    album/disc/track/title tuple: Picard, beets, Navidrome, Lyrion, iTunes, MusicBee,
    Jellyfin); Plex adds a partial file hash; Roon keys on the audio content ("File
    signatures are now computed based on audio content instead of the contents of the whole
    file", Roon 1.1 build 69 release notes, `Observed`); cross-encode matching is an acoustic
    fingerprint (Chromaprint, LGPL-2.1, `fpcalc` local, AcoustID lookup online and not used).
    We cannot rely on MBIDs because watched folders are never written to and downloads or
    generated tracks have none, so the audio key is justified. Layers:
    1. a stable internal `track_id` (UUID) owns comments, versions, playlist positions and
       statistics;
    2. path plus size plus mtime, checked on every scan without ffmpeg;
    3. an audio-payload hash, computed only for a new path or a changed file: the FLAC
       STREAMINFO MD5 when non-zero, otherwise SHA-256 over a stream copy
       (`ffmpeg -i f -map 0:a -c:a copy -f hash -hash sha256 -`; `Observed` by CC on
       2026-11-19: a title/artist retag changed the whole-file hash and left this one
       unchanged); a match relinks automatically;
    4. a local Chromaprint fingerprint, later and optional, that only suggests links;
    5. missing tracks are kept, not deleted (Navidrome's model); a match on layer 4 or on
       metadata alone asks the person before relinking.
    Tags in watched folders outside `~/.dpc/music` are never rewritten; lyrics go to a
    sidecar `.lrc`. Still to test before code: an APE-tagged file and a remux.
    *Built 2026-11-19/20 (`1cdce551`, `6f797e02`; `music/identity.py`, `music/library.py`):
    layers 1, 2, 3 and 5; layer 4 (Chromaprint) is not built. A generated take is written once
    and its tags are DPC's own (`DPC_PROVENANCE`), outside the "never rewritten" rule, which
    covers the person's folders; re-tagging a generated file leaves its track identity
    unchanged (checked on the 55 re-tagged files, PROVENANCE §7). Without ffmpeg the hash
    falls back to the whole file (N12). The APE and remux cases were not tested.*
- **Q5. Names.** Under the vocabulary rule, before code, with GLOSSARY rows in the same
  commit:
  - the button label (Mike said "Music Player");
  - the core folder name `music/` (no objection from Forge);
  - the domain words **track**, **take**, **version** and **watched folder** (Ark);
  - the tool for posting into a chat: **decided by Mike (2026-11-19 07:32): `chat_post_file`**,
    not `music_post_to_chat`, "because the same mechanism will be used for other file
    types too" (Forge's proposal); `music_` stays reserved for domain actions;
  - each `music_*` tool's timeout and wait budget, declared with its name (Ark;
    Consequences).
  — @Mike
  *2026-11-24: the tool names are chosen: ten, listed in D42 with the timeout and the wait
  budget of each (the figures not yet chosen are marked "value set at build, recorded here");
  `chat_post_file` is needed by the music flows after all (D40, D41). Built at `29383864` and
  `e3b0ea6c` with the figures now recorded in D42; not observed live.*
  *2026-11-22: the button label is "MUSIC PLAYER" (`Sidebar.svelte:1002`), the folder
  `music/` and the package `music/` are in use; no tool exists, so no tool name was chosen
  beyond `chat_post_file` (not needed by the music flows, Q1); the domain words have no
  GLOSSARY rows (Q9).*
- **Q6. Streaming card.** Filed (`backlog.md:132`); Forge votes to keep it. Still open:
  the outbound path (N9) and account handling, stated as security choices in that card.
  — CC, on Mike's verb
  *Implementation note (2026-11-20):* SoundCloud/Yandex through `yt-dlp` is postponed
  (Mike: later, no rush); nothing is built and `yt-dlp` is not a core dependency.
- **Q7. Axis.** Forge suggests `honesty` instead of `network`: the record's honesty rules
  ("`unsupported` with its reason", never a simulated result, WER as advisory) fit it, and
  two axes are the limit. **Decided by Mike (2026-11-19 07:39): "more from personal to
  collective"**, so the front matter is now `collective, knowledge` (CC kept `knowledge`
  as the second axis for the agent working on the library; `network` dropped).
- **Q8. The ADR number.** The parked draft
  `ideas/parked/adr-051-draft-group-extraction-target-2026-11-15.md` is older and also
  says `adr: 051`; `build.py --check` cannot see it (Consequences). Zcode: drop the
  number from the parked draft now. Ark: give it 052 or the free 049, and record the
  reservation. Note on 049: it is not only a backlog claim. `Observed`: the backlog plans
  "ADR-049: the audience field" (`backlog.md:1166`), and ADR-048 (`048-…md:187`, "team
  knowledge is ADR-049's store") and `docs/GLOSSARY.md:88` already cite ADR-049 as a
  record (the chain audit at `backlog.md:1174` says the same). Giving 049 to the parked
  draft would make those citations land on a different subject. CC recommends 052 for
  the parked draft, if Mike agrees; this file is not touched. **Mike (2026-11-19 07:40):
  the parked draft will be renumbered later**; nothing is changed now, and whoever revives
  it gives it a free number (052 recommended).
  *2026-11-22: 052 is taken (the card lease), so that recommendation is void. `docs/decisions`
  holds 048, 050, 051 and 052; 049 is cited as a planned record (above) and 053 is free. The
  parked file still says `adr: 051`.*

- **Q9. Where the code, SPEC, EVOLUTION, GLOSSARY and this record disagree** (found in the
  2026-11-22 revision; CC did not guess, each needs Mike or the owner of the file). None is a
  decision of Mike's.
  - **Closed 2026-11-22** (ai-studio `067523f`, dpc-messenger fix commit of the same day): SPEC
    decision 32 now carries Mike #70's amendment (music swaps an idle server when its queue is
    empty; ADR-052) and says what the code does: one card read per job, and a failed read
    treated as free, which is not intended and S4 replaces. **Still open inside it:** once per
    job versus before each variant; neither this record nor ADR-052 decides it, and SPEC §5 and §6
    still say "before each variant". SPEC decision 30 now says N8 is built (`9bb7599d`).
    The capabilities report no longer says comments are not built:
    `_EDIT_ACTIONS["note"]` is `supported` (the comment action of `mp.comment/1`; the other four
    edit actions stay `planned`), pinned by
    `test_the_music_capabilities_report_claims_no_engine_that_is_not_built.py`. PROVENANCE §7
    and EVOLUTION v3 §14 item 10 say `abcaaa44` is on `origin/dev`. SPEC decision 40 records
    Mike #198 (в), "да" to voice adapters in renders (2026-11-21 04:03 UTC); "none by default"
    stays CC's [P].
  - **D36's date.** This record and SPEC decision 41 date Mike's MP3 ask 2026-11-22 and cite
    #210; in session A #210 is 2026-11-21 06:49Z, and the backlog card dates the first ask
    2026-11-22. The other entries here use the SPEC's Bangkok date. One of the dates is a day
    off; not resolved.
  - **GLOSSARY.** No rows for **track**, **take**, **version**, **watched folder**, **genome**
    or the `mp.*` records, though Consequences promised them in the same commit as the code
    (Ark). The glossary's **engine** means the program a local LLM provider answers with; the
    music engines need their own row or a qualifier (this record now says "music engine").
    GLOSSARY is not edited here.
  - **Status.** `accepted` stays. The lifecycle makes `implemented` a claim that the decision
    is built; the tools, QC, region edits, the audio input, the voice clone, SoundCloud/Yandex
    and the card lease are not. Mike decides when this record is `implemented`, or whether the
    unbuilt parts move to their own records.
  - **ADR-052 is `proposed` and `depends_on` this record.** The card part of D18 and N11 now
    leans on it, so this record lists it as `related`, not `depends_on`, to avoid a cycle.
    If Mike rejects or reshapes ADR-052, D18, D37 and N11 return to being this record's.
  - **EVOLUTION v3 §14 items 10-15** (genome close-out, the reason dictionary against
    measurement rule 2, failure-class meanings, YuE2 edge rules, the first reader of the ids,
    comments against selection events) were found in the fold-in and not put to Mike; D29
    still lists the style battery, graph per song, agent notes and the `N_delta` warning as
    open.

## Authors

- **Mike**: Decision (D1-D39; the specifics CC proposed inside D29-D39 are marked [P] and are
  not his)
- **CC**: Spec, prototype, cards, this draft, its revision after review, the status
  rewrites of 2026-11-20 to 2026-11-22 and the whole-document revision of 2026-11-22
  (against the code at `1fa73a26`, the SPEC, EVOLUTION v3, PROVENANCE and ADR-052)
- **Zcode, Forge, Ark** (reviews, 2026-11-19 06:03, 06:04 and 06:33, AI Studio group
  chat): Zcode re-checked the code references by subagent at `95acf009`, found the D5
  over-promise, the number collision and the stale streaming row, and proposed option (c)
  for Q4 and the LRCLIB cache. Forge measured the payload hash for Q4, corrected D14,
  found the `list_model_files` overlap, the registry category for N11, the QC control
  pair, `swap_wait_s = 1800`, the `blob:` option for N8 and the peaks alternative, and
  reported the weights and disk figures. Ark corrected the working-tree claim and the log
  path, and added the four Scope items (`context.py` and agent file access, the metadata
  source, the tool timeouts), the 9998 server, the gateway, the swaps per run, the card
  name policy and option (b) for Q4.
- **Warren, Johnny**: Review in the thread before the draft; Zcode also proposed spiking
  the GPU card and audio input first.

## References

- `ai-studio/experiments/music-player/SPEC.md`: §1 goal and modes, §3 flows 22-27, §6
  contract and "One service layer, two front doors", §7 and §7.1 engines, §10 GPU, §11 DPC
  integration, §12 decisions, §13 risks.
- `ai-studio/experiments/music-player/docs/dpc-integration.html` (phase plan, commands
  and tools, on-disk layout) and `docs/flow.html` (Order A versus Order B).
- `ai-studio/experiments/music-player/prototype/index.html`: the simulated harness
  (`sampleRate: 22050` at `:881`).
- [ADR-032](032-group-attachment-local-first-and-voice-modality.md): local-first group
  attachments (phase 3, files, deferred) and voice routing by node count.
- [ADR-040](040-local-inference-second-path-as-a-dpc-owned-llama-server.md): the
  `llama-server` child that hosts Music Flamingo.
- [ADR-041](041-openai-compatible-gateway-as-a-dpc-surface.md): the gateway; its doors
  refuse `input_audio` today (N14).
- [ADR-044](044-strata-as-a-second-supervised-local-engine.md): Strata, the card queue
  and the idle swap (amendment 2026-11-02).
- [ADR-046](046-the-backend-owns-every-process-it-starts.md): every process DPC starts
  goes through the registry, which covers any engine the service launches.
- [ADR-047](047-multi-source-file-transfer-and-peer-file-search.md): content-addressed
  file transfer, relevant if a library or its takes are ever shared between nodes.
- [ADR-052](052-the-card-lease-lets-every-gpu-client-in-dpc-hold-wait-for-and-yield-the-one-card.md):
  the card lease. The music engines are lease clients in S4, the player's Transcribe in S3,
  music swaps an idle server by Mike's #70 (amending SPEC decision 32), and `stop_local_engine`
  on a lease alias and the `card_wait.holders` schema are S2 / S4 items (D18, D37, N11).
- `docs/GLOSSARY.md`: **engine**, **card hold**, **card lease**, **local engine swap**,
  **decision status**.
- `ai-studio/experiments/music-player/docs/EVOLUTION.md` (v3, 2026-11-22),
  `docs/PROVENANCE.md` (§7, built at `abcaaa44`), `docs/RUACH-BORROW.md` and
  `engine/yue2-cpp/trial.md` (the YuE2 trial of 2026-11-21): the single home of the
  evolution model, the record inside the file, what is borrowed from Ruach Studio, and the
  measurements behind D35.
- `~/.dpc/logs/dpc-client.log.1:75624-75625`: the silent group-file send of 2026-11-19.
- `~/.dpc/config.ini:181`: `swap_wait_s = 1800` on this node.
- AI Studio group chat, 2026-11-19 06:03-06:33: the three reviews of this record.
