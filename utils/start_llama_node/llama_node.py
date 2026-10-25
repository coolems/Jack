#!/usr/bin/env python3
"""COOLEMS llama-node launcher (2026-10 fleet) - ADMIN path.

Lives in utils/start_llama_node/ for admins who want to configure and start ANY
node they like (this machine, a remote GPU box, any port). Regular users never
touch this folder: ZZZ_SERVER.bat auto-starts the local node from the settings
init wrote into config/llama_node.json.

Runs on a GPU machine that is NOT the brain. Starts ONE llama-server with the same
settings the brain uses, verifies it, prints the brain entry, and exits leaving
llama-server running detached. The brain attaches to it and discovers slots/model.

Usage:
    python utils\\start_llama_node\\llama_node.py configure   # interactive wizard -> config/llama_node.json
    python utils\\start_llama_node\\llama_node.py start [--force]  # idempotent start (scheduled-task friendly)
    ... status / stop / restart / cmd

Double-click ZZZ_LLAMA_NODE.bat (this folder) for a menu.
"""

import argparse
import os
import sys

# Ensure the repo ROOT is importable when run from anywhere: this file lives in
# <repo>/utils/start_llama_node/, so the root is TWO levels up.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.providers.llama import node_launcher  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="llama_node.py",
        description="Start/manage one llama-server node for a COOLEMS fleet.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("configure", help="interactive wizard -> config/llama_node.json")
    s = sub.add_parser("start", help="start the node (idempotent)")
    s.add_argument("--force", action="store_true",
                   help="start even if the node was intentionally stopped")
    sub.add_parser("status", help="probe this node and print its settings")
    sub.add_parser("stop", help="stop this node's llama-server")
    sub.add_parser("restart", help="stop + start")
    sub.add_parser("cmd", help="print the raw llama-server command")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    return node_launcher.dispatch(args.command, force=getattr(args, "force", False))


if __name__ == "__main__":
    sys.exit(main())
