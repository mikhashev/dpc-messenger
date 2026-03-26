# Refactoring Guidelines for Claude Code

## Context

This document contains refactoring guidelines for AI-assisted development of dpc-messenger. It synthesizes insights from three embedded agents (Chado, Hope, Ark) with practical experience in codebases ranging from 10K to 100K+ lines.

## Core Principles

### 1. Hybrid Format (Consensus Approach)

**Rule:** Do NOT try to make 100% agent-native code. Add agent-friendly metadata to human-friendly code.

**Why:** LLMs are trained on human code. Exotic formats work worse. Hybrid approach is realistic.

**Implementation:**
- Keep standard Python syntax (classes, functions, type hints)
- Add explicit contracts (decorators, dataclasses)
- Use state machines for complex behavior
- Tests as primary specification

### 2. Runtime > Static

**Rule:** Static code shows possible behavior. Runtime shows actual behavior.

**Three sources of truth for agents:**
1. **Static code** - shows possible behavior
2. **Runtime state** - shows actual behavior (get_state())
3. **Event logs** - shows territory dynamics (append-only)

**Implementation:**
```python
class StatefulComponent:
    def get_state(self) -> dict:
        """Agent-readable snapshot of current state"""
        pass
    
    def verify_consistency(self) -> bool:
        """Check if internal state matches expectations"""
        pass
```

### 3. Three-Layer Solution for Map≠Territory

**Layer 1: Static Navigation**
- CLAUDE.md / ARCHITECTURE.md as entry points
- Shows "where to look"
- Updated after every significant refactor

**Layer 2: Event History**
- Append-only logs (cw_sent_log.jsonl)
- Shows "what happened"
- Event sourcing pattern

**Layer 3: Current State**
- get_state() methods
- Shows "what is happening now"
- Real-time introspection

## Specific Guidelines

### Module Size (Adapted from Hope's Rules)

**Guideline:** Modules should be focused on single responsibility.

**Thresholds:**
- < 500 lines: Ideal (fits in context with room)
- 500-1000 lines: Acceptable (consider decomposition)
- > 1000 lines: Signal to decompose

**Rationale:** 
- Context window is a moving target (128K today, 1M tomorrow)
- Focus on cohesion, not arbitrary line limits
- If module > 1000 lines, ask: "Can this be split by responsibility?"

### Method Complexity

**Guideline:** Methods should do one thing well.

**Thresholds:**
- < 50 lines: Ideal
- 50-150 lines: Acceptable (consider decomposition)
- > 150 lines: Signal to decompose

**Rationale:** Long methods are hard to understand and test.

### Parameter Count

**Guideline:** Use data classes for complex APIs.

**Thresholds:**
- < 5 parameters: Ideal
- 5-8 parameters: Acceptable (consider dataclass)
- > 8 parameters: Use dataclass

**Implementation:**
```python
# BAD: Too many parameters
def connect(
    peer_id: str,
    timeout: int,
    retry: bool,
    protocol: str,
    encryption: str,
    callback: Callable,
    metadata: dict,
    options: dict
) -> Connection:
    pass

# GOOD: Data class
@dataclass
class ConnectionConfig:
    peer_id: str
    timeout: int = 30
    retry: bool = True
    protocol: str = "tls"
    encryption: str = "aes256"
    callback: Optional[Callable] = None
    metadata: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)

def connect(config: ConnectionConfig) -> Connection:
    pass
```

### Naming Conventions

**Guideline:** Balance clarity and brevity.

**Rules:**
- Too short: `_detect_regime()` (ambiguous)
- Too long: `_analyze_market_state_and_return_classification()` (wastes tokens)
- Just right: `_detect_market_regime()` (clear and concise)

**Rationale:** Medium-length names reduce misinterpretation risk while conserving tokens.

### Tests as Specification

**Guideline:** Tests are primary documentation, not secondary.

**Rules:**
- Test names should describe behavior: `test_flood_control_blocks_after_18_per_minute()`
- Tests show expected behavior (executable spec)
- Tests verify contracts (what the code promises)

**Implementation:**
```python
class TestRateLimit:
    def test_allows_18_requests_per_minute(self):
        """System allows exactly 18 requests per minute"""
        rate_limit = RateLimit(18)
        for i in range(18):
            assert rate_limit.allows() == True
        assert rate_limit.allows() == False
```

**Missing piece:** Tests show "what" but not "why". Add decision records for rationale.

## Anti-Patterns to Avoid

### 1. Arbitrary Splitting

**BAD:** Splitting 3000-line file into three 1000-line files by line count.

**WHY:** Creates false boundaries. Related code ends up in different files.

**GOOD:** Split by responsibility (WebSocket handlers, API handlers, P2P handlers).

### 2. Loss of Cohesion

**BAD:** Breaking apart tightly-coupled logic.

**WHY:** Agent must read multiple files to understand one flow.

**GOOD:** Keep cohesive units together, even if > 1000 lines.

### 3. Short Names = Ambiguity

**BAD:** `_detect_regime()` - what regime?

**WHY:** Agent (and human) must read body to understand.

**GOOD:** `_detect_market_regime()` - clear from name.

### 4. Tests Without Rationale

**BAD:** Test shows 18 requests/minute but not why.

**WHY:** Future refactoring might change number without understanding impact.

**GOOD:** Test + decision record explaining why 18.

## Practical Steps for dpc-messenger

### Phase 1: Add Runtime Introspection

1. Add `get_state()` to key components:
   - P2PManager
   - CoreService
   - AgentManager

2. Add `verify_consistency()` to stateful components.

