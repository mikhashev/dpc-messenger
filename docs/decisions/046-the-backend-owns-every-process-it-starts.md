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

> **Accepted, 2026-10-25 (Mike's call). Not implemented.** The sequence Mike set is this
> record and the amendments to ADR-040, ADR-044 and ADR-036 first, then the code. Mike's
> own calls in the thread were three: an agent sees only its own processes; Strata and
> the `llama-server` child join the registry and the job; the list is shown on screen,
> and shows duration, who started a process, when, and what it consumes, so that it is
> useful to agents and to people. Everything else below is the mechanism Ark, Zcode and
> CC converged on, taken into this record by his go. The words the record introduces are
> listed in **New words** and are proposed until he accepts them.
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
   **start time** (psutil creation time; pids are reused); the **launcher** (the existing
   word) and, for an agent's call, the **spawn origin** `agent_id`, `conversation_id`,
   `tool`, `round`, read from a context variable (below); the **command**, bounded in
   length as the log line already bounds it (120 characters, the full text where an
   agent's tool record holds it); start and end time; the **exit reason** (point 4) with
   the return code; **resource peaks**; and `last_output_at`.
   - **Attribution by contextvar.** The tool dispatcher sets `(agent_id, conversation_id,
     tool, round)` in a `contextvars.ContextVar` before it runs a tool; every spawn site
     reads it. The signature of `run_supervised` and its callers stay as they are, and a
     site added next year is attributed for free. The neighbour is
     `llm_adapter.CALL_PROGRESS`. Tools run in pool threads, which do not inherit the
     caller's context by themselves, so the dispatcher must run the tool inside a copy of
     the context (`contextvars.copy_context().run`) or set the variable inside the thread;
     a test spawns from a tool through the real pool and reads the origin back. The field is
     `conversation_id`, the code's word; "chat" is the screen's. A spawn that no tool made
     carries the **subsystem** in `launcher` instead: `strata`, `llamacpp_server` (their
     provider types), `browser`, `device_probe`, `git_helper`.
   - **Resources are peaks, sampled.** One sampler thread, every 2 seconds, over the
     registry: peak working set (RSS) and peak **commit (private bytes)** over the tree,
     CPU seconds, IO read and write bytes, and for the engines peak VRAM. No series is
     stored; a series is read by nobody and grows. Commit is shown beside RSS because
     ADR-044 documents a death by commit with RAM to spare. The sampler reuses
     `_tree_memory_mb` and the `_watch_memory` poll's tree walk (`process.py`), and psutil,
     which is already a dependency.
   - **`last_output_at`** is when a child last wrote. `run_supervised` today reads output
     with one `communicate(timeout=…)`, which hides it, so this field means reading the
     streams incrementally; for an engine it is the modification time of its log file.
     It is what makes a stuck `| more` visible in a glance instead of an afternoon in logs.
   - **Persisted** on disk, written atomically (a temporary file and a replace, never a
     truncating open), on spawn, on end and when a peak rises by a threshold, so that the
     sweep of point 6 can read a crashed run's open entries. Finished entries are kept for
     a bounded window; its size is the implementation card's, not this record's.
   - **A log line at both ends,** one at the start and one at the end that carries the exit
     reason; the start line `spawned pid N …` exists and gains the origin.

4. **Exit reasons are a closed vocabulary,** one word, the same in the log, the API and
   the UI. The seven Ark proposed, with the meaning this record gives each:
   - `exited` — ended on its own with an ordinary return code, zero or not.
   - `timeout` — killed by the caller's timeout.
   - `memory_ceiling` — killed by the memory ceiling (`process.py`, `ceiling_mb`).
   - `backend_stop` — killed by the backend's orderly stop (point 7).
   - `cancelled_by_agent` — killed by its own agent through `kill_my_process` (point 8).
   - `orphan_swept` — a process of an earlier run found alive at start and killed (point 6).
   - `crashed` — ended by a signal or by an NTSTATUS-class code (a code in `0xC0000000`
     and up) that nobody sent; a commit-limit death is this. `0xC000013A`
     (STATUS_CONTROL_C_EXIT) is not: it is what the CTRL_BREAK a supervisor sends
     produces, so the registry, which knows the stop was sent, does not read it as a
     crash. The chat did not define the word; this reading is mine and is marked proposed
     with the rest.
   A new reason is a change to this list and is argued here; a code path that ends a
   process without one of these words is a defect.

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

6. **A startup sweep.** At start, before the first spawn, the backend reads the persisted
   registry. An entry still open whose `pid` is alive **and whose start time matches**
   is killed (its tree, and its process group on POSIX) and closed `orphan_swept`. The
   start time is required: a pid is reused, and killing "pid 39208" a day later kills a
   stranger. Never its own pid and never an ancestor's (the `uv` launcher is the
   backend's parent). An entry whose process is gone is closed without a kill; which word
   closes it is an Open Question. This is the second line behind the job: it is what
   covers a crash on POSIX, a job that failed to apply, and a pid in a run where the
   job did not exist.

7. **Shutdown order.** The job is the backstop, not the mechanism.
   1. **Stop accepting spawns:** the registry refuses new entries (a tool thread in the
      middle of a call gets an error it can report), so nothing is born after the sweep
      below.
   2. **Command children are killed hard and early,** right after the background tasks
      are cancelled and before the group histories are saved. There is no soft signal for
      them on Windows: a `CREATE_NO_WINDOW` child has a console of its own and no process
      group of ours, `GenerateConsoleCtrlEvent` reaches only processes sharing the
      sender's console, and it has no window to close. They are one-off processes, and
      the aim is to leave none, not to say goodbye. Reason `backend_stop`.
   3. **Engines are stopped softly by their supervisors,** as ADR-040 and ADR-044 say:
      CTRL_BREAK (SIGINT elsewhere) with their grace period, then a kill. Their own stop
      stays first and is not replaced by the job. **Strata session files, when DPC saves
      them (ADR-044 point 7: it does not yet), are saved before the job can close,** from
      the same thread, because a hard kill in the middle of a write truncates the file.
   4. **Browsers** close as today, force-killed after 5 s.
   5. **Verify:** for every entry still open, the `pid` is gone, and for the engines the
      card's VRAM is released (ADR-044 point 2 already does this for Strata).
   6. **One log line per process** with its exit reason.
   7. **Then the job closes** when the process exits, ending anything missed.
   The backend never kills its own pid, which is checked against the registry's own
   entries: a registry row whose pid is the backend's is a defect and is skipped and logged.

8. **Visibility, and what an agent may do.**
   - A **local API command** lists the live and the recent processes (proposed name
     `list_processes`, after `list_providers`, `list_ai_chats`). It is the one source for
     the UI and for the agent's tool.
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
   - A **human kill** from the UI is not decided and has no exit reason; if it is added,
     it needs a word in point 4 first.

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
  `communicate`.
- **Neutral:** the memory ceiling stays a poll (`process.py` says why). The POSIX arm of
  the breakaway question stays unmeasured until the Linux node runs it.
- **Not decided here, on purpose:** file permissions from peers (the `proved_connection`
  default and the `no_answer` deadline) were decided the same day and are not part of
  this record; they live in CLAUDE.md and the glossary, as Mike's call set it.

## Confirmation

Compliance, not progress. Every item is a test or a recorded measurement.

- [ ] **Job survives nothing.** On Windows: start the backend's job setup in a child
  interpreter, spawn a grandchild through `run_supervised`, `TerminateProcess` the
  interpreter, assert the grandchild is gone. The same for a grandchild started by a
  `cmd /c` in the first milliseconds.
- [ ] **Nesting.** The Strata launcher (mock engine) under the backend's job: kill the
  backend, assert the launcher and its engine are gone.
- [ ] **Failure is survivable.** With job creation forced to fail, the backend starts,
  logs the WARNING, and the sweep still reaps a recorded orphan.
- [ ] **Sweep, with pid reuse.** An entry whose pid is alive with a different start time
  is left alone; with the same start time, it is killed and closed `orphan_swept`.
- [ ] **Never its own pid,** and never an ancestor's.
- [ ] **Origin from a pool thread.** A tool run through the real `_DaemonToolPool` that
  spawns reads back `(agent_id, conversation_id, tool, round)`.
- [ ] **The structural test** finds the sites it guards (a positive control), fails on a
  planted `subprocess.run` outside `process.py`, and passes with the allow-list.
- [ ] **`kill_my_process`** refuses a pid that is not in the registry, another agent's
  pid and the backend's pid, and ends its own as `cancelled_by_agent`.
- [ ] **Exit reasons** are one of the seven words on every path that ends a process; a
  test enumerates the code paths.
- [ ] **Shutdown order** holds on a fake clock: spawns refused, then commands killed, then
  engines through their supervisors, then the verify step and one line per process.
- [ ] **Camoufox and Playwright** launch, browse and close under the job without a
  breakaway error (Windows, once, by hand, recorded).

## Scope

- `dpc-client/core/run_service.py` — the job, before `dependency_setup`; the sweep.
- `dpc-client/core/dpc_client_core/dpc_agent/tools/process.py` — registry hook in
  `run_supervised`, the probe helper, incremental output, the sampler, the exit reasons.
- New module for the registry (name proposed, see New words) — entries, persistence,
  sweep, sampler, the contextvar.
- `dpc-client/core/dpc_client_core/managers/llama_server_supervisor.py`,
  `managers/strata_supervisor.py` — register at `_spawn`, report the end; version probes
  through the helper.
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
| `managers/llama_server_supervisor.py` | 2 `subprocess.run` (version probes) and 1 `create_subprocess_exec` |
| `managers/strata_supervisor.py` | 1 `create_subprocess_exec` |
| `dpc_agent/tools/comfyui.py` | 1 `subprocess.run` (ffmpeg; another developer's) |
| `service.py` | 1 `subprocess.run` (`ip addr show`, Linux) |
| **Total** | **27 in 7 files** (24 run, 1 Popen, 2 exec) |
| `run_service.py`, beside the package | 1 `subprocess.run` (`nvidia-smi`) |

So 25 sites lie outside `process.py`: two are the supervisors' `_spawn`, which stay on
the allow-list, and 23 move to the module. Ark's count was 22 `subprocess.run` and two
supervisors; it is the same list minus the two `llama-server` version probes and
`run_supervised`'s own `Popen`. `run_supervised` has four callers (`shell.py` 1, `git.py`
2, `document.py` 1, by `rg`). Orbit (`run_sql`) could confirm only the callers of
`run_supervised`: it resolves no edge to `subprocess.run`, and its per-file counts for the
four callers did not match `rg` (it returned 14 for `git.py` where the file has two), so
the table rests on `rg` and the reading, not on the graph. Not counted: `webbrowser.open`
(`hub_client.py`), and Playwright's and Camoufox's own launches.

## Implementation Status

| Task | Status | Commit |
|------|--------|--------|
| This record and the amendments to ADR-040, ADR-044, ADR-036 | Written 2026-10-25 | — |
| Job in `run_service.py`, with its tests | Pending | — |
| Registry, contextvar, exit reasons, sampler, persistence | Pending | — |
| Spawn sites to the module; structural test | Pending | — |
| Startup sweep | Pending | — |
| Shutdown order in `service.shutdown` | Pending | — |
| Local API command; agent tools | Pending | — |
| UI: AGENTS row and Agent Models Configuration (own card) | Pending | — |

## Open Questions

- **Q1 — the word for an entry whose process is gone at the next start.** The vocabulary
  has no word for "ended while the backend was away, cause unknown". On Windows the cause
  is the job (`backend_stop` in spirit); on POSIX it may be anything. CC's pick: close it
  `backend_stop`, with the log line saying it was found closed at start; the alternative
  is a new word. Mike.
- **Q2 — what `crashed` means,** as defined in point 4. Ark proposed the word without a
  definition. Ark, Mike.
- **Q3 — the browser the OAuth login opens.** `hub_client` opens the user's default
  browser with `webbrowser.open`. If that browser starts as a descendant of the backend it
  would be in the job and die with it, which is wrong for a program the user runs. Not
  checked. Test: with the browser closed, open the login URL, close the job, see whether
  the browser survives. If it does not, that one launch needs `BREAKAWAY_OK` or a
  launch through the shell. CC.
- **Q4 — Camoufox and Playwright under a job with no breakaway.** Firefox-family
  browsers use job objects and may ask for breakaway. Not checked; see Confirmation. CC.
- **Q5 — the detail view: current work only, or the last ten runs.** Asked of Mike on
  2026-10-25, no answer in the thread. Belongs to the UI card.
- **Q6 — a human kill from the UI,** and its exit reason. Not decided.
- **Q7 — the size and age of the finished-entry window** and the file's name. The
  implementation card.
- **Q8 — an engine stopped by its supervisor while the backend runs.** An idle unload, a
  stop from the UI and a switch to the other local engine end the child with a stop DPC
  sent, outside the backend's own stop; none of the seven words says that. Candidates: a
  new word for any stop DPC asked for outside its shutdown (`stopped`), or `backend_stop`
  widened to every stop DPC asked for. Mike.

## New words

Every name this record introduces. Each is built from words the project already has and
is **proposed for Mike's acceptance;** none is in `docs/GLOSSARY.md` and none is in code.
`rg --no-ignore` over the repository (this record excluded) on 2026-10-25 found no hit
for any compound name below, and one for `last_output_at`, on the board card that proposes
it. The plain words in the exit-reason list (`exited`, `timeout`, `crashed`) are ordinary
English with earlier uses of their own.

| Word | Built from | Note |
|---|---|---|
| **process registry** (`ProcessRegistry`, a module `process_registry.py`, a file `~/.dpc/process_registry.json`) | `AgentRegistry` in `dpc_agent/utils.py`: a list the backend keeps and persists | "registry" is crowded (agents, tools); the qualifier carries it. "Ledger" was not used: the **node ledger** is the usage record. |
| **exit reason** (`exit_reason`), the seven values `exited`, `timeout`, `memory_ceiling`, `backend_stop`, `cancelled_by_agent`, `orphan_swept`, `crashed` | `timed_out`, `ceiling_mb`, `exceeded_mb`, `killed` on `SupervisedRun`; "stop" from `stop_all_supervisors`; "orphan" from `retire_orphan` | `crashed` has no earlier use; its meaning is Q2. |
| **spawn origin** (`SPAWN_ORIGIN`, a context variable) with `agent_id`, `conversation_id`, `tool`, `round` | `CALL_PROGRESS` in `llm_adapter.py`; `agent_id` and `conversation_id` are the code's words | "origin" also means who raised a board entry (glossary, **origin**); the two senses are different and do not meet in one file. |
| **launcher** values `strata`, `llamacpp_server`, `browser`, `device_probe`, `git_helper` | `launcher` already exists in `run_supervised` (`run_shell`, `git_tool(sandbox)`, `read_document`); `strata` and `llamacpp_server` are provider types | The first two are existing words; the last three are new labels. |
| **resource peaks:** `peak_rss_mb`, `peak_commit_mb`, `peak_vram_mib`, `cpu_seconds`, `io_read_bytes`, `io_write_bytes`; **`last_output_at`** | `exceeded_mb`, `ceiling_mb`; `--min-free-vram-mib`; psutil's names | Units follow the neighbours: megabytes for process memory, MiB for VRAM. |
| **local API command `list_processes`;** agent tools **`list_my_processes`**, **`kill_my_process`** | `list_providers`, `list_ai_chats`; `list_my_tools`, `list_my_skills` | Ark named the two tools; `list_processes` is CC's. |
| **the backend's job** (a phrase, not an identifier) | "kill-on-close job object" of ADR-044 | The OS object is a Job Object; this is how the records refer to the backend's. |
| **orphan sweep** (a phrase for point 6) | `orphan_swept` | Not a function name yet. |

## Authors

- **Mike** — the three calls named in the status note; the go.
- **Ark** — the registry-at-the-source and structural-guard conditions, the closed
  vocabulary, peaks, `last_output_at`, the agent's own-process check, the startup sweep,
  the count of spawn sites.
- **Zcode** — no soft stop for a `CREATE_NO_WINDOW` child, the job as backstop and the
  session files before it closes, the nested-job test, commit beside RSS.
- **CC** — the backend-in-the-job design, this record.

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
