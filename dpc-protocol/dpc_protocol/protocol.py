# dpc/dpc/protocol.py

import asyncio
import json
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

def create_hello_message(node_id: str, name: str = None) -> Dict[str, Any]:
    """Creates a HELLO message with optional display name."""
    payload = {"node_id": node_id}
    if name:
        payload["name"] = name
    return {"command": "HELLO", "payload": payload}

def create_get_context_message() -> Dict[str, Any]:
    return {"command": "GET_CONTEXT"}

def create_context_data_message(context_dict: dict) -> Dict[str, Any]:
    return {"command": "CONTEXT_DATA", "payload": context_dict}

def create_ok_response(message: str) -> Dict[str, Any]:
    return {"status": "OK", "message": message}

def create_error_response(message: str) -> Dict[str, Any]:
    return {"status": "ERROR", "message": message}

def create_send_text_message(text: str) -> Dict[str, Any]:
    """Creates a message for sending text to a peer."""
    # For now, we don't need a chat_id, the P2PManager knows the sender.
    return {"command": "SEND_TEXT", "payload": {"text": text}}

def create_remote_inference_request(request_id: str, prompt: str, model: str = None, provider: str = None, images: list = None) -> Dict[str, Any]:
    """
    Creates a remote inference request message.

    Args:
        request_id: Unique identifier for this request
        prompt: Text prompt for the model
        model: Optional model name to use
        provider: Optional provider alias to use
        images: Optional list of image dicts for vision models (Phase 2: Remote Vision)
                Each image dict contains: {path: str, base64: str, mime_type: str}
    """
    payload = {
        "request_id": request_id,
        "prompt": prompt
    }
    if model:
        payload["model"] = model
    if provider:
        payload["provider"] = provider
    if images:
        payload["images"] = images
    return {"command": "REMOTE_INFERENCE_REQUEST", "payload": payload}

def create_remote_inference_response(
    request_id: str,
    response: str = None,
    error: str = None,
    tokens_used: int = None,
    prompt_tokens: int = None,
    response_tokens: int = None,
    model_max_tokens: int = None,
    model: str = None,
    provider: str = None
) -> Dict[str, Any]:
    """Creates a remote inference response message with optional token and model metadata."""
    payload = {"request_id": request_id}
    if response is not None:
        payload["response"] = response
        payload["status"] = "success"
        # Add token metadata if provided
        if tokens_used is not None:
            payload["tokens_used"] = tokens_used
        if prompt_tokens is not None:
            payload["prompt_tokens"] = prompt_tokens
        if response_tokens is not None:
            payload["response_tokens"] = response_tokens
        if model_max_tokens is not None:
            payload["model_max_tokens"] = model_max_tokens
        # Add model and provider metadata if provided
        if model is not None:
            payload["model"] = model
        if provider is not None:
            payload["provider"] = provider
    else:
        payload["error"] = error or "Unknown error"
        payload["status"] = "error"
    return {"command": "REMOTE_INFERENCE_RESPONSE", "payload": payload}

def create_remote_transcription_request(
    request_id: str,
    audio_base64: str,
    mime_type: str,
    model: str = None,
    provider: str = None,
    language: str = "auto",
    task: str = "transcribe"
) -> Dict[str, Any]:
    """
    Creates a remote transcription request message.

    Args:
        request_id: Unique identifier for this request
        audio_base64: Base64-encoded audio data
        mime_type: Audio MIME type (e.g., audio/webm, audio/opus)
        model: Optional model name to use
        provider: Optional provider alias to use
        language: Language code or "auto" for detection
        task: "transcribe" (default) or "translate" (to English)
    """
    payload = {
        "request_id": request_id,
        "audio_base64": audio_base64,
        "mime_type": mime_type,
        "language": language,
        "task": task
    }
    if model:
        payload["model"] = model
    if provider:
        payload["provider"] = provider
    return {"command": "REMOTE_TRANSCRIPTION_REQUEST", "payload": payload}

def create_remote_transcription_response(
    request_id: str,
    text: str = None,
    error: str = None,
    language: str = None,
    duration_seconds: float = None,
    provider: str = None
) -> Dict[str, Any]:
    """Creates a remote transcription response message with optional metadata."""
    payload = {"request_id": request_id}
    if text is not None:
        payload["text"] = text
        payload["status"] = "success"
        # Add metadata if provided
        if language is not None:
            payload["language"] = language
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        if provider is not None:
            payload["provider"] = provider
    else:
        payload["error"] = error or "Unknown error"
        payload["status"] = "error"
    return {"command": "REMOTE_TRANSCRIPTION_RESPONSE", "payload": payload}

def create_get_providers_message() -> Dict[str, Any]:
    """Creates a request to get available AI providers from a peer."""
    return {"command": "GET_PROVIDERS"}

def create_providers_response(providers: list) -> Dict[str, Any]:
    """Creates a response containing available AI providers.

    Args:
        providers: List of provider dicts with keys: alias, model, type
    """
    return {"command": "PROVIDERS_RESPONSE", "payload": {"providers": providers}}

