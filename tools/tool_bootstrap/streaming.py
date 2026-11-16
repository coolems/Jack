"""Streaming subprocess runner with a per-line progress hook.
run_streaming is the drop-in upgrade of subprocess.run(..., capture_output=True) used by the
tool runtime setup steps: it calls on_line(line) for EVERY completed stdout line while the
process runs (pip redraws its bar with ' ', so one read can hold minutes of repaints
- _split_output_lines turns each repaint into an event). stderr is drained by a background
  thread; timeout>0 kills the child and raises subprocess.TimeoutExpired exactly like
  subprocess.run does.
"""

import codecs
import logging
import re
import subprocess
import threading

#: A child that writes this many bytes with no '\n'/'\\r' at all is pathological (binary
#: output): emit the buffer as one line instead of growing memory without bound.
_MAX_LINE_CARRY = 1024 * 1024


logger = logging.getLogger("COOLEMS.Tools.Bootstrap")

def _split_output_lines(raw: str):
    """Split pip/venv output into real lines.

    pip redraws its download progress bar in place with carriage returns, so no newline
    arrives per update; each \r segment is its own line.
    """
    return [seg for seg in re.split(r"[\r\n]+", raw) if seg.strip()]


def run_streaming(cmd: list, timeout: int = 0, env: dict = None, input_text: str = None,
                  on_line=None):
    """Run *cmd* streaming stdout line-by-line (live progress hook)."""
    # NOTE: text=False on purpose - see the REALTIME FIX in the module docstring. The
    # child's bytes are decoded here, chunk by chunk, with a carry for split UTF-8.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=(subprocess.PIPE if input_text is not None else subprocess.DEVNULL),
                            env=env)

    # Drain stderr concurrently (bounded list - setup children are chatty but finite).
    stderr_chunks: list = []

    def _drain_stderr():
        try:
            for chunk in iter(proc.stderr.readline, b""):
                stderr_chunks.append(chunk)
        except Exception:
            pass

    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    if input_text is not None and proc.stdin:
        try:
            proc.stdin.write(input_text.encode("utf-8"))
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
        # Raw byte reads: read1() performs a SINGLE raw read and returns as soon as any
        # data reaches the OS pipe - no text buffer, no "wait for n bytes" stall. The
        # incremental decoder buffers a UTF-8 sequence split across two chunks; the
        # carry buffer holds an unfinished line so callers only ever see COMPLETE events
        # ('\r' splits still turn every pip repaint into its own event).
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        carry = ""

        def _dispatch(text: str) -> None:
            for seg in _split_output_lines(text):
                stdout_lines.append(seg)
                if on_line is not None:
                    try:
                        on_line(seg)
                    except Exception as e:  # progress hook must never break the install
                        logger.debug(f"[bootstrap] on_line hook failed (ignored): {e}")

        while True:
            chunk = proc.stdout.read1(65536)
            if not chunk:
                break  # EOF - child closed stdout (normal exit or killed by the timer)
            carry += decoder.decode(chunk, final=False)
            cut = max(carry.rfind("\n"), carry.rfind("\r"))
            if cut < 0:
                if len(carry) >= _MAX_LINE_CARRY:  # no separator at all - flush as one line
                    _dispatch(carry)
                    carry = ""
                continue
            _dispatch(carry[:cut + 1])
            carry = carry[cut + 1:]
        tail = carry + decoder.decode(b"", final=True)  # flush the decoder + last line at EOF
        if tail:
            _dispatch(tail)
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
    err_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    return rc, stdout_lines, err_text
