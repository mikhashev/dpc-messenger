# dpc/dpc/protocol.py

import asyncio
import json
from typing import Dict, Any

def create_hello_message(node_id: str) -> Dict[str, Any]:
    return {"command": "HELLO", "payload": {"node_id": node_id}}

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

def create_remote_inference_request(request_id: str, prompt: str, model: str = None, provider: str = None) -> Dict[str, Any]:
    """Creates a remote inference request message."""
    payload = {
        "request_id": request_id,
        "prompt": prompt
    }
    if model:
        payload["model"] = model
    if provider:
        payload["provider"] = provider
    return {"command": "REMOTE_INFERENCE_REQUEST", "payload": payload}

def create_remote_inference_response(request_id: str, response: str = None, error: str = None) -> Dict[str, Any]:
    """Creates a remote inference response message."""
    payload = {"request_id": request_id}
    if response is not None:
        payload["response"] = response
        payload["status"] = "success"
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
    except (asyncio.IncompleteReadError, ConnectionResetError, ValueError) as e:
        print(f"Protocol error or connection closed: {e}")
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
        print(f"Could not write message, connection closed: {e}")