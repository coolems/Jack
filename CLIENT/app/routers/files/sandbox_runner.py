"""Sandboxed execution seam for the HTTP exec endpoints (SECURITY fix 2026-11-14).

WHAT THIS FIXES
===============
/api/run-python and /api/run-file ran a file with a bare
``subprocess.run([python_exe, safe_path])`` -- NO runtime sandbox, NO containment.
Anyone able to reach the CLIENT's HTTP port could upload a .py (/api/upload keeps the
basename) and execute it with FULL filesystem and network access outside working_root;
the only gate was the SERVER-delivered ``is_subprocess_allowed()`` limitation. That made
the HTTP surface strictly weaker than the agent channel, where python_exec prepends its
runtime sandbox to every payload.

THE SEAM
========
Both endpoints now execute through the SAME machinery the delivered python_exec tool uses.
The CLIENT ships no tools/exec_tools locally (tools arrive from the SERVER), so the
pieces are taken from the compiled python_exec module's globals -- the very objects the
tool itself uses, so containment is byte-identical rather than a second weaker copy:

  * ``SANDBOX_BOOTSTRAP``  -- runtime containment prefix (every open / enumeration /
    raw-Win32 / spawn / socket decision checked against working_root at the moment it
    happens).
  * ``_CHILD_PREAMBLE`` / ``_CHILD_ENTRY`` / ``_wrap_user_code`` -- the same payload
    assembly and stdin delivery the tool uses (byte-exact, preserves user line numbers).
  * ``_run_sandboxed_subprocess`` -- foreground runs (captured stdout/stderr, timeout,
    process-group kill on POSIX).
  * A detached variant for the GUI launch branch, which needs a real console window:
    same payload + same env pins, spawned with CREATE_NEW_CONSOLE instead of pipes.

FAIL-CLOSED
===========
No compiled python_exec, no bootstrap, no payload assembly -> execution is REFUSED
(403). The endpoints NEVER fall back to an unsandboxed spawn. The UI consent relay is
not wired here (HTTP has no chat channel), so the child gets JACK_PYEXEC_NET_CONSENT=0:
an out-of-sandbox operation is hard-blocked, exactly as before the consent gate existed.
Admin open-network (JACK_PYEXEC_NETWORK=1) is forwarded by the env pins, unchanged.
"""

import asyncio
import os
import subprocess
import sys

from .middleware import logger


class SandboxUnavailable(Exception):
    """The sandboxed runner is not available -- the caller must refuse execution."""


async def _sandbox_pieces():
    """Resolve the sandbox pieces from the compiled python_exec tool.

    Raises SandboxUnavailable when the tool or any piece cannot be resolved.
    """
    from app.keys import _get_tool_orchestrator

    orch = _get_tool_orchestrator()
    if orch is None:
        raise SandboxUnavailable(
            "the SERVER has not delivered this CLIENT's tool configuration yet")

    tools = getattr(orch, "tools", None)
    func = tools.get("python_exec") if isinstance(tools, dict) else None
    if func is None:
        # Not compiled yet -- fetch it from the SERVER, like every other tool load.
        try:
            await orch.ensure_tool_loaded("python_exec")
        except Exception as e:
            raise SandboxUnavailable("the python_exec tool could not be loaded (%s)" % (e,))
        tools = getattr(orch, "tools", None)
        func = tools.get("python_exec") if isinstance(tools, dict) else None
    if func is None:
        raise SandboxUnavailable("the python_exec tool is not available in this CLIENT build")

    gl = getattr(func, "__globals__", None) or {}
    missing = [k for k in ("SANDBOX_BOOTSTRAP", "_CHILD_PREAMBLE", "_CHILD_ENTRY",
                           "_wrap_user_code", "_run_sandboxed_subprocess")
               if k not in gl]
    if missing:
        raise SandboxUnavailable(
            "the python_exec runtime sandbox pieces %s could not be resolved" % (missing,))
    if not callable(gl["_run_sandboxed_subprocess"]) or not callable(gl["_wrap_user_code"]):
        raise SandboxUnavailable("the python_exec runtime sandbox is not callable")
    return gl


