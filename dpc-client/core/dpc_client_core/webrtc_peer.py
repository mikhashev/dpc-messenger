# dpc-client/core/dpc_client_core/webrtc_peer.py

import asyncio
import json
from typing import Dict, Any, Callable
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCDataChannel
from aiortc.contrib.media import MediaBlackhole


class WebRTCPeerConnection:
    """
    Wrapper for aiortc RTCPeerConnection that manages WebRTC data channel communication.
    Provides the same interface as PeerConnection for seamless integration.
    """
    
    def __init__(self, node_id: str, is_initiator: bool = False):
        self.node_id = node_id
        self.is_initiator = is_initiator
        
        # Create RTCPeerConnection with STUN servers for NAT traversal
        self.pc = RTCPeerConnection(configuration={
            "iceServers": [
                {"urls": "stun:stun.l.google.com:19302"},
                {"urls": "stun:stun1.l.google.com:19302"},
                {"urls": "stun:stun2.l.google.com:19302"},
            ]
        })
        
        self.data_channel: RTCDataChannel = None
        self.ready = asyncio.Event()
        self.on_ice_candidate: Callable = None
        self.on_message: Callable = None
        
        # Set up event handlers
        self._setup_handlers()
    
    def _setup_handlers(self):
        """Set up WebRTC event handlers."""
        
        @self.pc.on("datachannel")
        def on_datachannel(channel: RTCDataChannel):
            """Handle incoming data channel (for answerer)."""
            print(f"Data channel '{channel.label}' received from peer")
            self.data_channel = channel
            self._setup_channel_handlers()
        
        @self.pc.on("icecandidate")
        def on_icecandidate(candidate):
            """Forward ICE candidates to signaling."""
            if candidate and self.on_ice_candidate:
                asyncio.create_task(self.on_ice_candidate({
                    "candidate": candidate.candidate,
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex
                }))
        
        @self.pc.on("connectionstatechange")
        async def on_connectionstatechange():
            state = self.pc.connectionState
            print(f"WebRTC connection state: {state}")
            
            if state == "connected":
                print(f"WebRTC connection established with {self.node_id}")
                self.ready.set()
            elif state in ["failed", "closed"]:
                print(f"WebRTC connection {state} with {self.node_id}")
                await self.close()
    
    def _setup_channel_handlers(self):
        """Set up data channel event handlers."""
        if not self.data_channel:
            return
        
        @self.data_channel.on("open")
        def on_open():
            print(f"Data channel opened with {self.node_id}")
            self.ready.set()
        
        @self.data_channel.on("message")
        def on_message(message):
            """Handle incoming messages."""
            if self.on_message:
                try:
                    data = json.loads(message)
                    asyncio.create_task(self.on_message(data))
                except json.JSONDecodeError as e:
                    print(f"Failed to decode message: {e}")
        
        @self.data_channel.on("close")
        def on_close():
            print(f"Data channel closed with {self.node_id}")
    
    async def create_offer(self) -> Dict[str, Any]:
        """Create WebRTC offer (initiator side)."""
        # Create data channel (initiator creates it)
        self.data_channel = self.pc.createDataChannel("dpc-data")
        self._setup_channel_handlers()
        
        # Create offer
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        
        return {
            "type": "offer",
            "sdp": self.pc.localDescription.sdp
        }
    
    async def handle_answer(self, answer: Dict[str, Any]):
        """Handle WebRTC answer (initiator side)."""
        rdesc = RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
        await self.pc.setRemoteDescription(rdesc)
    
    async def handle_offer(self, offer: Dict[str, Any]) -> Dict[str, Any]:
        """Handle WebRTC offer and create answer (answerer side)."""
        rdesc = RTCSessionDescription(sdp=offer["sdp"], type=offer["type"])
        await self.pc.setRemoteDescription(rdesc)
        
        # Create answer
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        
        return {
            "type": "answer",
            "sdp": self.pc.localDescription.sdp
        }
    
    async def add_ice_candidate(self, candidate_dict: Dict[str, Any]):
        """Add ICE candidate."""
        if not candidate_dict or not candidate_dict.get("candidate"):
            return
        
        candidate = RTCIceCandidate(
            candidate=candidate_dict["candidate"],
            sdpMid=candidate_dict.get("sdpMid"),
            sdpMLineIndex=candidate_dict.get("sdpMLineIndex")
        )
        await self.pc.addIceCandidate(candidate)
    
    async def send(self, message: Dict[str, Any]):
        """Send a message over the data channel."""
        if not self.data_channel or self.data_channel.readyState != "open":
            raise ConnectionError(f"Data channel not ready. State: {self.data_channel.readyState if self.data_channel else 'None'}")
        
        json_str = json.dumps(message)
        self.data_channel.send(json_str)
    
    async def wait_ready(self, timeout: float = 30.0):
        """Wait for connection to be ready."""
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise ConnectionError(f"WebRTC connection timeout with {self.node_id}")
    
    async def close(self):
        """Close the WebRTC connection."""
        if self.data_channel:
            self.data_channel.close()
        await self.pc.close()