"""Live-child regression check for tools/tool_bootstrap/streaming.py (2026-10-23).

WHY THIS EXISTS
===============
The 2026-10-23 "realtime progress" fix was verified against a FAKE pipe only, so two
fatal defects shipped unnoticed:
  1. Popen without text=True gives a BufferedReader, which has no `.buffer` attribute -
     `proc.stdout.buffer` raised AttributeError on EVERY setup call (venv/pip/model
     pre-flight), not just on slow downloads.
  2. Even with the right object, BufferedReader.read(65536) loops until 64 KB or EOF,
     so small flushed progress lines were held back LONGER than the original 8 KB text
     buffer. read1() is the call that returns as soon as bytes reach the OS pipe.

This check spawns a REAL child process (like the production callers do) and asserts
the actual delivery timing + line integrity, so neither defect can come back silently.

Run: python tools/test_tools/check_streaming_realtime.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_streaming():
    path = os.path.join(ROOT, "tools", "tool_bootstrap", "streaming.py")
    spec = importlib.util.spec_from_file_location("jack_streaming_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    st = _load_streaming()
    failures = []

    def check(label, cond, detail=""):
        print(("PASS  " if cond else "FAIL  ") + label + (f"  [{detail}]" if detail and not cond else ""))
        if not cond:
            failures.append(label)

    # ------------------------------------------------------------------ 1) realtime
    # Child flushes one small JSON line every 0.25 s. If stdout is buffered until a
    # size threshold (text buffer or read(n)), every line arrives at once at EOF.
    child = (
        "import json,sys,time\n"
        "for i in range(6):\n"
        "    sys.stdout.write(json.dumps({'type':'file','i':i})+'\\n')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.25)\n"
    )
    arrivals = []

    def on_line(line):
        arrivals.append((time.time(), line))

    t0 = time.time()
    rc, lines, err = st.run_streaming([sys.executable, "-c", child], timeout=30, on_line=on_line)
    elapsed = time.time() - t0
    gaps = [arrivals[i + 1][0] - arrivals[i][0] for i in range(len(arrivals) - 1)]
    check("R1 callback got all 6 lines", len(arrivals) == 6, f"got {len(arrivals)}")
    check("R2 return contract (rc=0, 6 stdout lines, empty stderr)",
          rc == 0 and len(lines) == 6 and err == "", f"rc={rc} lines={len(lines)} err={err!r}")
    check("R3 lines delivered LIVE, not batched at EOF",
          bool(gaps) and max(gaps) < 0.9, f"max gap {max(gaps):.2f}s")
    check("R4 first line arrived within 1 s of start", bool(arrivals) and arrivals[0][0] - t0 < 1.0,
          f"first at {arrivals[0][0] - t0:.2f}s" if arrivals else "none")
    check("R5 total runtime matches the child's own pacing (~1.25 s)",
          elapsed < 4.0, f"{elapsed:.2f}s")
    check("R6 every callback line is a complete JSON object",
          all(_is_json(l) for (_, l) in arrivals))

    # ------------------------------------------------- 2) '\r' repaints become events
    child_cr = (
        "import sys,time\n"
        "for p in (10,45,90,100):\n"
        "    sys.stdout.write('wheel.whl %d%%\\r' % p)\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.15)\n"
        "sys.stdout.write('\\n')\n"
    )
    got = []
    rc, lines, _ = st.run_streaming([sys.executable, "-c", child_cr], timeout=20,
                                    on_line=got.append)
    check("R7 four pip repaints delivered as four events", len(got) == 4, f"got {got}")
    check("R8 repaint payloads intact", got == ["wheel.whl 10%", "wheel.whl 45%",
                                                "wheel.whl 90%", "wheel.whl 100%"], str(got))
    check("R9 repaints also returned in stdout_lines", lines == got)

    # ------------------------------------ 3) a line split across writes stays whole
    # The child writes half a JSON line, pauses 0.4 s (a separate pipe write), then the
    # rest. A chunk-splitting reader that decodes per chunk would emit the fragment and
    # hand the parser a broken event.
    partial = json.dumps({"type": "file", "filename": "x" * 200, "size": 10, "downloaded": 5})
    half = len(partial) // 2
    child_split = (
        "import sys,time,json\n"
        f"p = {partial!r}\n"
        f"sys.stdout.write(p[:{half}]); sys.stdout.flush()\n"
        "time.sleep(0.4)\n"
        f"sys.stdout.write(p[{half}:] + '\\n'); sys.stdout.flush()\n"
    )
    got = []
    rc, lines, _ = st.run_streaming([sys.executable, "-c", child_split], timeout=20,
                                    on_line=got.append)
    check("S1 split line delivered exactly once", len(got) == 1, f"got {got}")
    check("S2 split line reassembled byte-for-byte", got == [partial], str(got)[:120])
    check("S3 reassembled line is valid JSON", bool(got) and _is_json(got[0]))

    # --------------------------------------- 4) UTF-8 sequence split across writes
    text = "naïve — 日本語 ✓"
    child_utf = (
        "import sys,time\n"
        f"b = {text!r}.encode('utf-8')\n"
        "sys.stdout.buffer.write(b[:3]); sys.stdout.buffer.flush()\n"
        "time.sleep(0.3)\n"
        "sys.stdout.buffer.write(b[3:] + b'\\n'); sys.stdout.buffer.flush()\n"
    )
    got = []
    rc, lines, _ = st.run_streaming([sys.executable, "-c", child_utf], timeout=20,
                                    on_line=got.append)
    check("U1 multibyte char split across reads decoded correctly", got == [text], str(got))

    # -------------------------------------------------- 5) stderr / input / rc / timeout
    child_io = (
        "import sys\n"
        "data = sys.stdin.read()\n"
        "sys.stdout.write('got:' + data + '\\n')\n"
        "sys.stderr.write('warn-line\\n')\n"
        "sys.exit(3)\n"
    )
    rc, lines, err = st.run_streaming([sys.executable, "-c", child_io], timeout=20,
                                      input_text="hello")
    check("C1 stdin written in binary mode", lines == ["got:hello"], str(lines))
    check("C2 stderr captured as text", "warn-line" in err, repr(err))
    check("C3 non-zero return code propagated", rc == 3, f"rc={rc}")

    try:
        st.run_streaming([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
    check("C4 timeout kills the child and raises TimeoutExpired", timed_out)

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def _is_json(line: str) -> bool:
    try:
        json.loads(line)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
