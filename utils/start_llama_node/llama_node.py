#!/usr/bin/env python3
"""COOLEMS llama fleet launcher (2026-11 multi-config fleet) - ADMIN path.

Lives in utils/start_llama_node/ for admins who want to see, configure and start the
llama-services on this machine. Regular users never touch this folder: ZZZ_SERVER.bat
starts the LOCAL entry of config/llama_servers.json automatically (after checking that
the model actually fits in the free RAM/VRAM).

The single source of truth is config/llama_servers.json: a JSON LIST of complete configs,
one per llama-service (name, host, port, model, slots, ctx/slot, threads, gpu layers,
bind, extra_args, auto_start). Three entries on three ports = three llama-services with
three different models on one machine, as long as the memory is there.

Usage (name = the entry's name, or its number from 'list', or host:port):
    python utils\\start_llama_node\\llama_node.py list
    python utils\\start_llama_node\\llama_node.py configure [name|new]
    python utils\\start_llama_node\\llama_node.py start <name|all>
    python utils\\start_llama_node\\llama_node.py stop <name>
    python utils\\start_llama_node\\llama_node.py status [name]
    python utils\\start_llama_node\\llama_node.py restart <name>
    python utils\\start_llama_node\\llama_node.py cmd <name>
    python utils\\start_llama_node\\llama_node.py delete <name>

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
        description="List, configure and start the llama-services in config/llama_servers.json.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="every config + what is actually running on its port")

    c = sub.add_parser("configure", help="wizard: edit one entry or add a new one")
    c.add_argument("name", nargs="?", default="",
                   help="entry name (empty/'new'/'add' = create a new config)")

    # Every name is optional: the .bat menu may pass an empty string (user just pressed
    # Enter), and dispatch() then reports the available names instead of an argparse usage
    # error. An empty name is never treated as "all".
    s = sub.add_parser("start", help="start one config (idempotent) or 'all'")
    s.add_argument("name", nargs="?", default="",
                   help="config name / number / host:port, or 'all'")

    st = sub.add_parser("stop", help="stop one config's llama-server")
    st.add_argument("name", nargs="?", default="")

    stat = sub.add_parser("status", help="probe /health + /props (all configs when no name)")
    stat.add_argument("name", nargs="?", default="")

    r = sub.add_parser("restart", help="stop + start one config")
    r.add_argument("name", nargs="?", default="")

    cm = sub.add_parser("cmd", help="print the raw llama-server command for one config")
    cm.add_argument("name", nargs="?", default="")

    d = sub.add_parser("delete", help="remove one entry from config/llama_servers.json")
    d.add_argument("name", nargs="?", default="")

    sub.add_parser("start-all", help="start every local entry with auto_start=true (memory-gated)")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    return node_launcher.dispatch(args.command,
                                  getattr(args, "name", "") or "",
                                  force=getattr(args, "force", False))


if __name__ == "__main__":
    sys.exit(main())
