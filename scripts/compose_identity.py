"""Ephemeral HTTPS/JWKS fixture issuer for isolated Compose checks, never users.

No verification code is overridden. The normal API validates TLS, ES256, issuer,
audience/expiry and C02 database memberships against this explicitly test issuer.
"""

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI

PRINCIPAL = "4c6a8532-956e-4aa9-a21c-de9cf9ca4c11"
PROVIDER = "97c553b2-b1ca-4b68-893a-1de87ca13c12"
ISSUER = "https://slice-identity:8443/auth/v1"


def prepare(folder):
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "slice-identity")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("slice-identity")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    (folder / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (folder / "tls-key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    signing_key = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(signing_key.public_key()))
    jwk.update(kid="compose-ephemeral-key", use="sig", alg="ES256")
    (folder / "jwks.json").write_text(json.dumps({"keys": [jwk]}))
    token = jwt.encode(
        {
            "sub": PRINCIPAL,
            "role": "authenticated",
            "iat": now,
            "exp": now + timedelta(minutes=45),
            "iss": ISSUER,
            "aud": "authenticated",
        },
        signing_key,
        algorithm="ES256",
        headers={"kid": jwk["kid"]},
    )
    (folder / "token").write_text(token)
    # Disposable test material must be readable by the non-root backend image.
    # No existing key/token is copied here; the signing private key is not saved.
    for path in folder.iterdir():
        path.chmod(0o644)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "serve"))
    parser.add_argument("--folder", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("ULOP_TEST_IDENTITY") != "1":
        raise ValueError("Ephemeral issuer is only enabled for the Compose test")
    if args.operation == "prepare":
        prepare(args.folder)
        return
    app = FastAPI()

    @app.get("/auth/v1/.well-known/jwks.json")
    def keys():
        return json.loads((args.folder / "jwks.json").read_text())

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8443,
        access_log=False,
        ssl_keyfile=str(args.folder / "tls-key.pem"),
        ssl_certfile=str(args.folder / "cert.pem"),
    )


if __name__ == "__main__":
    main()
