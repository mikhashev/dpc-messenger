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