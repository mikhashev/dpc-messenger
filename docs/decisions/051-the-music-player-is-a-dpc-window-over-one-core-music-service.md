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
related: [ADR-032, ADR-041, ADR-047]
supersedes: []
session: "AI Studio group chat (group-60aff6110f02), 2026-11-19 03:17-05:33 — epic THE-AI-MUSIC-PLAYER-RUNS-ONLY-AS-A-SIMULATED-BROWSER-PROTOTYPE-AND-DPC-HAS-NO-PLACE-TO-HOST-IT; Mike asked for this ADR at 05:31; reviews by Zcode, Forge and Ark at 06:03-06:33"
---

# ADR-051 — The music player is a DPC window over one core music service

> **Proposed 2026-11-19, accepted; Phase 0 and the listener part of Phase 1 are built on dev
> (2026-11-20, see Implementation Status).** Mike asked for this record at
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
> **How claims are marked.** `Observed` means read by CC on `dpc-messenger` branch `dev`
> at `2d333000` (HEAD, 2026-11-19 13:07 local), or in a log or a config file with the
> place given. `2d333000` sits one commit above `95acf009`, which the reviewers read, and
> that commit changes only `docs/decisions/044-…md`, so every code line the reviewers
> cited at `95acf009` is the same line at `2d333000`. **The working tree is not clean**:
> on top of `2d333000` there is this ADR (untracked) and a regenerated `ROADMAP.md`
> (modified, the axis table now lists ADR-051). Every `path:line` in this file is at
> `2d333000` unless it says otherwise; line numbers will drift. `Inferred` is reasoning
> not run. `Reported` is stated by an agent in the thread and not re-checked here.
>
> **Axis `knowledge, network`**, the same as the epic. `knowledge` because the player is a
> place where a person and an agent work on one track: a comment becomes a job, the job
> returns takes, and the person accepts one. `network` because every engine is local
> compute on the one 32 GB card that Strata also uses, and the swap is how they share it.
> Forge suggests `honesty` in place of `network` (Q7); the field is unchanged until Mike
> decides.

## Context and Problem Statement

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
is committed in ai-studio: `experiments/music-player/` is untracked at `f341db1`. None of
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
  existing swap (ADR-044, amendment of 2026-11-02) is how the card changes hands.
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
    ComfyUI graph or MCP (Decided D12-D13). `Reported` (Zcode): YuE2-Studio documents a
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
    CLI;
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
  *Implementation note (2026-11-20): built on `dev` in `9bb7599d`, pushed (on `origin/dev`, checked 2026-11-21); checked by
  tests, not yet in the Linux window.* Asked for after the Linux node measured the `blob:`
  path as a live stream (`loadedmetadata duration=0.00`, `seekable` only what had played).
  See the N8 entry for the shape.

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

- **D30. DPC supervises the acestep.cpp server** (2026-11-21, #159 (a), 19:33; SPEC §12
  decision 31). Asked by CC: "does DPC itself manage the acestep.cpp server?" Mike: "да, мы
  же обсуждали уже это" (yes, we already discussed this). The engine is acestep.cpp (Mike's
  choice 2026-11-20, built from source, recipe `experiments/music-player/engine/acestep-cpp/BUILD.md`
  in ai-studio). DPC starts, stops and watches the server, takes models from
  `~/.dpc/models/music/ace-step/` (D14) and binds it to loopback. This replaces the earlier
  "Mike starts ACE-Step himself, DPC only connects" (2026-11-20 09:56). The settings keys,
  port and restart policy are CC's proposal, not Mike's. Backlog:
  DPC-DOES-NOT-START-OR-WATCH-THE-ACESTEP-CPP-SERVER-SO-NO-MUSIC-JOB-CAN-REACH-AN-ENGINE.

- **D31. Slice 1 engine settings and output format** (SPEC §12 decision 34; defaults accepted
  by Mike 2026-11-20 09:56, engine line rewritten 2026-11-21). acestep.cpp turbo, Q8, 8 steps.
  The engine writes **wav24** (it has no FLAC) and DPC converts it to FLAC for the library.
  **The wav24 + FLAC plan is the group's consensus after Mike chose acestep.cpp, not a
  recorded word of Mike's.** DPC records `synth_batch_size` and the batch index per take: a
  measurement of 2026-11-21 showed the batch size changes the audio for the same seed
  (PROVENANCE section 2). It replaces the earlier "ACE-Step API, `audio_format: flac`".

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

