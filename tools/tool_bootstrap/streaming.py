"""Streaming subprocess runner with a per-line progress hook.

run_streaming() is the drop-in upgrade of subprocess.run(..., capture_output=True) used by
the tool runtime setup steps: it calls on_line(line) for EVERY completed stdout line while
the process runs (pip redraws its bar with '\r', so one readline can hold minutes of
repaints - _split_output_lines turns each repaint into an event). stderr is drained by a
background thread; timeout>0 kills the child and raises subprocess.TimeoutExpired exactly
like subprocess.run does.
"""

import logging
import re
import subprocess
import threading


logger = logging.getLogger("COOLEMS.Tools.Bootstrap")

def _split_output_lines(raw: str):
    """Split a child's raw stdout into logical lines on BOTH '\n' and '\r'.

    pip redraws its download progress bar in place with carriage returns - no newline
    arrives until the wheel finishes (minutes for torch). Splitting on '\r' too turns
    every bar repaint into an event, so the UI gets live percent/speed/ETA instead of a
    silent 'downloading' line that only ends at EOF.
    """
    return [seg for seg in re.split(r"[\r\n]+", raw) if seg.strip()]


def run_streaming(cmd: list, timeout: int = 0, env: dict = None, input_text: str = None,
                  on_line=None):
    """Run *cmd* streaming stdout line-by-line (live progress hook).

    Drop-in upgrade of subprocess.run(..., capture_output=True) for the setup steps:
      * on_line(line) is called for EVERY completed stdout line while the process runs
        (the callback may never break setup - exceptions are swallowed and logged);
      * returns (returncode, [stdout lines], stderr_text);
      * timeout>0 kills the process after *timeout* seconds and raises
        subprocess.TimeoutExpired exactly like subprocess.run does;
      * input_text is written to stdin when given.

    stderr is drained by a background thread so a chatty child can never deadlock on
    its pipe buffer while we are busy reading stdout lines.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=(subprocess.PIPE if input_text is not None else subprocess.DEVNULL),
                            text=True, env=env)

    # Drain stderr concurrently (bounded list - setup children are chatty but finite).
    stderr_chunks: list = []

    def _drain_stderr():
        try:
            for chunk in iter(proc.stderr.readline, ""):
                stderr_chunks.append(chunk)
        except Exception:
            pass

    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    if input_text is not None and proc.stdin:
        try:
            proc.stdin.write(input_text)
            proc.stdin.close()
        except Exception:
            pass

    stdout_lines: list = []
    timed_out = [False]  # set by the kill timer when the budget is exceeded
    timer = None
    if timeout and timeout > 0:
        def _kill():
            timed_out[0] = True
            try:
                proc.kill()
            except Exception:
                pass
        timer = threading.Timer(timeout, _kill)
        timer.daemon = True
        timer.start()

    try:
        while True:
            raw = proc.stdout.readline()
            if not raw:
                break  # EOF - child closed stdout (normal exit or killed by the timer)
            # pip redraws its progress bar with '\r' (no newline until the wheel is done),
            # so one readline() can hold MINUTES of repaints - split them into events.
            for seg in _split_output_lines(raw):
                stdout_lines.append(seg)
                if on_line is not None:
                    try:
                        on_line(seg)
                    except Exception as e:  # progress hook must never break the install
                        logger.debug(f"[bootstrap] on_line hook failed (ignored): {e}")
    finally:
        if timer is not None:
            timer.cancel()

    try:
        rc = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    err_thread.join(timeout=10)

    # Close the pipe ends explicitly - the child has exited (or been killed), so no more
    # data can arrive; leaving them open leaks file descriptors (ResourceWarning).
    for _stream in (proc.stdout, proc.stderr):
        try:
            if _stream is not None:
                _stream.close()
        except Exception:
            pass

    # A child that went SILENT past the budget is killed by the timer and then exits on
    # EOF above - surface it as TimeoutExpired (same contract as subprocess.run), not as
    # a plain non-zero return code.
    if timed_out[0]:
        raise subprocess.TimeoutExpired(cmd, timeout)
    return rc, stdout_lines, "".join(stderr_chunks)
