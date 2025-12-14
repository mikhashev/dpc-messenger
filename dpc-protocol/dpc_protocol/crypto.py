# dpc/dpc/crypto.py

import hashlib
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Tuple

from cryptography import x509

logger = logging.getLogger(__name__)
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

DPC_HOME_DIR = Path.home() / ".dpc"
KEY_FILE = DPC_HOME_DIR / "node.key"
CERT_FILE = DPC_HOME_DIR / "node.crt"
NODE_ID_FILE = DPC_HOME_DIR / "node.id"

def generate_node_id(public_key: rsa.RSAPublicKey) -> str:
    """
    Generate node ID from RSA public key.

    Uses first 32 hex characters (128 bits) of SHA256 hash of public key.
    This ensures compatibility with Kademlia DHT which expects 128-bit node IDs.

    Returns:
        Node ID in format: dpc-node-[32 hex chars] (e.g., "dpc-node-a1b2c3d4...")
    """
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    sha256_hash = hashlib.sha256(public_bytes).hexdigest()
    # Use first 32 hex chars (128 bits) for Kademlia DHT compatibility
    return f"dpc-node-{sha256_hash[:32]}"

def generate_identity():
    logger.info("Generating new node identity")

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    public_key = private_key.public_key()

    node_id = generate_node_id(public_key)
    logger.info("Node ID: %s", node_id)

    builder = x509.CertificateBuilder()
    builder = builder.subject_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, node_id),
    ]))
    builder = builder.issuer_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, node_id),
    ]))
    builder = builder.not_valid_before(datetime.now(timezone.utc))
    builder = builder.not_valid_after(datetime.now(timezone.utc) + timedelta(days=3650))
    builder = builder.serial_number(x509.random_serial_number())
    builder = builder.public_key(public_key)
    builder = builder.add_extension(
        x509.BasicConstraints(ca=True, path_length=None), critical=True,
    )
    
    certificate = builder.sign(
        private_key=private_key, algorithm=hashes.SHA256(),
    )

    DPC_HOME_DIR.mkdir(exist_ok=True)

    with open(KEY_FILE, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    logger.info("Private key saved to %s", KEY_FILE)

    with open(CERT_FILE, "wb") as f:
        f.write(certificate.public_bytes(serialization.Encoding.PEM))
    logger.info("Certificate saved to %s", CERT_FILE)

    with open(NODE_ID_FILE, "w") as f:
        f.write(node_id)
    logger.info("Node ID saved to %s", NODE_ID_FILE)

def load_identity() -> Tuple[str, Path, Path]:
    if not all([DPC_HOME_DIR.exists(), KEY_FILE.exists(), CERT_FILE.exists(), NODE_ID_FILE.exists()]):
        raise FileNotFoundError("Identity not found. Please run 'dpc init' first.")

    node_id = NODE_ID_FILE.read_text().strip()
    return node_id, KEY_FILE, CERT_FILE


def encrypt_with_public_key(data: bytes, public_key) -> bytes:
    """
    Encrypt data with RSA public key using OAEP padding.

    Args:
        data: Plaintext bytes to encrypt
        public_key: RSA public key from certificate

    Returns:
        Encrypted ciphertext bytes

    Note:
        Used for end-to-end encryption in gossip protocol.
        Maximum data size limited by RSA key size (2048 bits = 190 bytes with OAEP).
        For larger payloads, use encrypt_with_public_key_hybrid() instead.
    """
    from cryptography.hazmat.primitives.asymmetric import padding

    ciphertext = public_key.encrypt(
        data,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None
        )
    )
    return ciphertext


def decrypt_with_private_key(ciphertext: bytes, private_key) -> bytes:
    """
    Decrypt data with RSA private key using OAEP padding.

    Args:
        ciphertext: Encrypted bytes to decrypt
        private_key: RSA private key from node.key file

    Returns:
        Decrypted plaintext bytes

    Raises:
        ValueError: If decryption fails (wrong key, corrupted data, etc.)

    Note:
        Used for end-to-end encryption in gossip protocol.
    """
    from cryptography.hazmat.primitives.asymmetric import padding

    plaintext = private_key.decrypt(
        ciphertext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None
        )
    )
    return plaintext


