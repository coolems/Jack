"""Tests: the SERVER/CLIENT shared copies must stay byte-identical.

Four modules exist twice on purpose - SERVER and CLIENT each need their own importable
copy, so they cannot be merged into one file:
  app/provider_manager.py  <->  CLIENT/app/provider_manager.py
      the CLIENT imports it at boot, before any WebSocket exists, so it cannot be
      delivered over the wire.
  app/providers/coolems/ssl_pinning.py  <->  CLIENT/app/providers/coolems/ssl_pinning.py
      TLS trust is an exact SHA-256 fingerprint; both ends must apply the identical policy
      or one side accepts what the other rejects.
  tools/agent_prompt/get_agent_system_prompt.py  <->  CLIENT/app/prompts/get_agent_system_prompt.py
  tools/agent_prompt/get_time_awareness_instruction.py
      <->  CLIENT/app/prompts/get_time_awareness_instruction.py
      the prompt contract both sides build the conversation from.

Editing one copy without the other gives SERVER and CLIENT different behaviour on the
same wire. Same convention as test_dna_json_copies_identical.py.
PROTOCOL_VERSION parity is checked here as a tripwire only - the real enforcement is
runtime: ws_auth.py closes the socket on a mismatch and the CLIENT raises ConnectionError.
The version is read from module source with AST (no import), so this test has no config
side effects.
"""

import ast
import hashlib
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))

# (SERVER path, CLIENT path) - each pair must be byte-for-byte identical.
SHARED_COPY_PAIRS = [
    ("app/provider_manager.py", "CLIENT/app/provider_manager.py"),
    ("app/providers/coolems/ssl_pinning.py", "CLIENT/app/providers/coolems/ssl_pinning.py"),
    ("tools/agent_prompt/get_agent_system_prompt.py", "CLIENT/app/prompts/get_agent_system_prompt.py"),
    ("tools/agent_prompt/get_time_awareness_instruction.py", "CLIENT/app/prompts/get_time_awareness_instruction.py"),
]

CONFIG_FILES = ["config/config.py", "CLIENT/config/config.py"]


def _repo_path(rel):
    return os.path.join(_REPO_ROOT, rel)


def _read_bytes(rel):
    with open(_repo_path(rel), "rb") as fh:
        return fh.read()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def test_shared_copies_exist():
    for server_rel, client_rel in SHARED_COPY_PAIRS:
        for rel in (server_rel, client_rel):
            assert os.path.isfile(_repo_path(rel)), f"missing shared copy: {rel}"


def test_shared_copies_are_byte_identical():
    """Each SERVER/CLIENT pair must stay byte-for-byte the same (edit both together)."""
    drifted = []
    for server_rel, client_rel in SHARED_COPY_PAIRS:
        server = _read_bytes(server_rel)
        client = _read_bytes(client_rel)
        if server != client:
            drifted.append(
                f"  {server_rel}: {_sha(server)} ({len(server)} bytes)\n"
                f"  {client_rel}: {_sha(client)} ({len(client)} bytes)"
            )
    if drifted:
        raise AssertionError(
            "SERVER/CLIENT shared copies drifted (edit both files together):\n"
            + "\n".join(drifted)
        )


def _protocol_version(rel):
    """Read the PROTOCOL_VERSION literal from a config module without importing it."""
    tree = ast.parse(_read_bytes(rel).decode("utf-8"))
    for node in tree.body:
        target_ids = []
        if isinstance(node, ast.AnnAssign):
            target_ids = [getattr(node.target, "id", None)]
        elif isinstance(node, ast.Assign):
            target_ids = [getattr(t, "id", None) for t in node.targets]
        if "PROTOCOL_VERSION" in target_ids:
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, int):
                return value.value
            raise AssertionError(f"{rel}: PROTOCOL_VERSION is not an int literal")
    raise AssertionError(f"{rel}: PROTOCOL_VERSION not defined at module level")


def test_protocol_version_parity():
    """SERVER and CLIENT must agree on the WS protocol version (handshake enforces it)."""
    versions = {rel: _protocol_version(rel) for rel in CONFIG_FILES}
    unique = set(versions.values())
    if len(unique) != 1:
        detail = "\n".join(f"  {rel}: {versions[rel]}" for rel in CONFIG_FILES)
        raise AssertionError(
            "PROTOCOL_VERSION mismatch - bump both config files together:\n" + detail
        )


if __name__ == "__main__":
    for fn in (
        test_shared_copies_exist,
        test_shared_copies_are_byte_identical,
        test_protocol_version_parity,
    ):
        fn()
    print("shared SERVER/CLIENT copy checks passed")
