"""
DPC Agent — Skill Store.

Manages agent skills stored in {agent_root}/skills/.

Each skill lives in its own directory:
    skills/{skill-name}/SKILL.md    ← strategy + frontmatter
    skills/_stats.json              ← performance tracking (separate from SKILL.md)

Skills encode *how to act* for a class of problems (procedural knowledge),
complementing the knowledge/ system which stores *what to know* (declarative knowledge).

Skills are bootstrapped with 5 starter strategies on first agent creation,
mirroring how memory.py bootstraps identity.md and scratchpad.md.
"""
from __future__ import annotations

import json
import logging
import pathlib
import re
from typing import Any, Dict, List, Optional, Tuple

from .skill_schema import SkillManifest
from .utils import utc_now_iso, read_text, write_text

log = logging.getLogger(__name__)

try:
    import yaml as _yaml
    _YAML_OK = True
except ImportError:
    _YAML_OK = False
    log.debug("PyYAML not available; skill frontmatter will use fallback parser")


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def _parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """
    Split YAML frontmatter from markdown body.
    Returns (frontmatter_dict, body_text).
    """
    content = content.lstrip()
    if not content.startswith("---"):
        return {}, content

    end = content.find("\n---", 3)
    if end == -1:
        return {}, content

    fm_text = content[3:end].strip()
    body = content[end + 4:].lstrip("\n")

    if _YAML_OK:
        try:
            data = _yaml.safe_load(fm_text) or {}
            return data, body
        except Exception as e:
            log.warning(f"YAML parse error in skill frontmatter: {e}")
            return {}, body

    # Minimal fallback: flat key: value lines only (no nested objects)
    data: Dict[str, Any] = {}
    for line in fm_text.splitlines():
        m = re.match(r'^(\w[\w_-]*)\s*:\s*(.+)$', line.strip())
        if m:
            data[m.group(1)] = m.group(2).strip().strip("\"'")
    return data, body


# ---------------------------------------------------------------------------
# SkillStore
# ---------------------------------------------------------------------------

