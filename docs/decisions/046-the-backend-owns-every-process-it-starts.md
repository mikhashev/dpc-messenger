---
adr: 046
title: "The backend owns every process it starts: it puts itself in a kill-on-close job, keeps one registry of its children, and ends them when it stops"
status: accepted
date: 2026-10-25
axis: knowledge, network
deciders: [Mike]
consulted: [Ark, Zcode, CC]
informed: []
depends_on: [ADR-040, ADR-044]
related: [ADR-030, ADR-042]
supersedes: []
session: "DPC Project group chat, 2026-10-25 — card THE-BACKEND-KEEPS-NO-LIST-OF-THE-PROCESSES-IT-STARTED-AND-DOES-NOT-END-THEM-WHEN-IT-STOPS; Mike's call 2026-10-25"
---

# ADR-046 — The backend owns every process it starts

> **Accepted, 2026-10-25 (Mike's call). Implemented: point 1, the job (`2ebdb0a3`); points
> 3, 4, 6 and 7 in the registry core (`4ea0560f`: registry, sweep, shutdown order,
> `run_supervised` and both supervisors) with the review fixes of the same day (`91a75a2e`);
> the resource sampler and `last_output_at` of point 3, and the spawn sites moved to the
> module with the structural test of point 5 (Implementation Status). Pending: the
> descendants the sampler sees entered with their parent fields, the local API command
> and the agent tools (point 8), and the UI.** The
> sequence Mike set is this record and the amendments to ADR-040, ADR-044
> and ADR-036 first, then the code. Mike's own calls in the thread were four: an agent
> sees only its own processes; Strata and the `llama-server` child join the registry and
> the job; the list is shown on screen, and shows duration, who started a process, when,
> and what it consumes, so that it is useful to agents and to people; and the registry is
> shown as a graph of who started what, when and how it ended. Everything else below is
> the mechanism Ark, Zcode and CC converged on, taken into this record by his go, with the
> amendments of the same day (parent fields, `launcher_kind`, `exit_code` and `signal`,
> `stopped` with `stop_kind`, the cross-platform rule), which he accepted as a set. The
> words the record introduces are listed in **New words** and are accepted (Mike's call,
> 2026-10-25).
>
> **Review fixes of 2026-10-25 (Mike's call, on the reviews of Ark, Zcode and CC).** The
> field `stop_reason` is `stop_kind` (the gateway writes `stop_reason` on the wire as the
> Anthropic Messages API field, which cannot be renamed, so the registry yields; an older
> file's key is read as `stop_kind`). `engine_died` is added to the stop kinds and
> `engine_switch` removed, under the rule in point 4. The precedence of `backend_stop` applies to
> stops announced after the backend's stop began. The state file is fsynced before the swap and the
> finished entries are also appended to daily files (answers Q7). The text about a
> foreign `taskkill`, the signal's name and the weaker guarantee on POSIX is corrected
> to what the code does and what was measured.
>
> **Axis `knowledge, network`.** `knowledge` because most spawns are an agent's tool
> calls and the board card carries that axis; `network` because the two local engines of
> ADR-040 and ADR-044 (both `network`) join the same ownership. `honesty` was considered
> and not taken: the registry will feed it, but the decision is about who owns a process,
> not about whether a number is true.

## Context and Problem Statement

The backend starts processes for several reasons and has no single answer to "which are
running, who asked for them, and what happens to them when I stop".

What exists, read in code on 2026-10-25:

- **Agent commands** (`run_shell`, `git_tool`, DjVuLibre in `read_document`) go through
  `run_supervised` in `dpc_agent/tools/process.py`. It logs `spawned pid N for <launcher>`
  with the command, but `launcher` is the name of the tool, not of the agent or the chat,
  which are found only in that agent's `tools.jsonl`.
- **A stop does not end them.** Agent tools run on `loop._DaemonToolPool` daemon threads,
  and the timeout `run_supervised` enforces lives in that thread; it dies with the
  process. Before `b26f440e` a Ctrl+C on the shared console reached a running command;
  since that commit every child gets `CREATE_NO_WINDOW` and a console of its own, so
  nothing does, and a command that was running when the backend stopped outlives it.
  A `| more` waiting for a key waits for good.
- **Engines and the browser are handled, each its own way.** The `llama-server` and
  Strata supervisors stop their children (CTRL_BREAK, then a bound; ADR-040, ADR-044);
  `CoreService.shutdown` closes live Camoufox browsers and force-kills after 5 s.
- **Everything else is in no list.** Counted in `dpc-client/core/dpc_client_core` without
  tests: 27 call expressions in 7 files (24 `subprocess.run`, one `subprocess.Popen`, two
  `asyncio.create_subprocess_exec`), plus one in `run_service.py` beside the package.
  Method and per-file figures are under **Scope**. The short probes are not short in
  number: nine in the device collector, nine in `dpc_agent/utils.py`.
- **There is no list anywhere you can ask.** No local API command, no UI view and no
  agent tool answers "what is running now". The incidents that cost real time were all
  found by a person looking at Task Manager or at a log:
  `A-CHILD-PROCESS-OUTLIVES-THE-KILL-AIMED-AT-ITS-WRAPPER` (two writers ran from 16:02 to
  15:48 the next day, about four hours past the kill aimed at their launcher),
  `A-SHELL-COMMANDS-GRANDCHILD-CAN-HOLD-THE-WHOLE-PROCESS-OPEN-FOR-EVER` (two
  grandchildren held 165 GB of commit for nine and a half hours, and the `llama-server`
  their hung parent never reaped held 28.8 GB of VRAM the whole time), and
  `THE-SERVICE-CONSOLE-IS-LEFT-IN-RAW-MODE-SO-CTRL-C-NEVER-REACHES-THE-BACKEND` (a
  killed `more` left the shared console raw; its fix, `b26f440e`, is what removed the
  accidental Ctrl+C that used to end the commands).

Two earlier records bound the decision. ADR-044 gave the Strata tree one fate through a
kill-on-close job object in the launcher, and noted that the job can fail to apply. And
`process.py` already explains why the memory ceiling is a poll and not a job limit; this
record uses the job for one thing only, the kill, and leaves the ceiling where it is.

## Decision Drivers

- **A crash and a closed window must end the children too.** An orderly stop is the case
  we can write code for; the incidents were the other cases.
- **No race.** A mechanism that adds each child to a job after `Popen` leaves a window in
  which a grandchild is born outside it (the shell starts its command at once; Strata's
  launcher starts `strata.exe` at once).
- **A new spawn site must not be able to opt out by being forgotten.** Twenty-seven sites
  today; a rule held by memory will not outlive the next one.
- **Attribution is a fact at the moment of birth.** A launcher name that is the tool's
  name shows `run_shell, 40 s` and leaves the agent and the chat to be guessed.
- **Useful, not another log nobody reads.** Mike asked that the list serve agents and
  people: who, when, how long, what it consumes, how it ended.
- **Cross-platform.** Windows is where the machine lives and where the soft stop does not
  exist for our children; Linux and macOS must not get a weaker rule by omission.

## Decision

**The backend owns every process it starts. It is the one that decides, and records, how
each one ends.** Eight points.

**Cross-platform (Mike's call, 2026-10-25).** Point 1 is Windows-only, because only
Windows has the job object. The registry, the exit reasons, the startup sweep, the
shutdown order and the API are the same on Windows, Linux and macOS (processes are read
through psutil, which is already a dependency). On POSIX the backend ends what it tracks
at an orderly stop (point 2); a crash there is covered by the sweep (point 6), not by the
kernel. **So the guarantee is weaker on Linux and macOS:** with no kernel mechanism, after
a backend crash the children live until the next start's sweep. "The backend owns every
process it starts" is the rule everywhere; "and they die with it, whatever happens to it"
holds on Windows only.

1. **Windows: the backend puts itself in a kill-on-close job before it spawns anything.**
   At start, in `run_service.py` before `dependency_setup` (which already spawns
   `nvidia-smi`) and before `CoreService` exists, the backend creates a Job Object with
   `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, **without** `JOB_OBJECT_LIMIT_BREAKAWAY_OK`,
   assigns its own process to it, and keeps the handle (not inheritable) for the life of
   the process. A process created by a member of a job is a member from birth, so every
   descendant is inside before its first instruction, and all of them die when the last
   handle closes: Ctrl+C, a stop from the UI, a crash, a closed window, an end-task. This
   removes the race of a per-child assignment, covers all 27 sites and the Playwright
   driver before any registry code is written, and needs no `CREATE_SUSPENDED`.
   - **Failure is not fatal.** If creating the job or assigning the process fails
     (an older Windows, a restricting parent job), the backend logs a WARNING and goes
     on; points 6 and 7 remain. This is the stance cargo takes, below.
   - **Nesting.** Strata's launcher puts the engine and `strata-vision.exe` in a
     kill-on-close job of its own (`serve/winjob.py`, ADR-044). Nested jobs work since
     Windows 8, so the launcher's job becomes a child of the backend's. It has not been
     tried on this machine and gets a test: start the backend, start the Strata launcher
     (mock engine is enough), kill the backend with `TerminateProcess`, and assert that
     the launcher and its engine are gone.
   - **No breakaway is a choice with a price.** Cargo sets `BREAKAWAY_OK`; we do not,
     because breakaway is how a descendant escapes the job
     (`A-DESCENDANT-THAT-BREAKS-AWAY-ESCAPES-BOTH-THE-TREE-KILL-AND-THE-PROCESS-GROUP`).
     A descendant that asks for `CREATE_BREAKAWAY_FROM_JOB` then fails to start instead of
     escaping, per Microsoft's `CreateProcess` documentation (recalled, not re-read for this
     record); the test above also proves it on this machine, and the Camoufox and Playwright launch must be run
     once under the job before the decision is relied on (Open Questions).
   - **Prior art, read 2026-10-25.** Cargo does this: `src/util/job.rs` in
     `github.com/rust-lang/cargo` (master of that day, read through
     `raw.githubusercontent.com`) creates a job with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE |
     JOB_OBJECT_LIMIT_BREAKAWAY_OK`, assigns the current process to it, and returns a token
     that the entry point (`src/bin/cargo/main.rs`, `cargo::util::job::setup()`) holds for
     the whole run. Its header comment gives the same reason: on Windows Ctrl+C does not
     take the tree down, a job does. It ignores every error from the setup on purpose.
     Differences from our decision: cargo allows breakaway and we do not; cargo's POSIX
     branch does nothing outside a test hook.

2. **POSIX: children keep their own sessions; the backend kills what it tracks at an
   orderly stop.** `run_supervised` already starts each child with `start_new_session`,
   so one `killpg` reaches every descendant that did not deliberately leave
   (`process.py:_kill_process_tree`); an orderly stop kills the tracked process groups and
   every descendant it can walk (`_descendants`, `/proc`, `ps`). A crash is covered by the
   startup sweep (point 6), not by the kernel. macOS has no better tool and gets exactly
   this. Two Linux extras were weighed and are **not** taken:
   - `PR_SET_PDEATHSIG` is delivered when the *thread* that created the child exits, not
     when the process does. This backend spawns from `_DaemonToolPool` daemon threads,
     executor threads and asyncio; a pool thread ending would kill a live command.
     Rejected.
   - `PR_SET_CHILD_SUBREAPER` would let the backend find double-forked orphans, but it
     then has to reap them, and a `waitpid(-1)` loop steals exit statuses from the
     `Popen` and asyncio child watchers that wait on their own pids. Later, and only if
     the POSIX arm of the descendant card is measured red on the Linux node; the arm has
     never been run (that card says so).

3. **One registry of the backend's children.** Each entry holds: `pid` and the process's
   **start time** (psutil creation time; pids are reused); its **parent**, `parent_pid`
   and `parent_start_time` (the time for the same reason: a pid a day later may be a
   stranger's); the **launcher** (the existing word), `launcher_kind` and, for an agent's
   call, the **spawn origin** `agent_id`, `conversation_id`, `tool`, `round`, read from a
   context variable (below); the **command**, bounded in length as the log line already
   bounds it (120 characters, the full text where an agent's tool record holds it); start
   and end time; the **exit reason** with its `stop_kind` where it has one (point 4);
   `exit_code` and `signal`; **resource peaks**; and `last_output_at`.
   - **The parent, and the graph.** Every entry names its parent by pid and start time, so
     the registry is a tree rooted at the backend's own process, and the graph Mike asked
     for is a view over it: backend → agent → conversation → tool call → process → its
     children. The three levels above the process are read from its spawn origin; the
     levels from the process down are the parent fields. What the parent is, by kind of
     entry: for a `run_supervised` child, the backend; for an engine (Strata,
     `llamacpp_server`), the backend; for a descendant the sampler sees and the backend
     did not start (the `ping` under a `cmd /c`), its real parent as psutil reports it,
     entered with both fields. A parent whose start time no longer matches its pid is not
     followed: the link is shown broken rather than drawn to a stranger.
   - **`launcher_kind` is `tool` or `subsystem`,** written at birth. `launcher` alone
     holds three forms (a tool's name, a tool's name with a mode in brackets such as
     `git_tool(sandbox)`, a subsystem); the kind says which, so "everything the agents
     started" is a filter and not knowledge by heart. An entry that says `tool` and carries
     no spawn origin is a lost attribution, a defect (the pool threads inherit no context,
     below); it is never read as "DPC started it itself".
   - **`exit_code` and `signal` are stored on every entry,** `exited` with 0 included, so
     that "how it ended" is a fact and not only a word. `exit_code: null` means this
     backend did not see the end; `signal` is the POSIX signal's **name** where there was
     one (`SIGKILL`, stored as that string, never the number).
   - **Attribution by contextvar.** The tool dispatcher sets `(agent_id, conversation_id,
     tool, round)` in a `contextvars.ContextVar` before it runs a tool; every spawn site
     reads it. The signature of `run_supervised` and its callers stay as they are, and a
     site added next year is attributed for free. The neighbour is
     `llm_adapter.CALL_PROGRESS`. Tools run in pool threads, which do not inherit the
     caller's context by themselves, so the dispatcher must run the tool inside a copy of
     the context (`contextvars.copy_context().run`) or set the variable inside the thread;
     a test spawns from a tool through the real pool and reads the origin back. The field is
     `conversation_id`, the code's word; "chat" is the screen's. A spawn that no tool made
     is `launcher_kind: subsystem` and carries the **subsystem** in `launcher` instead:
     `strata`, `llamacpp_server` (their provider types), `browser`, `device_probe`,
     `git_helper`. **A subsystem started inside an agent's tool call** (an engine woken by
     that agent's request) keeps that call's origin, because `register` reads the context
     variable at the moment of the spawn, and this is left as it is: **for a subsystem the
     spawn origin is the call that woke it; its owner is the backend, which is also its
     parent.** The graph can draw agent, tool call, engine, and the parent fields still
     say whose child it is.
   - **Resources are peaks, sampled.** One sampler thread, every 2 seconds, over the
     registry: peak working set (RSS) and peak **commit (private bytes)** over the tree,
     CPU seconds, IO read and write bytes, and for the engines peak VRAM. No series is
     stored; a series is read by nobody and grows. Commit is shown beside RSS because
     ADR-044 documents a death by commit with RAM to spare. The sampler reuses
     `_tree_memory_mb` and the `_watch_memory` poll's tree walk (`process.py`), and psutil,
     which is already a dependency. What a figure is worth: `cpu_seconds` and the IO of
     descendants are a lower bound, because a descendant that exits between two samples
     loses at most one interval of its last figures; on Windows the root's own figures are
     exact, read through its handle at the end. That end reading sees the root process
     only, so when a command's root is a launcher shim (a venv's `python.exe`, which starts
     the real interpreter as its child), a command shorter than the first samples can show
     only the shim's few megabytes.
   - **`last_output_at`** is when a child last wrote. `run_supervised` today reads output
     with one `communicate(timeout=…)`, which hides it, so this field means reading the
     streams incrementally; for an engine it is the modification time of its log file.
     It is what makes a stuck `| more` visible in a glance instead of an afternoon in logs.
     The readers tell the registry at most once per 100 ms while a child writes, and give
     it the exact time of the last bytes once more before the end, so the field is the
     time of the last bytes and not of the last note; times are compared as times, never
     as ISO strings. `last_output_at: null` means no output was seen; a failed reading
     is logged once as a WARNING and leaves the field as it was. When a timeout's kill
     misses a descendant that holds the pipes, the bounded drain gives up, returns the
     text the readers already hold, and stops them: from then on a reader keeps nothing,
     leaves at its next chunk and closes its own end of the pipe, so a descendant that
     keeps writing does not grow the backend; a reader blocked on a silent pipe stays
     blocked without growing.
   - **Persisted in two places,** both written only by the persistent registry
     (`start_persistent()`, which `run_service.py` calls once; the in-memory default and the
     tests never touch the home). The state file `<DPC_HOME>/process_registry.json` holds the
     open entries and the newest 200 finished ones (none older than 7 days): what the sweep
     of point 6 and "what runs now" read. It is rewritten whole on spawn and on end through a
     temporary file that is fsynced and then swapped in with `os.replace`, never a truncating
     open, so a crash or a power cut leaves the old list or the new one (and when a peak
     rises by a threshold, once the sampler exists). Every finished entry is also appended
     as one JSON line to `<DPC_HOME>/process_registry/YYYY-MM-DD.jsonl`, the UTC date of
     its end, the way the node ledger appends (a `.lock` sibling, `O_APPEND`, a torn last
     line healed by the next append); files dated more than 7 days back are removed at
     start, and a failed append is logged once and never takes a spawn or an end down. The
     graph of point 8 reads the daily files: 200 entries would cut it to hours, and
     rewriting thousands of entries on every spawn and every end is too heavy. A line holds
     the entry as it was at its end; an `exit_code` that only a later report supplies is not
     appended again. The daily history begins with the first start of this code
     (2026-10-26 12:08 +07); the entries of 2026-10-25 exist only in the state file,
     without resources, and are not backfilled.
   - **A log line at both ends,** one at the start and one at the end that carries the exit
     reason; the start line `spawned pid N …` exists and gains the origin.

4. **Exit reasons are a closed vocabulary,** one word, the same in the log, the API and
   the UI. Eight: the seven Ark proposed and `stopped`, with the meaning this record
   gives each:
   - `exited` — ended on its own with an ordinary return code, zero or not.
   - `timeout` — killed by the caller's timeout.
   - `memory_ceiling` — killed by the memory ceiling (`process.py`, `ceiling_mb`).
   - `backend_stop` — killed by the backend's orderly stop (point 7).
   - `cancelled_by_agent` — killed by its own agent through `kill_my_process` (point 8).
   - `orphan_swept` — a process of an earlier run found alive at start and killed (point 6).
   - `stopped` — a stop DPC asked for while the backend runs, with a second field,
     `stop_kind`: `ui_stop` (a person stopped it from the UI), `failed_start` (a start
     that did not finish, or that the launcher refused at the version check or the smoke,
     cleared away), `engine_died` (the engine died after its start had succeeded and DPC
     stopped the launcher: Strata's watcher and a 503 naming the engine's exit code; the
     llama-server supervisor has no such branch, so Strata is the only producer today and
     the word waits ready for a watcher there), `browser_closed` (the browser closed on
     purpose) and `idle_unload` (an engine unloaded after sitting idle). The word says that
     DPC did it; `stop_kind` says what kind of stop. The field is named `stop_kind`, not
     `stop_reason`, so as not to collide with the `stop_reason` the gateway writes on the
     wire (the Anthropic Messages API field), which cannot be renamed. Mike's call,
     2026-10-25, on the proposal of Ark and CC; the rename and `engine_died` the same day
     on the reviews of Ark, Zcode and CC. A switch of local engines has no kind of its
     own: it reaches the supervisor as the UI's stop button and is `ui_stop`.
   - `crashed` — ended by a signal or by an NTSTATUS error code (`0xC0000000` up to, not
     including, `0xE0000000`: the customer bit marks codes a program chose, so `exit(-1)`,
     `0xFFFFFFFF`, stays an ordinary exit) that nobody sent. The rule is **who sent, not
     which number:** the word is set
     if and only if the registry sent this process no stop, and the number or the signal
     only confirms it. A commit-limit death is this (`0xC000012D`, STATUS_COMMITMENT_LIMIT,
     recalled and not measured here). `0xC000013A` (STATUS_CONTROL_C_EXIT) is not: it is
     what the CTRL_BREAK a supervisor sends produces, and the registry, which knows the
     stop was sent, does not read it as a crash.
     **A kill nobody in DPC sent reads differently by platform, and this is the honest
     asymmetry.** On POSIX a foreign SIGKILL arrives as a negative return code and reads
     `crashed`, `signal` `SIGKILL`. On Windows a foreign `taskkill /F` leaves exit code 1,
     which is outside the NTSTATUS range, so it reads `exited` and cannot be told from an
     ordinary exit with code 1 (Zcode measured it live; the test
     `test_on_windows_a_foreign_taskkill_leaves_code_1_and_reads_exited` pins it, and an
     earlier version of this record said the opposite). What protects the reading on
     Windows is the rule above, not the number: a stop the registry sent is never read as
     a crash, whatever code it leaves.
   **Precedence:** a stop **announced after the backend's orderly stop began** (point 7) is
   `backend_stop` whoever sent the signal, and so is the orderly stop's own kill. A stop
   **announced before** it keeps the word it was announced with: a start that failed at
   10:00:00 and is still being cleared away when Ctrl+C arrives is `stopped`/`failed_start`,
   because the refusal stopped it and the shutdown only found it on the way out. `stopped`
   is otherwise only for a stop while the backend runs. A timeout and a memory-ceiling kill
   keep their own words in both cases.
   **Which values stay in the closed lists.** A value stays only if something produces it
   or a named decision will create its producer; a value with neither is removed and comes
   back in the commit that adds its producer. The list is an allowlist, so a value nothing
   produces cannot appear in the data by accident, and the only way it gets in is a person
   writing it, whereupon the entry says something that did not happen and the check stays
   silent. By that rule `engine_switch` was removed (no path to it will ever exist),
   `browser_closed` stays (its producer is browser registration, point 5, the next phase),
   and `idle_unload` stays tied to an open question: its producer exists only if the owner
   decides that DPC, and not the engine, unloads an idle model (the question about Strata
   session files, open when the reviews were written). Today the engine unloads its model itself and the
   launcher stays up.
   `inference_engine_stop` was proposed and not taken: it would give one event at
   shutdown two names, and it names a class of process, where the other words answer who
   ended it and why. A new reason is a change to this list and is argued here; a code path
   that ends a process without one of these words is a defect.

5. **Every spawn goes through `dpc_agent/tools/process.py`.** `run_supervised` for a
   command that is waited for, and a registered helper in the same module for a short
   probe, so that a probe is in the registry for its few milliseconds like any other
   (and a 9-call collector costs nine entries, not nine blind spots). The two supervisors
   keep `asyncio.create_subprocess_exec` because they stream to a log file and stop with
   CTRL_BREAK; they register their child with the registry at `_spawn` and report its end.
   - **A structural test fails** on `subprocess.run`, `subprocess.Popen`,
     `asyncio.create_subprocess_exec` and `asyncio.create_subprocess_shell` anywhere
     under `dpc_client_core` and in `run_service.py` outside `process.py`, except a
     written allow-list with a reason per entry. The model is
     `test_every_lifecycle_coroutine_runs_on_the_owner_loop`, and the existing
     `test_a_spawned_process_gets_no_console.py` already walks the tools directory in the
     same way. The test must first prove that it finds the sites it guards (an AST search
     that finds nothing passes for every program; that test's own docstring says so).
   - **Allow-list on day one:** the two supervisors' `_spawn`. Third-party spawns
     (Playwright's node driver, the Camoufox browser, `webbrowser.open` for the OAuth
     login) are not ours to route; the job covers them on Windows, and `browser` is
     registered by pid when the AuthBrowser knows it.
   - **`dpc_agent/tools/comfyui.py` belongs to another developer.** It holds one site
     (ffmpeg). This record names it and plans no edit to it; the change to it is agreed
     with them first, and until then it is the one entry on the allow-list that has an
     owner and a due date instead of a reason.
   - **As built (2026-10-26).** The helper is `run_probe` in
     `process.py`: `subprocess.run(cmd, capture_output=True, …)` with the same return (a
     `CompletedProcess`) and the same exceptions (`FileNotFoundError` and any `OSError`
     from the spawn, `TimeoutExpired`, `CalledProcessError` with `check`), so each site
     kept its own code and its own handlers and changed only the function it calls. It
     runs through `run_supervised` (a `launcher_kind` argument, `subsystem` here, and the
     bytes as written), so a probe gets what every child gets: a hidden console of its
     own on Windows, a session of its own on POSIX, a closed stdin, the memory ceiling, a
     tree kill at the timeout and the bounded drain. Its `launcher` is `device_probe` or
     `git_helper`, both now in `SUBSYSTEMS`; inside a tool call the entry keeps that
     call's spawn origin (point 3). Text is decoded as text mode decodes, after the end is
     recorded: the locale's encoding when the site names none, strict unless it names
     `errors`; every moved site kept `text=True` with no encoding, or bytes. **Moved, 22
     sites:** `device_context_collector.py` 9, `dpc_agent/utils.py` 9 (`run_cmd` and
     `get_git_info` among them, which have no production caller by `rg` and Orbit;
     `tests/test_a_short_probe_is_in_the_registry_and_answers_as_subprocess_run_did.py`
     exercises them, and both are kept), the llama-server
     supervisor's two `nvidia-smi` memory queries, `service.py`'s `ip addr` (Linux) and
     `run_service.py`'s `nvidia-smi`. **The structural test** walks every `.py` under
     `dpc-client/core` (the package, `run_service.py` and every other script beside it,
     an uncommitted one included; not `tests/`, a virtual environment, `__pycache__`,
     hidden directories or build output), resolves import aliases, and fails on
     `subprocess.run`/`Popen`/`call`/`check_call`/`check_output`/`getoutput`/`getstatusoutput`,
     `asyncio.create_subprocess_exec`/`_shell` (also as `loop.subprocess_exec`/`_shell`),
     `os.system`, `os.popen`, `os.spawn*`, `os.exec*`, `os.posix_spawn*` and
     `os.startfile` outside `process.py`. **The allow-list, keyed by file and enclosing
     function:** `LlamaServerSupervisor._spawn` and `StrataSupervisor._spawn` (a log file
     and CTRL_BREAK; they register their child); `job_object._launch_explorer` (added after
     the count of 2026-10-25: a hand-off whose purpose is to leave the backend's
     kill-on-close job. `explorer.exe <url>` passes the URL to the running shell and exits
     at once, and the browser the shell starts is outside the job; registering the
     launcher would enter in the backend's list, as the backend's own, a process opened on
     purpose for the user to keep, which the job does not hold); `comfyui.comfyui_convert`,
     owner "the developer of the ComfyUI tools", owner only, no date agreed (`due: None`);
     and `probe_rename_orphan.py`'s `alive`, `llama_pids` and `main` (four calls, two of
     them `taskkill`: a one-off repair probe run by hand, not the backend, so the
     registry has nothing to say about it). An entry gives a `reason`, or an `owner` and a
     `due` that is an ISO date, after which the test fails, or `None`, written on purpose
     when no date was agreed. A spawn stored in a variable and called through it is not
     seen by the walk.
   - **What a registered probe costs, measured 2026-10-26 on the owner's Windows machine**
     (median of 30, a temporary `DPC_HOME`). One `git rev-parse`: 171 ms through
     `subprocess.run`, 324 ms through `run_probe` with the in-memory registry, 340 ms with
     a full state file (200 finished entries). The registry's part is the ~15 ms of the
     two state-file writes; almost all the rest is the hidden console: the same
     `subprocess.run` with `CREATE_NO_WINDOW` alone takes 304 ms against 159 ms, which is
     a `conhost` started per child (the ceiling's watcher costs nothing measurable). An
     auto-commit (`git add` and `git commit`) went from 527 ms to 845 ms. The device
     collector's start-up reading (`_collect_hardware` and `_collect_software`, which run
     ten probes on this machine: `nvidia-smi` twice, `nvcc`, and seven `--version`) went
     from 3.4 s to 4.8 s. On Linux the console part does not exist. How often they run:
     the collector once per start (ten probes here; up to about eighteen where every tool
     is installed); `run_service`'s `nvidia-smi` only when torch lacks CUDA; `ip addr` on
     Linux once per `get_status`; the VRAM queries once or twice per engine start, once a
     second while Strata waits for its card after a stop, and twice per peer request that
     finds Strata stopped; the git helpers six times per agent created and twice per
     identity update, which the agents' `tools.jsonl` show 48 times since 2026-05 (five
     in 2026-10).

6. **A startup sweep.** At start, before the first spawn, the backend reads the persisted
   registry. An entry still open whose `pid` is alive **and whose start time matches**
   is killed (its tree, and its process group on POSIX) and closed `orphan_swept`. The
   start time is required: a pid is reused, and killing "pid 39208" a day later kills a
   stranger. Never its own pid and never an ancestor's (the `uv` launcher is the
   backend's parent). An entry whose process is gone is closed without a kill, as
   `backend_stop` with `exit_code: null`, and the log line says it was found closed at
   start: on Windows the cause is the job, on POSIX it may be anything, and the null is
   what keeps that guess visible (Q1). This is the second line behind the job: it is what
   covers a crash on POSIX, a job that failed to apply, and a pid in a run where the
   job did not exist.

7. **Shutdown order.** The job is the backstop, not the mechanism.
   1. **Stop accepting spawns:** the registry refuses new entries (a tool thread in the
      middle of a call gets an error it can report), so nothing is born after the sweep
      below. **One exception, the stop's own reading of the card** (2026-10-26): the
      stop runs one device probe itself, Strata's check that the card was released after
      its engine stopped (`_verify_vram_released` reads free VRAM through `_free_vram_mib`
      once a second up to `vram_release_timeout_s`, ADR-044 point 2), and a refusal there
      would not fail loudly: the probe's caller reads it as an unreadable card, waits out
      the whole timeout and records `vram_released: None` where the card was in fact free.
      So that call, and only it, says so: `_free_vram_mib(while_stopping=True)`, carried
      through `run_probe` and `run_supervised` to `check_open`, which lets a
      `device_probe` subsystem through only with that argument; the launcher's name alone
      lets nothing through. A device probe only reads the machine, is bounded by its own
      timeout (10 s at most today) and is registered like any other, so the one let
      through ends `exited` or `timeout` as usual. Everything else is refused as before:
      a `git_helper` (the stop runs none; a refused auto-commit logs its WARNING and leaves
      the change for the next commit's `git add -A`), and the device probes that race the
      stop with no argument, which raise `ProcessRegistryClosed` like any refused spawn: a
      Strata or llama-server start's VRAM reading (the engine spawn that would follow is
      refused at `_spawn` anyway), a peer request's reading of a stopped Strata, and
      `ip addr` on Linux from a `get_status`; the collector and `run_service`'s
      `nvidia-smi` run at start only. A git already running when the stop begins is
      given time to end, steps 2 and 5.
   2. **Command children are killed hard and early,** right after the background tasks
      are cancelled and before the group histories are saved. There is no soft signal for
      them on Windows: a `CREATE_NO_WINDOW` child has a console of its own and no process
      group of ours, `GenerateConsoleCtrlEvent` reaches only processes sharing the
      sender's console, and it has no window to close. They are one-off processes, and
      the aim is to leave none, not to say goodbye. Reason `backend_stop`. **Git goes
      last and gets time first** (Mike's call, 2026-10-26): a git killed in the middle
      of a commit leaves `.git/index.lock`, and every later git command in that
      repository fails until a person deletes it. For an agent's auto-commit
      (`git_helper`) that silently breaks all its later auto-commits; for an agent's
      `git_tool` working in the owner's own repository it blocks the owner's commits. So
      the other command children are killed at once, then the `git_tool(...)` children
      get up to `GIT_STOP_GRACE_S` (10 s, `process_registry.py`) to end on their own,
      which they record as `exited`, and those still running are killed as the others
      were, still `backend_stop`, with a WARNING that a `.git/index.lock` may be left in
      the repository of the directory the child ran in (the registry keeps it in memory,
      from `run_supervised`'s `cwd`; a message says so when it is not known) and must be
      removed by hand if git then refuses. The 10 s is one budget for the whole stop,
      counted from the first wait for a git, not 10 s each.
   3. **Engines are stopped softly by their supervisors,** as ADR-040 and ADR-044 say:
      CTRL_BREAK (SIGINT elsewhere) with their grace period, then a kill. Their own stop
      stays first and is not replaced by the job. An engine ended here is `backend_stop`,
      not `stopped` (the precedence rule of point 4). **Strata session files, when DPC saves
      them (ADR-044 point 7: it does not yet), are saved before the job can close,** from
      the same thread, because a hard kill in the middle of a write truncates the file.
   4. **Browsers** close as today, force-killed after 5 s.
   5. **Verify:** for every entry still open, the `pid` is gone, and for the engines the
      card's VRAM is released (ADR-044 point 2 already does this for Strata). An entry
      still running is killed and logged at ERROR, but a git (`git_helper`, or a
      `git_tool(...)` step 2 did not reach): probes are not command children, so step 2
      never touched a `git_helper`, and a git still running here is not a defect. It
      gets what is left of the same budget as step 2, so a stop that spent it there kills
      it at once, and is then killed with the same WARNING about `.git/index.lock`.
   6. **One log line per process** with its exit reason.
   7. **Then the job closes** when the process exits, ending anything missed.
   The backend never kills its own pid, which is checked against the registry's own
   entries: a registry row whose pid is the backend's is a defect and is skipped and logged.

8. **Visibility, and what an agent may do.**
   - A **local API command**, `list_processes` (after `list_providers`, `list_ai_chats`),
     lists the live and the recent processes. It is the one source for the UI and for the
     agent's tool. Its answer is `{"status", "processes", "backend_pid",
     "backend_start_time", "free_commit"}`: the entries as persisted, each live one with
     `commit_mb` read at the request; the daily history added for `since`/`until`; and
     the system part `free_commit` (the GLOSSARY rows **free commit** and **commit_mb**).
   - **An agent sees, and may kill, only its own.** Two agent tools, `list_my_processes`
     (after `list_my_tools`, `list_my_skills`) and `kill_my_process(pid)`. The kill checks
     that the `pid` is a registry entry whose spawn origin names this agent, **never** a
     pid taken from the model's text; a pid that is not in the registry, or is another
     agent's, or is the backend's, is refused with the reason. A process killed this way
     ends `cancelled_by_agent`. Both tools are registered with `default_enabled`
     explicitly (CLAUDE.md, "Adding a new agent tool"); `kill_my_process` is opt-in.
     Agents also see their own resource use against their ceiling before they reach it.
   - **On screen,** a card of its own (the UI card waits on the API command): activity in
     the agent's row of the AGENTS list (working or idle, which chat, since when, thinking
     or tool or command, live process count) and details in Agent Models Configuration,
     shaped like an agent map: the agent, its current and recent runs, their tool calls
     and the processes they started with pid, command, start, resources and how each
     ended. The system section lists Strata, `llamacpp_server` and the browser with RSS and
     VRAM, which are not visible in the UI today. Whether the detail view keeps only current
     work or also the last ten runs was put to Mike and is not decided here.
   - **The graph.** The same card draws the tree of point 3: one branch per agent (its
     conversations, their tool calls, the processes those started and their children), and
     a separate system branch for Strata, `llamacpp_server` and the browser, each rooted at
     the backend. Every node carries its start, duration, resources and exit reason, so
     the path from the root is the answer to who started it, when, from where and how it
     ended. The API command returns the entries with their parent fields; the UI draws the
     tree and the registry does not store a picture.
   - A **human stop** from the UI ends `stopped` with `stop_kind: ui_stop` (point 4);
     it needs no word of its own.

### Rationale

- **Putting the backend itself in the job is the only place that closes the race.** A
  child assigned after `Popen` can already have a child of its own; a process born inside
  the job cannot. It also needs no registry to work, so it protects the sites the registry
  has not reached yet.
- **The sweep is the second line because the job has a hole, and POSIX has none to
  offer.** A job that failed to apply, a pid reused after a crash, and a Linux crash are
  all invisible to a kernel mechanism and visible to a file that names pid and start time.
- **A hard early kill for commands is not rude.** They are one-off, they hold no state we
  keep, and what the incidents cost was the ones left behind.
- **A closed vocabulary because "how it ended" was already in three places in three
  spellings:** the log, `SupervisedRun` (`timed_out`, `exceeded_mb`, `killed`) and the
  tool's text to the agent. One word is what lets a screen count them.
- **A structural test because nothing else holds a rule across 27 sites.** The class has
  a precedent in this repository and it is the one guard that a new contributor cannot
  skip by not knowing.

## Considered Options

- **A. Assign each child to a job after `Popen`.** Rejected: leaves the window in which a
  grandchild is born outside. It is also a per-site rule, so a forgotten site is
  unprotected.
- **B. Create the child suspended (`CREATE_SUSPENDED`), assign it, resume it
  (`NtResumeProcess` through ctypes).** Closes the race for the direct child and is
  correct. Rejected: it is ctypes over an undocumented call, does not exist on Linux or
  macOS, and the backend-in-the-job route makes it unnecessary.
- **C. The startup sweep alone.** Rejected as the only line: it kills only after the next
  start, so a crashed backend leaves its children running until someone starts it again,
  holding the card and the page file the whole time.
- **D. Status quo: kill by tree at the timeout, supervisors for engines.** Rejected: that
  is the situation that produced the three incidents and the question of 2026-10-25.
- **E. The registry only, with the job later.** Rejected as an order of work: the job is
  smaller, covers more, and needs none of the registry to be right; it goes first.
- **F. `PR_SET_PDEATHSIG` and a subreaper on Linux.** Not taken, see point 2.

## Consequences

- **Positive:** an agent can no longer leave a process running past the backend on
  Windows; a crash and a closed window end the tree; every spawn has a named origin;
  "what is running and why" has an answer in the API, the UI and the agent's own tools;
  the engines' fate and the commands' fate are one rule; a new spawn site that bypasses
  the module fails a test.
- **Negative:** a background process an agent starts on purpose (a dev server it leaves
  running) now ends with the backend. That was never promised and is now explicit. A
  future self-restart of the backend, where the new process must outlive the old one,
  would need `BREAKAWAY_OK` for that one launch. The sampler thread and the registry file
  are new moving parts. `run_supervised` reads output incrementally instead of with one
  `communicate`. The state file is rewritten under the registry lock; measured
  2026-10-26 on the owner's Windows machine with a full file (200 finished entries,
  156 KB), `register` takes a median 4.0 ms (p90 4.3) and `end` a median 8.5 ms (p90
  9.1), the JSONL append included; the sampler adds one such write at most every 10 s.
  CPU and IO of descendants are a lower bound (point 3).
- **Neutral:** the memory ceiling stays a poll (`process.py` says why). The POSIX arm of
  the breakaway question stays unmeasured until the Linux node runs it.
- **Not decided here, on purpose:** file permissions from peers (the `proved_connection`
  default and the `no_answer` deadline) were decided the same day and are not part of
  this record; they live in CLAUDE.md and the glossary, as Mike's call set it.

## Confirmation

Compliance, not progress. Every item is a test or a recorded measurement. The tests marked
"registry tests" are in
`dpc-client/core/tests/test_the_process_registry_records_how_every_child_ends.py`
(run 2026-10-25 on Windows; the CI run for the ref has not been read).

- [x] **Job survives nothing.** On Windows: start the backend's job setup in a child
  interpreter, spawn a grandchild through `run_supervised`, `TerminateProcess` the
  interpreter, assert the grandchild is gone. The same for a grandchild started by a
  `cmd /c` in the first milliseconds. (2026-10-25, with a control that survives without
  the job.)
- [x] **Nesting.** The Strata launcher (mock engine) under the backend's job: kill the
  backend, assert the launcher and its engine are gone. (2026-10-25, the real
  `serve.server --engine mock` and `winjob.contain()`.)
- [ ] **Failure is survivable.** With job creation forced to fail, the backend starts,
  logs the WARNING, and the sweep still reaps a recorded orphan.
- [x] **Sweep, with pid reuse.** An entry whose pid is alive with a different start time
  is left alone; with the same start time, it is killed and closed `orphan_swept`.
  (Registry tests `TestTheStartupSweep`: the same-start-time and the different-start-time
  cases, with a real child.) On POSIX a killed child is a zombie until its parent reaps it
  and psutil still gives its creation time, so the probe reads a zombie as gone; without
  that the same-start-time case left the entry open on Linux (CI on `4ea0560f`).
  (`TestAZombieIsGone`.)
- [x] **Never its own pid,** and never an ancestor's.
  (`test_never_its_own_pid_and_never_an_ancestors`,
  `test_the_stop_never_aims_at_the_backends_own_pid`.)
- [x] **Origin from a pool thread.** A tool run through the real `_DaemonToolPool` that
  spawns reads back `(agent_id, conversation_id, tool, round)`.
  (`TestTheOriginReachesThePoolThread`.)
- [x] **The structural test** finds the sites it guards (a positive control), fails on a
  planted `subprocess.run` outside `process.py`, and passes with the allow-list.
  (`dpc-client/core/tests/test_every_spawn_goes_through_the_process_module.py`:
  `test_it_finds_every_allow_listed_site`, `test_it_finds_the_modules_own_spawns`,
  `test_a_planted_spawn_outside_the_module_is_flagged` over fourteen planted forms, the
  aliased `sp.Popen` and a bare `run` imported from `subprocess` among them,
  `test_a_spawn_planted_in_a_new_file_at_the_top_is_flagged` (a new script beside
  `run_service.py` is read; the same under `tests/`, `__pycache__` or a venv is not),
  `test_a_passed_due_a_due_in_words_and_an_entry_with_neither_are_reported`,
  `test_the_same_spawn_inside_the_module_is_not`, `test_what_is_not_a_spawn_is_not_flagged`,
  `test_no_spawn_outside_the_process_module_but_the_allow_list`,
  `test_every_allow_list_entry_still_matches_a_call`, `test_a_stale_entry_is_reported`.)
  Falsified: the `job_object` entry removed, a moved `get_git_info` call put back as an
  aliased `sp.run`, and the alias resolution cut each went red. The helper and the stop
  rule: `dpc-client/core/tests/test_a_short_probe_is_in_the_registry_and_answers_as_subprocess_run_did.py`
  (the same return and exceptions as `subprocess.run`, the locale decoding, the entry,
  the origin inside a tool call; during the stop a `git_helper` refused with no child
  born, a `device_probe` let through only with `while_stopping`
  (`test_a_device_probe_without_the_argument_is_refused_and_no_child_is_born`,
  `test_the_card_reading_of_a_start_or_a_peer_request_is_refused_during_the_stop`), and
  `test_strata_reads_the_card_released_after_its_engine_stops`, which reads `None`
  instead of `True` with the exception removed). Run 2026-10-26 on Windows; Linux waits
  for CI.
- [x] **Git gets time to finish at the stop** (point 7, steps 2 and 5): a git child that
  ends inside the grace is not killed and ends `exited`; one that outlives it is killed,
  `backend_stop`, and the WARNING names `index.lock` and its directory; two slow git
  tools in step 2 cost the grace once, and a git helper at step 5 finds it spent; a
  command that is not git is still killed at once.
  (`TestGitGetsTimeToFinishAtTheStop` in
  `dpc-client/core/tests/test_the_process_registry_records_how_every_child_ends.py`.)
  Run 2026-10-26 on Windows only.
- [ ] **`kill_my_process`** refuses a pid that is not in the registry, another agent's
  pid and the backend's pid, and ends its own as `cancelled_by_agent`.
- [ ] **Exit reasons** are one of the eight words on every path that ends a process, a
  `stopped` carries one of the five `stop_kind` values, and `exit_code` and `signal` are
  stored on every entry (`exited` with 0 included); a test enumerates the code paths.
  (Partly done: the registry refuses a word outside the lists, tests
  `TestTheClosedVocabulary`; the enumeration of every code path is not written.)
- [x] **Stop kinds.** The field is `stop_kind`, not `stop_reason`; a file written before the
  rename is read with its old key as `stop_kind` and written back under the new one;
  `engine_switch` is refused by the closed check. (Registry tests
  `test_the_field_is_stop_kind_so_as_not_to_collide_with_the_gateways_wire_field`,
  `test_a_file_written_before_the_rename_is_read_with_its_old_key_as_stop_kind`,
  `test_the_list_holds_what_something_produces_and_engine_switch_is_not_in_it`.)
- [x] **`engine_died` against `failed_start`.** A Strata engine that died after its start
  succeeded (the watcher, a 503 naming its exit code) is `stopped`/`engine_died`; a start
  the launcher refused (no engine version, the smoke) stays `stopped`/`failed_start`, through
  the real launch. (Registry tests `TestTheSupervisorsRegisterAndEndTheirChild`:
  `test_a_strata_engine_that_died_after_the_start_succeeded_is_stopped_as_engine_died`,
  `test_a_503_naming_the_exit_code_stops_the_launcher_as_engine_died`,
  `test_a_start_the_launcher_refuses_stays_failed_start_through_the_real_launch`.)
- [x] **Precedence.** A stop announced after the backend's orderly stop began is
  `backend_stop`; a stop announced before it keeps its word (a `failed_start` still being
  cleared when the stop begins stays `stopped`/`failed_start`); while the backend runs a
  stop is `stopped`. (Registry tests `TestPrecedence`, and
  `test_the_orderly_stop_is_backend_stop_even_though_the_supervisor_sent_the_signal`.)
  The idle-unload example this item once gave is gone with its producer (point 4).
- [x] **Closed at start.** An entry whose process is gone at the next start is closed
  `backend_stop` with `exit_code: null`, and the log line says it was found closed at
  start. (`test_a_process_that_is_gone_is_closed_backend_stop_with_no_code_and_says_so`.)
- [x] **`crashed` is decided by who sent.** A POSIX timeout (SIGKILL) is recorded
  `timeout`, never `crashed`. Python returns an NTSTATUS death as a negative returncode on
  Windows (`0xC000013A` arrives as −1073741510), so any comparison uses the unsigned value
  and the number only confirms, never decides. A foreign kill reads differently by
  platform: on Windows a `taskkill /T /F` from outside the registry leaves code 1 and reads
  `exited` (`test_on_windows_a_foreign_taskkill_leaves_code_1_and_reads_exited`, run); on
  POSIX a foreign SIGKILL reads `crashed` with `signal` `SIGKILL`
  (`test_on_posix_a_foreign_sigkill_reads_crashed_with_the_signal_by_name`, passed on CI,
  ubuntu, run 40272540455 on `90e1f139`: read from the skip count, two more than on
  `4ea0560f`, exactly the two new Windows-only tests). `0xC000012D` (a commit-limit death) is
  named as an example of `crashed` from recall; it is not measured here.
- [x] **Storage.** The state file is fsynced before the swap; every finished entry is
  appended as one line to the daily file of its end's UTC date, and the state file keeps
  only the newest 200; daily files dated more than 7 days back are removed at start; a
  failed append is logged once and raises nothing; a torn last line and a stale lock are
  survived. (Registry tests `TestTheStateFileAndTheDailyHistory`; the in-memory registry
  writes nothing.)
- [x] **Resource peaks.** Tests in
  `dpc-client/core/tests/test_the_sampler_records_what_each_child_used.py`. With injected
  readers: the tree's peak is the sum and never below the child's own peak, commit stays
  None where the OS gives none, CPU and IO do not fall when a child exits, VRAM is read
  for an engine over its tree and never for a command (`TestThePeaks`); a pid whose probe
  says another process, or whose walk found a different creation time, is not sampled,
  and the same live child recorded with a wrong creation time is not either
  (`TestOnlyTheRecordedProcessIsSampled`); a failing reader and a missing psutil leave
  None after one WARNING (`TestFailuresAreSaidOnce`); a new entry is sampled at once and
  gets its early samples, and the thread stops at `finish_shutdown`
  (`TestTheThread`); the state file is rewritten on a rise of a quarter and not on a
  small one, never twice inside the throttle (`TestThePeaksReachTheFileWhenTheyRiseEnough`).
  Real children: a child holding 150 MB for 3 s has `peak_rss_mb` of at least 100, with
  `peak_commit_mb` on Windows and None on POSIX
  (`test_a_child_that_holds_150_mb_for_three_seconds_has_a_peak_of_at_least_100`); a
  0.2 s command gets RSS, commit, CPU and IO on Windows, RSS, CPU and IO on Linux, RSS and
  CPU on macOS (`test_a_command_shorter_than_the_interval_still_gets_a_reading`); the PDH
  reader returns a mapping on Windows, the NVML reader None where its library is absent.
  Run 2026-10-25 on Windows only; the Linux arms (`VmHWM`, no commit, NVML absent) wait for
  CI on the ref; macOS is not covered by CI and was not run. NVML with a real NVIDIA card on
  Linux was never run. Not tested, and written down in point 3 instead: the lower bound on
  descendants' CPU and IO, and the launcher shim the Windows end reading sees alone.
- [x] **`last_output_at`.** A child that prints, sleeps 2.5 s and prints again has
  `last_output_at` after the second print and its text unchanged
  (`test_the_time_of_the_last_output_is_after_the_second_print_and_the_text_is_unchanged`);
  output with CRLF, a lone CR, invalid UTF-8 and a character split across two reads comes
  back exactly as text mode gave it
  (`test_output_with_crlf_a_lone_cr_and_invalid_bytes_comes_back_as_text_mode_gave_it`);
  the timeout with a grandchild holding the pipe and a kill that misses still returns
  within its bound and returns what the child printed before the timeout
  (`test_the_timeout_returns_within_its_bound_when_a_grandchild_holds_the_pipe`);
  an engine's is its log's mtime from its start on, Strata's the newer of its two logs
  (`test_an_engines_last_output_is_its_logs_mtime_from_its_start_on`,
  `test_a_strata_entry_reads_the_newer_of_the_launchers_and_the_engines_log`). Run
  2026-10-25 on Windows; Linux waits for CI; macOS uncovered. The review fixes of
  2026-10-26: a descendant that escaped the kill and keeps writing to both pipes does not
  keep the readers alive, the call returns within its bound with the text read before
  the timeout, and the bytes held stop growing while the writer still lives
  (`test_an_abandoned_drain_stops_readers_that_an_escaped_writer_keeps_busy`); a child
  writing about a thousand chunks a second is noted at most duration / 0.1 s + 2 times
  and ends with `last_output_at` within 0.2 s of its last write
  (`test_a_chatty_command_is_noted_about_ten_times_a_second_and_ends_at_its_last_bytes`);
  with the throttle wider than the run the end still carries the time of the last bytes
  (`test_the_end_carries_the_time_of_the_last_bytes_and_not_of_the_last_note`); an mtime
  and a stored value whose ISO strings order the other way from their times (another
  offset, no milliseconds, a `Z`) are ordered by time, and a note earlier than the held
  time does not move it back (`TestTimesAreComparedAsTimes`). Each was falsified (the
  stop flag, the returned text, the throttle, the final note, the numeric comparison)
  and went red. Run 2026-10-26 on Windows only.
- [ ] **Parent fields.** A descendant the backend did not start (the `ping` under a
  `cmd /c`) is entered with `parent_pid` and `parent_start_time`, the tree from the
  backend down to it can be rebuilt from the entries alone, and a parent whose start time
  differs from its pid's current one is not followed.
- [x] **`launcher_kind`.** An entry written with `launcher_kind: tool` and no spawn origin
  is reported as a defect, and a subsystem spawn (`strata`, `llamacpp_server`, `browser`)
  is `subsystem`. (`test_a_tool_child_without_an_origin_is_a_lost_attribution`,
  `test_a_subsystem_names_its_parent_and_a_made_up_one_is_refused`; `browser` is not
  registered yet.)
- [x] **No breakaway.** A member of the job that asks for `CREATE_BREAKAWAY_FROM_JOB`
  fails to start (Windows refused with WinError 5), and the control without the job
  starts: `test_a_member_of_the_job_that_asks_for_breakaway_fails_to_start` in
  `dpc-client/core/tests/test_a_backend_in_a_kill_on_close_job_takes_every_process_with_it.py`.
  (Run 2026-10-25, passed.)
- [x] **Shutdown order** holds: spawns refused, then commands killed, then
  engines through their supervisors, then the verify step and one line per process.
  (`test_the_order_is_refuse_then_commands_then_histories_then_engines_then_verify`, a
  recorded order and not a fake clock.)
- [x] **Camoufox and Playwright** launch, browse and close under the job without a
  breakaway error (Windows, 2026-10-25, the opt-in test with `DPC_JOB_CHECKS=1`).

## Scope

- `dpc-client/core/run_service.py` — the job, before `dependency_setup`; the sweep.
- `dpc-client/core/dpc_client_core/dpc_agent/tools/process.py` — registry hook in
  `run_supervised`, the probe helper, incremental output, the sampler, the exit reasons.
- New module for the registry (name proposed, see New words) — entries, persistence,
  sweep, sampler, the contextvar.
- `dpc-client/core/dpc_client_core/managers/llama_server_supervisor.py`,
  `managers/strata_supervisor.py` — register at `_spawn`, report the end; the
  llama-server supervisor's two `nvidia-smi` memory queries through the helper.
- `dpc_agent/utils.py` (9 sites), `device_context_collector.py` (9), `service.py` (1,
  `ip addr`), `run_service.py` (1, `nvidia-smi`) — to the helper.
- `dpc_agent/tools/comfyui.py` — **another developer's;** named, not planned.
- The tool dispatcher in `dpc_agent/loop.py` — sets the context variable.
- `service.py` `shutdown` — the order of point 7; the local API command.
- Two agent tools and their `ToolEntry` defaults; the UI card (separate).
- A structural test; the tests listed under Confirmation.

**Spawn sites counted, 2026-10-25,** in `dpc-client/core/dpc_client_core`, tests excluded,
by `rg` for `subprocess.(run|Popen|call|check_output|check_call)(`,
`create_subprocess_(exec|shell)(`, `os.system(`, `os.popen(`, `os.spawn` and
`multiprocessing` (the last three found nothing), comment lines read out by hand, then
confirmed by reading each hit:

| File | Calls |
|---|---|
| `device_context_collector.py` | 9 `subprocess.run` |
| `dpc_agent/utils.py` | 9 `subprocess.run` (git) |
| `dpc_agent/tools/process.py` | 2 `subprocess.run` (`taskkill`, `ps`: the kill itself) and 1 `subprocess.Popen` (inside `run_supervised`) |
| `managers/llama_server_supervisor.py` | 2 `subprocess.run` (`nvidia-smi` memory queries, free and total) and 1 `create_subprocess_exec` |
| `managers/strata_supervisor.py` | 1 `create_subprocess_exec` |
| `dpc_agent/tools/comfyui.py` | 1 `subprocess.run` (ffmpeg; another developer's) |
| `service.py` | 1 `subprocess.run` (`ip addr show`, Linux) |
| **Total** | **27 in 7 files** (24 run, 1 Popen, 2 exec) |
| `run_service.py`, beside the package | 1 `subprocess.run` (`nvidia-smi`) |

So 25 sites lie outside `process.py`: two are the supervisors' `_spawn`, which stay on
the allow-list, and 23 move to the module. Ark's count was 22 `subprocess.run` and two
supervisors; it is the same list minus the llama-server supervisor's two `nvidia-smi`
memory queries and
`run_supervised`'s own `Popen`. `run_supervised` has four callers (`shell.py` 1, `git.py`
2, `document.py` 1, by `rg`). Orbit (`run_sql`) could confirm only the callers of
`run_supervised`: it resolves no edge to `subprocess.run`, and its per-file counts for the
four callers did not match `rg` (it returned 14 for `git.py` where the file has two), so
the table rests on `rg` and the reading, not on the graph. Not counted: `webbrowser.open`
(`hub_client.py`), and Playwright's and Camoufox's own launches.

**Recounted 2026-10-26, after the move,** by the structural test's walk (every `.py` under
`dpc_client_core` and `run_service.py`, tests excluded, aliases resolved, the call forms of
point 5) and confirmed by `rg`; the count before the move was the table above plus
`job_object.py`'s `subprocess.Popen` (the `explorer.exe` hand-off of `2ebdb0a3`, after the
first count), so 29 calls:

| File | Calls |
|---|---|
| `dpc_agent/tools/process.py` | 2 `subprocess.run` (`taskkill`, `ps`) and 1 `subprocess.Popen` (`run_supervised`, which `run_probe` uses) |
| `managers/llama_server_supervisor.py` | 1 `create_subprocess_exec` (`_spawn`, allow-listed) |
| `managers/strata_supervisor.py` | 1 `create_subprocess_exec` (`_spawn`, allow-listed) |
| `job_object.py` | 1 `subprocess.Popen` (`_launch_explorer`, allow-listed) |
| `dpc_agent/tools/comfyui.py` | 1 `subprocess.run` (ffmpeg, allow-listed with its owner) |
| **Total** | **7 in 5 files**; 22 moved to `run_probe` |

The walk was widened the same day, after review, to every script under `dpc-client/core`
(point 5); the one script beside `run_service.py` that spawns is `probe_rename_orphan.py`,
4 `subprocess.run` (`tasklist` twice, `taskkill` twice), allow-listed: **11 calls in 6
files.**

## Implementation Status

| Task | Status | Commit |
|------|--------|--------|
| This record and the amendments to ADR-040, ADR-044, ADR-036 | Written 2026-10-25 | — |
| Job in `run_service.py`, with its tests (`job_object.py`); the Hub login opens its URL through `explorer.exe`, outside the job | Done 2026-10-25 | `2ebdb0a3` |
| Registry (`process_registry.py`): entries with parent and `launcher_kind`, `SPAWN_ORIGIN` set in the pool thread, the eight exit reasons, persistence; `run_supervised` and both supervisors register and end their children | Done 2026-10-25 | `4ea0560f` |
| Startup sweep | Done 2026-10-25 | `4ea0560f` |
| Shutdown order in `service.shutdown` | Done 2026-10-25 | `4ea0560f` |
| Review fixes: `stop_kind` (old key read), `engine_died`, `engine_switch` removed, precedence for stops announced after the stop began, fsync before the swap, the state file plus daily JSONL history, the asymmetry of a foreign kill written down; tests in `tests/test_the_process_registry_records_how_every_child_ends.py` | Done 2026-10-25 | `91a75a2e` |
| Resource sampler (peaks, `last_output_at`): `ResourceSampler` in `process_registry.py`, one daemon thread every 2 s started by `start_resource_sampler()` from `run_service.py` after logging, stopped by `finish_shutdown()`; per open entry, only while `probe_identity` says "same", the tree's peak RSS and commit (never below the child's own OS peak), CPU and IO summed by (pid, creation time), VRAM for the engines (PDH on Windows, NVML on Linux, none on macOS); a new entry wakes it and gets early samples at 0, 0.1, 0.3 and 0.7 s, and on Windows `end()` reads the child's own lifetime figures through its `Popen` handle; the state file is rewritten when a peak rises by 25 % and 64 MB (256 MiB for VRAM), at most once per 10 s. `run_supervised` reads its pipes as they arrive (text decoded once at the end as text mode gave it) and notes `last_output_at`; an engine's is its log files' newest mtime (`log_paths` from both supervisors; Strata names the launcher's and the engine's log). Tests in `tests/test_the_sampler_records_what_each_child_used.py` | Done 2026-10-26 | `3d380706` |
| Review fixes of the sampler and the readers: an abandoned drain returns the text read so far and stops its readers (they keep nothing more, leave at the next chunk and close their own end of the pipe); the output note throttled to once per 100 ms (`_OUTPUT_NOTE_MIN_INTERVAL_S`) with the exact time of the last bytes given before `end`, and `note_output` never moves the time back; `last_output_at` compared as a time, not an ISO string; the lower bound on descendants' CPU and IO, the launcher shim, the meaning of `null`, the start of the daily history and the measured cost of the state-file write written down | Done 2026-10-26 | `c5966027` |
| Descendants entered by the sampler (parent fields) | Pending | — |
| Spawn sites to the module; structural test: `run_probe` in `process.py` (`run_supervised` with `launcher_kind: subsystem` and bytes), `device_probe` and `git_helper` in `SUBSYSTEMS`, a `device_probe` let through during the stop (point 7); 22 sites moved, the allow-list of four (point 5); tests in `tests/test_every_spawn_goes_through_the_process_module.py` and `tests/test_a_short_probe_is_in_the_registry_and_answers_as_subprocess_run_did.py` | Done 2026-10-26 | `067ca25d` |
| Review fixes of the move: the structural test walks every script under `dpc-client/core` (`probe_rename_orphan.py` allow-listed), an entry's `due` is an ISO date or `None` and fails once passed (`comfyui`: `None`), the `explorer.exe` entry's reason; a device probe let through the stop only with `while_stopping` (Strata's release check alone passes it); git given `GIT_STOP_GRACE_S` (10 s, one budget) at steps 2 and 5 with the `index.lock` WARNING; `SupervisedRun.stdout`/`stderr` typed `str` or `bytes`; tests in the two files above and `tests/test_the_process_registry_records_how_every_child_ends.py` | Done 2026-10-26 | the commit that carries this row |
| Local API command `list_processes` (`ProcessRegistry.list_processes`: the kept entries, the daily history for `since`/`until`, `commit_mb` read at the request on Windows; `free_commit` by `commit_memory.read_free_commit_or_physical`, physical memory by psutil and commit null off Windows); agent tools `list_my_processes` and `kill_my_process` (`ProcessRegistry.cancel_by_agent`, the orderly stop's announce-kill-end path with `cancelled_by_agent`); `FreeCommitReading` and `freeCommitLine` in the UI take a reading with no commit; tests in `tests/test_the_local_api_answers_which_processes_run_now.py` and `tests/test_an_agent_sees_and_ends_only_its_own_processes.py` | Done 2026-10-27 | the commit that carries this row |
| UI: AGENTS row and Agent Models Configuration (own card) | Pending | — |

## Open Questions

- **Q1 — the word for an entry whose process is gone at the next start. Answered,
  2026-10-25 (Mike's call).** The vocabulary has no word for "ended while the backend was
  away, cause unknown". On Windows the cause is the job (`backend_stop` in spirit); on
  POSIX it may be anything. The entry is closed `backend_stop` with `exit_code: null`,
  and the log line says it was found closed at start. The null means "this backend did
  not see the end", so the guess is visible in the data and no ninth word is needed
  (Ark's condition, taken; Zcode's `found_dead` withdrawn).
- **Q2 — what `crashed` means. Answered, 2026-10-25 (Mike's call).** The registry sent
  this process no stop; the number or the signal only confirms it (point 4).
- **Q3 — the browser the OAuth login opens. Answered for that launch, 2026-10-25.** A
  classic Win32 program started through ShellExecute from a member of the job is in the
  job (measured with `ping.exe` and a `.vbs` run by `wscript.exe`: `IsProcessInJob` True,
  parent the backend's interpreter), so a browser the login starts while none is running
  would die with the backend. Started through `explorer.exe <target>`, the same `.vbs`
  landed outside the job in three runs out of three (parent an `explorer.exe` of the
  shell). `hub_client` now opens the login URL with `job_object.open_url_outside_the_job`,
  which takes that road when the job is in place on Windows and falls back to
  `webbrowser.open` otherwise. Not measured with Chrome itself (no real browser was
  opened), and a machine with no running shell would start the target inside the job. A
  browser an agent's own command opens (`start <url>`) is in the job and ends with the
  backend, as point 8's consequence says.
- **Q4 — Camoufox and Playwright under a job with no breakaway. Answered, 2026-10-25.**
  The browser tool's headless launch, a page and a close ran from inside the job with no
  breakaway error; the node driver and all seven `camoufox.exe` were members of the job,
  and nothing was left after the close.
- **Q5 — the detail view: current work only, or the last ten runs.** Asked of Mike on
  2026-10-25, no answer in the thread. Belongs to the UI card.
- **Q6 — a human stop from the UI, and its exit reason. Answered, 2026-10-25 (Mike's
  call).** `stopped` with `stop_kind: ui_stop` (point 4).
- **Q7 — the size and age of the finished-entry window** and the file's name. **Answered,
  2026-10-25 (Mike's call, on the reviews).** The state file `process_registry.json` keeps
  the open entries and the newest 200 finished ones (none older than 7 days); every
  finished entry is also appended to `process_registry/YYYY-MM-DD.jsonl`, files older than
  7 days removed at start (point 3, "Persisted in two places"). A single 200-entry window
  would have cut the graph to hours, and a whole-file rewrite of thousands of entries on
  every spawn and end was judged too heavy.
- **Q8 — an engine stopped by its supervisor while the backend runs. Answered,
  2026-10-25 (Mike's call).** A stop from the UI (a switch to the other local engine
  arrives as the same button) and an unfinished or refused start cleared away end the child
  with a stop DPC sent, outside the backend's own stop; an engine that died after it was up
  and had its launcher stopped is `engine_died`. The word is `stopped`, with `stop_kind`
  saying what kind; a stop announced after the orderly stop began is `backend_stop`
  whoever sent the signal (point 4). An idle unload would be `idle_unload` if DPC ever does
  it (point 4). `inference_engine_stop` and a widened `backend_stop` were not taken.

## New words

Every name this record introduces. Each is built from words the project already has and
is **accepted (Mike's call, 2026-10-25);** the rows are in `docs/GLOSSARY.md`, section
"Processes the backend starts"; the names are in code since `4ea0560f` (the registry core),
the sampler's fields' values since `3d380706`, and the API command and the agent tools
since the commit of their Implementation Status row. `rg --no-ignore` over the repository (this record excluded) on 2026-10-25, before the code,
found no hit for any compound name below, and one for `last_output_at`, on the board card
that proposes it; the same search for `parent_pid`, `parent_start_time` and `launcher_kind`
found hits only on the board card. The plain words in the exit-reason list (`exited`,
`timeout`, `crashed`, `stopped`) are ordinary English with earlier uses of their own.
`stop_kind` was `stop_reason` in the first draft; that name is the gateway's wire field
and is not used for the registry.

| Word | Built from | Note |
|---|---|---|
| **process registry** (`ProcessRegistry`, a module `process_registry.py`, a state file `~/.dpc/process_registry.json` and daily files `~/.dpc/process_registry/YYYY-MM-DD.jsonl`) | `AgentRegistry` in `dpc_agent/utils.py`: a list the backend keeps and persists; the daily files follow the **node ledger**'s way of appending | "registry" is crowded (agents, tools); the qualifier carries it. "Ledger" was not used: the **node ledger** is the usage record. |
| **exit reason** (`exit_reason`), the eight values `exited`, `timeout`, `memory_ceiling`, `backend_stop`, `cancelled_by_agent`, `orphan_swept`, `crashed`, `stopped` | `timed_out`, `ceiling_mb`, `exceeded_mb`, `killed` on `SupervisedRun`; "stop" from `stop_all_supervisors`; "orphan" from `retire_orphan`; `stopped` from `provider_unavailable` ("stopped after an engine failure") | `crashed` has no earlier use; its meaning is Q2, answered in point 4. |
| **`stop_kind`** with `idle_unload`, `ui_stop`, `engine_died`, `failed_start`, `browser_closed` | `launcher_kind` (a closed set that qualifies a broader word); `idle_unload_s` of ADR-044; `_clear_unfinished_start`; `_fail_closed` | Present only on `stopped`. Named `stop_kind` so as not to collide with the `stop_reason` the gateway writes on the wire (the Anthropic Messages API field; the browser tool's scroll loop and the file search have a `stop_reason` of their own too); a file written under the first spelling is read as `stop_kind`. Not `stop_cause`: beside `exit_reason` it would read as a synonym. |
| **`exit_code`**, **`signal`** | `returncode` on `SupervisedRun`; POSIX signal names | Stored on every entry; `exit_code: null` means this backend did not see the end; `signal` is the signal's name (`SIGKILL`). |
| **`parent_pid`**, **`parent_start_time`** | `pid` and the start time of point 3 | Make the registry a tree; the graph of point 8 is a view over it. |
| **spawn origin** (`SPAWN_ORIGIN`, a context variable) with `agent_id`, `conversation_id`, `tool`, `round` | `CALL_PROGRESS` in `llm_adapter.py`; `agent_id` and `conversation_id` are the code's words | "origin" also means who raised a board entry (glossary, **origin**); the two senses are different and do not meet in one file, and both glossary rows now say so. |
| **launcher** values `strata`, `llamacpp_server`, `browser`, `device_probe`, `git_helper`; **`launcher_kind`** `tool` or `subsystem` | `launcher` already exists in `run_supervised` (`run_shell`, `git_tool(sandbox)`, `read_document`); `strata` and `llamacpp_server` are provider types | The first two are existing words; the last three are new labels. `launcher_kind` is new. |
| **resource peaks:** `peak_rss_mb`, `peak_commit_mb`, `peak_vram_mib`, `cpu_seconds`, `io_read_bytes`, `io_write_bytes`; **`last_output_at`** | `exceeded_mb`, `ceiling_mb`; `--min-free-vram-mib`; psutil's names | Units follow the neighbours: megabytes for process memory, MiB for VRAM. |
| **local API command `list_processes`;** agent tools **`list_my_processes`**, **`kill_my_process`** | `list_providers`, `list_ai_chats`; `list_my_tools`, `list_my_skills` | Ark named the two tools; `list_processes` is CC's. |
| **`commit_mb`** on a live entry; the answer's **`backend_pid`**, **`backend_start_time`** and **`free_commit`**, with `source` `psutil` off Windows | `peak_commit_mb`; the state file's envelope; `commit_memory.read_free_commit`'s keys | Put by CC, reviewed by Ark and Zcode; Mike's go, 2026-10-26. |
| **the backend's job** (a phrase, not an identifier) | "kill-on-close job object" of ADR-044 | The OS object is a Job Object; this is how the records refer to the backend's. |
| **orphan sweep** (a phrase for point 6) | `orphan_swept` | Not a function name yet. |

## Authors

- **Mike** — the four calls named in the status note; the go; the graph; the
  cross-platform rule.
- **Ark** — the registry-at-the-source and structural-guard conditions, the closed
  vocabulary, peaks, `last_output_at`, the agent's own-process check, the startup sweep,
  the count of spawn sites; the parent fields, `launcher_kind`, `exit_code: null`,
  `stopped` with its second field and the precedence rule, "who sent" as the test of
  `crashed`, the breakaway check. In the review of the registry core: the name
  `stop_kind` and why (the wire field), `engine_died`, the rule for what stays in the closed
  lists, the 200-entry ceiling on the graph, the window in which a stop announced before
  the backend's stop was rewritten, and the weaker guarantee on POSIX.
- **Zcode** — no soft stop for a `CREATE_NO_WINDOW` child, the job as backstop and the
  session files before it closes, the nested-job test, commit beside RSS; the negative
  returncode of an NTSTATUS death, the MB and MiB families. In the review of the registry
  core: the live measurement that a foreign `taskkill /F` reads `exited` on Windows, the
  signal stored by name, the missing fsync, and the question of a subsystem started inside
  a tool call.
- **CC** — the backend-in-the-job design, this record and its amendments of 2026-10-25;
  the storage split (state file and daily history).

## References

- Cards (gitignored `backlog.md`):
  `THE-BACKEND-KEEPS-NO-LIST-OF-THE-PROCESSES-IT-STARTED-AND-DOES-NOT-END-THEM-WHEN-IT-STOPS`,
  `THE-AGENTS-LIST-SHOWS-WHICH-MODEL-AN-AGENT-RUNS-BUT-NOT-WHAT-IT-IS-DOING`,
  `A-CHILD-PROCESS-OUTLIVES-THE-KILL-AIMED-AT-ITS-WRAPPER`,
  `A-DESCENDANT-THAT-BREAKS-AWAY-ESCAPES-BOTH-THE-TREE-KILL-AND-THE-PROCESS-GROUP`,
  `A-SHELL-COMMANDS-GRANDCHILD-CAN-HOLD-THE-WHOLE-PROCESS-OPEN-FOR-EVER`,
  `THE-SERVICE-CONSOLE-IS-LEFT-IN-RAW-MODE-SO-CTRL-C-NEVER-REACHES-THE-BACKEND`.
- [ADR-040](040-local-inference-second-path-as-a-dpc-owned-llama-server.md) and
  [ADR-044](044-strata-as-a-second-supervised-local-engine.md) — the engines whose
  supervisors stop them first; both amended the same day.
- [ADR-030](030-run-shell-safety-guardrails.md) — what `run_shell` may run; this record
  decides what happens to it afterwards.
- `dpc-client/core/dpc_client_core/dpc_agent/tools/process.py` — `run_supervised`,
  `_kill_process_tree`, `_watch_memory`, `_tree_memory_mb`.
- `dpc-client/core/dpc_client_core/service.py` — `CoreService.shutdown`.
- `dpc-client/core/tests/test_a_spawned_process_gets_no_console.py` — the existing walk of
  the tools directory for spawn sites.
- Cargo, `src/util/job.rs` and `src/bin/cargo/main.rs` in `github.com/rust-lang/cargo`,
  master as read on 2026-10-25.
- `b26f440e` — children without the service console; `811a5303` — the console guard.