def create_send_image_message(
    request_id: str,
    prompt: str,
    images: list,
    model: str = None,
    provider: str = None
) -> Dict[str, Any]:
    """Creates a SEND_IMAGE message for remote vision inference.

    Args:
        request_id: Unique identifier for this request
        prompt: Text prompt for vision model
        images: List of image dicts with keys: path, base64, mime_type
        model: Optional vision model name
        provider: Optional provider alias

    Returns:
        SEND_IMAGE message dict
    """
    payload = {
        "request_id": request_id,
        "prompt": prompt,
        "images": images
    }
    if model:
        payload["model"] = model
    if provider:
        payload["provider"] = provider
    return {"command": "SEND_IMAGE", "payload": payload}


def create_propose_new_session_message(
    proposal_id: str,
    conversation_id: str,
    proposer_node_id: str
) -> Dict[str, Any]:
    """Creates a PROPOSE_NEW_SESSION message for collaborative session reset.

    Args:
        proposal_id: Unique proposal identifier
        conversation_id: Conversation to reset
        proposer_node_id: Node ID of proposer

    Returns:
        PROPOSE_NEW_SESSION message dict
    """
    from datetime import datetime, timezone
    return {
        "command": "PROPOSE_NEW_SESSION",
        "payload": {
            "proposal_id": proposal_id,
            "conversation_id": conversation_id,
            "proposer_node_id": proposer_node_id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    }


def create_vote_new_session_message(
    proposal_id: str,
    voter_node_id: str,
    vote: str
) -> Dict[str, Any]:
    """Creates a VOTE_NEW_SESSION message.

    Args:
        proposal_id: Proposal being voted on
        voter_node_id: Node ID of voter
        vote: "approve" or "reject"

    Returns:
        VOTE_NEW_SESSION message dict
    """
    from datetime import datetime, timezone
    return {
        "command": "VOTE_NEW_SESSION",
        "payload": {
            "proposal_id": proposal_id,
            "voter_node_id": voter_node_id,
            "vote": vote,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    }


def create_new_session_result_message(
    proposal_id: str,
    status: str,
    votes: list
) -> Dict[str, Any]:
    """Creates a NEW_SESSION_RESULT message.

    Args:
        proposal_id: Proposal identifier
        status: "approved" or "rejected"
        votes: List of vote dicts with keys: node_id, vote

    Returns:
        NEW_SESSION_RESULT message dict
    """
    from datetime import datetime, timezone
    return {
        "command": "NEW_SESSION_RESULT",
        "payload": {
            "proposal_id": proposal_id,
            "status": status,
            "votes": votes,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    }


def create_request_chat_history_message(
    conversation_id: str,
    since_timestamp: str = None
) -> Dict[str, Any]:
    """Creates a REQUEST_CHAT_HISTORY message.

    Args:
        conversation_id: Conversation ID to sync
        since_timestamp: Optional ISO 8601 timestamp to filter messages

    Returns:
        REQUEST_CHAT_HISTORY message dict
    """
    payload = {"conversation_id": conversation_id}
    if since_timestamp:
        payload["since_timestamp"] = since_timestamp
    return {"command": "REQUEST_CHAT_HISTORY", "payload": payload}


def create_chat_history_response_message(
    conversation_id: str,
    messages: list
) -> Dict[str, Any]:
    """Creates a CHAT_HISTORY_RESPONSE message.

    Args:
        conversation_id: Conversation ID
        messages: List of message dicts with keys: role, text, timestamp, sender_node_id (optional)

    Returns:
        CHAT_HISTORY_RESPONSE message dict
    """
    return {
        "command": "CHAT_HISTORY_RESPONSE",
        "payload": {
            "conversation_id": conversation_id,
            "messages": messages
        }
    }


async def read_message(reader: asyncio.StreamReader) -> dict | None:
    try:
        header = await reader.readexactly(10)
        payload_length = int(header.decode())

        payload = await reader.readexactly(payload_length)

        return json.loads(payload.decode())
    except asyncio.IncompleteReadError as e:
        # Graceful disconnect: 0 bytes read means connection closed cleanly
        if len(e.partial) == 0:
            logger.info("Connection closed by peer")
        else:
            logger.warning("Protocol error: incomplete message (%d bytes received)", len(e.partial))
        return None
    except (ConnectionResetError, BrokenPipeError) as e:
        logger.warning("Connection lost: %s", e)
        return None
    except ValueError as e:
        logger.warning("Protocol error: invalid message format (%s)", e)
        return None

async def write_message(writer: asyncio.StreamWriter, data: dict):
    try:
        payload = json.dumps(data).encode()
        payload_length = len(payload)
        
        header = f"{payload_length:010d}".encode()
        
        writer.write(header)
        writer.write(payload)
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError) as e:
        logger.warning("Could not write message, connection closed: %s", e)