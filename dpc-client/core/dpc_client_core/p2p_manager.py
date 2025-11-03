# dpc-client/core/dpc_client_core/p2p_manager.py

import asyncio
import ssl
from typing import Dict, Tuple
from dataclasses import asdict

import typer # We'll keep this for styled output in the console
from aiortc import RTCPeerConnection, RTCSessionDescription

# Re-using our robust protocol and crypto modules
from dpc_protocol.crypto import generate_node_id, load_identity
from dpc_protocol.protocol import (
    read_message, write_message, create_hello_message,
    create_context_data_message, create_error_response
)
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
        self.node_id, self.key_file, self.cert_file = load_identity()
        self.local_context = PCMCore().load_context() # Load context for serving
        self._server_task = None
        print("P2PManager initialized.")

    async def start_server(self):
        """
        The P2PManager doesn't need a traditional listening server anymore,
        as connections are brokered by the Hub. This method can be used
        for any startup logic if needed in the future.
        """
        print("P2PManager is ready to accept brokered connections.")
        # In a pure P2P model, we would start a listener here.
        # In the hub-assisted model, this is handled by incoming signals.
        pass

    async def handle_incoming_signal(self, signal: Dict[str, Any], hub_client: HubClient):
        """
        Processes a signaling message received from the Hub, typically an 'offer'
        from another peer wanting to connect.
        """
        sender_node_id = signal.get("sender_node_id")
        payload = signal.get("payload")
        if not sender_node_id or not payload:
            print(f"Invalid signal received: {signal}")
            return

        if payload.get("type") == "offer":
            print(f"Received connection offer from {sender_node_id}")
            
            pc = RTCPeerConnection()
            peer = Peer(pc=pc, data_channel=None) # Data channel will be created on connection
            self.peers[sender_node_id] = peer

            @pc.on("datachannel")
            def on_datachannel(channel):
                print(f"Data channel '{channel.label}' created by {sender_node_id}")
                peer.data_channel = channel
                # We now have a bidirectional channel. Start handling messages.
                asyncio.create_task(self.handle_data_channel(sender_node_id, channel))

            @pc.on("icecandidate")
            async def on_icecandidate(candidate):
                if candidate:
                    await hub_client.send_signal(sender_node_id, {"type": "ice-candidate", "candidate": candidate.to_sdp()})

            await pc.setRemoteDescription(RTCSessionDescription(sdp=payload["sdp"], type=payload["type"]))
            
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            
            # Send the answer back to the initiator via the Hub
            await hub_client.send_signal(sender_node_id, {"type": "answer", "sdp": pc.localDescription.sdp})
            print(f"Sent answer to {sender_node_id}")

        elif payload.get("type") == "answer":
            if sender_node_id in self.peers:
                pc = self.peers[sender_node_id].pc
                await pc.setRemoteDescription(RTCSessionDescription(sdp=payload["sdp"], type=payload["type"]))
                print(f"Processed answer from {sender_node_id}")

        elif payload.get("type") == "ice-candidate":
            if sender_node_id in self.peers:
                # This part is complex and requires parsing SDP, aiortc handles some of it
                # For now, we assume aiortc handles candidates correctly after offer/answer
                print(f"Received ICE candidate from {sender_node_id}")


    async def connect_to_peer(self, target_node_id: str, hub_client: HubClient):
        """Initiates a connection to a peer using the Hub for signaling."""
        if target_node_id in self.peers:
            print(f"Already connected or connecting to {target_node_id}")
            return

        print(f"Initiating P2P connection to {target_node_id}...")
        pc = RTCPeerConnection()
        peer = Peer(pc=pc, data_channel=pc.createDataChannel("dpc-channel"))
        self.peers[target_node_id] = peer

        @pc.on("icecandidate")
        async def on_icecandidate(candidate):
            if candidate:
                await hub_client.send_signal(target_node_id, {"type": "ice-candidate", "candidate": candidate.to_sdp()})

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        # Send the offer to the target via the Hub
        await hub_client.send_signal(target_node_id, {"type": "offer", "sdp": pc.localDescription.sdp})
        print(f"Sent offer to {target_node_id}")
        
        # Now we wait for signals (answer, candidates) to be processed by handle_incoming_signal

    async def handle_data_channel(self, peer_node_id: str, channel):
        """Handles the lifecycle of an established data channel."""
        print(f"Data channel with {peer_node_id} is open and ready.")
        
        # TODO: Implement the DPTP HELLO handshake over the data channel
        
        @channel.on("message")
        async def on_message(message):
            # This is where we receive DPTP commands
            print(f"Received message from {peer_node_id}: {message[:100]}...")
            # TODO: Parse message using protocol.py
            # TODO: Check firewall for permissions
            # TODO: Send response
            pass

        @channel.on("close")
        def on_close():
            print(f"Data channel with {peer_node_id} closed.")
            if peer_node_id in self.peers:
                del self.peers[peer_node_id]

    async def shutdown(self):
        """Closes all active P2P connections."""
        print("Shutting down P2P Manager and closing all peer connections...")
        for peer_id, peer in list(self.peers.items()):
            print(f"Closing connection with {peer_id}...")
            await peer.pc.close()
        self.peers.clear()
        print("All P2P connections closed.")