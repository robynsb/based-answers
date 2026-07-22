"""A `pi --mode rpc` subprocess, driven as a deterministic subroutine.

The pipeline owns the control flow: `prompt()` blocks until pi reports the
run has fully settled, then returns and the checkers take over. Feedback for
the next round is another `prompt()` on the same PiSession, which is the same
conversation — no session id to discover, no `--session` to pass.

Protocol (docs/rpc.md in the pi tarball): JSON objects on stdin, one per
line; events and command responses as JSON lines on stdout. Framing is
strict JSONL with LF as the *only* record delimiter, so stdout is read as
bytes and split on b"\\n" — Python's text-mode universal newlines would also
split on a bare CR.
"""

import functools
import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path


class PiError(RuntimeError):
    pass


# How a round ends depends on the pi version.
#
# pi >= 0.80 emits `agent_settled` once it will not continue automatically —
# the exact boundary this pipeline wants. pi 0.79 (the version in nixpkgs)
# has no such event: there, `agent_end` is itself terminal.
#
# The version is resolved once per process rather than re-discovered by
# racing a timer on every prompt: on a pi that emits `agent_settled` the
# round ends strictly on that event, and on one that does not it ends on
# `agent_end`. Waiting out a grace period on every prompt would otherwise
# cost seconds per round on exactly the version we ship.
_SETTLE_EVENT_MIN_VERSION = (0, 80)
# Only used when the version cannot be determined: behave like the old pi and
# give a possible `agent_settled` a moment to arrive before calling it done.
_SETTLE_GRACE = 2.0
_WORK_RESUMED = frozenset({
    "agent_start", "auto_retry_start", "compaction_start",
    "summarization_retry_attempt_start",
})


