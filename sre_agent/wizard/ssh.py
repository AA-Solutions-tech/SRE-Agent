"""SSH helpers for the wizard: create the agent's key and trust the log server's host key (only after the user
confirms its fingerprint). When the wizard runs as root, created files are handed to the service user."""
import base64
import hashlib
import os
import shutil
import socket
import subprocess
from pathlib import Path

SERVICE_USER = "sre-agent"


def _give_to_service_user(*paths: Path) -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    try:
        import pwd
        entry = pwd.getpwnam(SERVICE_USER)
    except (ImportError, KeyError):
        return
    for p in paths:
        if p.exists():
            os.chown(p, entry.pw_uid, entry.pw_gid)


def generate_key(key_path: Path) -> str:
    """Create an ed25519 key pair (no passphrase) unless it exists. Returns the public key line."""
    pub = key_path.with_name(key_path.name + ".pub")
    if not key_path.exists():
        if not shutil.which("ssh-keygen"):
            raise RuntimeError("ssh-keygen not found (install openssh-client / openssh-clients)")
        key_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(key_path.parent, 0o700)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"sre-agent@{socket.gethostname()}",
                        "-f", str(key_path)], check=True, capture_output=True)
        _give_to_service_user(key_path.parent, key_path, pub)
    return pub.read_text(encoding="utf-8").strip()


def fetch_host_key(host: str, port: int, timeout: float = 10.0) -> tuple[str, str, str]:
    """(key type, base64 key, SHA256 fingerprint) offered by the server. Does not authenticate."""
    import paramiko

    sock = socket.create_connection((host, port), timeout=timeout)
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        blob = key.asbytes()
    finally:
        transport.close()
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return key.get_name(), base64.b64encode(blob).decode(), fingerprint


def known_hosts_line(host: str, port: int, key_type: str, key_b64: str) -> str:
    name = host if port == 22 else f"[{host}]:{port}"
    return f"{name} {key_type} {key_b64}"


def add_known_host(known_hosts: Path, line: str) -> bool:
    """Append the line unless already present. Returns True when the file changed."""
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    existing = known_hosts.read_text(encoding="utf-8").splitlines() if known_hosts.exists() else []
    if line in existing:
        return False
    with known_hosts.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    os.chmod(known_hosts, 0o644)
    _give_to_service_user(known_hosts)
    return True
