# dpc-client/core/dpc_client_core/webrtc_peer.py

import asyncio
import json
import os
from pathlib import Path
from typing import Dict, Any, Callable, Optional
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCIceCandidate,
    RTCDataChannel,
    RTCConfiguration,
    RTCIceServer
)

from .settings import Settings


class WebRTCPeerConnection:
    """
    Wrapper for aiortc RTCPeerConnection that manages WebRTC data channel communication.
    Provides the same interface as PeerConnection for seamless integration.
    """

    def __init__(self, node_id: str, is_initiator: bool = False):
        self.node_id = node_id
        self.is_initiator = is_initiator

        # Load TURN credentials from Settings (environment variables or config file)
        dpc_home = Path.home() / ".dpc"
        settings = Settings(dpc_home)
        turn_username = settings.get_turn_username()
        turn_credential = settings.get_turn_credential()

        # Create RTCConfiguration with STUN and TURN servers for NAT traversal
        ice_servers = [
            # STUN servers (for discovering public IP)
            RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
            RTCIceServer(urls=["stun:stun1.l.google.com:19302"]),
            RTCIceServer(urls=["stun:global.stun.twilio.com:3478"]),
        ]

        # Add TURN servers if credentials are configured
        if turn_username and turn_credential:
            # Metered.ca TURN servers (user's account)
            ice_servers.append(
                RTCIceServer(
                    urls=[
                        "stun:stun.relay.metered.ca:80",
                        "turn:global.relay.metered.ca:80",
                        "turn:global.relay.metered.ca:80?transport=tcp",
                        "turn:global.relay.metered.ca:443",
                        "turns:global.relay.metered.ca:443?transport=tcp",
                    ],
                    username=turn_username,
                    credential=turn_credential
                )
            )
            print(f"[WebRTC] Using configured TURN credentials (username: {turn_username[:8]}...)")
        else:
            print("[WebRTC] Warning: No TURN credentials configured")
            print("         Set DPC_TURN_USERNAME and DPC_TURN_CREDENTIAL environment variables")
            print("         or add [turn] section to ~/.dpc/config.ini")
            print("         WebRTC connections may fail without TURN relay!")

            # Fallback: Try free OpenRelay servers (unreliable)
            ice_servers.extend([
                RTCIceServer(
                    urls=[
                        "turn:openrelay.metered.ca:80",
                        "turn:openrelay.metered.ca:443",
                        "turn:openrelay.metered.ca:443?transport=tcp"
                    ],
                    username="openrelayproject",
                    credential="openrelayproject"
                ),
            ])
            print("[WebRTC] Falling back to free OpenRelay servers (may not work)")

        configuration = RTCConfiguration(iceServers=ice_servers)

        # Create RTCPeerConnection with proper configuration
        self.pc = RTCPeerConnection(configuration=configuration)

        self.data_channel: RTCDataChannel = None
        self.ready = asyncio.Event()
        self.on_ice_candidate: Callable = None  # Not used in aiortc (candidates in SDP)
        self.on_message: Callable = None
        self.on_close: Callable = None
        self._ice_gathering_complete = asyncio.Event()
        self._keepalive_task: asyncio.Task = None
        self._keepalive_interval = 20.0  # Send keepalive every 20 seconds
        self._closing = False  # Track if we're intentionally closing

        # Set up event handlers
        self._setup_handlers()
    
    def _setup_handlers(self):
        """Set up WebRTC event handlers."""
        
        @self.pc.on("datachannel")
        def on_datachannel(channel: RTCDataChannel):
            """Handle incoming data channel (for answerer)."""
            print(f"Data channel '{channel.label}' received from peer {self.node_id}")
            self.data_channel = channel
            self._setup_channel_handlers()

        @self.pc.on("icegatheringstatechange")
        async def on_icegatheringstatechange():
            """Track ICE gathering state."""
            state = self.pc.iceGatheringState
            print(f"[{self.node_id}] ICE gathering state: {state}")
            if state == "complete":
                self._ice_gathering_complete.set()
                # Log candidate types found in SDP
                if self.pc.localDescription:
                    sdp = self.pc.localDescription.sdp
                    host_count = sdp.count("typ host")
                    srflx_count = sdp.count("typ srflx")
                    relay_count = sdp.count("typ relay")
                    print(f"[{self.node_id}] ICE gathering complete - "
                          f"host:{host_count} srflx:{srflx_count} relay:{relay_count}")
                else:
                    print(f"[{self.node_id}] ICE gathering complete - SDP includes all candidates")
        
        @self.pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            """Track ICE connection state."""
            state = self.pc.iceConnectionState
            print(f"[{self.node_id}] ICE connection state: {state}")

            if state == "completed":
                print(f"[{self.node_id}] ICE connection established!")
            elif state == "failed":
                print(f"[{self.node_id}] ICE connection FAILED - NAT traversal unsuccessful")
                print(f"[{self.node_id}] Possible causes:")
                print(f"  - Symmetric NAT requiring TURN relay")
                print(f"  - Firewall blocking UDP traffic")
                print(f"  - TURN server unavailable or misconfigured")
            elif state == "disconnected":
                print(f"[{self.node_id}] ICE connection DISCONNECTED - network change or timeout")
                print(f"[{self.node_id}] Connection may recover automatically...")
            elif state == "closed":
                print(f"[{self.node_id}] ICE connection CLOSED")
        
        @self.pc.on("connectionstatechange")
        async def on_connectionstatechange():
            state = self.pc.connectionState
            print(f"[{self.node_id}] WebRTC connection state: {state}")

            if state == "connected":
                print(f"✅ WebRTC connection established with {self.node_id}")
                # Don't set ready here - wait for data channel to be open
            elif state in ["failed", "closed"]:
                # Stop keepalive on connection failure/close
                self._stop_keepalive()

                # Only notify if this wasn't an intentional close
                if not self._closing:
                    print(f"❌ WebRTC connection {state} with {self.node_id}")
                    if self.on_close:
                        asyncio.create_task(self.on_close(self.node_id))
                else:
                    print(f"[{self.node_id}] WebRTC connection closed (intentional)")
            
    def _setup_channel_handlers(self):
        """Set up data channel event handlers."""
        if not self.data_channel:
            return

        @self.data_channel.on("open")
        def on_open():
            print(f"✅ Data channel opened with {self.node_id}")
            self.ready.set()
            # Start keepalive task when data channel opens
            self._start_keepalive()

        @self.data_channel.on("message")
        def on_message(message):
            """Handle incoming messages."""
            if self.on_message:
                try:
                    data = json.loads(message)
                    # Ignore keepalive pings/pongs - don't pass to application
                    if data.get("type") in ["ping", "pong"]:
                        return
                    asyncio.create_task(self.on_message(data))
                except json.JSONDecodeError as e:
                    print(f"Failed to decode message from {self.node_id}: {e}")

        @self.data_channel.on("close")
        def on_close():
            ice_state = self.pc.iceConnectionState
            conn_state = self.pc.connectionState
            if not self._closing:
                print(f"⚠️  Data channel closed with {self.node_id} (unexpected)")
                print(f"   ICE state: {ice_state}, Connection state: {conn_state}")
            else:
                print(f"Data channel closed with {self.node_id} (intentional)")
            # Stop keepalive task when data channel closes
            self._stop_keepalive()

        # Check if data channel is already open (happens on answerer side)
        if self.data_channel.readyState == "open":
            print(f"✅ Data channel already open with {self.node_id}")
            self.ready.set()
            # Start keepalive task for already-open channel
            self._start_keepalive()
    
    async def create_offer(self) -> Dict[str, Any]:
        """Create WebRTC offer (initiator side)."""
        print(f"[{self.node_id}] Creating offer...")
        
        # Create data channel (initiator creates it)
        self.data_channel = self.pc.createDataChannel("dpc-data")
        self._setup_channel_handlers()
        
        # Create offer
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        
        print(f"[{self.node_id}] Waiting for ICE gathering to complete...")
        # Wait for ICE gathering to complete (candidates are added to SDP automatically)
        try:
            await asyncio.wait_for(self._ice_gathering_complete.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            print(f"[{self.node_id}] Warning: ICE gathering timeout after 10s, sending SDP anyway")

        # Log ICE candidates after gathering completes
        if self.pc.localDescription:
            sdp = self.pc.localDescription.sdp
            host_count = sdp.count("typ host")
            srflx_count = sdp.count("typ srflx")
            relay_count = sdp.count("typ relay")
            print(f"[{self.node_id}] Offer SDP contains - host:{host_count} srflx:{srflx_count} relay:{relay_count}")

        print(f"[{self.node_id}] Offer created with {len(self.pc.localDescription.sdp)} bytes SDP")
        
        return {
            "type": "offer",
            "sdp": self.pc.localDescription.sdp
        }
    
    async def handle_answer(self, answer: Dict[str, Any]):
        """Handle WebRTC answer (initiator side)."""
        print(f"[{self.node_id}] Processing answer...")
        rdesc = RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
        await self.pc.setRemoteDescription(rdesc)
        print(f"[{self.node_id}] Remote description set, ICE checking will begin")
    
    async def handle_offer(self, offer: Dict[str, Any]) -> Dict[str, Any]:
        """Handle WebRTC offer and create answer (answerer side)."""
        print(f"[{self.node_id}] Processing offer...")
        rdesc = RTCSessionDescription(sdp=offer["sdp"], type=offer["type"])
        await self.pc.setRemoteDescription(rdesc)
        
        # Create answer
        print(f"[{self.node_id}] Creating answer...")
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        
        print(f"[{self.node_id}] Waiting for ICE gathering to complete...")
        # Wait for ICE gathering
        try:
            await asyncio.wait_for(self._ice_gathering_complete.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            print(f"[{self.node_id}] Warning: ICE gathering timeout after 10s, sending SDP anyway")

        # Log ICE candidates after gathering completes
        if self.pc.localDescription:
            sdp = self.pc.localDescription.sdp
            host_count = sdp.count("typ host")
            srflx_count = sdp.count("typ srflx")
            relay_count = sdp.count("typ relay")
            print(f"[{self.node_id}] Answer SDP contains - host:{host_count} srflx:{srflx_count} relay:{relay_count}")

        print(f"[{self.node_id}] Answer created with {len(self.pc.localDescription.sdp)} bytes SDP")
        
        return {
            "type": "answer",
            "sdp": self.pc.localDescription.sdp
        }
    
    async def add_ice_candidate(self, candidate_dict: Dict[str, Any]):
        """
        Add ICE candidate. 
        NOTE: In aiortc, this is typically not needed as candidates are in the SDP.
        This method is kept for compatibility but may not be used.
        """
        if not candidate_dict or not candidate_dict.get("candidate"):
            return
        
        try:
            candidate = RTCIceCandidate(
                candidate=candidate_dict["candidate"],
                sdpMid=candidate_dict.get("sdpMid"),
                sdpMLineIndex=candidate_dict.get("sdpMLineIndex")
            )
            await self.pc.addIceCandidate(candidate)
            print(f"[{self.node_id}] Added external ICE candidate")
        except Exception as e:
            print(f"[{self.node_id}] Failed to add ICE candidate: {e}")
    
    async def send(self, message: Dict[str, Any]):
        """Send a message over the data channel."""
        if not self.data_channel or self.data_channel.readyState != "open":
            raise ConnectionError(
                f"Data channel not ready for {self.node_id}. "
                f"State: {self.data_channel.readyState if self.data_channel else 'None'}"
            )
        
        json_str = json.dumps(message)
        self.data_channel.send(json_str)
    
    async def wait_ready(self, timeout: float = 30.0):
        """Wait for connection and data channel to be ready."""
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # Provide more helpful error message
            ice_state = self.pc.iceConnectionState
            conn_state = self.pc.connectionState
            dc_state = self.data_channel.readyState if self.data_channel else None
            raise ConnectionError(
                f"WebRTC connection timeout with {self.node_id} after {timeout}s. "
                f"ICE state: {ice_state}, Connection state: {conn_state}, "
                f"Data channel state: {dc_state}. "
                f"This usually means NAT traversal failed or data channel didn't open. Try: "
                f"1) Testing on same local network, "
                f"2) Checking firewall/UDP settings, "
                f"3) Verifying TURN server is accessible"
            )

    def _start_keepalive(self):
        """Start the keepalive task to maintain WebRTC connection."""
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            print(f"[{self.node_id}] Keepalive task started (interval: {self._keepalive_interval}s)")

    def _stop_keepalive(self):
        """Stop the keepalive task."""
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            print(f"[{self.node_id}] Keepalive task stopped")

    async def _keepalive_loop(self):
        """Background task that sends periodic keepalive pings over data channel."""
        ping_count = 0
        try:
            while True:
                await asyncio.sleep(self._keepalive_interval)

                # Check if data channel is still open
                if not self.data_channel or self.data_channel.readyState != "open":
                    print(f"[{self.node_id}] Data channel not open, stopping keepalive")
                    break

                try:
                    ping_count += 1
                    ping_message = {
                        "type": "ping",
                        "timestamp": asyncio.get_event_loop().time()
                    }
                    self.data_channel.send(json.dumps(ping_message))
                    # Only log every 5th ping to reduce noise
                    if ping_count % 5 == 0:
                        print(f"[{self.node_id}] Keepalive ping #{ping_count} sent")

                except Exception as e:
                    print(f"[{self.node_id}] Error sending keepalive ping #{ping_count}: {e}")
                    break

        except asyncio.CancelledError:
            print(f"[{self.node_id}] Keepalive cancelled (sent {ping_count} pings)")
        except Exception as e:
            print(f"[{self.node_id}] Keepalive loop error: {e}")

    async def close(self):
        """Close the WebRTC connection."""
        self._closing = True
        self._stop_keepalive()
        if self.data_channel:
            self.data_channel.close()
        await self.pc.close()