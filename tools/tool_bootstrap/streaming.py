"""Streaming subprocess runner with a per-line progress hook.

run_streaming() is the drop-in upgrade of subprocess.run(..., capture_output=True) used by
the tool runtime setup steps: it calls on_line(line) for EVERY completed stdout line while
the process runs (pip redraws its bar with '\r', so one read can hold minutes of repaints
- _split_output_lines turns each repaint into an event). stderr is drained by a background
thread; timeout>0 kills the child and raises subprocess.TimeoutExpired exactly like
subprocess.run does.

REALTIME FIX (2026-10-23, corrected 2026-10-23): stdout is read as RAW BYTES with
read1(), never through Python's text-mode wrapper and never with a size-hinting read().
  * Popen(text=True) wraps stdout in a TextIOWrapper whose readline() only returns when
    its INTERNAL buffer (~8 KB) fills or the pipe hits EOF - a child flushing one small
    JSON line every 0.5 s therefore delivered nothing for ~20 s at a time.
  * Drop text=True and proc.stdout becomes a BufferedReader, which has NO .buffer
    attribute (only TextIOWrapper does) - touching it raises AttributeError and kills
    every setup call outright.
  * BufferedReader.read(n) is NOT a "give me what is available" call either: it loops
    until n bytes or EOF, so read(65536) stalls until 64 KB accumulate - strictly worse
    than the 8 KB text buffer it was meant to replace. read1(n) performs at most ONE
    raw read and returns the moment any bytes reach the OS pipe (measured: every 0.5 s
    flush delivered within ~50 ms; read(65536) delivered 8 lines only at EOF, 2.08 s).
Chunks are decoded with an incremental UTF-8 decoder and reassembled into COMPLETE
lines by a carry buffer, so a JSON event or a pip repaint split across two reads is
never emitted half-finished. on_line() therefore fires within milliseconds of every
child flush and .setup_progress.json stays live.
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
        - and in REALTIME: read1() returns as soon as any bytes reach the OS pipe, so
        small flushed lines are delivered immediately (see the REALTIME FIX above);
        (the callback may never break setup - exceptions are swallowed and logged);
      * returns (returncode, [stdout lines], stderr_text);
      * timeout>0 kills the process after *timeout* seconds and raises
        subprocess.TimeoutExpired exactly like subprocess.run does;
      * input_text is written to stdin when given.

    stderr is drained by a background thread so a chatty child can never deadlock on
    its pipe buffer while we are busy reading stdout lines.
    """
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
