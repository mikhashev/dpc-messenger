"""Handlers for group chat commands."""

import hashlib
import time
import uuid
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from . import MessageHandler
from dpc_protocol.message_signing import PREIMAGE_VERSION, message_content_hash
from ..conversation_monitor import (
    Message as ConvMessage,
    ConversationMonitor,
    authors_that_differ_between,
    digest_for,
)
from .group_access import may_share_group, refuse_group_access


class GroupCreateHandler(MessageHandler):
    """Handles GROUP_CREATE messages (group invite from creator)."""

    @property
    def command_name(self) -> str:
        return "GROUP_CREATE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_CREATE message.

        Creator is inviting this node to a new group. Store metadata locally
        and notify UI for acceptance.

        Args:
            sender_node_id: Node ID of group creator
            payload: Group metadata dict (group_id, name, topic, members, etc.)
        """
        group_id = payload.get("group_id")
        name = payload.get("name", "")

        self.logger.info(
            "Received GROUP_CREATE from %s: group=%s name='%s'",
            sender_node_id[:20], group_id, name
        )

        # Apply via sync (creates local copy)
        group = self.service.group_manager.apply_sync(payload)
        if group:
            # Notify UI of new group invite
            await self.service.local_api.broadcast_event("group_invite_received", {
                "group_id": group.group_id,
                "name": group.name,
                "topic": group.topic,
                "created_by": group.created_by,
                "creator_name": self.service.peer_metadata.get(
                    sender_node_id, {}
                ).get("name", sender_node_id),
                "members": group.members,
            })

            # Request conversation history from the sender (group creator/admin)
            import uuid
            request_id = str(uuid.uuid4())[:8]
            self.service.history_requests.note(sender_node_id, group.group_id, request_id)
            await self.service.p2p_manager.send_message_to_peer(sender_node_id, {
                "command": "REQUEST_CHAT_HISTORY",
                "payload": {
                    "conversation_id": group.group_id,
                    "request_id": request_id,
                }
            })
            self.logger.info("Requested history for group %s from %s", group.group_id, sender_node_id[:16])

        return None


class GroupTextHandler(MessageHandler):
    """Handles GROUP_TEXT messages (text messages in group chat)."""

    @property
    def command_name(self) -> str:
        return "GROUP_TEXT"

    def _authenticate_author(self, transport_node_id, payload):
        """Decide who authored this, and how sure we are.

        Returns (author_node_id, verification, signature_fields), where
        verification is one of:
          verified   — the signature checks out against the claimed author
          unverified — cannot check yet (peer certificate not cached); kept,
                       flagged, and re-checkable later. Rejecting here would be
                       a denial of service against ourselves on first contact.
          legacy     — no signature fields at all; a node that predates this.
                       Author falls back to the transport, because a claimed
                       sender_node_id with nothing behind it is worth less than
                       the socket it came from.
          rejected   — a signature that is present and wrong.
        """
        claimed = payload.get("sender_node_id")
        content_hash = payload.get("content_hash")
        signature = payload.get("signature")
        signer = payload.get("signer_node_id")

        if not (content_hash and signature and signer):
            return transport_node_id, "legacy", None

        if payload.get("preimage_version") != PREIMAGE_VERSION:
            # Signed over a preimage we do not know how to recompute. Treated
            # as legacy rather than rejected: this is what a node one version
            # ahead or behind looks like, and cutting it off is not a security
            # decision, it is an outage.
            return transport_node_id, "legacy", None

        if signer != claimed:
            self.logger.warning(
                "Rejecting group message %s: signed by %s but claims %s",
                str(payload.get("message_id"))[:8], str(signer)[:20], str(claimed)[:20]
            )
            return transport_node_id, "rejected", None

        expected = message_content_hash(
            conversation_id=payload.get("group_id"),
            message_id=payload.get("message_id"),
            sender_node_id=claimed,
            sender_name=payload.get("sender_name"),
            sender_type=payload.get("sender_type"),
            agent_owner=payload.get("agent_owner"),
            timestamp=payload.get("timestamp"),
            content=payload.get("text") or "",
            tool_calls=payload.get("tool_calls"),
        )
        if expected != content_hash:
            self.logger.warning(
                "Rejecting group message %s from %s: content does not match its hash",
                str(payload.get("message_id"))[:8], str(claimed)[:20]
            )
            return transport_node_id, "rejected", None

        try:
            from dpc_protocol.commit_integrity import CommitSigner
            result = CommitSigner.verify_signature(signer, content_hash, signature)
        except Exception as e:
            self.logger.warning(
                "Rejecting group message %s: signature check failed: %s",
                str(payload.get("message_id"))[:8], e
            )
            return transport_node_id, "rejected", None

        if result is False:
            self.logger.warning(
                "Rejecting group message %s: invalid signature from %s",
                str(payload.get("message_id"))[:8], str(signer)[:20]
            )
            return transport_node_id, "rejected", None

        fields = {
            "content_hash": content_hash,
            "signature": signature,
            "signer_node_id": signer,
            "preimage_version": PREIMAGE_VERSION,
        }
        if result is None:
            self.logger.info(
                "Storing group message %s from %s unverified: no cached certificate",
                str(payload.get("message_id"))[:8], str(signer)[:20]
            )
            return claimed, "unverified", fields

        return claimed, "verified", fields

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_TEXT message.

        Routes message to group conversation, deduplicates, broadcasts to UI,
        and feeds to conversation monitor for knowledge extraction.

        Args:
            sender_node_id: Node ID of message sender
            payload: Contains group_id, text, sender_name, message_id (v0.20.0+)
        """
        group_id = payload.get("group_id")
        text = payload.get("text")
        sender_name = payload.get("sender_name", sender_node_id)

        # Who wrote this, as opposed to who handed it over. In a star the two
        # differ: a relayed message arrives on the relay's socket, and taking
        # the author from the transport recorded seven of nine messages under
        # the wrong node on the edges (measured 2026-08-06). A signature the
        # relay cannot forge is what tells them apart.
        author_node_id, verification, signature_fields = self._authenticate_author(
            sender_node_id, payload
        )
        if verification == "rejected":
            return None
        if verification == "unverified" and author_node_id != sender_node_id:
            # Stored anyway — refusing on first contact would be a denial of
            # service against ourselves — but ask, so "unverified" is a state
            # this record passes through rather than one it retires in.
            await self._ask_for_certificate(author_node_id, sender_node_id)
        sender_node_id = author_node_id

        # v0.20.0: Use sender-provided message_id if available, else generate for backwards compat
        message_id = payload.get("message_id")
        if not message_id:
            # Fallback for older clients
            message_id = hashlib.sha256(
                f"{group_id}:{sender_node_id}:{text}:{int(time.time() * 1000)}".encode()
            ).hexdigest()[:16]

        # Deduplication: runtime set (survives within session)
        dedup_key = f"{group_id}:{message_id}"
        if dedup_key in self.service._processed_message_ids:
            self.logger.debug("Duplicate group message from %s, skipping", sender_node_id)
            return None

        # Deduplication: check history on disk (survives across restarts)
        monitor_key = group_id
        monitor = self.service.conversation_monitors.get(monitor_key)
        if monitor and hasattr(monitor, "message_ids") and message_id in monitor.message_ids:
            self.logger.debug("Duplicate group message %s (already in history), skipping", message_id[:8])
            self.service._processed_message_ids.add(dedup_key)
            return None

        self.service._processed_message_ids.add(dedup_key)

        # Clean up old IDs
        if len(self.service._processed_message_ids) > self.service._max_processed_ids:
            to_remove = list(self.service._processed_message_ids)[:self.service._max_processed_ids // 2]
            for mid in to_remove:
                self.service._processed_message_ids.discard(mid)

        # Relay to group members the sender may not be directly connected to.
        # In a star topology (B↔A↔C, but B↛C), A must relay B's message to C.
        # Dedup key already set above prevents relay loops.
        group = self.service.group_manager.get_group(group_id)
        if group:
            relay_msg = {"command": "GROUP_TEXT", "payload": payload}
            for member_id in group.members:
                if member_id == self.service.p2p_manager.node_id:
                    continue  # Skip self
                if member_id == sender_node_id:
                    continue  # Skip original sender
                if member_id in self.service.p2p_manager.peers:
                    try:
                        await self.service.p2p_manager.send_message_to_peer(member_id, relay_msg)
                        self.logger.debug(
                            "Relayed GROUP_TEXT %s from %s to %s",
                            message_id[:8], sender_node_id[:20], member_id[:20]
                        )
                    except Exception as e:
                        self.logger.error(
                            "Failed to relay group message to %s: %s", member_id[:20], e
                        )

        # Use sender-provided timestamp if available (v0.20.0)
        timestamp = payload.get("timestamp", datetime.now(timezone.utc).isoformat())

        # Store first, then tell the UI. The order is the whole fix for the
        # missing numbers: msg_index is assigned when the monitor writes the
        # record, so a broadcast sent beforehand had nothing to carry and every
        # message from a peer arrived unnumbered. Both send paths in service.py
        # already feed the monitor first for exactly this reason.
        msg_index = None
        try:
            monitor = self.service._get_or_create_conversation_monitor(group_id)

            conv_message = ConvMessage(
                message_id=message_id,
                conversation_id=group_id,
                sender_node_id=sender_node_id,
                sender_name=sender_name,
                text=text,
                timestamp=timestamp,  # v0.20.0: Use sender-provided timestamp
                sender_type=payload.get("sender_type"),
                agent_owner=payload.get("agent_owner"),
                signature_fields=signature_fields,
            )

            # Buffer message for manual extraction
            await monitor.on_message(conv_message)
            monitor.save_history()

            history = monitor.get_message_history()
            if history and history[-1].get("id") == message_id:
                msg_index = history[-1].get("msg_index")
        except Exception as e:
            self.logger.error("Error in group conversation monitoring: %s", e, exc_info=True)

        # Broadcast to UI
        await self.service.local_api.broadcast_event("group_text_received", {
            "group_id": group_id,
            "sender_node_id": sender_node_id,
            "sender_name": sender_name,
            "sender_type": payload.get("sender_type", "human"),
            "agent_owner": payload.get("agent_owner"),
            "text": text,
            "message_id": message_id,
            "timestamp": timestamp,
            "mentions": payload.get("mentions", []),
            "verification": verification,
            "msg_index": msg_index,
        })

        # Detect @Ark / @CC mentions and route to agents
        await self._handle_agent_mentions(group_id, payload, text, sender_name, sender_node_id)

        return None

    async def _handle_agent_mentions(
        self,
        group_id: str,
        payload: Dict[str, Any],
        text: str,
        sender_name: str,
        sender_node_id: str,
    ) -> None:
        """Detect @agent and @CC mentions and route to respective agents."""
        import re

        # Anti-loop guard: is_agent is set by send_group_agent_message() server-side only.
        # sender_name alone is insufficient — a user could rename themselves to match agent names.
        if payload.get("is_agent", False):
            return

        mentions = re.findall(r'@(\w+)\b', text, re.IGNORECASE)
        mention_names = {m.lower() for m in mentions}

        # Get allowed agents for this group from metadata
        group = self.service.group_manager.get_group(group_id) if self.service.group_manager else None
        node_id = self.service.p2p_manager.node_id
        allowed_agents = set(group.agents.get(node_id, [])) if group else set()

        # Check if any mention matches agent name or agent_id
        agent_id = self.service._get_default_agent_id()
        agent_name = self.service._get_agent_display_name(agent_id).lower()
        if agent_name in mention_names or agent_id in mention_names:
            if agent_id in allowed_agents:
                await self._invoke_agent(group_id, text, sender_name,
                                         payload.get("message_id"))
            else:
                self.logger.debug("Skipping @%s — agent %s not in metadata.agents for %s", agent_name, agent_id, group_id)

        cc_name = self.service.get_cc_display_name().lower()
        if cc_name in mention_names:
            # Broadcast event — the MCP server bridge subscribes and queues it
            await self.service.local_api.broadcast_event("cc_group_mention", {
                "group_id": group_id,
                "text": text,
                "sender_name": sender_name,
                "sender_node_id": sender_node_id,
            })

    async def _invoke_agent(self, group_id: str, text: str, sender_name: str,
                            trigger_message_id: Optional[str] = None) -> None:
        """Invoke the default agent and post response to the group."""
        try:
            dpc_provider = self.service.llm_manager.providers.get("dpc_agent")
            if not dpc_provider:
                self.logger.warning("_invoke_agent: dpc_agent provider not found")
                return
            agent_id = self.service._get_default_agent_id()
            manager = dpc_provider.get_manager(agent_id)
            prompt = f"[Group chat — {sender_name} says]: {text}"
            response = await manager.process_message(
                message=prompt,
                conversation_id=group_id,
                sender_name=sender_name,
                _skip_history=True,
                trigger_message_id=trigger_message_id,
            )
            if response:
                agent_display = self.service._get_agent_display_name(agent_id)
                await self.service.send_group_agent_message(group_id, agent_display, response)
        except Exception as e:
            self.logger.error("Agent group response failed: %s", e, exc_info=True)


class GroupLeaveHandler(MessageHandler):
    """Handles GROUP_LEAVE messages (member departing group)."""

    @property
    def command_name(self) -> str:
        return "GROUP_LEAVE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_LEAVE message.

        Remove departing member from local group metadata.

        Args:
            sender_node_id: Node ID of member leaving
            payload: Contains group_id
        """
        group_id = payload.get("group_id")

        self.logger.info(
            "Received GROUP_LEAVE from %s for group %s",
            sender_node_id[:20], group_id
        )

        group = self.service.group_manager.remove_member(group_id, sender_node_id)
        if group:
            await self.service.local_api.broadcast_event("group_member_left", {
                "group_id": group_id,
                "node_id": sender_node_id,
                "member_name": self.service.peer_metadata.get(
                    sender_node_id, {}
                ).get("name", sender_node_id),
                "remaining_members": group.members,
            })

        return None


class GroupDeleteHandler(MessageHandler):
    """Handles GROUP_DELETE messages (creator deleting group)."""

    @property
    def command_name(self) -> str:
        return "GROUP_DELETE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_DELETE message.

        Creator deleted the group. Remove local copy and clean up.

        Args:
            sender_node_id: Node ID of creator who deleted
            payload: Contains group_id
        """
        group_id = payload.get("group_id")

        self.logger.info(
            "Received GROUP_DELETE from %s for group %s",
            sender_node_id[:20], group_id
        )

        # Verify sender is the creator
        group = self.service.group_manager.get_group(group_id)
        if group and group.created_by != sender_node_id:
            self.logger.warning(
                "Ignoring GROUP_DELETE from non-creator %s (creator: %s)",
                sender_node_id, group.created_by
            )
            return None

        # Capture group name before deletion for UI notification
        group_name = group.name if group else group_id

        # Remove local group data
        self.service.group_manager.handle_group_deleted(group_id)

        # Clean up conversation monitor
        if group_id in self.service.conversation_monitors:
            del self.service.conversation_monitors[group_id]

        # Notify UI
        await self.service.local_api.broadcast_event("group_deleted", {
            "group_id": group_id,
            "deleted_by": sender_node_id,
            "group_name": group_name,
        })

        return None


class GroupSyncHandler(MessageHandler):
    """Handles GROUP_SYNC messages (metadata reconciliation on connect)."""

    @property
    def command_name(self) -> str:
        return "GROUP_SYNC"

    async def _honour_session_marker(self, local, marker_before, applied) -> None:
        """Clear what predates a newly learned session boundary.

        This is the half of ADR-038 Q3 that pays for the field. A node that was
        away when the group agreed to start over comes back holding the whole
        history, and the next sync hands its copy to everyone else — the reset
        undone with nobody noticing. Reading the boundary and dropping what is
        older than it ends that, and it ends it symmetrically: whoever was away
        does the clearing, not whoever was present.

        The marker is only obeyed when its own evidence proves the quorum, so a
        peer cannot erase a history by announcing a reset that never happened.
        Unprovable evidence is left alone rather than trusted — the certificate
        may simply not have arrived yet, and the marker will be honoured when it
        does.
        """
        if applied is None:
            return
        marker = getattr(applied, "session_started_at", None)
        if not marker or marker == marker_before:
            return
        if marker_before and marker <= marker_before:
            return

        evidence = getattr(applied, "session_reset_evidence", None) or {}
        from dpc_client_core.signing import quorum_is_proven

        if not quorum_is_proven(
            proposal_id=evidence.get("proposal_id"),
            conversation_id=evidence.get("conversation_id"),
            participants=evidence.get("participants"),
            votes=evidence.get("votes"),
        ):
            self.logger.warning(
                "Session marker on %s is not backed by a provable quorum — history untouched",
                applied.group_id,
            )
            return

        monitor = self.service.conversation_monitors.get(applied.group_id)
        if monitor is None:
            return
        dropped = monitor.clear_before(marker)
        if dropped:
            self.logger.info(
                "Session marker on %s: dropped %d message(s) older than %s",
                applied.group_id, dropped, marker,
            )
            await self.service.local_api.broadcast_event(
                "conversation_reset", {"conversation_id": applied.group_id}
            )

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_SYNC message.

        Reconcile group metadata with remote peer. Highest version wins.

        Args:
            sender_node_id: Node ID of sync source
            payload: Group metadata dict with version
        """
        group_id = payload.get("group_id")
        remote_version = payload.get("version", 0)

        self.logger.info(
            "Received GROUP_SYNC from %s for group %s (v%d)",
            sender_node_id[:20], group_id, remote_version
        )

        # apply_sync decides by "highest version wins" and never learns who
        # sent it, so without this the roster belongs to whoever bids highest —
        # any connected peer, member or not. An invitation is GROUP_CREATE;
        # a sync is not a way into a group we have never heard of.
        local = self.service.group_manager.get_group(group_id) if group_id else None
        if not local:
            self.logger.warning(
                "Ignoring GROUP_SYNC from %s for unknown group %s",
                sender_node_id[:20], group_id
            )
            return None
        if sender_node_id not in local.members:
            self.logger.warning(
                "Ignoring GROUP_SYNC for %s: %s is not a member",
                group_id, sender_node_id[:20]
            )
            return None

        marker_before = local.session_started_at

        result = self.service.group_manager.apply_sync(payload)
        await self._honour_session_marker(local, marker_before, result)
        # Re-added by the same peer that refused us: the standing refusal is
        # spent, and without this the group would stay unasked until a restart.
        if result and self.service.p2p_manager.node_id in getattr(result, "members", ()):
            self.service.clear_group_access_denied(sender_node_id, group_id)
        if result:
            # Notify UI of updated group
            await self.service.local_api.broadcast_event("group_updated", {
                "group_id": result.group_id,
                "name": result.name,
                "topic": result.topic,
                "members": result.members,
                "agents": result.agents,
                "agent_names": result.agent_names,
                "version": result.version,
            })

        # Request history if we have no local messages for this group
        if group_id:
            conv_dir = self.service.group_manager._get_conversation_dir(group_id)
            history_path = conv_dir / "history.json" if conv_dir else None
            needs_history = not history_path or not history_path.exists()
            if not needs_history and history_path and history_path.exists():
                try:
                    import json as _json
                    with open(history_path, encoding="utf-8") as f:
                        data = _json.load(f)
                    needs_history = len(data.get("messages", [])) == 0
                except Exception:
                    needs_history = True

            if needs_history:
                import uuid
                request_id = str(uuid.uuid4())[:8]
                self.service.history_requests.note(sender_node_id, group_id, request_id)
                await self.service.p2p_manager.send_message_to_peer(sender_node_id, {
                    "command": "REQUEST_CHAT_HISTORY",
                    "payload": {
                        "conversation_id": group_id,
                        "request_id": request_id,
                    }
                })
                self.logger.info("Requested history for group %s from %s (local history empty)", group_id, sender_node_id[:16])

        return None


class GroupHistoryRequestHandler(MessageHandler):
    """Handles GROUP_HISTORY_REQUEST messages (peer requesting group chat history)."""

    @property
    def command_name(self) -> str:
        return "GROUP_HISTORY_REQUEST"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_HISTORY_REQUEST message.

        Send our group conversation history to the requesting peer.

        Args:
            sender_node_id: Node ID of requester
            payload: Contains group_id, and optionally `authors` — the node ids
                the requester found divergent. β has been sending that list
                since `5b160a93`; this handler used to ignore it and answer with
                the whole history, so every sync cost the full file no matter
                how little differed. A peer that predates the field sends none,
                and still gets everything.
        """
        group_id = payload.get("group_id")
        authors = payload.get("authors")

        self.logger.info(
            "Received GROUP_HISTORY_REQUEST from %s for group %s (%s)",
            sender_node_id[:20], group_id,
            "whole history" if authors is None else f"{len(authors)} author(s)",
        )

        if not may_share_group(self.service.group_manager, group_id, sender_node_id):
            await refuse_group_access(
                self.service.p2p_manager, sender_node_id, group_id,
                "GROUP_HISTORY_REQUEST", self.logger,
            )
            return None

        # Get conversation monitor for this group; load from disk if not in memory
        monitor = self.service.conversation_monitors.get(group_id)
        if not monitor:
            monitor = self.service._get_or_create_conversation_monitor(group_id)
        if not monitor:
            self.logger.debug("No conversation history for group %s", group_id)
            return None

        # Export history and send back
        history = monitor.export_history(authors=authors) if hasattr(monitor, "export_history") else []
        response = {
            "group_id": group_id,
            "history": history,
        }
        # Echoed so the asker can tell this answer from an assertion.
        request_id = payload.get("request_id")
        if request_id:
            response["request_id"] = request_id
        # Say what the answer covers. Without it a filtered reply is
        # indistinguishable from a complete one that happens to be short.
        if authors is not None:
            response["authors"] = authors
        await self.service.p2p_manager.send_message_to_peer(sender_node_id, {
            "command": "GROUP_HISTORY_RESPONSE",
            "payload": response,
        })

        return None


class GroupHistoryResponseHandler(MessageHandler):
    """Handles GROUP_HISTORY_RESPONSE messages (receiving group chat history)."""

    @property
    def command_name(self) -> str:
        return "GROUP_HISTORY_RESPONSE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_HISTORY_RESPONSE message.

        Import received group conversation history.

        Args:
            sender_node_id: Node ID of history provider
            payload: Contains group_id and history array
        """
        group_id = payload.get("group_id")
        history = payload.get("history", [])
        # Present when the answer was limited to the authors we asked about, so
        # a short reply can be told apart from a short history.
        authors = payload.get("authors")

        self.logger.info(
            "Received GROUP_HISTORY_RESPONSE from %s for group %s (%d messages, %s)",
            sender_node_id[:20], group_id, len(history),
            "whole history" if authors is None else f"limited to {len(authors)} author(s)",
        )

        # An answer, not a request — so it is dropped rather than refused out
        # loud. Sending a denial here would answer a message we never invited.
        if not may_share_group(self.service.group_manager, group_id, sender_node_id):
            self.logger.warning(
                "Discarding GROUP_HISTORY_RESPONSE from %s for group %s: "
                "unknown group or sender is not a member",
                sender_node_id[:20], group_id,
            )
            return None

        # A GROUP_HISTORY_RESPONSE merges into a conversation, so an unclaimed
        # one is an assertion rather than a reply. The 1:1 twin has refused
        # unclaimed answers since `4d3b7442`; the group path was added by the
        # v0.20.0 hash sync and never got it, so any connected member could push
        # a history nobody asked for. `claim_any` tolerates a peer on the older
        # build that answers without echoing the id.
        request_id = payload.get("request_id")
        claimed = (
            self.service.history_requests.claim(sender_node_id, group_id, request_id)
            if request_id
            else self.service.history_requests.claim_any(sender_node_id, group_id)
        )
        if not claimed:
            self.logger.warning(
                "Discarding unsolicited group history from %s for %s (request_id %s)",
                sender_node_id[:20], group_id, request_id,
            )
            return None

        if not history:
            return None

        monitor = self.service._get_or_create_conversation_monitor(group_id)

        # v0.20.0: Use merge_history instead of import_history
        # This handles duplicates and saves to disk
        if hasattr(monitor, "merge_history"):
            added = monitor.merge_history(history)
            self.logger.info("Merged %d new messages into group %s history", added, group_id)
        elif hasattr(monitor, "import_history"):
            # Fallback for older monitors
            monitor.import_history(history)

        # Notify UI to refresh chat
        await self.service.local_api.broadcast_event("group_history_synced", {
            "group_id": group_id,
            "message_count": len(history),
        })

        return None


class GroupHistoryStatusHandler(MessageHandler):
    """Handles GROUP_HISTORY_STATUS messages (v0.20.0 hash-based sync).

    Exchange history hashes to determine if sync is needed.
    """

    @property
    def command_name(self) -> str:
        return "GROUP_HISTORY_STATUS"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_HISTORY_STATUS message.

        Compare hashes and request history if peer has newer/different messages.

        Args:
            sender_node_id: Node ID of peer
            payload: Contains group_id, history_hash, message_count
        """
        group_id = payload.get("group_id")
        remote_hash = payload.get("history_hash", "")
        remote_count = payload.get("message_count", 0)
        is_reply = payload.get("is_reply", False)

        self.logger.debug(
            "Received GROUP_HISTORY_STATUS from %s for group %s (hash=%s, count=%d)",
            sender_node_id[:20], group_id, remote_hash[:16], remote_count
        )

        # Before the reply, which would otherwise disclose our count and digest
        # for a group this peer has no part in.
        if not may_share_group(self.service.group_manager, group_id, sender_node_id):
            await refuse_group_access(
                self.service.p2p_manager, sender_node_id, group_id,
                "GROUP_HISTORY_STATUS", self.logger,
            )
            return None

        # Get local monitor
        monitor = self.service.conversation_monitors.get(group_id)

        # Compute local hash and digest (peek disk when the monitor is not
        # loaded this session — which, monitors being lazy, is the usual case).
        # The digest used to be None whenever there was no monitor, and both
        # sides then fell back to comparing chain tips: a comparison that never
        # matches between two honest nodes, so every connection either reported
        # divergence or shipped the whole history.
        if monitor and hasattr(monitor, "compute_history_hash"):
            local_hash = monitor.compute_history_hash()
            local_count = len(monitor.message_history)
            local_digest = monitor.history_digest()
        else:
            disk_messages = ConversationMonitor.peek_group_messages(group_id)
            local_count = len(disk_messages)
            local_hash = ConversationMonitor.history_hash_for(disk_messages)
            local_digest = digest_for(disk_messages)

        # Reply only to the initiating STATUS (not to replies), to prevent infinite ping-pong.
        # A sends STATUS → B replies once with is_reply=True → A does NOT reply again.
        if not is_reply:
            reply = {
                "group_id": group_id,
                "history_hash": local_hash,
                "message_count": local_count,
                "is_reply": True,
            }
            if local_digest:
                reply["history_digest"] = local_digest
            await self.service.p2p_manager.send_message_to_peer(sender_node_id, {
                "command": "GROUP_HISTORY_STATUS",
                "payload": reply,
            })

        remote_digest = payload.get("history_digest")
        if remote_digest and local_digest:
            # The order-independent comparison. `history_hash` below is the tip
            # of a chain covering msg_index, prev_hash and role — the first two
            # follow arrival order and the third is per reader, so between two
            # honest nodes it never matched and the alarm never stopped.
            differing = authors_that_differ_between(local_digest, remote_digest)
            if not differing:
                self.logger.debug("Group %s: histories agree (%d messages)", group_id, local_count)
                return None
            self.logger.info(
                "Requesting history sync for group %s: differs for %d author(s)",
                group_id, len(differing)
            )
            request_id = uuid.uuid4().hex[:8]
            self.service.history_requests.note(sender_node_id, group_id, request_id)
            await self.service.p2p_manager.send_message_to_peer(sender_node_id, {
                "command": "GROUP_HISTORY_REQUEST",
                "payload": {"group_id": group_id, "authors": differing,
                            "request_id": request_id},
            })
            return None

        # Peer predates the digest: fall back to the old tip comparison, which
        # over-reports but is all a legacy node can answer.
        if remote_hash != local_hash:
            self.logger.info(
                "Requesting history sync for group %s (local: %d, remote: %d)",
                group_id, local_count, remote_count
            )
            request_id = uuid.uuid4().hex[:8]
            self.service.history_requests.note(sender_node_id, group_id, request_id)
            await self.service.p2p_manager.send_message_to_peer(sender_node_id, {
                "command": "GROUP_HISTORY_REQUEST",
                "payload": {"group_id": group_id, "request_id": request_id}
            })

        return None


class GroupDeletedStatusHandler(MessageHandler):
    """Handles GROUP_DELETED_STATUS messages (v0.20.0 offline deletion notification).

    Exchange deleted group IDs to ensure eventually consistent deletion.
    """

    @property
    def command_name(self) -> str:
        return "GROUP_DELETED_STATUS"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GROUP_DELETED_STATUS message.

        Check if we have any groups that were deleted by the peer and remove them.

        Args:
            sender_node_id: Node ID of peer
            payload: Contains deleted_groups list
        """
        deleted_groups = payload.get("deleted_groups", [])

        if not deleted_groups:
            return None

        self.logger.debug(
            "Received GROUP_DELETED_STATUS from %s with %d deleted groups",
            sender_node_id[:20], len(deleted_groups)
        )

        # Check if we have any of the deleted groups
        removed_count = 0
        for group_id in deleted_groups:
            group = self.service.group_manager.get_group(group_id)
            if group:
                # Verify the deleter was the creator
                if group.created_by == sender_node_id:
                    self.logger.info(
                        "Removing locally deleted group %s (deleted by creator %s)",
                        group_id, sender_node_id[:20]
                    )
                    group_name = group.name

                    # Remove local group data
                    self.service.group_manager.handle_group_deleted(group_id)

                    # Clean up conversation monitor
                    if group_id in self.service.conversation_monitors:
                        del self.service.conversation_monitors[group_id]

                    # Notify UI
                    await self.service.local_api.broadcast_event("group_deleted", {
                        "group_id": group_id,
                        "deleted_by": sender_node_id,
                        "group_name": group_name,
                    })
                    removed_count += 1

        if removed_count > 0:
            self.logger.info("Removed %d groups based on deleted status from %s", removed_count, sender_node_id[:20])

        return None


class GroupAccessDeniedHandler(MessageHandler):
    """A peer answers that we are not in a group our own roster still lists.

    Removal is never announced to the node being removed, so this refusal is
    how it finds out — see THE-REMOVED-MEMBER-IS-THE-ONE-NODE-NOT-TOLD. What it
    is *not* is authority over our roster: a peer cannot delete a group by
    saying no, so nothing is erased here. It stops us asking this peer about
    this group, and it tells the person. Undone the moment that same peer syncs
    a roster we are in again.
    """

    @property
    def command_name(self) -> str:
        return "GROUP_ACCESS_DENIED"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        group_id = payload.get("group_id")
        reason = payload.get("reason", "unspecified")
        self.logger.warning(
            "Access to group %s refused by %s (%s) — our roster still lists it",
            group_id, sender_node_id[:20], reason,
        )
        if not group_id:
            return None

        self.service.note_group_access_denied(sender_node_id, group_id)

        group = self.service.group_manager.get_group(group_id)
        await self.service.local_api.broadcast_event("group_access_denied", {
            "group_id": group_id,
            "group_name": getattr(group, "name", None),
            "peer_id": sender_node_id,
            "reason": reason,
        })
        return None