def _child_env(working_root_dir):
    """Env pins identical to python_exec's child: root lock + temp dir inside root.

    Any inherited JACK_PYEXEC_* state is dropped first -- never trust leftover state.
    """
    env = os.environ.copy()
    for k in list(env.keys()):
        if k.startswith("JACK_PYEXEC_"):
            del env[k]
    env["JACK_PYEXEC_ROOT"] = working_root_dir
    tmp = os.path.join(working_root_dir, ".temp")
    try:
        os.makedirs(tmp, exist_ok=True)
    except Exception:
        pass  # the bootstrap still enforces the root; tempfile falls back to the OS default
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = tmp
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # No UI relay on the HTTP surface -> the child hard-blocks out-of-sandbox operations.
    env["JACK_PYEXEC_NET_CONSENT"] = "0"
    env["JACK_PYEXEC_NETWORK"] = os.environ.get("JACK_PYEXEC_NETWORK", "0")
    env["JACK_PYEXEC_DEBUG"] = os.environ.get("JACK_PYEXEC_DEBUG", "0")
    return env


def _payload(gl, source):
    """Bootstrap prefix + the user's source, assembled exactly as python_exec does."""
    return (gl["_CHILD_PREAMBLE"] + gl["SANDBOX_BOOTSTRAP"] + "\n"
            + gl["_wrap_user_code"](source))


def _read_source(safe_path):
    with open(safe_path, "rb") as fh:
        return fh.read().decode("utf-8", errors="replace")


def _unavailable_detail(exc: SandboxUnavailable) -> str:
    return ("Execution blocked: %s. These endpoints run ONLY inside the python_exec "
            "runtime sandbox (working_root containment) -- an unsandboxed spawn is not "
            "allowed. Start the COOLEMS SERVER and let the CLIENT complete its bootstrap, "
            "then retry." % str(exc))


async def run_sandboxed_file(safe_path, working_root_dir, timeout_sec=30):
    """Run a .py file inside the python_exec runtime sandbox, capturing its output.

    Returns {"stdout", "stderr", "returncode"}. Raises SandboxUnavailable (caller maps
    it to 403) or subprocess.TimeoutExpired (caller maps it to a timeout result).
    """
    gl = await _sandbox_pieces()
    payload = _payload(gl, _read_source(safe_path))
    runner = gl["_run_sandboxed_subprocess"]

    def _run():
        # The runner does the env pins, stdin delivery, draining and timeout kill itself.
        return runner(payload, working_root_dir, timeout_sec)

    proc = await asyncio.to_thread(_run)
    return {"stdout": proc.stdout or "", "stderr": proc.stderr or "",
            "returncode": proc.returncode}


async def run_sandboxed_detached(safe_path, working_root_dir):
    """Run a .py file inside the sandbox in a NEW console window (GUI launch branch).

    Same bootstrap prefix and env pins as the captured run; only the delivery differs --
    pygame/tkinter apps need a real terminal window, so the child is spawned with
    CREATE_NEW_CONSOLE (POSIX: start_new_session) instead of captured pipes. Returns
    {"status": "launched"}.
    """
    gl = await _sandbox_pieces()
    payload = _payload(gl, _read_source(safe_path)).encode("utf-8")
    env = _child_env(working_root_dir)
    cmd = [sys.executable or "python", "-c", gl["_CHILD_ENTRY"]]

    def _spawn():
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, cwd=working_root_dir, env=env, stdin=subprocess.PIPE,
                                **kwargs)
        # Feed the payload on a worker so a GUI app that never reads stdin cannot wedge
        # the HTTP endpoint; the child closes fd 0 itself (see _CHILD_ENTRY).
        try:
            proc.stdin.write(payload)
            proc.stdin.close()
        except Exception:
            pass
        return proc.pid

    pid = await asyncio.to_thread(_spawn)
    logger.info("Sandboxed GUI launch: pid=%s file=%s" % (pid, os.path.basename(safe_path)))
    return {"status": "launched", "pid": pid}