- **D33. Blind A/B is not in slice 1** (2026-11-21, #159 (c); SPEC §12 decision 37). CC
  proposed "later; slice 1 gets a plain A/B with level matching". Mike: "ок позже" (ok,
  later). Slice 1 has the plain, level-matched A/B; blind A/B comes in a later slice.

- **D34. The library needs hide and delete** (2026-11-21, #159; SPEC §12 decision 38). **The
  requirement is Mike's:** raw engine outputs duplicated tracks in the library; he wrote
  "это плохо что есть такое, так же нужна возможность как то удалять или скрывать потом
  файлы сами (уже в UI я вижу очень много файлов лишних)" (it is bad that this exists; a way
  to delete or hide files myself is also needed; the UI already shows many extra files).
  **The specifics are CC's proposal, not decisions:** Hide is reversible and leaves the file
  untouched; Remove from library applies only to entries whose file is missing; Delete file
  applies only to files under `~/.dpc/music/generated/`; the person's own folders are never
  deleted from, only hidden. Being built; backlog card
  THE-MUSIC-LIBRARY-HAS-NO-WAY-TO-HIDE-OR-DELETE-A-TRACK-SO-RAW-ENGINE-OUTPUTS-AND-DUPLICATES-CLUTTER-IT.

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
  export only, verified on the decoded MP3 (the policy is Mike's call, EVOLUTION §14 item 6);
  provenance in ID3v2.4 per PROVENANCE §3, with `cmd_as_sent` reduced to file names (rule 3).

  *Numbering: ADR D30-D36 mirror SPEC §12 decisions 31, 34, 35, 37, 38, 40, 41 in that order. SPEC
  decisions 32 (GPU card), 33 (song from own text) and 39 (stress dictionary) have no ADR entry
  yet.*

### Not decided

Each item needs Mike's yes or a measurement. Where an item is built in practice, a dated
implementation note under it says so (N1, N6); the rest are not built.

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

  *Implementation note (2026-11-20):* the core library, the window and mini-player, the
  lyrics chain with Transcribe, and audio into a 1:1 agent chat are built on local `dev`
  (Implementation Status). SoundCloud/Yandex is postponed (Q6). Phase 2 is not started;
  the GPU card (EXTERNAL-GPU-JOBS-CANNOT-JOIN-THE-CARD-QUEUE-SO-AN-IDLE-ENGINE-KEEPS-THE-CARD-THEY-NEED,
  N11) is what unblocks it.
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
- **N12. A metadata source** (Ark). `Observed`: `VoicePlayer` takes a required `duration:
  number` prop (`VoicePlayer.svelte:41`), and a `file` attachment carries no duration.
  `ffprobe` is called nowhere in `dpc_client_core`; `ffmpeg` is called only by
  `comfyui_convert` (`dpc_agent/tools/comfyui.py:932`). D19's "a `file` with `audio/*`
  plays" therefore needs core to read duration, sample rate and channels, either through
  `ffprobe` or through a pure-Python reader (licence to check; possibly the N7 library).
  `Reported`: ffmpeg 8.1.2 (winget) is on this node (Forge's measurement, relayed by Ark);
  on how many other nodes ffmpeg is present is unknown. The same choice decides what the
  payload hash of Q4 (a) costs.
- **N13. Waveform rendering.** (A) `wavesurfer.js` 7.12.12 bundled by Vite (BSD-3-Clause,
  Prior art), with `sampleRate: 22050` for the spectrogram; (B) peaks precomputed by
  ffmpeg into JSON, cached beside the file hash and drawn as a static wave (Forge's
  preference: no DSP library in the webview, cheap on a large library). Recorded so the
  choice is not made silently by installing a package.
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
  API server is asynchronous submit and poll, and YuE2-Studio has `/synth` and `/job`.
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
  - Every engine is a GPU process beside Strata. Until the card option (N11) is built,
    Strata is stopped and restarted by hand, and a job waits in `queued` with a reason.
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
    them (N1).
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
    tool names of Q5.
  - ROADMAP's axis table is generated (`tools/backlog/build.py --roadmap`). The working
    tree's `ROADMAP.md` already lists this record under `knowledge` and `network`.
  - **ADR number.** A parked draft,
    `ideas/parked/adr-051-draft-group-extraction-target-2026-11-15.md` (2026-11-15, older
    than this record), also declares `adr: 051`. `build.py` reads front matter only from
    `docs/decisions/[0-9]*.md` (`tools/backlog/build.py:432-438`), so the collision is
    invisible to `--check` (Ark). Renumbering is Mike's call (Q8).

### Risk: a file sent to a group of agents is dropped silently

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
- [ ] **Runner (B2).** One `generate_similar` job with 2 variants returns 2 `new_track`
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
- [ ] **GPU (D18, N11).** A music engine is registered under a named process category and
  has a VRAM row in the registry. A music job on a card held by an idle Strata leads to
  `stopped` / `swap` in the process registry, the job runs, and Strata comes back on the
  next demand. The run is timed against `swap_wait_s` (1800 s on this node).
- [ ] **Agents-only group (precondition).** An audio file sent to a group whose only node
  is the sender lands in its history and `files/`, and an agent can read its path. A send
  that reaches nobody does not report bare `success`.

## Scope

New code and files touched. Paths are relative to `dpc-client/`.

- `core/dpc_client_core/music/` (new; the name is proposed in `docs/dpc-integration.html`):
  the core service (library, folders and hashing, playlists, jobs, takes, lyrics with a
  local LRCLIB cache) and the engine adapters. Adapters resolve weights through
  `get_model_files_dir()` (D14).
- `core/dpc_client_core/service.py`: `local_api` commands as `async def`s on
  `CoreService`, dispatched by `getattr` (`local_api.py:689`). Also `send_group_file`
  (`:7882`) for the group-file card, and `list_model_files` (`:11232`) if N15 excludes
  `music/`.
- `core/dpc_client_core/dpc_agent/context.py` (Ark): `history_body` (`:864`) adds markers
  only from `image_attachment_markers` (`:877`, `:895-913`), which skips every
  non-image attachment. Every confirmation of the form "the agent names or reads the
  path" (B1, microphone, agents-only group) needs a marker for audio `file` attachments.
- **Agent file access to `~/.dpc/music`** (Ark). `Observed`: an agent's paths outside its
  own sandbox are granted per profile through `sandbox_extensions` `read_only` /
  `read_write` in the firewall (`firewall.py:1222-1225`; checked by
  `is_extended_path_allowed`, `:1386`; extended read is gated per agent,
  `get_extended_read_enabled`, `:1326`). The `context.py` docstring (`:895-913`) notes
  that no default grants read access to the conversations folder. `Inferred`: the
  `music_*` tools read through the core service, but an agent that opens a take with
  `read_file` or `transcribe` needs `~/.dpc/music` (and any watched folder it should
  read) granted. Which paths are granted, and to which agents, is a security choice to
  state in the tools card.
- `core/dpc_client_core/dpc_agent/tools/music.py` (new): the `music_*` `ToolEntry`s,
  `default_enabled=False`, autoloaded like `comfyui.py`. Each declares a `timeout_sec`
  and a wait budget consistent with `swap_wait_s`, in the same commit as its name
  (Consequences).
- A metadata reader (N12): `ffprobe` or a pure-Python library, its licence, and the
  ffmpeg presence check per node.
- `core/dpc_client_core/providers/llamacpp_server_provider.py` (`:1761-1790`,
  `supports_vision` at `:1198`): audio content parts for Flamingo. **Open:** the gateway
  (ADR-041) either carries them or the judge bypasses it (N14).
- `core/dpc_client_core/managers/llama_server_supervisor.py` (`_CARD_CLAIMS` at `:657`,
  typed for `LlamaServerSupervisor`) and `local_api.py`: the GPU card option (N11).
- `core/dpc_client_core/process_registry.py` (`SUBSYSTEMS` at `:218`, `ENGINES` at
  `:277`, the unknown-subsystem `ValueError` at `:1145`) and `dpc_agent/tools/process.py`
  (`run_supervised` at `:983`): any engine DPC starts goes through the registry (ADR-046
  point 5), under a named category with a VRAM probe (N11).
- `core/dpc_client_core/file_server.py` (`:22-79`): only if N8 chooses to grow it
  (`Range`, a token, the folders it may serve). *2026-11-20: not grown; N8 is a separate
  listener, `core/dpc_client_core/music/stream.py` (`9bb7599d`).*
- `ui/src/lib/components/MusicPlayer.svelte` (new), `Sidebar.svelte` (the button beside
  `:992`), `utils/windowGeometry.ts` (reused).
- `ui/src/lib/components/ChatMessageList.svelte` (`:445`): the `audio/*` file row as a
  player. `types.ts` is unchanged (D19). `VoicePlayer.svelte` (`duration` at `:41`)
  receives its duration from N12.
- `ui/src/lib/panels/ChatPanel.svelte` (`:987-998`): sending a recording as audio, and
  in groups of one node plus agents only if Q3 says yes (then ADR-032 Part B is amended).
- `ui/package.json` (`Observed`: no `wavesurfer` today): `wavesurfer.js` 7.12.12 bundled
  by Vite, never loaded from a CDN, decoding at `sampleRate: 22050`; this is the only new
  UI dependency. Or no package, if N13 chooses precomputed peaks.
- `ui/src-tauri/tauri.conf.json` (`:27-33`): only if N8 or N9 chooses a scope or CSP
  change.
- `~/.dpc/music/` (library, folders, playlists, recordings, imported files) and
  `get_model_files_dir() / "music" / <engine>` (D14).
- `docs/GLOSSARY.md`: track, take, version, watched folder, the button label and the tool
  names, in the same commit as the code that introduces each.

## Implementation Status

Accepted by Mike on 2026-11-19 (08:21). **Status as of 2026-11-20**, read from the code on
branch `dev` at `4c58a1d4`. Built: the group-file fix (pushed), and on local `dev`, **not
pushed yet** (`git log origin/dev..dev`), the core music library, the window and
mini-player, the lyrics chain with Transcribe, and audio into a 1:1 agent chat. Phase 2
is not started; the GPU card unblocks it. The cards are in `dpc-messenger/backlog.md`
(gitignored); a card's heading there can still say open after part of it is built, so the
Status column describes the code. Cards were filed 2026-11-19 unless a date is given.
Card names are kept as filed (D19).

| Task (card id) | Status | Commit |
|---|---|---|
| Epic: THE-AI-MUSIC-PLAYER-RUNS-ONLY-AS-A-SIMULATED-BROWSER-PROTOTYPE-AND-DPC-HAS-NO-PLACE-TO-HOST-IT | Open | — |
| B1: AN-AGENT-CANNOT-POST-A-FILE-INTO-ITS-OWN-CHAT-AND-THE-CHAT-HAS-NO-AUDIO-ATTACHMENT-TYPE (title kept; dated correction per D19 in the body) | Pending | — |
| B2: NO-MUSIC-JOB-RUNNER-EXISTS-SO-A-COMMENT-OR-A-GENERATE-REQUEST-CANNOT-BECOME-A-TAKE | In progress (2026-11-21), on local `dev`, not pushed. Step 1 built: the music job store and capabilities report (SPEC §5 states, payloads written before send, takes with seed, `synth_batch_size` and index, a Discard that needs a reason), job and take commands on `local_api`. Step 2 built: see Engine supervision. Reason dictionary (D32): `9ccd950a`. Step 4 (window UI: Generate from own text, A/B at matched level, Keep / Discard with a reason) not started; Mike: after the YuE2 listening set (#192). Left: QA stage-1 gates | `f97a454e`, `8d716430`, `6b34b44c`, `9ccd950a` |
| Engine supervision (2026-11-21): DPC-DOES-NOT-START-OR-WATCH-THE-ACESTEP-CPP-SERVER-SO-NO-MUSIC-JOB-CAN-REACH-AN-ENGINE | Built on local `dev`, not pushed (2026-11-21, slice 1 step 2): `core/dpc_client_core/music/acestep.py` supervises `ace-server` on 127.0.0.1 (`[music] acestep_*` settings, `get_music_engine_status` / `start_music_engine` / `stop_music_engine`), and `music/runner.py` runs `generate_from_text` to review: one `/synth` per job with `synth_batch_size` = variants, wav24 to 24-bit FLAC, sidecar, library entry. One live run on the Windows card (2 x 30 s). Open: Linux and macOS have no build recipe (the engine reports `not_installed`); llama-server and Strata do not see acestep.cpp as a card holder  Live smoke test: 2 takes of 30 s in 16.9 s, peak 9344 MiB, FLAC 24-bit/48k in the library, **accepted by Mike (#191)**. Known differences from SPEC (open for Mike, not decided): `ACESTEP_OFFLOAD_TO_CPU` and `thinking off` have no acestep.cpp equivalent; the card is checked once per job, not per variant; `variant` is 1-based in names, the batch index is recorded separately (0-based) | `6b34b44c` |
| YuE2 default engine (2026-11-21, D35, Mike #198): no card yet | Built on local `dev`, not pushed: `core/dpc_client_core/music/yue2.py` supervises `yue-server` on 127.0.0.1:9994 (`[music] yue2_*` settings; `--max-batch 1`; backbone `stock` / `rs-tw`), `music/engine_base.py` is the supervisor both engines share, `music/engines.py` keeps one music engine on the card (idle one stopped as `swap`, busy one waited for or `engine_busy`), `music/runner.py` runs yue2 jobs one `/synth` per variant at batch 1 (seed and lm_seed `seed + i`, payload per variant before send, replay JSON kept as a payload, sidecar with commit, backbone sha256, adapters with source and split sha256s). Voice adapters are split by key prefix into music (AR) and sound (NAR) files under `<models>/yue2/loras/.split/`, tensor bytes unchanged (byte-identical to the trial's split). `params.engine` defaults to `yue2`; `ace-step-1.5:text2music` and engine-less records still load as acestep. Engine commands take an optional `engine`. Create panel: engine, backbone and voice selects, short-cap warning. Live smoke (2026-11-21, scratch DPC home, real `yue-server`, card free): 2 variants at a 120 s cap in 39.7 s wall from proposal to review, peak 5258 MiB, takes of 74.0 and 74.4 s (the model ended the song), seeds 42/43 with lm_seed 42/43, 24-bit/48k FLAC in that home's library; awaiting Mike's listen. Open: Linux/macOS build recipe; the voice adapters are not yet heard through DPC | `ade158b7` |
| Library hide/delete (2026-11-21): THE-MUSIC-LIBRARY-HAS-NO-WAY-TO-HIDE-OR-DELETE-A-TRACK-SO-RAW-ENGINE-OUTPUTS-AND-DUPLICATES-CLUTTER-IT | Built on local `dev`, not pushed (D34): hide, remove missing, delete a generated file; ownership is decided by path under `~/.dpc/music/generated/`, and the person's own folders are only hidden. Awaiting Mike's try in the app | `8e134ec6`, `76a081c4`, `2ae20b01`, `8d716430` |
| MP3 export (2026-11-22, D36, Mike #210): A-TRACK-CAN-ONLY-BE-KEPT-AS-WAV-OR-FLAC-SO-THERE-IS-NO-MP3-TO-SHARE-OR-PLAY-ELSEWHERE | Built on local `dev`, not pushed (design [P]): `music/export.py` (one root, identity index, portable names, playlists as one level of hard links, -1 dBTP limiter checked on the decoded MP3, ID3v2.4 with `DPC_PROVENANCE`); the library scan skips exports; `export_music_track` / `export_music_playlist` / `get_music_export_info`; Export MP3 in the window with an "mp3" chip. Tests and real ffmpeg runs on this machine only: not tried in the app, Linux and macOS not run | `98001dc7` |
| Library: THE-MUSIC-PLAYER-HAS-NO-LIBRARY-OR-PLAYLIST-SO-PLAIN-LISTENING-IS-NOT-ITS-DEFAULT-MODE | Core built, on `dev`, pushed (`20dcc3f1`, 2026-11-20): `core/dpc_client_core/music/` (watched folders indexed in place and polled, N6; track identity by `track_id` plus an audio-payload hash, Q4 layers 1-3 and 5; playlists; `local_api` commands; `[music] poll_interval_s` in a fresh `config.ini`). Queue, shuffle and repeat live in the window (B3 row) | `1cdce551`, `aa61925b`, `6f797e02`, `22d73fd0`, `b712712a` |
| B3: THE-MUSIC-PLAYER-HAS-NO-DPC-COMPONENT-SO-COMMENTS-LYRICS-TAKES-AND-AB-REVIEW-EXIST-ONLY-IN-THE-PROTOTYPE | Window part built, on `dev`, pushed (`20dcc3f1`, 2026-11-20): the Music Player button beside AGENTS MAP, a floating window that minimises to a mini-player, the mini-player with its own width and its own place (`ui/src/lib/components/music/MusicPlayer.svelte`). Since `9bb7599d` any library track plays through the N8 stream (next row). Comments, waveform, takes and A/B are not started | `32900e82`, `8bebfb6a`, `ec79687d`, `2457aa7c` |
| Byte serving (N8, D28; 2026-11-20, no card) | Built, on `dev`, pushed (`9bb7599d` is on `origin/dev`, checked 2026-11-21): a loopback track stream with `Range`, a per-process token and an `Origin` allowlist, keyed by `track_id` (`core/dpc_client_core/music/stream.py`, `get_music_stream_info`, CSP `media-src http://127.0.0.1:9996`); Linux plays every track from it, Windows and macOS the files outside `~/.dpc`. Tests only; the Linux window check is open (Confirmation) | `9bb7599d` |
| Lyrics: A-TRACK-HAS-NO-LYRICS-CHAIN-SO-TAGS-LRC-LRCLIB-AND-TRANSCRIBE-ARE-NOT-TRIED-IN-ORDER | Built, on `dev`, pushed (`20dcc3f1`, 2026-11-20): sidecar `.lrc`, then tags, then the local LRCLIB cache, with lrclib.net only on an explicit lookup (D6 note); manual lyrics; Transcribe only when the person asks, which unloads the Whisper model it loaded and records provider and model (`music/lyrics.py`, `MusicLyrics.svelte`) | `cb9af8a1`, `74c2de84`, `a94c7943` (chain); `3672ee23`, `7fd6a407` (Transcribe) |
| Transcribe quality (2026-11-20): TRANSCRIBED-LYRICS-CARRY-WHISPER-HALLUCINATIONS-AND-MERGE-LONG-SEGMENTS-INTO-ONE-LINE | Open | — |
| Button title (2026-11-20): THE-MUSIC-PLAYER-BUTTON-CUTS-A-LONG-TRACK-TITLE-INSTEAD-OF-SCROLLING-IT | Open | — |
| Mini-player place (2026-11-20): THE-MINI-PLAYER-FIT-TO-A-SMALLER-SCREEN-MOVES-ITS-SAVED-PLACE-FOR-GOOD | Open | — |
| Whisper switch (2026-11-20): THE-VOICE-WHISPER-SWITCH-CAN-UNLOAD-THE-MODEL-UNDER-A-RUNNING-TRANSCRIPTION | Open | — |
| Tools: DPC-AGENTS-HAVE-NO-MUSIC-TOOLS-SO-THEY-CANNOT-DRIVE-THE-PLAYER-THE-WAY-THEY-DRIVE-COMFYUI | Pending | — |
| Microphone: IN-AN-AGENT-CHAT-THE-MICROPHONE-ONLY-DICTATES-SO-A-HUMMED-MELODY-NEVER-REACHES-THE-AGENT-AS-AUDIO | Built for 1:1 agent chats, on `dev`, pushed (`20dcc3f1`, 2026-11-20): a recording (Dictate or Send as audio) or a paperclip file reaches the agent as an audio `file` attachment with its path in context; length measured, picked file capped, a refusal or an unconfirmed send (180 s) gives the draft back. Groups keep ADR-032 Part B dictation (Q3) | `b5c31fcd`, `3f2c6600`, `cc1c1e87`, `d218c432` |
| Audio input: THE-LLAMA-SERVER-PROVIDER-SENDS-ONLY-IMAGES-SO-AN-AUDIO-MODEL-LIKE-MUSIC-FLAMINGO-CANNOT-BE-A-DPC-ENGINE | Pending | — |
| QC: A-TAKE-ARRIVES-WITH-NO-QUALITY-CHECK-SO-TAKES-CANNOT-BE-RANKED-BEFORE-THE-PERSONS-AB | Pending | — |
| Spike: NOBODY-HAS-RUN-ACE-STEP-REPAINT-ON-AN-OWN-TRACK-SO-THE-REGION-EDIT-ENGINE-IS-UNCHOSEN | Pending (needs weights and the card, on Mike's go) | — |
| B4: REGION-EDIT-JOBS-HAVE-NO-ENGINE-SO-REGENERATE-REGION-AND-FIX-SEAM-ANSWER-UNSUPPORTED | Blocked by the repaint engine (checked live since D26, no separate spike) | — |
| YuE2: DPC-CANNOT-CALL-AN-MCP-SERVER-SO-YUE2-STUDIO-COVER-AND-LYRICS-TO-SONG-HAVE-NO-WAY-IN (scope now HTTP/CLI, D13) | Pending | — |
| Clone: NO-SINGING-VOICE-CLONE-IS-CHOSEN-SO-A-SONG-CANNOT-BE-SUNG-IN-THE-MUSICIANS-OWN-VOICE | Research | — |
| Streaming: THE-MUSIC-PLAYER-CANNOT-PLAY-OR-SAVE-SOUNDCLOUD-AND-YANDEX-MUSIC-TRACKS-SO-LISTENER-MODE-IS-LOCAL-ONLY (`backlog.md:132`) | Postponed (Mike, 2026-11-20: later, no rush); the `yt-dlp` path is not started. On the board (the first draft's "not on the board yet" was stale, Zcode and Ark) | — |
| Group file, agents-only (bug, MEDIUM): A-FILE-SENT-TO-A-GROUP-WITH-ONLY-AGENTS-VANISHES-AND-THE-UI-IS-TOLD-SUCCESS (`backlog.md:121`, filed by CC on Mike's request) | Fixed on Mike's verb (07:58), observed live by Mike after a restart and closed 2026-11-19; follow-up: one message for a file and its text, audio plays inline | `3837b3a5`, `3f2de3c7`, `dd446110` |
| Group file (dependency, 2026-08-11): GROUP-IMAGE-AND-FILE-ECHO-STILL-WAITS-ON-THE-TRANSFER (ADR-032 phase 3, `backlog.md:8393`) | Files half fixed by `3837b3a5` | `3837b3a5` |
| GPU (dependency, 2026-11-06): EXTERNAL-GPU-JOBS-CANNOT-JOIN-THE-CARD-QUEUE-SO-AN-IDLE-ENGINE-KEEPS-THE-CARD-THEY-NEED | Waiting on Mike's option (N11); it is what unblocks Phase 2, which is not started | — |
| Whisper residency (related, 2026-08-20): THE-WHISPER-ALIAS-IS-THE-BACKENDS-LAST-STANDING-CUDA-RESIDENT (`backlog.md:7212`) | Open; bears on swaps per run | — |
| Prototype and spec (ai-studio `experiments/music-player/`) | Exists, simulated, untracked | — |

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
- **Q2. Phase 0.** Go or no-go on the Phase 0 contents (N1), and whether the group-file
  fix (A-FILE-SENT-TO-A-GROUP-WITH-ONLY-AGENTS-VANISHES-AND-THE-UI-IS-TOLD-SUCCESS, the
  agents-only half of ADR-032 phase 3) joins Phase 0.
  - Zcode: go, and include the fix: it is small, mirrors `send_group_image`, and is a
    data-loss bug regardless of the player.
  - Forge: include it; without it the musician mode does not work in this chat at all.
  - Ark: did not vote on Q2; lists it among Mike's verbs.
  - CC recommends: include the fix. — @Mike
- **Q3. "In the chat" (D9).** Does it include group chats with one node plus agents, like
  AI Studio? If yes, ADR-032 Part B needs an amendment: the dictation branch for groups
  with one node or fewer (`ChatPanel.svelte:987-993`) gets a "send as audio" choice, and
  the microphone card's scope widens beyond `agent_*` chats. Both walls were hit live on
  2026-11-19: the mp3 at 12:34:49 and the microphone.
  - Zcode, Forge, Ark: all three yes. Ark: without it D9 holds nowhere except a private
    agent chat. Forge: otherwise D9 is false in the one group "we live in".
  - CC recommends: yes, with the ADR-032 amendment written as its own record or
    amendment. — @Mike
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

## Authors

- **Mike**: Decision (D1-D28)
- **CC**: Spec, prototype, cards, this draft and its revision after review
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
- `~/.dpc/logs/dpc-client.log.1:75624-75625`: the silent group-file send of 2026-11-19.
- `~/.dpc/config.ini:181`: `swap_wait_s = 1800` on this node.
- AI Studio group chat, 2026-11-19 06:03-06:33: the three reviews of this record.
