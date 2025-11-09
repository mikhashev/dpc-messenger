"""
Cryptographic validation for node identities.

This module handles validation of certificates, public keys, and node IDs
to ensure cryptographic identity integrity.
"""

import hashlib
import logging
from typing import Dict

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa

logger = logging.getLogger(__name__)


class CryptoValidationError(Exception):
    """Raised when cryptographic validation fails"""
    pass


def generate_node_id_from_public_key(public_key_pem: str) -> str:
    """
    Generate node_id from public key PEM string.
    This must match the client's generation logic exactly.
    
    Args:
        public_key_pem: PEM-formatted RSA public key
    
    Returns:
        node_id in format "dpc-node-{16 hex chars}"
    
    Raises:
        CryptoValidationError: If public key is invalid
    """
    try:
        public_key_bytes = public_key_pem.encode('utf-8')
        sha256_hash = hashlib.sha256(public_key_bytes).hexdigest()
        return f"dpc-node-{sha256_hash[:16]}"
    except Exception as e:
        raise CryptoValidationError(f"Failed to generate node_id: {str(e)}")


def validate_certificate(cert_pem: str) -> Dict[str, any]:
    """
    Validate a certificate and extract information.
    
    Checks:
    1. Certificate is valid PEM format
    2. Can extract public key
    3. Common Name (CN) is present
    4. CN matches expected node_id format
    
    Args:
        cert_pem: PEM-formatted X.509 certificate
    
    Returns:
        dict with:
            - public_key_pem: str (PEM format)
            - node_id: str (computed from public key)
            - common_name: str (from certificate CN)
            - valid: bool (always True if no exception)
    
    Raises:
        CryptoValidationError: If validation fails
    """
    try:
        # Parse certificate
        cert = x509.load_pem_x509_certificate(
            cert_pem.encode('utf-8'), 
            default_backend()
        )
        
        # Extract public key
        public_key = cert.public_key()
        
        # Verify it's an RSA key
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise CryptoValidationError("Certificate must contain RSA public key")
        
        public_key_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode('utf-8')
        
        # Generate node_id from public key
        node_id = generate_node_id_from_public_key(public_key_pem)
        
        # Extract Common Name from certificate
        common_name = None
        for attribute in cert.subject:
            if attribute.oid == x509.oid.NameOID.COMMON_NAME:
                common_name = attribute.value
                break
        
        if common_name is None:
            raise CryptoValidationError("Certificate must have a Common Name (CN)")
        
        # Validate that CN matches node_id format
        if not common_name.startswith("dpc-node-"):
            raise CryptoValidationError(
                f"Certificate CN must start with 'dpc-node-', got '{common_name}'"
            )
        
        # Validate that CN matches computed node_id
        if common_name != node_id:
            raise CryptoValidationError(
                f"Certificate CN '{common_name}' does not match "
                f"computed node_id '{node_id}'"
            )
        
        logger.info(f"Certificate validation successful for node_id: {node_id}")
        
        return {
            'public_key_pem': public_key_pem,
            'node_id': node_id,
            'common_name': common_name,
            'valid': True
        }
        
    except CryptoValidationError:
        raise
    except Exception as e:
        raise CryptoValidationError(f"Certificate validation failed: {str(e)}")


def validate_node_registration(
    node_id: str, 
    public_key_pem: str, 
    cert_pem: str
) -> bool:
    """
    Validate that a node registration is legitimate.
    
    Validation steps:
    1. Validate certificate format and structure
    2. Extract public key from certificate
    3. Generate node_id from public key
    4. Verify provided node_id matches computed node_id
    5. Verify certificate CN matches node_id
    6. Verify provided public key matches certificate's public key
    
    Args:
        node_id: Claimed node_id (format: dpc-node-{16 hex chars})
        public_key_pem: PEM-formatted RSA public key
        cert_pem: PEM-formatted X.509 certificate
    
    Returns:
        True if all validations pass
    
    Raises:
        CryptoValidationError: If any validation fails
    """
    # Validate node_id format
    if not node_id or not node_id.startswith("dpc-node-"):
        raise CryptoValidationError(
            f"Invalid node_id format: '{node_id}'. "
            f"Must start with 'dpc-node-'"
        )
    
    if len(node_id) != 25:  # "dpc-node-" (9) + 16 hex chars
        raise CryptoValidationError(
            f"Invalid node_id length: '{node_id}'. "
            f"Must be 25 characters (dpc-node- + 16 hex chars)"
        )
    
    # Validate certificate and extract info
    cert_info = validate_certificate(cert_pem)
    
    # Verify provided node_id matches computed one
    if node_id != cert_info['node_id']:
        raise CryptoValidationError(
            f"Provided node_id '{node_id}' does not match "
            f"computed node_id '{cert_info['node_id']}'"
        )
    
    # Verify provided public key matches certificate's public key
    provided_key_clean = public_key_pem.strip()
    cert_key_clean = cert_info['public_key_pem'].strip()
    
    if provided_key_clean != cert_key_clean:
        raise CryptoValidationError(
            "Provided public key does not match certificate's public key"
        )
    
    logger.info(f"Node registration validation successful for {node_id}")
    return True


def validate_node_id_format(node_id: str) -> bool:
    """
    Quick validation of node_id format without cryptographic checks.
    
    Args:
        node_id: Node ID to validate
    
    Returns:
        True if format is valid
    
    Raises:
        ValueError: If format is invalid
    """
    if not node_id:
        raise ValueError("node_id cannot be empty")
    
    if not node_id.startswith("dpc-node-"):
        raise ValueError(f"node_id must start with 'dpc-node-', got: {node_id}")
    
    if len(node_id) < 25:
        raise ValueError(
            f"node_id too short: {len(node_id)} chars. "
            f"Expected at least 25 (dpc-node- + 16 hex chars)"
        )
    
    # Allow temporary node_ids for migration
    if node_id.startswith("dpc-node-temp-"):
        return True
    
    # For normal node_ids, validate hex chars
    hex_part = node_id[9:]  # Skip "dpc-node-"
    if not all(c in '0123456789abcdef' for c in hex_part[:16]):
        raise ValueError(f"node_id must contain hex characters after 'dpc-node-'")
    
    return True