@functools.cache
def emits_agent_settled(pi_bin: str = "pi") -> bool | None:
    """Whether this pi emits `agent_settled`. None when it cannot be told.

    Cached per binary: this shells out once per process, not once per
    session (a run builds a fresh session for every check).
    """
    try:
        r = subprocess.run([pi_bin, "--version"], capture_output=True,
                           text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", r.stdout or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2))) >= _SETTLE_EVENT_MIN_VERSION


def build_command(
    *,
    extensions: list[str | Path],
    tools: list[str],
    system_prompt: str,
    session_dir: str | Path | None = None,
    model: str | None = None,
    pi_bin: str = "pi",
) -> list[str]:
    """The argv for a fully self-contained pi RPC run.

    Every isolation flag is deliberate: `--no-extensions` suppresses discovery
    from ~/.pi and .pi entirely, `--no-approve` ignores project-local files,
    and `--no-builtin-tools` plus `--tools` means the agent gets exactly the
    pipeline's three tools and nothing else (no bash, no edit, no web).
    """
    cmd = [
        pi_bin, "--mode", "rpc",
        "--no-extensions",
        "--no-approve",
        "--no-builtin-tools",
        "--system-prompt", system_prompt,
    ]
    # No --tools at all means no tools, which is what the checkers want:
    # they judge text and must not be able to go looking at the sources.
    if tools:
        cmd[6:6] = ["--tools", ",".join(tools)]
    for ext in extensions:
        cmd.extend(["-e", str(ext)])
    if session_dir is not None:
        cmd.extend(["--session-dir", str(session_dir)])
    if model:
        cmd.extend(["--model", model])
    return cmd


class PiSession:
    """One pi RPC subprocess = one persistent agent conversation."""

    def __init__(
        self,
        *,
        cwd: str | Path = ".",
        config_dir: str | Path,
        extensions: list[str | Path],
        tools: list[str],
        system_prompt: str,
        session_dir: str | Path | None = None,
        model: str | None = None,
        env: dict | None = None,
        stderr_to: int | None = None,
        pi_bin: str = "pi",
    ):
        self.cwd = str(cwd)
        cmd = build_command(
            extensions=extensions, tools=tools, system_prompt=system_prompt,
            session_dir=session_dir, model=model, pi_bin=pi_bin,
        )

        # PI_CODING_AGENT_DIR relocates settings/auth/trust/sessions off ~/.pi,
        # so a run cannot pick up or mutate global pi state. Because auth.json
        # moves too, credentials must come from the environment.
        full_env = {**os.environ, "PI_CODING_AGENT_DIR": str(config_dir)}
        if env:
            full_env.update(env)

        Path(config_dir).mkdir(parents=True, exist_ok=True)
        self.cmd = cmd
        # None (undeterminable) falls back to the grace period.
        self._settles_on_agent_end = emits_agent_settled(pi_bin) is False
        self.proc = subprocess.Popen(
            cmd,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_to if stderr_to is not None else subprocess.DEVNULL,
            env=full_env,
        )
        self._events: queue.Queue = queue.Queue()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True,
                                        name="pi-rpc-reader")
        self._reader.start()

    # -- protocol plumbing -------------------------------------------------

    def _read_stdout(self):
        """Split stdout on LF only and push decoded records onto the queue."""
        # bytearray, not bytes: a single record can be megabytes (a pdf_search
        # `get` over many pages), and re-copying it on every append is quadratic.
        buf = bytearray()
        try:
            while True:
                # read1, not read: read(n) on a BufferedReader blocks until it
                # has n bytes or EOF, which would withhold every event until
                # pi exits — and pi stays alive across rounds by design.
                chunk = self.proc.stdout.read1(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    self._push(bytes(buf[:nl]))
                    del buf[:nl + 1]
        finally:
            if buf.strip():
                self._push(bytes(buf))
            self._events.put(None)  # EOF sentinel

    def _push(self, raw: bytes):
        line = raw.rstrip(b"\r").strip()
        if not line:
            return
        try:
            self._events.put(json.loads(line.decode("utf-8", "replace")))
        except json.JSONDecodeError:
            pass  # non-protocol noise on stdout is ignored

    def _send(self, obj: dict):
        if self.proc.poll() is not None:
            raise PiError(f"pi exited with code {self.proc.returncode}")
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    # -- the one operation the pipeline needs ------------------------------

    def prompt(self, message: str, on_event=None, timeout: float = 900.0) -> dict:
        """Send a prompt and block until the agent run has fully settled.

        `on_event(event)` is called for every event in between, which is how
        the pipeline streams agent output to the browser and notices
        write_answer calls. Returns a small summary dict.
        """
        req_id = uuid.uuid4().hex[:12]
        self._send({"id": req_id, "type": "prompt", "message": message})

        summary = {"settled": False, "errors": []}
        # How long to wait after agent_end for an agent_settled that may not be
        # coming: none at all when the version says it never will.
        grace = 0.0 if self._settles_on_agent_end else _SETTLE_GRACE
        deadline = time.monotonic() + timeout
        # Set when agent_end says the run is over; see the module comment.
        settle_at = None

        while time.monotonic() < deadline:
            try:
                ev = self._events.get(timeout=0.1 if settle_at else 0.5)
            except queue.Empty:
                if settle_at is not None and time.monotonic() >= settle_at:
                    summary["settled"] = True
                    return summary
                continue

            if ev is None:
                if settle_at is not None:
                    # pi finished the run and then exited; a settle, not a crash.
                    summary["settled"] = True
                    return summary
                raise PiError(
                    f"pi exited before the run settled (code {self.proc.returncode})")

            etype = ev.get("type")

            # Command acknowledgement for our prompt.
            if etype == "response" and ev.get("id") == req_id:
                if not ev.get("success"):
                    raise PiError(f"pi rejected the prompt: {ev.get('error')}")
                continue

            if on_event is not None:
                on_event(ev)

            if etype == "extension_error":
                summary["errors"].append(ev.get("error") or "extension error")

            if etype == "agent_settled":
                summary["settled"] = True
                return summary
            if etype == "agent_end" and not ev.get("willRetry"):
                if grace <= 0:
                    summary["settled"] = True
                    return summary
                settle_at = time.monotonic() + grace
            elif etype in _WORK_RESUMED:
                settle_at = None

        raise PiError(f"pi did not settle within {timeout}s")

    def close(self, timeout: float = 10.0):
        """Close stdin so pi exits, then reap it."""
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self._reader.join(timeout=5)
        try:
            self.proc.stdout.close()
        except (OSError, ValueError):
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _delta(event: dict, kind: str) -> str | None:
    if event.get("type") != "message_update":
        return None
    ame = event.get("assistantMessageEvent") or {}
    if ame.get("type") == kind:
        return ame.get("delta") or ""
    return None


def text_delta(event: dict) -> str | None:
    """The streamed assistant text in a message_update event, if any."""
    return _delta(event, "text_delta")


def thinking_delta(event: dict) -> str | None:
    """The streamed reasoning text in a message_update event, if any.

    A reasoning model emits its thinking as its own content block, separate
    from the answer: `thinking_start`, a run of `thinking_delta`s, then
    `thinking_end`, before any `text_delta`. It is shown to the user but must
    never be mistaken for the assistant's reply — a checker that reasons "this
    could FAIL if…" and then answers PASS would be read as a failure.
    """
    return _delta(event, "thinking_delta")


USAGE_FIELDS = ("input", "output", "cacheRead", "cacheWrite")


def message_usage(event: dict) -> dict | None:
    """Token usage reported by a completed assistant message, if any.

    `message_end` carries the finished AssistantMessage, whose `usage` block
    is what the provider billed for that one API call. Summing these across
    a session is the real cost: a continuing conversation re-sends its whole
    context, so each call's `input` counts the transcript again — that is
    double counting of tokens, but not of money.
    """
    if event.get("type") != "message_end":
        return None
    msg = event.get("message") or {}
    if msg.get("role") != "assistant":
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    out = {k: int(usage.get(k) or 0) for k in USAGE_FIELDS}
    cost = usage.get("cost")
    out["cost"] = float((cost or {}).get("total") or 0.0) if isinstance(cost, dict) else 0.0
    out["total"] = sum(out[k] for k in USAGE_FIELDS)
    return out