def encrypt_with_public_key_hybrid(data: bytes, public_key) -> bytes:
    """
    Encrypt data using hybrid encryption (AES-GCM + RSA-OAEP).

    Algorithm:
    1. Generate random 256-bit AES key
    2. Encrypt data with AES-256-GCM (provides encryption + authentication)
    3. Encrypt AES key with RSA-OAEP (recipient's public key)
    4. Combine: encrypted_key || nonce || ciphertext || tag

    Args:
        data: Plaintext bytes to encrypt (unlimited size)
        public_key: RSA public key from certificate

    Returns:
        Encrypted blob: [encrypted_key_len(2 bytes)][encrypted_aes_key][nonce(12 bytes)][ciphertext][tag(16 bytes)]

    Note:
        - No size limit (unlike pure RSA)
        - Authenticated encryption (GCM mode)
        - Forward secrecy (random key per message)
        - Used for gossip protocol payloads

    Example:
        >>> plaintext = b"Large message content..."
        >>> encrypted = encrypt_with_public_key_hybrid(plaintext, bob_public_key)
        >>> # encrypted can be any size
    """
    import os
    import struct
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.asymmetric import padding

    # 1. Generate random AES-256 key (32 bytes)
    aes_key = os.urandom(32)

    # 2. Encrypt data with AES-GCM
    aesgcm = AESGCM(aes_key)
    nonce = os.urandom(12)  # 96-bit nonce for GCM
    ciphertext = aesgcm.encrypt(nonce, data, None)  # ciphertext includes authentication tag

    # 3. Encrypt AES key with RSA public key
    encrypted_key = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None
        )
    )

    # 4. Combine components: [key_len(2)][encrypted_key][nonce(12)][ciphertext+tag]
    key_len = len(encrypted_key)
    result = struct.pack('!H', key_len) + encrypted_key + nonce + ciphertext

    return result


def decrypt_with_private_key_hybrid(ciphertext: bytes, private_key) -> bytes:
    """
    Decrypt data using hybrid encryption (AES-GCM + RSA-OAEP).

    Reverses the process from encrypt_with_public_key_hybrid():
    1. Extract encrypted AES key
    2. Decrypt AES key with RSA private key
    3. Extract nonce and ciphertext
    4. Decrypt ciphertext with AES-GCM (also verifies authentication tag)

    Args:
        ciphertext: Encrypted blob from encrypt_with_public_key_hybrid()
        private_key: RSA private key from node.key file

    Returns:
        Decrypted plaintext bytes

    Raises:
        ValueError: If decryption fails (wrong key, corrupted data, authentication failure)

    Note:
        - Supports unlimited payload sizes
        - Verifies message authenticity (GCM tag)
        - Used for gossip protocol payloads
    """
    import struct
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.asymmetric import padding

    # 1. Extract encrypted AES key length
    if len(ciphertext) < 2:
        raise ValueError("Invalid ciphertext: too short")

    key_len = struct.unpack('!H', ciphertext[:2])[0]

    # 2. Extract encrypted AES key
    if len(ciphertext) < 2 + key_len + 12:
        raise ValueError("Invalid ciphertext: incomplete data")

    encrypted_key = ciphertext[2:2+key_len]

    # 3. Decrypt AES key with RSA private key
    aes_key = private_key.decrypt(
        encrypted_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None
        )
    )

    # 4. Extract nonce and encrypted data
    nonce_start = 2 + key_len
    nonce = ciphertext[nonce_start:nonce_start+12]
    encrypted_data = ciphertext[nonce_start+12:]

    # 5. Decrypt with AES-GCM (automatically verifies authentication tag)
    aesgcm = AESGCM(aes_key)
    try:
        plaintext = aesgcm.decrypt(nonce, encrypted_data, None)
    except Exception as e:
        raise ValueError(f"Decryption failed (authentication error or wrong key): {e}")

    return plaintext