class SkillStore:
    """
    Manages agent skills in {agent_root}/skills/.

    Usage:
        store = SkillStore(agent_root)
        store.ensure_starter_skills()          # bootstrap on first run
        skills = store.list_skills()           # [{name, description}] for router
        body = store.load_skill_body("code-analysis")  # inject into system prompt
        store.record_outcome("code-analysis", success=True, rounds=4)
    """

    STATS_FILE = "_stats.json"

    def __init__(self, agent_root: pathlib.Path):
        self.agent_root = agent_root

    @property
    def skills_dir(self) -> pathlib.Path:
        return self.agent_root / "skills"

    @property
    def stats_path(self) -> pathlib.Path:
        return self.skills_dir / self.STATS_FILE

    def skill_path(self, name: str) -> pathlib.Path:
        return self.skills_dir / name / "SKILL.md"

    # --- List / Load ---

    def list_skill_names(self) -> List[str]:
        """Return sorted list of installed skill names."""
        if not self.skills_dir.exists():
            return []
        return sorted(
            d.name for d in self.skills_dir.iterdir()
            if d.is_dir() and (d / "SKILL.md").exists()
        )

    def list_skills(self) -> List[Dict[str, str]]:
        """
        Return [{name, description}] for all skills.

        Used by the skill router to build the available-skills list for the LLM.
        The description is the routing key — make it specific and "pushy".
        """
        result = []
        for name in self.list_skill_names():
            manifest = self.load_manifest(name)
            if manifest and manifest.description:
                result.append({
                    "name": manifest.name or name,
                    "description": manifest.description,
                })
        return result

    def list_shareable_skills(self, tags: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Return [{name, description, tags, version}] for skills where sharing.shareable == True.

        Used by inter-agent and P2P skill sharing to expose what this agent is willing to share.
        Optionally filtered by tags (any match).
        """
        result = []
        for name in self.list_skill_names():
            manifest = self.load_manifest(name)
            if manifest is None:
                continue
            if not (manifest.sharing and manifest.sharing.shareable):
                continue
            skill_tags = list(manifest.metadata.tags) if manifest.metadata else []
            if tags and not any(t in skill_tags for t in tags):
                continue
            result.append({
                "name": manifest.name or name,
                "description": manifest.description or "",
                "tags": skill_tags,
                "version": manifest.version or 1,
            })
        return result

    def load_manifest(self, name: str) -> Optional[SkillManifest]:
        """Parse SKILL.md frontmatter for a skill. Returns None if not found."""
        path = self.skill_path(name)
        if not path.exists():
            return None
        try:
            content = read_text(path)
            fm, _ = _parse_frontmatter(content)
            return SkillManifest.from_dict(fm)
        except Exception as e:
            log.warning(f"Failed to load skill manifest '{name}': {e}")
            return None

    def load_skill_content(self, name: str) -> Optional[str]:
        """Load full SKILL.md content (frontmatter + body)."""
        path = self.skill_path(name)
        if not path.exists():
            return None
        try:
            return read_text(path)
        except Exception as e:
            log.warning(f"Failed to load skill content '{name}': {e}")
            return None

    def load_skill_body(self, name: str) -> Optional[str]:
        """Load only the markdown body (no frontmatter) for injection into prompts."""
        content = self.load_skill_content(name)
        if content is None:
            return None
        _, body = _parse_frontmatter(content)
        return body

    # --- Save ---

    def save_skill(self, name: str, content: str) -> None:
        """Write SKILL.md for a skill (creates subdirectory if needed)."""
        path = self.skill_path(name)
        write_text(path, content)
        log.info(f"Saved skill: {name}")

    def mark_as_shared(self, name: str) -> bool:
        """
        Mark a skill as shareable (sharing.shareable = true).

        Called when the user explicitly opts a skill into P2P/local sharing.
        Returns True if the skill was found and updated.
        """
        content = self.load_skill_content(name)
        if content is None:
            return False
        # Patch the shareable field in the YAML sharing block
        import re
        if not content.lstrip().startswith("---"):
            return False
        end = content.find("\n---", 3)
        if end == -1:
            return False
        fm = content[:end + 4]
        body = content[end + 4:]
        pattern = re.compile(r"^(\s+shareable\s*:).*$", re.MULTILINE)
        if pattern.search(fm):
            fm = pattern.sub(r"\1 true", fm)
        else:
            # Append sharing block if absent
            insert_at = end
            fm = content[:insert_at] + "\nsharing:\n  shareable: true" + content[insert_at:end + 4]
            body = content[end + 4:]
        write_text(self.skill_path(name), fm + body)
        log.info(f"Marked skill as shareable: {name}")
        return True

    # --- Stats ---

    def get_stats(self) -> Dict[str, Any]:
        """Load _stats.json. Returns empty dict if not found."""
        if not self.stats_path.exists():
            return {}
        try:
            return json.loads(read_text(self.stats_path))
        except Exception:
            return {}

    def record_outcome(self, skill_name: str, success: bool, rounds: int = 0) -> None:
        """
        Update _stats.json with a task outcome for a skill.

        Called after each task that used a skill (Phase 3 write phase).
        Stats are kept separate from SKILL.md to avoid YAML corruption during evolution.
        """
        stats = self.get_stats()
        entry = stats.get(skill_name, {
            "success_count": 0,
            "failure_count": 0,
            "last_used": "",
            "last_improved": "",
            "avg_rounds": 0.0,
            "improvement_log": [],
        })
        if success:
            entry["success_count"] = entry.get("success_count", 0) + 1
        else:
            entry["failure_count"] = entry.get("failure_count", 0) + 1
        entry["last_used"] = utc_now_iso()

        # Rolling average of LLM rounds (0 = not tracked)
        if rounds > 0:
            total = entry.get("success_count", 0) + entry.get("failure_count", 0)
            if total > 0:
                prev_avg = entry.get("avg_rounds", 0.0)
                entry["avg_rounds"] = round((prev_avg * (total - 1) + rounds) / total, 2)

        stats[skill_name] = entry
        write_text(self.stats_path, json.dumps(stats, indent=2, ensure_ascii=False))

    # --- Bootstrap ---

    def ensure_starter_skills(self) -> None:
        """
        Bootstrap the 5 starter skills if skills/ directory has no skills yet.

        Called on agent creation via create_agent_storage() in utils.py,
        mirroring memory.ensure_files() which bootstraps identity.md + scratchpad.md.
        """
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        if self.list_skill_names():
            return  # Already initialized

        now = utc_now_iso()
        starters = [
            ("skill-creator", self._default_skill_creator),
            ("code-analysis", self._default_code_analysis),
            ("knowledge-extraction", self._default_knowledge_extraction),
            ("p2p-research", self._default_p2p_research),
            ("web-research", self._default_web_research),
        ]
        for name, content_fn in starters:
            self.save_skill(name, content_fn(now))
        log.info(f"Bootstrapped {len(starters)} starter skills in {self.skills_dir}")

    # --- Default skill content ---

    def _default_skill_creator(self, now: str) -> str:
        return f"""---
name: skill-creator
version: 1
description: >
  Create or improve a skill strategy. Use when asked to 'learn from this task',
  'remember this strategy', 'improve how I handle X', or when the same multi-step
  approach recurs across tasks of the same type. Creates structured SKILL.md files
  that persist strategies for future use. Do NOT use for storing facts or knowledge
  — use knowledge_write for that.
provenance:
  source: bootstrapped
  created_at: {now}
  author_node_id: null
  author_name: system
  parent_skill: null
  origin_peer: null
sharing:
  shareable: false
  shared_with_nodes: []
  shared_with_groups: []
  dht_announced: false
metadata:
  execution_mode: knowledge
  required_tools:
    - update_scratchpad
    - drive_write
  required_permissions: []
  agent_profiles:
    - default
  tags:
    - meta
    - learning
---

## Strategy

1. Identify the task class — what general type of problem is this? (code analysis, research, writing, etc.)
2. If improving an existing skill: load the current SKILL.md with `drive_read`, review it, then write an improved version.
3. If creating a new skill:
   a. Choose a kebab-case name (e.g. `code-analysis`, `web-research`)
   b. Write the frontmatter: name, version, description (routing key!), provenance, tags
   c. Write the body: Strategy (numbered steps), When to Use, When NOT to Use, Examples, Common Failures
   d. Save to `{{skills_dir}}/{{name}}/SKILL.md` using `drive_write`
4. The description field is the most important — it must be specific enough for the LLM router to pick this skill correctly.

## When to Use

- User says "learn from this", "remember this approach", "improve how you handle X"
- You notice you're repeating the same multi-step pattern for a class of problems
- A task failed and the strategy could have been better
- You successfully completed a complex task and want to preserve the approach

## When NOT to Use

- Storing facts or information from a conversation → use `knowledge_write` instead
- Quick one-off tasks that won't recur
- Writing code or application files (this skill creates skill strategies only)

## Examples

Creating a new skill after a successful code review:
```
1. Reflect: "I always start with repo_list, then search_in_file for patterns..."
2. Name the skill: "code-analysis"
3. Write SKILL.md with these steps as the Strategy section
4. Save to skills/code-analysis/SKILL.md
```

## Common Failures

- **Description too vague**: "Do things" won't route correctly — be specific about trigger phrases
- **Strategy too abstract**: Write concrete tool call sequences, not high-level descriptions
- **Forgetting edge cases**: Add a "Common Failures" section from real experience

## Update History

- v1 ({now[:10]}): Initial bootstrap
"""

    def _default_code_analysis(self, now: str) -> str:
        return f"""---
name: code-analysis
version: 1
description: >
  Analyze code to understand architecture, find bugs, or review quality. Use when
  asked to examine, review, investigate, or understand code files, repositories, or
  specific functions and classes. Also use when looking for patterns, tracing call
  graphs, or explaining what code does. Do NOT use for writing new code.
provenance:
  source: bootstrapped
  created_at: {now}
  author_node_id: null
  author_name: system
  parent_skill: null
  origin_peer: null
sharing:
  shareable: false
  shared_with_nodes: []
  shared_with_groups: []
  dht_announced: false
metadata:
  execution_mode: knowledge
  required_tools:
    - repo_read
    - repo_list
    - search_files
    - search_in_file
  required_permissions: []
  agent_profiles:
    - default
    - coder
  tags:
    - code
    - analysis
    - review
---

## Strategy

1. **Establish scope**: What exactly needs to be analyzed? (file, module, function, pattern?)
2. **Start broad**: Use `repo_list` to understand directory structure before diving in.
3. **Find entry points**: Search for the main class/function with `search_in_file`.
4. **Read key files**: Use `repo_read` to read identified files. Start with the most relevant.
5. **Trace call graphs**: Follow function calls — if X calls Y, read Y too.
6. **Search for patterns**: Use `search_files` with regex to find all usages of a symbol.
7. **Synthesize**: After reading, explain architecture, data flow, or issues found.

## When to Use

- "Analyze this code", "review this file", "explain how X works"
- "Find the bug in...", "trace the call to...", "what does X call?"
- "Review for security issues", "check code quality", "understand architecture"
- User shares a file path or asks about a specific module

## When NOT to Use

- Writing new code or making changes — this is read-only analysis
- Running the code — use a shell tool for execution
- Simple factual questions about language syntax (no files needed)

## Examples

Analyzing a module's architecture:
```
1. repo_list("dpc_agent/") → understand what files exist
2. search_in_file("agent.py", "class DpcAgent") → find the main class
3. repo_read("agent.py") → read the full class
4. search_in_file("agent.py", "def ") → list all methods
5. Follow imports → read Memory, ToolRegistry, etc.
```

## Common Failures

- **Reading too much at once**: Read entry point first, then follow references
- **Missing the bigger picture**: Always start with repo_list before reading files
- **Ignoring imports**: The import section reveals the dependency graph
- **Not searching for usages**: A class definition alone doesn't show how it's used

## Update History

- v1 ({now[:10]}): Initial bootstrap
"""

    def _default_knowledge_extraction(self, now: str) -> str:
        return f"""---
name: knowledge-extraction
version: 1
description: >
  Extract reusable knowledge from conversation for a knowledge commit. Use when the
  conversation contains a decision, finding, architectural insight, or lesson worth
  committing to long-term memory. Also use when asked to 'remember this', 'save that',
  or 'document this decision'. Do NOT use for scratchpad notes or temporary working
  memory — only for durable, shareable knowledge.
provenance:
  source: bootstrapped
  created_at: {now}
  author_node_id: null
  author_name: system
  parent_skill: null
  origin_peer: null
sharing:
  shareable: false
  shared_with_nodes: []
  shared_with_groups: []
  dht_announced: false
metadata:
  execution_mode: knowledge
  required_tools:
    - knowledge_write
    - knowledge_list
    - knowledge_read
  required_permissions: []
  agent_profiles:
    - default
  tags:
    - knowledge
    - memory
    - learning
---

## Strategy

1. **Identify what's worth keeping**: Is this a decision, insight, lesson learned, or fact useful later?
2. **Check existing knowledge**: Use `knowledge_list` to see existing topics. Use `knowledge_read(topic)` if a related topic exists.
3. **Choose a topic name**: Kebab-case, descriptive (e.g. `authentication-design`, `gpu-requirements`)
4. **Structure the content** (markdown format):
   - Summary: 1-3 sentence overview
   - Key Points: bullet list of the most important facts/decisions
   - Context: why this was decided / what problem it solves
   - Examples: concrete examples if applicable
5. **If topic exists**: Merge new information — don't overwrite, build on it.
6. **Write**: Use `knowledge_write(topic, content)` to save.

## When to Use

- User says "remember this", "save that", "document this", "make a note of"
- Conversation produces a clear decision with rationale (architecture, design, policy)
- A lesson was learned from a failure or debugging session
- A factual insight that will be useful in future conversations

## When NOT to Use

- Temporary notes for current task → use `update_scratchpad` instead
- Personal preferences without clear facts
- Information already in code comments or docs
- Routine task execution with no lasting insight

## Examples

Extracting an architecture decision:
```
User: "We decided to use SQLite for the knowledge base because..."
1. knowledge_list() → check if "knowledge-storage-architecture" exists
2. Structure: Summary + Decision + Rationale + Alternatives Considered
3. knowledge_write("knowledge-storage-architecture", structured_content)
```

## Common Failures

- **Too granular**: One fact per commit is too fine — group related facts into a topic
- **No context**: "We use X" without "because Y" loses the rationale
- **Overwriting**: Always check knowledge_read first, then merge
- **Wrong topic name**: Too generic ("notes") or too specific ("meeting-2026-03-24")

## Update History

- v1 ({now[:10]}): Initial bootstrap
"""

    def _default_p2p_research(self, now: str) -> str:
        return f"""---
name: p2p-research
version: 1
description: >
  Research using connected DPC peers — their knowledge base, AI capabilities, or GPU
  resources. Use when the task benefits from multiple perspectives, when asking about
  shared projects or knowledge, or when local inference is insufficient. Also use when
  asked to consult peers, get a second opinion, or leverage a peer's specialized model.
  Do NOT use for internet research — use web-research for that.
provenance:
  source: bootstrapped
  created_at: {now}
  author_node_id: null
  author_name: system
  parent_skill: null
  origin_peer: null
sharing:
  shareable: false
  shared_with_nodes: []
  shared_with_groups: []
  dht_announced: false
metadata:
  execution_mode: knowledge
  required_tools:
    - send_user_message
    - request_inference
  required_permissions:
    - compute.enabled
  agent_profiles:
    - default
  tags:
    - p2p
    - research
    - peers
    - inference
---

## Strategy

1. **Identify the right peer**: Who has the relevant knowledge or capability?
   - For specialized models: check who has compute sharing enabled
   - For shared knowledge: ask peers working on the same project
2. **Frame the request clearly**: Be specific about what you need (knowledge query vs. inference)
3. **For inference requests**: Use `request_inference(peer_node_id, query, context)` to send to a peer's LLM
4. **For knowledge sharing**: Use `send_user_message` to reach the human who can consult their peer network
5. **Synthesize responses**: Multiple perspectives may conflict — identify consensus and dissent
6. **Attribute sources**: Note which peer provided which information

## When to Use

- Task requires a specialized model the local agent doesn't have
- Multiple perspectives would improve quality (architecture decisions, code review)
- User explicitly asks to "ask Alice" or "check with the team"
- Local knowledge is insufficient and a connected peer may have the answer
- Heavy compute task better suited to a peer with a stronger GPU

## When NOT to Use

- Information available locally or via web search
- Tasks where peer latency would be prohibitive
- Private or sensitive queries that shouldn't leave local context
- When no peers are connected

## Examples

Getting a second opinion on architecture:
```
1. Identify peers with relevant knowledge (from connected peers list)
2. Frame specific questions: "Does this handle concurrent writes correctly?"
3. request_inference(peer_node_id, question, context={{"code": relevant_code}})
4. Compare response with local analysis
```

## Common Failures

- **Asking vague questions**: Peers need specific questions to give useful answers
- **Not checking peer availability**: Peers may be offline — have a local fallback
- **Privacy leak**: Don't send sensitive code or personal data to peers without user consent
- **Over-relying on peers**: Local analysis first, peer consultation for validation

## Update History

- v1 ({now[:10]}): Initial bootstrap
"""

    def _default_web_research(self, now: str) -> str:
        return f"""---
name: web-research
version: 1
description: >
  Research a topic using web search and page analysis. Use when asked to find current
  information, verify facts, investigate something online, or when local knowledge may
  be outdated. Start with search_web, read top results with browse_page, and
  cross-reference key claims. Do NOT use for code in the local repository — use
  code-analysis for that.
provenance:
  source: bootstrapped
  created_at: {now}
  author_node_id: null
  author_name: system
  parent_skill: null
  origin_peer: null
sharing:
  shareable: false
  shared_with_nodes: []
  shared_with_groups: []
  dht_announced: false
metadata:
  execution_mode: knowledge
  required_tools:
    - search_web
    - browse_page
  required_permissions: []
  agent_profiles:
    - default
  tags:
    - web
    - research
    - search
---

## Strategy

1. **Formulate query**: Extract the core question. Add qualifiers (year, version) if recency matters.
2. **Search**: `search_web(query)` — get top results.
3. **Select sources**: Prefer official docs, reputable publications, recent dates.
4. **Read top 2-3 pages**: `browse_page(url)` for each selected result.
5. **Cross-reference**: If two sources agree on a key fact, it's likely correct. Conflicts need resolution.
6. **Synthesize**: Combine findings into a clear answer with source citations.
7. **Note currency**: If information might change (APIs, versions), note the retrieval date.

## When to Use

- "Find information about X", "what is the current X", "look up X"
- Verifying a claim that may be outdated in local knowledge
- API documentation, library versions, compatibility tables
- News, recent events, current best practices

## When NOT to Use

- Local code analysis — use code-analysis skill instead
- Simple factual questions answerable from memory
- Private internal documents (web search won't find them)
- When user explicitly says to use local knowledge only

## Examples

Researching a library version:
```
1. search_web("pytorch 2.5 cuda compatibility matrix")
2. browse_page(official_pytorch_docs_url)
3. browse_page(secondary_source_url)
4. Cross-reference CUDA version requirements
5. Report: "PyTorch 2.5 requires CUDA 11.8+ (verified from pytorch.org, 2026-03)"
```

## Common Failures

- **Single source**: One source can be wrong — always cross-reference important claims
- **Ignoring dates**: Tech docs go stale; check publication dates
- **Too broad queries**: "Python" returns millions of results; be specific
- **Not reading the page**: Search snippets are often incomplete — browse_page for full context

## Update History

- v1 ({now[:10]}): Initial bootstrap
"""
