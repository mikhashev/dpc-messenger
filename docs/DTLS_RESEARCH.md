# DTLS Library Research Summary

**Date:** 2025-12-09
**Purpose:** Select DTLS library for v0.10.1 UDP hole punch encryption
**Requirement:** Asyncio-compatible DTLS for privacy-first messaging

---

## Libraries Evaluated

### 1. aioquic (QUIC, not DTLS)
- **Type:** QUIC protocol implementation (not DTLS)
- **Status:** ❌ **Not suitable** - QUIC is different from DTLS
- **Async Support:** ✅ Native asyncio
- **Why not:** We need DTLS specifically, not QUIC

**Sources:**
- [aioquic documentation](https://aioquic.readthedocs.io/en/latest/asyncio.html)
- [GitHub - aioquic](https://github.com/aiortc/aioquic)

### 2. pyOpenSSL (Synchronous DTLS)
- **Type:** Python wrapper for OpenSSL
- **Status:** ✅ **Has DTLS support** (version 25.3.0+)
- **Async Support:** ❌ Synchronous only (requires manual asyncio wrapping)
- **Version installed:** 25.3.0 (already in dependencies via aiortc)
- **Pros:** Mature, well-maintained, already installed
- **Cons:** Synchronous API (requires manual asyncio integration)

**Sources:**
- [pyOpenSSL Changelog](https://www.pyopenssl.org/en/latest/changelog.html)
- [pyOpenSSL SSL API](https://www.pyopenssl.org/en/24.0.0/api/ssl.html)

### 3. aio_dtls (Experimental)
- **Type:** Asyncio DTLS implementation
- **Status:** ⚠️ **Small/experimental project**
- **Async Support:** ✅ Native asyncio
- **Why not:** Less mature, smaller community, untested in production

**Sources:**
- [GitHub - aio_dtls](https://github.com/businka/aio_dtls)

### 4. aiortc's Internal DTLS (WINNER! ✅)
- **Type:** pyOpenSSL wrapper with asyncio integration
- **Status:** ✅ **Already in use** for WebRTC connections
- **Async Support:** ✅ Manual asyncio wrapping (proven pattern)
- **Version installed:** aiortc 1.14.0
- **Implementation:** `rtcdtlstransport.py` (418+ lines)
- **Pros:**
  - Already installed and tested
  - Proven in production (WebRTC)
  - Uses pyOpenSSL (mature)
  - Asyncio pattern we can copy
  - Same certificates we already use
- **Cons:** Internal API (not public), but we can copy the pattern

**Sources:**
- [aiortc GitHub](https://github.com/aiortc/aiortc)
- [aiortc documentation](https://aiortc.readthedocs.io/en/latest/api.html)

---

## ✅ Decision: Use pyOpenSSL with Asyncio Pattern from aiortc

### Rationale

1. **Already Installed:** pyOpenSSL 25.3.0 is already a dependency (via aiortc)
2. **Proven Pattern:** aiortc shows how to use pyOpenSSL with asyncio for DTLS
3. **Production Tested:** WebRTC connections already use this approach
4. **Certificate Compatibility:** Can reuse existing `node.crt` and `node.key` files
5. **Privacy First:** Mature, well-audited OpenSSL library

### Implementation Strategy

We will:
1. **Copy aiortc's approach** (not the code, just the pattern)
2. **Use pyOpenSSL's `SSL.Connection`** for DTLS handshake
3. **Use `cryptography` library** for X.509 certificate handling
4. **Wrap synchronous calls in asyncio** (run_in_executor for blocking operations)
5. **Simplify** - Remove SRTP (not needed), keep only DTLS

### Key Components from aiortc Pattern

**Certificate Setup (`cryptography` library):**
```python
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from OpenSSL import SSL

# Load existing node.crt and node.key
# Create SSL.Context with DTLS support
ctx = SSL.Context(SSL.DTLS_METHOD)  # DTLS instead of TLS
ctx.use_certificate(cert)
ctx.use_privatekey(key)
ctx.set_cipher_list(b"ECDHE-ECDSA-AES128-GCM-SHA256:...")
```

**DTLS Handshake (pyOpenSSL `SSL.Connection`):**
```python
# Create DTLS connection over UDP socket
ssl_conn = SSL.Connection(ctx, socket)
ssl_conn.set_connect_state()  # or set_accept_state()

# Perform handshake (manual asyncio wrapping)
await loop.run_in_executor(None, ssl_conn.do_handshake)
```

**Send/Receive (Encrypted):**
```python
# Send encrypted data
await loop.run_in_executor(None, ssl_conn.send, message_bytes)

# Receive encrypted data
data = await loop.run_in_executor(None, ssl_conn.recv, 4096)
```

### Files to Reference

**aiortc DTLS Implementation:**
- Location: `.venv/Lib/site-packages/aiortc/rtcdtlstransport.py`
- Key methods:
  - `RTCCertificate._create_context()` - SSL context setup (line 190-208)
  - `RTCDtlsTransport._connect()` - DTLS handshake logic
  - Certificate fingerprint validation

**Our Implementation:**
- New file: `dpc-client/core/dpc_client_core/transports/dtls_connection.py`
- Class: `DTLSPeerConnection`
- Pattern: Similar to aiortc but simpler (no SRTP, no RTP routing)

---

## Implementation Timeline

### Week 1: DTLS Integration (5-7 days)

**Day 1-2: ✅ Research Complete**
- Evaluated libraries
- Selected pyOpenSSL + aiortc pattern
- Reviewed aiortc's DTLS implementation

**Day 3-4: DTLSConnection Transport (NEXT)**
- Create `transports/dtls_connection.py`
- Implement `DTLSPeerConnection` class
- SSL context setup with node.crt/node.key
- Handshake method with asyncio wrapping
- Send/receive methods (encrypted)

**Day 5-7: Integration**
- Modify `connection_strategies/udp_hole_punch.py`
- Add DTLS upgrade after successful hole punch
- Fallback to relay on DTLS failure
- Configuration options

---

## Security Considerations

### Certificate Validation

**Node ID Verification:**
```python
# After DTLS handshake, verify peer certificate
peer_cert = ssl_conn.get_peer_certificate()
peer_node_id = extract_node_id_from_cert(peer_cert)

if peer_node_id != expected_node_id:
    raise SecurityError("Certificate node_id mismatch")
```

### Cipher Suites

Use strong ciphers only (from aiortc):
```
ECDHE-ECDSA-AES128-GCM-SHA256
ECDHE-ECDSA-CHACHA20-POLY1305
ECDHE-ECDSA-AES128-SHA
ECDHE-ECDSA-AES256-SHA
```

### DTLS Version

Use DTLS 1.2 or later (not DTLS 1.0):
```python
ctx = SSL.Context(SSL.DTLSv1_2_METHOD)  # DTLS 1.2
```

---

## Testing Strategy

### Unit Tests
- [ ] DTLS handshake success (both peers)
- [ ] Certificate validation (node_id match)
- [ ] Encryption/decryption (send/receive)
- [ ] Handshake timeout handling
- [ ] Invalid certificate rejection

### Integration Tests
- [ ] UDP hole punch + DTLS upgrade
- [ ] Fallback to relay on DTLS failure
- [ ] Connection via all 6 strategies

### Security Tests
- [ ] Man-in-the-middle detection (invalid cert)
- [ ] Replay attack prevention (DTLS nonces)
- [ ] Downgrade attack prevention

---

## References

### Official Documentation
- [pyOpenSSL](https://www.pyopenssl.org/)
- [cryptography library](https://cryptography.io/)
- [OpenSSL DTLS](https://www.openssl.org/docs/man1.1.1/man3/DTLS_method.html)

### Research Sources
- [aioquic asyncio API](https://aioquic.readthedocs.io/en/latest/asyncio.html)
- [pyOpenSSL Changelog](https://www.pyopenssl.org/en/latest/changelog.html)
- [aiortc GitHub](https://github.com/aiortc/aiortc)
- [aio_dtls GitHub](https://github.com/businka/aio_dtls)

### aiortc DTLS Pattern
- File: `.venv/Lib/site-packages/aiortc/rtcdtlstransport.py`
- Lines of interest:
  - 190-208: SSL context creation
  - 341-398: RTCDtlsTransport class
  - Certificate fingerprint validation
  - Asyncio handshake handling

---

## Next Steps

1. ✅ Research complete - pyOpenSSL + aiortc pattern selected
2. **NEXT:** Implement `DTLSPeerConnection` class (Day 3-4)
3. Integrate with UDP hole punch strategy (Day 5-7)
4. Write tests (Week 2)
5. Update documentation (Week 2)
6. Release v0.10.1 with full encryption

---

**Status:** Research complete, ready for implementation
**Decision:** pyOpenSSL with asyncio pattern from aiortc
**Confidence:** High (proven in production via WebRTC)
