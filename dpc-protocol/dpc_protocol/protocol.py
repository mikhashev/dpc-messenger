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
    model_max_tokens: int = None
) -> Dict[str, Any]:
    """Creates a remote inference response message with optional token metadata."""
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
    else:
        payload["error"] = error or "Unknown error"
        payload["status"] = "error"
    return {"command": "REMOTE_INFERENCE_RESPONSE", "payload": payload}

def create_get_providers_message() -> Dict[str, Any]:
    """Creates a request to get available AI providers from a peer."""
    return {"command": "GET_PROVIDERS"}

def create_providers_response(providers: list) -> Dict[str, Any]:
    """Creates a response containing available AI providers.

    Args:
        providers: List of provider dicts with keys: alias, model, type
    """
    return {"command": "PROVIDERS_RESPONSE", "payload": {"providers": providers}}

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