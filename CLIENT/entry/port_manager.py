"""Port management utilities for COOLEMS CLIENT.

Handles detecting and killing processes blocking the target port.
Uses hybrid approach: socket binding check + netstat PID detection + taskkill.
"""

import subprocess
import time
import socket
import logging

logger = logging.getLogger(__name__)


def ensure_port_free(port, max_retries=3):
    """Kill any process blocking the target port before starting uvicorn.

    Uses hybrid approach for maximum reliability:
    1. Socket binding check (no false positives from netstat parsing)
    2. netstat to find PIDs if blocked
    3. taskkill /F to force kill processes
    4. Retry with socket verification after each kill attempt

    Args:
        port: Port number to free up
        max_retries: Number of retry attempts (default 3)

    Returns:
        bool: True if port is free, False if still blocked after retries
    """

    def is_port_in_use():
        """Check if port is in use via socket binding (most reliable method)."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('0.0.0.0', port))
            s.close()
            return False
        except OSError:
            return True

    def get_pids_on_port():
        """Get PIDs listening on the exact port using netstat.

        The port is matched as a FIELD, never as a substring. The old filter
        `f":{port}" in line` (2026-11-06 code-review finding #4) matched
        ":8080" inside "0.0.0.0:80800", so every listener whose port merely
        CONTAINED the target became a taskkill /F target - with CLIENT_UI_PORT
        set to 80 it force-killed unrelated processes (including the CLIENT on
        8000). netstat -ano line shape: TCP <local> <foreign> <state> <pid>.
        """
        try:
            netstat_result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=5
            )
        except Exception as e:
            logger.warning(f"[PORT] netstat failed: {e}")
            return []

        target = str(port)
        pids = []
        for line in netstat_result.stdout.splitlines():
            parts = line.split()
            # Length check FIRST - blank/short lines have no parts[0].
            # TCP = IPv4 listener, TCP6 = IPv6 listener (netstat -ano on Windows).
            if len(parts) < 5 or parts[0] not in ("TCP", "TCP6"):
                continue
            # Port = digits after the LAST ':' of the local address, so the
            # bracketed IPv6 form Windows prints ("[::]:135" -> "135") is covered
            # and a prefix port ("80" vs "80800") can never match.
            if parts[1].rpartition(":")[2] != target:
                continue
            if parts[3] != "LISTENING":
                continue
            try:
                pids.append(int(parts[-1]))
            except ValueError:
                continue
        return pids

    for attempt in range(max_retries):
        if not is_port_in_use():
            logger.info(f"[PORT] Port {port} is free")
            return True

        pids = get_pids_on_port()
        if not pids:
            # Port blocked but no LISTENING process (TIME_WAIT, etc.)
            logger.warning(
                f"[PORT] Port {port} blocked with no LISTENING "
                f"process (TIME_WAIT?), retrying..."
            )
            time.sleep(1)
            continue

        for pid in set(pids):  # Unique PIDs only
            if not isinstance(pid, int) or pid < 0:
                logger.warning(f"[PORT] Invalid PID {pid}, skipping")
                continue
            kill_result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True, text=True, timeout=5
            )
            if kill_result.returncode == 0:
                logger.info(f"[PORT] Killed PID={pid} blocking port {port}")
            else:
                stderr = (
                    kill_result.stderr.strip()
                    if kill_result.stderr else "permission denied"
                )
                logger.warning(f"[PORT] Failed to kill PID={pid}: {stderr}")

        time.sleep(1.5)  # Wait for OS to release the port

    # Final verification after all retries exhausted
    if not is_port_in_use():
        logger.info(f"[PORT] Port {port} is now free after retries")
        return True

    logger.error(f"[PORT] Port {port} still blocked after {max_retries} attempts")
    return False
