"""signing keys whose private part anyone can read: production refuses to sign or verify with them"""

import hashlib

from cryptography.hazmat.primitives import serialization

# sha256 of the public key's DER SubjectPublicKeyInfo, so the list itself holds nothing secret
PUBLISHED = frozenset({
    # ops/identity/jwt_dev_private_key.pem: committed in 72ee538, deleted in a223033, still in the pushed history
    "5e0319c3994784084c1fe6b7ac412e13379a3aefa3e26e224aafeeb6dcb27ed0",
})


def fingerprint(public_key) -> str:
    der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


def is_published(public_key) -> bool:
    return fingerprint(public_key) in PUBLISHED
