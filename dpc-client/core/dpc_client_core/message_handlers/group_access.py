"""Who may ask us about a group's history.

`GroupSyncHandler` has refused an unknown group and a sender outside its roster
since `3e49b044`, with a comment saying why: a sync is not a way into a group we
have never heard of. The three handlers that carry the *messages* — status,
request, response — and the 1:1 request handler's group branch were written for
the v0.20.0 hash-based sync and never got the same check. Measured 2026-08-28:
the node that had removed us from a group still answered our status with its 28
messages, and we handed back our 21.

Two rules live here, and the second is the one that is easy to get wrong.

**The refusal is uniform.** «I do not know that group» and «you are not in it»
answer identically. A different answer for each is an oracle: a stranger could
walk group ids and learn which ones this node holds. One answer tells a former
member what it needs and a stranger nothing it did not already know.

**The refusal is spoken, not silent.** A removed node is not told it was removed
(THE-REMOVED-MEMBER-IS-THE-ONE-NODE-NOT-TOLD), so the first thing it does on
reconnect is ask. Answering with silence would leave it asking for ever; the
denial is how it finds out, and how it stops.
"""
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ACCESS_DENIED = "GROUP_ACCESS_DENIED"

# One reason string for both cases — see the module docstring.
REASON = "not_a_member"


def may_share_group(group_manager, group_id: Optional[str], sender_node_id: str) -> bool:
    """Whether this peer may see or change our copy of this group's history."""
    if not group_id or not sender_node_id:
        return False
    group = group_manager.get_group(group_id) if group_manager else None
    if group is None:
        return False
    return sender_node_id in getattr(group, "members", ())


def off_the_roster(group, *node_ids) -> Optional[str]:
    """The first of `node_ids` that is not on `group`'s `members`, else None.

    The receive side of the rule `posting_refusal` states for sending: what a
    node puts into a group (a post, a file, a transcription, a record handed
    over by history sync) is a member's, judged against our CURRENT roster.
    An empty id names nobody and is skipped: a record that names no author
    cannot be judged by the roster, and is left to the checks that judge it.
    """
    members = getattr(group, "members", None) or ()
    return next((n for n in node_ids if n and n not in members), None)


def posting_refusal(group, node_id: str, refused_by=()) -> Optional[str]:
    """Why `node_id` may not post into `group`, or None when it may.

    Only `members` lets a node post; `agents[node]` survives a removal on
    purpose (a re-add needs it) and grants nothing. `refused_by` holds the
    (peer, group) pairs that answered `not_a_member`; only the creator's
    counts, because the roster is the creator's (ADR-038 part 1) and another
    member may answer from a stale copy. History is never touched.
    """
    if group is None:
        return "this node does not hold that group"
    group_id = getattr(group, "group_id", None)
    if node_id not in (getattr(group, "members", None) or ()):
        return (f"this node is not on the roster of group {group_id}: it was removed, so it "
                f"cannot post there (its history here is kept)")
    creator = getattr(group, "created_by", "") or ""
    if creator and creator != node_id and (creator, group_id) in (refused_by or ()):
        return (f"the creator of group {group_id} answered {REASON}: this node was removed, so it "
                f"cannot post there (its history here is kept)")
    return None


async def refuse_group_access(
    p2p_manager,
    sender_node_id: str,
    group_id: Optional[str],
    command: str,
    log: Optional[logging.Logger] = None,
) -> None:
    """Say no, once, in the same words whatever the reason."""
    (log or logger).warning(
        "Refused %s from %s for group %s: unknown group or sender is not a member",
        command, sender_node_id[:20], group_id,
    )
    payload: Dict[str, Any] = {"group_id": group_id, "reason": REASON}
    try:
        await p2p_manager.send_message_to_peer(
            sender_node_id, {"command": ACCESS_DENIED, "payload": payload}
        )
    except Exception as exc:  # a peer that vanished mid-refusal is not an error
        (log or logger).debug("Could not deliver %s to %s: %s", ACCESS_DENIED, sender_node_id[:20], exc)
