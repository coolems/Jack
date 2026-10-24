#!/usr/bin/env python3
"""COOLEMS llama-node launcher (2026-10 fleet).

Runs on a GPU machine that is NOT the brain. Starts ONE llama-server with the same
settings the brain uses, verifies it, prints the brain entry, and exits leaving
llama-server running detached. The brain attaches to it and discovers slots/model.

Usage:
    python llama_node.py configure            # interactive wizard -> config/llama_node.json
    python llama_node.py start [--force]      # idempotent start (scheduled-task friendly)
    python llama_node.py status
    python llama_node.py stop
    python llama_node.py restart
    python llama_node.py cmd                  # print the raw llama-server command

Double-click ZZZ_LLAMA_NODE.bat for a menu.
"""

import argparse
import os
import sys

# Ensure the repo root is importable when run from anywhere.
_ROOT = os.path.dirname(os.path.abspath(__file__))
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
    sub.add_parser("cmd", help="print the llama-server command (for a plain .bat)")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    return node_launcher.dispatch(args.command, force=getattr(args, "force", False))


if __name__ == "__main__":
    sys.exit(main())
