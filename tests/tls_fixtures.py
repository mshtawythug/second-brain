"""A synthetic HTTPS server on 127.0.0.1 for the ``--fetch`` certificate tests (RM-41, rev-114).

A throwaway CA signs ONE server certificate, for ``a.example`` only. The server
keeps connections alive (as a CDN does), answers by ``Host``, and records which
hosts it served. Nothing here touches the network beyond the loopback socket.
``cryptography`` is present in every install: ``pdfminer.six`` (through
``pdfplumber``, a base dependency) requires it.
"""
from __future__ import annotations

import socket
import ssl
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CERT_HOST = "a.example"


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def make_ca_and_server_cert(directory: Path, host: str = CERT_HOST) -> tuple[Path, Path, Path]:
    """``(ca.pem, server.pem, server.key)``: a test CA and a cert for ``host`` alone."""
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("brain test CA"))
        .issuer_name(_name("brain test CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(host))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .add_extension(x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = directory / "ca.pem"
    cert_pem = directory / "server.pem"
    key_pem = directory / "server.key"
    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_pem, cert_pem, key_pem


@dataclass
class KeepAliveServer:
    """HTTPS on 127.0.0.1 that answers by ``Host`` and keeps each connection open."""

    cert: Path
    key: Path
    routes: dict[str, tuple[int, dict[str, str], bytes]]
    served: list[str] = field(default_factory=list)
    connections: int = 0

    def __post_init__(self) -> None:
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(self.cert, self.key)
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(5)
        self._listener.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._connection, args=(raw,), daemon=True).start()

    def _connection(self, raw: socket.socket) -> None:
        raw.settimeout(5)
        try:
            conn = self._context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()  # the client refused our certificate
            return
        with conn:
            buffer = b""
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except (ssl.SSLError, OSError):
                    return
                if not chunk:
                    return
                buffer += chunk
                while b"\r\n\r\n" in buffer:
                    head, buffer = buffer.split(b"\r\n\r\n", 1)
                    self._answer(conn, head.decode("latin-1"))

    def _answer(self, conn: ssl.SSLSocket, head: str) -> None:
        lines = head.split("\r\n")
        path = lines[0].split(" ")[1]
        host = next(
            (line.split(":", 1)[1].strip() for line in lines[1:]
             if line.lower().startswith("host:")),
            "",
        ).split(":")[0]
        key = f"{host}{path}"
        self.served.append(key)
        status, headers, body = self.routes.get(key, (404, {}, b""))
        reason = {200: "OK", 302: "Found", 404: "Not Found"}[status]
        out = [f"HTTP/1.1 {status} {reason}", f"Content-Length: {len(body)}",
               "Connection: keep-alive"]
        out += [f"{name}: {value}" for name, value in headers.items()]
        conn.sendall(("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + body)
