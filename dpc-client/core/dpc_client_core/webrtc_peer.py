# dpc-client/core/dpc_client_core/webrtc_peer.py

import asyncio
import json
import logging
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

logger = logging.getLogger(__name__)


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
        # Load STUN servers from configuration (allows user customization)
        stun_urls = settings.get_stun_servers()
        ice_servers = [RTCIceServer(urls=[url]) for url in stun_urls]

        # Add TURN servers if credentials are configured
        if turn_username and turn_credential:
            # Load TURN server URLs from config.ini
            turn_urls = settings.get_turn_servers()
            if turn_urls:
                ice_servers.append(
                    RTCIceServer(
                        urls=turn_urls,
                        username=turn_username,
                        credential=turn_credential
                    )
                )
                logger.info("Using configured TURN servers (%d URLs) with username: %s...", len(turn_urls), turn_username[:8])
            else:
                logger.warning("TURN credentials provided but no TURN server URLs in config.ini [turn] servers")
        else:
            logger.warning("No TURN credentials configured - set DPC_TURN_USERNAME and DPC_TURN_CREDENTIAL environment variables or add [turn] username/credential to ~/.dpc/config.ini - WebRTC connections may fail without TURN relay")

            # Fallback: Try free public TURN servers from config (if configured)
            fallback_urls = settings.get_turn_fallback_servers()
            fallback_username = settings.get_turn_fallback_username()
            fallback_credential = settings.get_turn_fallback_credential()

            if fallback_urls and fallback_username and fallback_credential:
                ice_servers.append(
                    RTCIceServer(
                        urls=fallback_urls,
                        username=fallback_username,
                        credential=fallback_credential
                    )
                )
                logger.info("Using fallback public TURN servers (%d URLs) - may be unreliable", len(fallback_urls))
            else:
                logger.warning("No fallback TURN servers configured in config.ini")

        configuration = RTCConfiguration(
            iceServers=ice_servers,
            # Uncomment to force TURN relay (testing/troubleshooting only):
            # iceTransportPolicy="relay"
        )

        # Create RTCPeerConnection with proper configuration
        self.pc = RTCPeerConnection(configuration=configuration)

        self.data_channel: RTCDataChannel = None
        self.ready = asyncio.Event()
        self.on_ice_candidate: Callable = None  # Not used in aiortc (candidates in SDP)
        self.on_message: Callable = None
        self.on_close: Callable = None
        self._ice_gathering_complete = asyncio.Event()
        self._keepalive_task: asyncio.Task = None
        self._keepalive_interval = 5.0  # Send keepalive every 5 seconds (aggressive to prevent SCTP timeout)
        self._closing = False  # Track if we're intentionally closing

        # Set up event handlers
        self._setup_handlers()
    
    def _setup_handlers(self):
        """Set up WebRTC event handlers."""
        
        @self.pc.on("datachannel")
        def on_datachannel(channel: RTCDataChannel):
            """Handle incoming data channel (for answerer)."""
            logger.info("Data channel '%s' received from peer %s", channel.label, self.node_id)
            self.data_channel = channel
            self._setup_channel_handlers()

        @self.pc.on("icegatheringstatechange")
        async def on_icegatheringstatechange():
            """Track ICE gathering state."""
            state = self.pc.iceGatheringState
            logger.debug("[%s] ICE gathering state: %s", self.node_id, state)
            if state == "complete":
                self._ice_gathering_complete.set()
                # Log candidate types found in SDP
                if self.pc.localDescription:
                    sdp = self.pc.localDescription.sdp
                    host_count = sdp.count("typ host")
                    srflx_count = sdp.count("typ srflx")
                    relay_count = sdp.count("typ relay")
                    logger.info("[%s] ICE gathering complete - host:%d srflx:%d relay:%d",
                               self.node_id, host_count, srflx_count, relay_count)
                else:
                    logger.info("[%s] ICE gathering complete - SDP includes all candidates", self.node_id)
        
        @self.pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            """Track ICE connection state."""
            state = self.pc.iceConnectionState
            logger.debug("[%s] ICE connection state: %s", self.node_id, state)

            if state == "completed":
                logger.info("[%s] ICE connection established", self.node_id)
                # Try to log selected candidate pair for debugging
                try:
                    # Access transport to get selected candidate info
                    if hasattr(self.pc, '_RTCPeerConnection__sctp') and self.pc._RTCPeerConnection__sctp:
                        transport = self.pc._RTCPeerConnection__sctp.transport.transport
                        if hasattr(transport, '_connection') and transport._connection:
                            selected = transport._connection.selected_candidate_pair
                            if selected:
                                local_type = selected[0].type if selected[0] else "unknown"
                                remote_type = selected[1].type if selected[1] else "unknown"
                                local_addr = f"{selected[0].host}:{selected[0].port}" if selected[0] else "unknown"
                                remote_addr = f"{selected[1].host}:{selected[1].port}" if selected[1] else "unknown"
                                logger.info("[%s] Selected ICE candidate pair - Local: %s @ %s, Remote: %s @ %s",
                                           self.node_id, local_type, local_addr, remote_type, remote_addr)
                except Exception as e:
                    # Silently ignore if internal structure changed
                    pass
            elif state == "failed":
                logger.error("[%s] ICE connection FAILED - NAT traversal unsuccessful - Possible causes: Symmetric NAT requiring TURN relay, Firewall blocking UDP traffic, TURN server unavailable or misconfigured",
                            self.node_id)
            elif state == "disconnected":
                logger.warning("[%s] ICE connection DISCONNECTED - network change or timeout - Connection may recover automatically",
                              self.node_id)
            elif state == "closed":
                logger.info("[%s] ICE connection CLOSED", self.node_id)
        
        @self.pc.on("connectionstatechange")
        async def on_connectionstatechange():
            state = self.pc.connectionState
            logger.debug("[%s] WebRTC connection state: %s", self.node_id, state)

            if state == "connected":
                logger.info("WebRTC connection established with %s", self.node_id)
                # Don't set ready here - wait for data channel to be open
            elif state in ["failed", "closed"]:
                # Stop keepalive on connection failure/close
                self._stop_keepalive()

                # Only notify if this wasn't an intentional close
                if not self._closing:
                    logger.error("WebRTC connection %s with %s", state, self.node_id)
                    if self.on_close:
                        asyncio.create_task(self.on_close(self.node_id))
                else:
                    logger.info("[%s] WebRTC connection closed (intentional)", self.node_id)
            
    def _setup_channel_handlers(self):
        """Set up data channel event handlers."""
        if not self.data_channel:
            return

        @self.data_channel.on("open")
        def on_open():
            logger.info("Data channel opened with %s", self.node_id)
            self.ready.set()
            # Start keepalive task when data channel opens
            self._start_keepalive()

        @self.data_channel.on("message")
        def on_message(message):
            """Handle incoming messages."""
            try:
                data = json.loads(message)

                # Handle keepalive ping - respond with pong
                if data.get("type") == "ping":
                    try:
                        pong_message = {
                            "type": "pong",
                            "timestamp": data.get("timestamp")
                        }
                        self.data_channel.send(json.dumps(pong_message))
                        logger.debug("[%s] Received ping, sent pong", self.node_id)
                    except Exception as e:
                        logger.error("[%s] Failed to send pong: %s", self.node_id, e)
                    return

                # Handle pong responses - just log receipt
                if data.get("type") == "pong":
                    logger.debug("[%s] Received pong response", self.node_id)
                    return

                # Pass all other messages to application
                if self.on_message:
                    asyncio.create_task(self.on_message(data))

            except json.JSONDecodeError as e:
                logger.error("Failed to decode message from %s: %s", self.node_id, e)

        @self.data_channel.on("error")
        def on_error(error):
            """Handle data channel errors."""
            logger.error("Data channel error with %s: %s", self.node_id, error)

        @self.data_channel.on("close")
        def on_close():
            ice_state = self.pc.iceConnectionState
            conn_state = self.pc.connectionState
            dc_state = self.data_channel.readyState if self.data_channel else None
            if not self._closing:
                logger.warning("Data channel closed with %s (unexpected) - ICE state: %s, Connection state: %s, DC state: %s",
                              self.node_id, ice_state, conn_state, dc_state)
            else:
                logger.info("Data channel closed with %s (intentional)", self.node_id)
            # Stop keepalive task when data channel closes
            self._stop_keepalive()

        # Check if data channel is already open (happens on answerer side)
        if self.data_channel.readyState == "open":
            logger.info("Data channel already open with %s", self.node_id)
            self.ready.set()
            # Start keepalive task for already-open channel
            self._start_keepalive()
    
    async def create_offer(self) -> Dict[str, Any]:
        """Create WebRTC offer (initiator side)."""
        logger.info("[%s] Creating offer", self.node_id)

        # Create data channel (initiator creates it)
        # Use default reliable, ordered channel (no maxRetransmits/maxPacketLifeTime)
        # This ensures SCTP uses fully reliable mode
        self.data_channel = self.pc.createDataChannel("dpc-data", ordered=True)
        self._setup_channel_handlers()

        # Create offer
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)

        logger.debug("[%s] Waiting for ICE gathering to complete", self.node_id)
        # Wait for ICE gathering to complete (candidates are added to SDP automatically)
        try:
            await asyncio.wait_for(self._ice_gathering_complete.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("[%s] ICE gathering timeout after 10s, sending SDP anyway", self.node_id)

        # Log ICE candidates after gathering completes
        if self.pc.localDescription:
            sdp = self.pc.localDescription.sdp
            host_count = sdp.count("typ host")
            srflx_count = sdp.count("typ srflx")
            relay_count = sdp.count("typ relay")
            logger.info("[%s] Offer SDP contains - host:%d srflx:%d relay:%d",
                       self.node_id, host_count, srflx_count, relay_count)

        logger.info("[%s] Offer created with %d bytes SDP", self.node_id, len(self.pc.localDescription.sdp))

        return {
            "type": "offer",
            "sdp": self.pc.localDescription.sdp
        }
    
    async def handle_answer(self, answer: Dict[str, Any]):
        """Handle WebRTC answer (initiator side)."""
        logger.info("[%s] Processing answer", self.node_id)
        rdesc = RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
        await self.pc.setRemoteDescription(rdesc)
        logger.info("[%s] Remote description set, ICE checking will begin", self.node_id)

    async def handle_offer(self, offer: Dict[str, Any]) -> Dict[str, Any]:
        """Handle WebRTC offer and create answer (answerer side)."""
        logger.info("[%s] Processing offer", self.node_id)
        rdesc = RTCSessionDescription(sdp=offer["sdp"], type=offer["type"])
        await self.pc.setRemoteDescription(rdesc)

        # Create answer
        logger.info("[%s] Creating answer", self.node_id)
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)

        logger.debug("[%s] Waiting for ICE gathering to complete", self.node_id)
        # Wait for ICE gathering
        try:
            await asyncio.wait_for(self._ice_gathering_complete.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("[%s] ICE gathering timeout after 10s, sending SDP anyway", self.node_id)

        # Log ICE candidates after gathering completes
        if self.pc.localDescription:
            sdp = self.pc.localDescription.sdp
            host_count = sdp.count("typ host")
            srflx_count = sdp.count("typ srflx")
            relay_count = sdp.count("typ relay")
            logger.info("[%s] Answer SDP contains - host:%d srflx:%d relay:%d",
                       self.node_id, host_count, srflx_count, relay_count)

        logger.info("[%s] Answer created with %d bytes SDP", self.node_id, len(self.pc.localDescription.sdp))

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
            logger.debug("[%s] Added external ICE candidate", self.node_id)
        except Exception as e:
            logger.warning("[%s] Failed to add ICE candidate: %s", self.node_id, e)
    
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
            logger.debug("[%s] Keepalive task started (interval: %ss)", self.node_id, self._keepalive_interval)

    def _stop_keepalive(self):
        """Stop the keepalive task."""
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            logger.debug("[%s] Keepalive task stopped", self.node_id)

    async def _keepalive_loop(self):
        """Background task that sends periodic keepalive pings over data channel."""
        ping_count = 0
        try:
            while True:
                await asyncio.sleep(self._keepalive_interval)

                # Check if data channel is still open
                if not self.data_channel or self.data_channel.readyState != "open":
                    logger.debug("[%s] Data channel not open, stopping keepalive", self.node_id)
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
                        logger.debug("[%s] Keepalive ping #%d sent", self.node_id, ping_count)

                except Exception as e:
                    logger.error("[%s] Error sending keepalive ping #%d: %s", self.node_id, ping_count, e)
                    break

        except asyncio.CancelledError:
            logger.debug("[%s] Keepalive cancelled (sent %d pings)", self.node_id, ping_count)
        except Exception as e:
            logger.error("[%s] Keepalive loop error: %s", self.node_id, e, exc_info=True)

    def get_external_ip(self) -> str | None:
        """
        Extract external IP address from server reflexive (srflx) ICE candidates.

        Returns:
            External IP address if found in SDP, None otherwise
        """
        if not self.pc or not self.pc.localDescription:
            return None

        try:
            import re
            sdp = self.pc.localDescription.sdp

            # Match srflx candidates in SDP
            # Format: a=candidate:<foundation> <component> <protocol> <priority> <ip> <port> typ srflx
            srflx_pattern = r'a=candidate:\S+ \S+ \S+ \S+ (\d+\.\d+\.\d+\.\d+) \S+ typ srflx'
            matches = re.findall(srflx_pattern, sdp)

            if matches:
                # Return the first external IP found
                return matches[0]
        except Exception as e:
            logger.warning("[%s] Error extracting external IP from SDP: %s", self.node_id, e)

        return None

    async def close(self):
        """Close the WebRTC connection."""
        self._closing = True
        self._stop_keepalive()
        if self.data_channel:
            self.data_channel.close()
        await self.pc.close()