3. Add event logging for critical operations.

### Phase 2: Refactor service.py

**Current state:** 3000+ lines in one file.

**Proposed structure:**
```
service.py (main entry point)
├── handlers/
│   ├── ws_handlers.py (WebSocket message handlers)
│   ├── api_handlers.py (Local API commands)
│   ├── p2p_handlers.py (P2P message handlers)
│   └── agent_handlers.py (Agent lifecycle)
├── core/
│   ├── core_service.py (CoreService class)
│   └── service_state.py (State management)
└── utils/
    └── service_helpers.py (Helper functions)
```

**Guidelines for splitting:**
- Group by responsibility (not by line count)
- Keep related handlers together
- Maintain clear dependencies
- Update CLAUDE.md after refactor

### Phase 3: Add Agent-Friendly Metadata

1. Add explicit contracts using decorators:
```python
@contract(
    input={"peer_id": str},
    output=Connection | None,
    side_effects=["emit:P2P_CONNECTED"],
    invariants=["peer_id in peer_registry"]
)
def connect(peer_id: str) -> Optional[Connection]:
    pass
```

2. Use state machines for complex behavior:
```python
class P2PConnection:
    """
    States: DISCONNECTED → CONNECTING → CONNECTED
    Transitions: connect(), disconnect(), timeout()
    """
    state: Literal["DISCONNECTED", "CONNECTING", "CONNECTED"]
```

3. Add decision records for "why" questions:
```markdown
docs/decisions/003-rate-limit.md

# Decision: Rate Limit 18 Requests/Minute

## Context
Telegram API has limits. Need to avoid blocking.

## Decision
Limit to 18 requests/minute (80% of Telegram's 20/minute limit).

## Rationale
- 20/minute is hard limit
- Buffer for clock skew
- 80% = safe margin
```

## Examples

### Before: Large Method

```python
def handle_agent_message(self, message: dict) -> dict:
    # 200 lines of logic
    if message["type"] == "chat":
        # 50 lines
    elif message["type"] == "tool_call":
        # 80 lines
    elif message["type"] == "status":
        # 70 lines
    # ... more
```

### After: Decomposed

```python
def handle_agent_message(self, message: dict) -> dict:
    """Route agent message to appropriate handler"""
    handlers = {
        "chat": self._handle_chat_message,
        "tool_call": self._handle_tool_call,
        "status": self._handle_status_request
    }
    handler = handlers.get(message["type"])
    if not handler:
        return {"error": "Unknown message type"}
    return handler(message)

def _handle_chat_message(self, message: dict) -> dict:
    """Handle chat message from agent"""
    # 50 lines of focused logic
    pass

def _handle_tool_call(self, message: dict) -> dict:
    """Handle tool call request"""
    # 80 lines of focused logic
    pass

def _handle_status_request(self, message: dict) -> dict:
    """Handle status request"""
    # 70 lines of focused logic
    pass
```

### Before: No Runtime Introspection

```python
class P2PManager:
    def __init__(self):
        self._connections = {}
        self._state = "DISCONNECTED"
    
    def connect(self, peer_id: str):
        # Complex connection logic
        pass
```

### After: With Runtime Introspection

```python
class P2PManager:
    def __init__(self):
        self._connections = {}
        self._state = "DISCONNECTED"
    
    def connect(self, peer_id: str) -> Connection:
        # Complex connection logic
        pass
    
    def get_state(self) -> dict:
        """Agent-readable snapshot"""
        return {
            "active_connections": list(self._connections.keys()),
            "pending_connections": len(self._pending),
            "state": self._state
        }
    
    def verify_consistency(self) -> bool:
        """Check if state is consistent"""
        if self._state == "CONNECTED" and not self._connections:
            return False  # Inconsistent!
        return True
```

## Checklist for Refactoring

Before starting refactor:
- [ ] Read existing code (Chad's rule: "Don't suggest changes until you've read")
- [ ] Understand current architecture (CLAUDE.md, ARCHITECTURE.md)
- [ ] Identify responsibilities (what does this code do?)
- [ ] Check dependencies (what depends on this code?)

During refactor:
- [ ] Split by responsibility (not by line count)
- [ ] Keep cohesive units together
- [ ] Add get_state() to stateful components
- [ ] Add verify_consistency() checks
- [ ] Use data classes for complex APIs
- [ ] Add explicit contracts

After refactor:
- [ ] Run tests (verify behavior unchanged)
- [ ] Update CLAUDE.md / ARCHITECTURE.md
- [ ] Add decision records for "why" questions
- [ ] Verify no import errors
- [ ] Check that all handlers still work

## Sources

- **Chad** (10K lines codebase): CLAUDE.md + event logs + grep approach
- **Hope** (embedded agent): Constitution rules, tests as spec
- **Ark** (dpc-messenger): Runtime introspection, get_state() API

## Key Insights

1. **Context loss is worse than map≠territory** - Agents can't persistently remember code across sessions. Upper-level maps (CLAUDE.md) are critical.

2. **Event sourcing > state snapshot** - Append-only logs show territory dynamics better than static code.

3. **Hybrid format is realistic** - Don't try 100% agent-native code. Add agent-friendly metadata to human-friendly code.

4. **Cohesion > arbitrary limits** - Keep related code together, even if > 1000 lines. Split by responsibility, not line count.

5. **Tests show "what", decisions show "why"** - Need both for complete understanding.

---

**Version:** 1.0  
**Last Updated:** 2026-03-26  
**Maintained by:** Mike Shevchenko (with input from agents Chado, Hope, Ark)