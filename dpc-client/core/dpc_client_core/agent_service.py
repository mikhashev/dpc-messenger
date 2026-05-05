# dpc-client/core/dpc_client_core/agent_service.py
"""
AgentService — DPC Agent lifecycle management (Phase 1d of grand refactor).

Extracted from CoreService. Owns:
- Agent CRUD: create, list, get, update, delete
- Agent task board: get_tasks, get_task_result, schedule_task, cancel_task
- Agent learning progress
- Agent permission profiles
- Token limit resolution

Stays in CoreService (too coupled to inference stack):
- _execute_agent_query
- execute_ai_query
- _agent_auto_revise_proposal (knowledge consensus integration)
"""

import json
import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class AgentService:
    """DPC Agent lifecycle management."""

    def __init__(
        self,
        llm_manager,
        local_api,
        firewall,
        peer_metadata: Dict,
    ):
        self.llm_manager = llm_manager
        self.local_api = local_api
        self.firewall = firewall
        self.peer_metadata = peer_metadata

    def get_state(self) -> Dict[str, Any]:
        """Runtime snapshot for debugging/introspection."""
        try:
            dpc_agent_provider = self.llm_manager.providers.get("dpc_agent") if self.llm_manager else None
            active_agents = list(dpc_agent_provider._managers.keys()) if dpc_agent_provider and hasattr(dpc_agent_provider, '_managers') else []
        except Exception:
            active_agents = []
        return {
            "active_agents": active_agents,
            "agent_provider_configured": (
                "dpc_agent" in self.llm_manager.providers if self.llm_manager else False
            ),
        }

    # --- Core Agent CRUD ---

    async def prepare_agent(self) -> Dict[str, Any]:
        """
        Pre-initialize the DPC Agent provider and start the Telegram bridge.

        Called by the UI when the user creates a new AI Agent chat, ensuring
        that the agent (including Telegram bridge) is ready before any queries
        are sent. This moves initialization from lazy to eager.
        """
        if "dpc_agent" not in self.llm_manager.providers:
            return {
                "status": "error",
                "message": "DPC Agent provider is not configured. Add a 'dpc_agent' provider to ~/.dpc/providers.json"
            }

        try:
            provider = self.llm_manager.providers["dpc_agent"]

            from dpc_client_core.llm_manager import DpcAgentProvider

            if not isinstance(provider, DpcAgentProvider):
                return {
                    "status": "error",
                    "message": f"Provider 'dpc_agent' is not a DpcAgentProvider (type: {type(provider).__name__})"
                }

            current_status = provider.get_status()
            if current_status.get("initialized"):
                logger.info("DPC Agent already initialized")
                return {
                    "status": "success",
                    "message": "DPC Agent already initialized",
                    "agent_status": current_status
                }

            logger.info("Pre-initializing DPC Agent...")
            await provider._ensure_manager()

            agent_status = provider.get_status()

            return {
                "status": "success",
                "message": "DPC Agent initialized successfully",
                "agent_status": agent_status
            }

        except Exception as e:
            logger.error("Error preparing agent: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def create_agent(
        self,
        name: str,
        provider_alias: str = "dpc_agent",
        profile_name: str = "",
        instruction_set_name: str = "general",
        budget_usd: float = 50.0,
        max_rounds: int = 200,
        compute_host: str = "",
    ) -> Dict[str, Any]:
        """
        Create a new DPC Agent with isolated storage.

        Args:
            name: Human-readable agent name
            provider_alias: AI provider to use (from providers.json or remote peer)
            profile_name: Permission profile name (defaults to agent_id for per-agent profiles)
            instruction_set_name: Instruction set for the agent
            budget_usd: Budget limit in USD
            max_rounds: Maximum LLM rounds per task
            compute_host: Optional remote peer node_id — routes LLM calls to that peer
        """
        from .dpc_agent.utils import (
            generate_agent_id,
            create_agent_storage,
            AgentRegistry,
        )

        try:
            agent_id = generate_agent_id(name)
            actual_profile_name = profile_name if profile_name else agent_id

            config = create_agent_storage(
                agent_id=agent_id,
                name=name,
                provider_alias=provider_alias,
                profile_name=actual_profile_name,
                instruction_set_name=instruction_set_name,
                budget_usd=budget_usd,
                max_rounds=max_rounds,
                **({"compute_host": compute_host} if compute_host else {}),
            )

            self.firewall.create_agent_profile(actual_profile_name, copy_from_global=True)

            await self.local_api.broadcast_event("agent_created", {
                "agent_id": agent_id,
                "name": name,
                "provider_alias": provider_alias,
                "profile_name": actual_profile_name,
            })

            return {
                "status": "success",
                "agent_id": agent_id,
                "config": config,
            }

        except Exception as e:
            logger.error("Error creating agent: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e),
            }

    async def list_agents(self) -> Dict[str, Any]:
        """List all registered DPC Agents."""
        from .dpc_agent.utils import AgentRegistry, load_agent_config

        try:
            registry = AgentRegistry()
            agents = registry.list_agents()

            for agent in agents:
                agent_id = agent.get("agent_id", "")
                if agent_id:
                    cfg = load_agent_config(agent_id) or {}
                    if cfg.get("name"):
                        agent["name"] = cfg["name"]
                    if cfg.get("compute_host"):
                        agent["compute_host"] = cfg["compute_host"]

            return {
                "status": "success",
                "agents": agents,
            }

        except Exception as e:
            logger.error("Error listing agents: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e),
            }

    async def get_agent_config(self, agent_id: str) -> Dict[str, Any]:
        """Get configuration for a specific agent."""
        from .dpc_agent.utils import load_agent_config, AgentRegistry

        try:
            registry = AgentRegistry()
            agent_meta = registry.get_agent(agent_id)
            if not agent_meta:
                return {
                    "status": "error",
                    "message": f"Agent not found: {agent_id}",
                }

            config = load_agent_config(agent_id)

            return {
                "status": "success",
                "config": config,
                "metadata": agent_meta,
            }

        except Exception as e:
            logger.error("Error getting agent config: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e),
            }

    async def update_agent_config(
        self,
        agent_id: str,
        updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update configuration for a specific agent."""
        from .dpc_agent.utils import load_agent_config, save_agent_config, AgentRegistry

        try:
            registry = AgentRegistry()

            if not registry.get_agent(agent_id):
                return {
                    "status": "error",
                    "message": f"Agent not found: {agent_id}",
                }

            config = load_agent_config(agent_id)
            config.update(updates)
            config["updated_at"] = self._get_iso_timestamp()
            save_agent_config(agent_id, config)

            registry_updates = {}
            if "name" in updates:
                registry_updates["name"] = updates["name"]
            if "provider_alias" in updates:
                registry_updates["provider_alias"] = updates["provider_alias"]
            if "profile_name" in updates:
                registry_updates["profile_name"] = updates["profile_name"]
            if registry_updates:
                registry.update_agent(agent_id, registry_updates)

            await self.local_api.broadcast_event("agent_updated", {
                "agent_id": agent_id,
                "updates": updates,
            })

            return {
                "status": "success",
                "config": config,
            }

        except Exception as e:
            logger.error("Error updating agent config: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e),
            }

    async def delete_agent(self, agent_id: str) -> Dict[str, Any]:
        """Delete a DPC Agent and its storage."""
        from .dpc_agent.utils import delete_agent_storage, AgentRegistry

        try:
            if agent_id == "default":
                return {
                    "status": "error",
                    "message": "Cannot delete default agent",
                }

            registry = AgentRegistry()

            if not registry.get_agent(agent_id):
                return {
                    "status": "error",
                    "message": f"Agent not found: {agent_id}",
                }

            success = delete_agent_storage(agent_id)

            if success:
                await self.local_api.broadcast_event("agent_deleted", {
                    "agent_id": agent_id,
                })
                return {
                    "status": "success",
                    "message": f"Agent {agent_id} deleted",
                }
            else:
                return {
                    "status": "error",
                    "message": "Failed to delete agent storage",
                }

        except Exception as e:
            logger.error("Error deleting agent: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e),
            }

    async def list_agent_profiles(self) -> Dict[str, Any]:
        """List available agent permission profiles."""
        try:
            profiles = self.firewall.list_agent_profiles() if hasattr(self.firewall, "list_agent_profiles") else ["default"]
            return {
                "status": "success",
                "profiles": profiles,
            }
        except Exception as e:
            logger.error("Error listing agent profiles: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e),
            }

    # --- Agent Task Board Methods (v0.20.0) ---

    async def get_agent_tasks(self, agent_id: str = None) -> Dict[str, Any]:
        """Get agent task history for the Task Board panel.

        Reads:
        - state/task_queue.json for pending/scheduled tasks
        - task_results/*.json for completed tasks (primary source)
        - logs/events.jsonl for legacy completed/failed entries without a result file
        """
        try:
            from .dpc_agent.utils import get_agent_root
            agent_root = get_agent_root(agent_id)

            running: list = []
            scheduled: list = []
            completed: list = []
            failed: list = []

            # --- Pending/scheduled tasks from queue file ---
            queue_file = agent_root / "state" / "task_queue.json"
            if queue_file.exists():
                try:
                    queue_data = json.loads(queue_file.read_text(encoding="utf-8"))
                    for t in queue_data.get("tasks", []):
                        entry = {
                            "id": t.get("id", ""),
                            "type": t.get("task_type", "chat"),
                            "preview": (
                                (t.get("data", {}) or {}).get("message") or
                                (t.get("data", {}) or {}).get("task") or
                                (t.get("data", {}) or {}).get("prompt") or
                                ""
                            )[:200],
                            "status": t.get("status", "pending"),
                            "started_at": t.get("started_at"),
                            "completed_at": t.get("completed_at"),
                            "scheduled_at": t.get("scheduled_at"),
                            "result_preview": None,
                        }
                        if t.get("status") == "running":
                            running.append(entry)
                        elif t.get("status") == "pending":
                            scheduled.append(entry)
                except Exception as e:
                    logger.warning("Failed to parse task queue: %s", e)

            # --- Completed/failed from task_results/ (primary source) ---
            results_dir = agent_root / "task_results"
            seen_task_ids: set = set()
            if results_dir.exists():
                try:
                    result_files = sorted(
                        results_dir.glob("*.json"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    for rf in result_files:
                        try:
                            data = json.loads(rf.read_text(encoding="utf-8"))
                            tid = data.get("task_id", rf.stem)
                            seen_task_ids.add(tid)
                            completed.append({
                                "id": tid,
                                "type": data.get("task_type", "chat"),
                                "preview": (data.get("prompt") or "")[:120],
                                "status": "completed",
                                "started_at": data.get("started_at"),
                                "completed_at": data.get("completed_at"),
                                "scheduled_at": None,
                                "result_preview": (data.get("response") or "")[:200],
                                "has_full_result": True,
                            })
                        except Exception:
                            continue
                except Exception as e:
                    logger.warning("Failed to read task_results dir: %s", e)

            # --- Legacy fallback: events.jsonl for entries without a result file ---
            events_file = agent_root / "logs" / "events.jsonl"
            if events_file.exists():
                try:
                    lines = events_file.read_text(encoding="utf-8").splitlines()
                    pending_start = None
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except Exception:
                            continue
                        ev_type = ev.get("type", "")
                        if ev_type == "task_start":
                            pending_start = ev
                        elif ev_type == "task_complete" and pending_start:
                            tid = ev.get("task_id", "")
                            if tid not in seen_task_ids:
                                completed.append({
                                    "id": tid,
                                    "type": "chat",
                                    "preview": pending_start.get("text_preview", "")[:120],
                                    "status": "completed",
                                    "started_at": pending_start.get("ts"),
                                    "completed_at": ev.get("ts"),
                                    "scheduled_at": None,
                                    "result_preview": (ev.get("response_preview") or "")[:200],
                                    "has_full_result": False,
                                })
                            pending_start = None
                        elif ev_type == "task_failed" and pending_start:
                            tid = ev.get("task_id", "")
                            if tid not in seen_task_ids:
                                failed.append({
                                    "id": tid,
                                    "type": "chat",
                                    "preview": pending_start.get("text_preview", "")[:120],
                                    "status": "failed",
                                    "started_at": pending_start.get("ts"),
                                    "completed_at": ev.get("ts"),
                                    "scheduled_at": None,
                                    "result_preview": None,
                                    "has_full_result": False,
                                })
                            pending_start = None
                except Exception as e:
                    logger.warning("Failed to parse events log: %s", e)

            def _sort_key(e: dict) -> str:
                return e.get("completed_at") or e.get("started_at") or ""
            completed.sort(key=_sort_key, reverse=True)

            return {
                "status": "success",
                "agent_id": agent_id,
                "running": running,
                "scheduled": scheduled,
                "completed": completed[:50],
                "failed": failed,
                "total_completed": len(completed),
            }

        except Exception as e:
            logger.error("Error in get_agent_tasks: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def get_agent_task_result(self, agent_id: str = None, task_id: str = "") -> Dict[str, Any]:
        """Get full result for a specific task from task_results/{task_id}.json."""
        try:
            from .dpc_agent.utils import get_agent_root
            agent_root = get_agent_root(agent_id)
            result_file = agent_root / "task_results" / f"{task_id}.json"
            if not result_file.exists():
                return {"status": "error", "message": f"No result file for task {task_id}"}
            data = json.loads(result_file.read_text(encoding="utf-8"))
            return {"status": "success", **data}
        except Exception as e:
            logger.error("Error in get_agent_task_result: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def get_agent_learning(self, agent_id: str = None) -> Dict[str, Any]:
        """Get agent learning progress from knowledge/llm_learning_schedule.md.

        Parses the '## Progress Tracking' section with standardized format:
            ### Task 1.1: Title
            Status: in_progress
            Last Activity: 2026-03-03
            Session Summary: ...
            Next Step: ...

        Returns stalled status if in_progress and Last Activity > 3 days ago.
        """
        try:
            from .dpc_agent.utils import get_agent_root
            agent_root = get_agent_root(agent_id)

            learning_file = agent_root / "knowledge" / "llm_learning_schedule.md"
            if not learning_file.exists():
                return {
                    "status": "error",
                    "message": f"Learning file not found: {learning_file}",
                }

            content = learning_file.read_text(encoding="utf-8")

            tracking_start = None
            for i, line in enumerate(content.splitlines()):
                if line.strip().startswith("## Progress Tracking"):
                    tracking_start = i + 1
                    break

            if tracking_start is None:
                return {
                    "status": "error",
                    "message": "No '## Progress Tracking' section found in learning file.",
                }

            lines = content.splitlines()
            tracking_lines = []
            for line in lines[tracking_start:]:
                if line.startswith("## ") and tracking_lines:
                    break
                tracking_lines.append(line)

            tracking_text = "\n".join(tracking_lines)
            task_blocks = re.split(r'\n(?=### )', tracking_text.strip())

            tasks_raw = []
            for block in task_blocks:
                block = block.strip()
                if not block.startswith("###"):
                    continue
                first_line = block.splitlines()[0]
                m = re.match(r'^### Task\s+(\d+\.\d+):\s+(.+)$', first_line.strip())
                if not m:
                    continue
                task_num = m.group(1)
                task_title = m.group(2).strip()

                fields: Dict[str, str] = {}
                for line in block.splitlines()[1:]:
                    if ": " in line:
                        key, _, val = line.partition(": ")
                        fields[key.strip()] = val.strip()

                status_raw = fields.get("Status", "pending").lower().strip()
                last_activity_str = fields.get("Last Activity", fields.get("Completed", ""))
                started_str = fields.get("Started", "")
                completed_str = fields.get("Completed", "")

                last_activity_date = None
                for date_str in [last_activity_str, completed_str, started_str]:
                    if date_str:
                        try:
                            last_activity_date = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                            break
                        except ValueError:
                            pass

                days_stalled = None
                status = status_raw
                if status == "in_progress" and last_activity_date:
                    now = datetime.now(timezone.utc)
                    days_since = (now - last_activity_date).days
                    if days_since > 3:
                        status = "stalled"
                        days_stalled = days_since

                tasks_raw.append({
                    "id": f"Task {task_num}",
                    "title": task_title,
                    "status": status,
                    "started_at": started_str or None,
                    "completed_at": completed_str or None,
                    "last_activity": last_activity_str or None,
                    "days_stalled": days_stalled,
                    "session_summary": fields.get("Session Summary") or None,
                    "next_step": fields.get("Next Step") or None,
                })

            phase_map: Dict[str, list] = {}
            for task in tasks_raw:
                major = task["id"].split(".")[0].replace("Task ", "")
                phase_key = f"Phase {major}"
                if phase_key not in phase_map:
                    phase_map[phase_key] = []
                phase_map[phase_key].append(task)

            phases = [{"title": k, "tasks": v} for k, v in sorted(phase_map.items())]

            all_dates = []
            for task in tasks_raw:
                for date_str in [task.get("last_activity"), task.get("completed_at"), task.get("started_at")]:
                    if date_str:
                        try:
                            all_dates.append(datetime.strptime(date_str[:10], "%Y-%m-%d"))
                        except ValueError:
                            pass

            last_session_str = None
            streak_days = 0
            if all_dates:
                last_session_dt = max(all_dates)
                last_session_str = last_session_dt.strftime("%Y-%m-%d")
                now_local = datetime.now()
                days_since = (now_local.date() - last_session_dt.date()).days
                streak_days = 1 if days_since <= 1 else 0

            return {
                "status": "success",
                "agent_id": agent_id,
                "phases": phases,
                "streak_days": streak_days,
                "last_session": last_session_str,
            }

        except Exception as e:
            logger.error("Error in get_agent_learning: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def schedule_agent_task(
        self,
        agent_id: str = None,
        task_type: str = "chat",
        data: Dict[str, Any] = None,
        priority: str = "NORMAL",
        delay_seconds: int = 0,
    ) -> Dict[str, Any]:
        """Schedule an agent task from the Task Board UI."""
        try:
            from .dpc_agent.task_queue import TaskPriority

            dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")
            if not dpc_agent_provider:
                return {"status": "error", "message": "DPC Agent provider not available"}

            if agent_id not in dpc_agent_provider._managers:
                return {"status": "error", "message": f"Agent '{agent_id}' not running. Open an agent chat first."}

            agent_manager = dpc_agent_provider._managers[agent_id]
            await agent_manager.ensure_started()

            if not agent_manager._agent:
                return {"status": "error", "message": "Agent not initialized"}

            try:
                prio = TaskPriority[priority.upper()]
            except KeyError:
                prio = TaskPriority.NORMAL

            task = agent_manager._agent.schedule_task(
                task_type=task_type,
                data=data or {},
                priority=prio,
                delay_seconds=delay_seconds if delay_seconds > 0 else 0,
            )

            return {
                "status": "success",
                "task_id": task.id,
                "task_type": task_type,
                "scheduled_at": task.scheduled_at,
            }

        except Exception as e:
            logger.error("Error in schedule_agent_task: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def cancel_agent_task(self, agent_id: str = None, task_id: str = "") -> Dict[str, Any]:
        """Cancel a pending or running agent task."""
        if not task_id:
            return {"status": "error", "message": "task_id required"}
        try:
            dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")
            if not dpc_agent_provider or not hasattr(dpc_agent_provider, '_managers'):
                return {"status": "error", "message": "Agent provider not available"}

            agent_manager = dpc_agent_provider._managers.get(agent_id)
            if not agent_manager:
                return {"status": "error", "message": f"Agent {agent_id} not found"}

            agent = getattr(agent_manager, '_agent', None)
            if not agent or not hasattr(agent, 'queue'):
                return {"status": "error", "message": "Agent queue not available"}

            queue = agent.queue

            if queue.cancel(task_id):
                logger.info("Cancelled pending task %s for agent %s", task_id, agent_id)
                return {"status": "success", "message": "Task cancelled"}

            proc_task = getattr(queue, '_processor_task', None)
            if proc_task and not proc_task.done():
                running_task = next(
                    (t for t in queue._queue if t.id == task_id and t.status == "running"),
                    None,
                )
                if running_task:
                    proc_task.cancel()
                    logger.info("Cancelled running task %s for agent %s", task_id, agent_id)
                    return {"status": "success", "message": "Running task cancelled"}

            return {"status": "error", "message": "Task not found or already completed"}

        except Exception as e:
            logger.exception("cancel_agent_task error: %s", e)
            return {"status": "error", "message": str(e)}

    # --- Utility Methods ---

    def _resolve_agent_token_limit(self, agent_id: str) -> int:
        """Resolve the context window for an agent using its stored config.

        Priority:
        1. config.json context_window (stored at creation time)
        2. Local provider lookup via provider_alias
        3. peer_metadata cache (for remote-host agents)
        4. Active local model fallback
        """
        if not self.llm_manager:
            return 0
        try:
            from .dpc_agent.utils import load_agent_config
            agent_cfg = load_agent_config(agent_id) or {}
            stored_cw = agent_cfg.get("context_window")
            if stored_cw:
                return int(stored_cw)
            provider_alias = agent_cfg.get("provider_alias", "")
            if provider_alias and provider_alias in self.llm_manager.providers:
                model = self.llm_manager.providers[provider_alias].model
                return self.llm_manager.get_context_window(model) or 0
            compute_host = agent_cfg.get("compute_host", "")
            if compute_host and provider_alias:
                peer_providers = self.peer_metadata.get(compute_host, {}).get("providers", [])
                for p in peer_providers:
                    if p.get("alias") == provider_alias:
                        cw = p.get("context_window")
                        if cw:
                            return int(cw)
                        if p.get("model"):
                            return self.llm_manager.get_context_window(p["model"]) or 0
                        break
            _model = self.llm_manager.get_active_model_name()
            return self.llm_manager.get_context_window(_model) or 0
        except Exception:
            return 0

    def _get_iso_timestamp(self) -> str:
        """Get current timestamp in ISO format."""
        return datetime.now(timezone.utc).isoformat()

    async def get_agent_model_config(self, agent_id: str, providers_getter) -> Dict[str, Any]:
        """Get per-agent model configuration (Main LLM + Sleep LLM)."""
        try:
            from dpc_client_core.dpc_agent.utils import load_agent_config, AgentRegistry
            registry = AgentRegistry()
            if not registry.get_agent(agent_id):
                return {"status": "error", "message": f"Agent not found: {agent_id}"}
            config = load_agent_config(agent_id)
            providers_data = await providers_getter()
            return {
                "status": "ok",
                "agent_id": agent_id,
                "provider_alias": config.get("provider_alias"),
                "sleep_provider_alias": config.get("sleep_provider_alias"),
                "providers": providers_data.get("providers", []),
                "default_provider": providers_data.get("default_provider", ""),
            }
        except Exception as e:
            logger.error("get_agent_model_config failed: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def save_agent_model_config(
        self, agent_id: str,
        provider_alias: str = None,
        sleep_provider_alias: str = None,
        providers_getter=None,
    ) -> Dict[str, Any]:
        """Save per-agent model configuration (Main LLM + Sleep LLM)."""
        try:
            from dpc_client_core.dpc_agent.utils import load_agent_config, save_agent_config, AgentRegistry
            registry = AgentRegistry()
            if not registry.get_agent(agent_id):
                return {"status": "error", "message": f"Agent not found: {agent_id}"}
            config = load_agent_config(agent_id)
            if provider_alias is not None:
                config["provider_alias"] = provider_alias
                registry.update_agent(agent_id, {"provider_alias": provider_alias})
            if sleep_provider_alias is not None:
                config["sleep_provider_alias"] = sleep_provider_alias
            save_agent_config(agent_id, config)
            providers_data = await providers_getter() if providers_getter else {"providers": [], "default_provider": ""}
            return {
                "status": "ok",
                "agent_id": agent_id,
                "provider_alias": config.get("provider_alias"),
                "sleep_provider_alias": config.get("sleep_provider_alias"),
                "providers": providers_data.get("providers", []),
                "default_provider": providers_data.get("default_provider", ""),
            }
        except Exception as e:
            logger.error("save_agent_model_config failed: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    # ─────────────────────────────────────────────────────────────
    # Session archive management
    # ─────────────────────────────────────────────────────────────

    async def get_session_archive_info(self, conversation_id: str) -> Dict[str, Any]:
        """Return archive metadata for a conversation's session archive folder."""
        try:
            archive_dir = Path.home() / ".dpc" / "conversations" / conversation_id / "archive"
            max_sessions = getattr(self.firewall, "history_max_archived_sessions", 0) if self.firewall else 0
            if self.firewall:
                profile = self.firewall.rules.get("agent_profiles", {}).get(conversation_id, {})
                hist = profile.get("history", {}) if profile else {}
                if "max_archived_sessions" in hist:
                    max_sessions = max(0, int(hist["max_archived_sessions"]))

            if not archive_dir.exists():
                return {
                    "status": "success",
                    "conversation_id": conversation_id,
                    "count": 0,
                    "max_sessions": max_sessions,
                    "archive_path": str(archive_dir),
                    "sessions": [],
                }

            archives = sorted(archive_dir.rglob("*_session.json"))
            sessions = []
            for p in archives:
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    sessions.append({
                        "filename": p.name,
                        "archived_at": data.get("archived_at", ""),
                        "reason": data.get("session_reason", ""),
                        "message_count": data.get("message_count", 0),
                    })
                except Exception:
                    sessions.append({"filename": p.name, "archived_at": "", "reason": "", "message_count": 0})

            return {
                "status": "success",
                "conversation_id": conversation_id,
                "count": len(archives),
                "max_sessions": max_sessions,
                "archive_path": str(archive_dir),
                "sessions": sessions,
            }
        except Exception as e:
            logger.error("Error getting session archive info: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def clear_session_archives(self, conversation_id: str, keep_latest: int = 0) -> Dict[str, Any]:
        """Delete archived sessions, optionally keeping the most recent N."""
        try:
            archive_dir = Path.home() / ".dpc" / "conversations" / conversation_id / "archive"

            if not archive_dir.exists():
                return {"status": "success", "deleted_count": 0, "remaining": 0}

            archives = sorted(archive_dir.rglob("*_session.json"))
            keep_latest = max(0, int(keep_latest))
            to_delete = archives[: max(0, len(archives) - keep_latest)]

            deleted = 0
            for p in to_delete:
                try:
                    p.unlink()
                    deleted += 1
                    try:
                        p.parent.rmdir()
                    except OSError:
                        pass
                except Exception as e:
                    logger.warning("Failed to delete archive %s: %s", p.name, e)

            remaining = len(archives) - deleted
            logger.info("Cleared %d archives for %s (%d remaining)", deleted, conversation_id, remaining)
            return {"status": "success", "deleted_count": deleted, "remaining": remaining}
        except Exception as e:
            logger.error("Error clearing session archives: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}
