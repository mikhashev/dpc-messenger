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