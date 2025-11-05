# dpc-client/core/dpc_client_core/p2p_manager.py

import asyncio
import json
from typing import Dict, Any
from dataclasses import asdict

from aiortc import RTCPeerConnection, RTCSessionDescription

# Re-using our robust protocol and crypto modules
from dpc_protocol.crypto import load_identity
from dpc_protocol.pcm_core import PCMCore

from .firewall import ContextFirewall
from .hub_client import HubClient # For type hinting

# A Peer object to hold state for each connection
class Peer:
    def __init__(self, pc: RTCPeerConnection, data_channel):
        self.pc = pc
        self.data_channel = data_channel

class P2PManager:
    """
    Manages all direct Peer-to-Peer connections using WebRTC for NAT traversal.
    """
    def __init__(self, firewall: ContextFirewall):
        self.firewall = firewall
        self.peers: Dict[str, Peer] = {}
        self.node_id, _, _ = load_identity()
        self.local_context = PCMCore().load_context()
        self.on_peer_list_change = None 
        print("P2PManager initialized.")

    async def start_server(self):
        """In the hub-assisted model, the server is always 'ready'."""
        print("P2PManager is ready to accept brokered connections.")
        pass

    def set_on_peer_list_change(self, callback):
        """Allows the CoreService to register a callback function."""
        self.on_peer_list_change = callback

    async def _notify_peer_change(self):
        """Calls the registered callback if it exists."""
        if self.on_peer_list_change and asyncio.iscoroutinefunction(self.on_peer_list_change):
            await self.on_peer_list_change()

    async def handle_incoming_signal(self, signal: Dict[str, Any], hub_client: HubClient):
        """Processes a signaling message from the Hub, typically an 'offer'."""
        sender_node_id = signal.get("sender_node_id")
        payload = signal.get("payload")
        if not sender_node_id or not payload:
            return

        if payload.get("type") == "offer":
            print(f"Received connection offer from {sender_node_id}")
            pc = RTCPeerConnection()
            peer = Peer(pc=pc, data_channel=None)
            self.peers[sender_node_id] = peer

            @pc.on("datachannel")
            def on_datachannel(channel):
                print(f"Data channel '{channel.label}' created by {sender_node_id}")
                peer.data_channel = channel
                asyncio.create_task(self.handle_data_channel(sender_node_id, channel))

            await pc.setRemoteDescription(RTCSessionDescription(sdp=payload["sdp"], type=payload["type"]))
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            
            await hub_client.send_signal(sender_node_id, {"type": "answer", "sdp": pc.localDescription.sdp})
            print(f"Sent answer to {sender_node_id}")

        elif payload.get("type") == "answer":
            if sender_node_id in self.peers:
                pc = self.peers[sender_node_id].pc
                await pc.setRemoteDescription(RTCSessionDescription(sdp=payload["sdp"], type=payload["type"]))
                print(f"Processed answer from {sender_node_id}")

    async def connect_to_peer(self, target_node_id: str, hub_client: HubClient):
        """Initiates a connection to a peer using the Hub for signaling."""
        if target_node_id in self.peers:
            print(f"Already connected or connecting to {target_node_id}")
            return

        print(f"Initiating P2P connection to {target_node_id}...")
        pc = RTCPeerConnection()
        
        # --- THE CORE FIX ---
        # Add the peer to the dictionary ONLY after we are sure we can proceed.
        # And wrap the entire process in a try/except/finally block for cleanup.
        try:
            channel = pc.createDataChannel("dpc-channel")
            peer = Peer(pc=pc, data_channel=channel)
            self.peers[target_node_id] = peer # Add to peers dict

            # Setup event handlers
            asyncio.create_task(self.handle_data_channel(target_node_id, channel))

            @pc.on("iceconnectionstatechange")
            async def on_iceconnectionstatechange():
                print(f"ICE connection state for {target_node_id} is {pc.iceConnectionState}")
                if pc.iceConnectionState == "failed":
                    await self.shutdown_peer_connection(target_node_id)

            # Create and send the offer
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)

            await hub_client.send_signal(target_node_id, {"type": "offer", "sdp": pc.localDescription.sdp})
            print(f"Sent offer to {target_node_id}")

        except Exception as e:
            print(f"Failed to initiate connection to {target_node_id}: {e}")
            # If anything fails, remove the peer from the dictionary
            if target_node_id in self.peers:
                del self.peers[target_node_id]
            await pc.close()
            raise # Re-raise the exception to inform the CoreService

    async def handle_data_channel(self, peer_node_id: str, channel):
        """
        Handles the lifecycle of an established data channel, including the
        DPTP protocol logic.
        """
        @channel.on("open")
        async def on_open():
            print(f"Data channel with {peer_node_id} is open. Sending HELLO.")
            # Send the HELLO message to confirm the protocol layer is ready
            hello_msg = {"command": "HELLO", "payload": {"node_id": self.node_id}}
            channel.send(json.dumps(hello_msg))
            await self._notify_peer_change()

        @channel.on("message")
        async def on_message(message_str: str):
            """This is the core protocol handler."""
            try:
                message = json.loads(message_str)
                command = message.get("command")
                print(f"Received command '{command}' from {peer_node_id}")

                if command == "HELLO":
                    # Peer has confirmed connection, we are ready for business.
                    print(f"Received HELLO from {peer_node_id}. Handshake complete.")

                elif command == "GET_CONTEXT":
                    path = message.get("payload", {}).get("path", "*") # Default to all
                    
                    # --- FIREWALL CHECK ---
                    if self.firewall.can_access(requester_identity=peer_node_id, resource_path=path):
                        print(f"Access granted for {peer_node_id} to '{path}'.")
                        # TODO: Implement pruning of context based on path
                        response = {
                            "command": "CONTEXT_DATA",
                            "payload": asdict(self.local_context)
                        }
                        channel.send(json.dumps(response))
                    else:
                        print(f"Access DENIED for {peer_node_id} to '{path}'.")
                        response = {
                            "command": "ERROR",
                            "payload": {"message": "Access Denied"}
                        }
                        channel.send(json.dumps(response))
                
                elif command == "REMOTE_INFERENCE":
                    # TODO: Implement in a future epic
                    print("REMOTE_INFERENCE command received but not yet implemented.")
                    response = {
                        "command": "ERROR",
                        "payload": {"message": "Remote inference not implemented yet."}
                    }
                    channel.send(json.dumps(response))

            except Exception as e:
                print(f"Error processing message from {peer_node_id}: {e}")
                try:
                    channel.send(json.dumps({"command": "ERROR", "payload": {"message": "Invalid message format."}}))
                except:
                    pass

        @channel.on("close")
        async def on_close():
            print(f"Data channel with {peer_node_id} closed.")
            if peer_node_id in self.peers:
                await self.peers[peer_node_id].pc.close()
                del self.peers[peer_node_id]
            # After a peer disconnects, notify the service
            await self._notify_peer_change()

    async def shutdown(self):
        """Closes all active P2P connections."""
        print("Shutting down P2P Manager and closing all peer connections...")
        for peer_id, peer in list(self.peers.items()):
            print(f"Closing connection with {peer_id}...")
            if peer.pc.connectionState != "closed":
                await peer.pc.close()
        self.peers.clear()
        print("All P2P connections closed.")

    async def shutdown_peer_connection(self, peer_id: str):
        """Closes a single peer connection."""
        if peer_id in self.peers:
            print(f"Closing connection with {peer_id}...")
            peer = self.peers[peer_id]
            if peer.pc.connectionState != "closed":
                await peer.pc.close()
            # The on_close handler in handle_data_channel will remove it from the dict
            
    async def shutdown_all(self):
        """Closes all active P2P connections."""
        print("Shutting down P2P Manager and closing all peer connections...")
        for peer_id in list(self.peers.keys()):
            await self.shutdown_peer_connection(peer_id)
        print("All P2P connections closed.")