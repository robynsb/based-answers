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

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path


class PiError(RuntimeError):
    pass


# How a round ends depends on the pi version, so both are handled.
#
# pi >= 0.80 emits `agent_settled` once it will not continue automatically —
# the exact boundary this pipeline wants. pi 0.79 (the version in nixpkgs)
# has no such event: there, `agent_end` is itself terminal.
#
# So `agent_end` arms a settle a moment in the future rather than ending the
# round outright. On a newer pi the `agent_settled` that follows wins the
# race and ends the round immediately; on 0.79 nothing follows and the grace
# expires. Any sign that pi resumed work disarms it, so an automatic retry or
# a queued continuation is still waited out on both versions.
_SETTLE_GRACE = 2.0
_WORK_RESUMED = frozenset({
    "agent_start", "auto_retry_start", "compaction_start",
    "summarization_retry_attempt_start",
})


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
        buf = b""
        try:
            while True:
                # read1, not read: read(n) on a BufferedReader blocks until it
                # has n bytes or EOF, which would withhold every event until
                # pi exits — and pi stays alive across rounds by design.
                chunk = self.proc.stdout.read1(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    self._push(raw)
        finally:
            if buf.strip():
                self._push(buf)
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

        accepted = False
        settled = False
        summary = {"accepted": False, "settled": False, "errors": [], "text": ""}
        deadline = threading.Event()
        timer = threading.Timer(timeout, deadline.set)
        timer.start()
        # Set when agent_end says the run is over, to a moment slightly in the
        # future: see _SETTLE_GRACE.
        settle_at = None
        try:
            while not deadline.is_set():
                try:
                    ev = self._events.get(timeout=0.25)
                except queue.Empty:
                    if settle_at is not None and time.monotonic() >= settle_at:
                        settled = summary["settled"] = True
                        break
                    continue
                if ev is None:
                    if settle_at is not None:
                        # pi finished the run and then exited; that is a settle,
                        # not a crash.
                        settled = summary["settled"] = True
                        break
                    raise PiError(
                        f"pi exited before the run settled "
                        f"(code {self.proc.returncode})"
                    )

                etype = ev.get("type")

                # Command acknowledgement for our prompt.
                if etype == "response" and ev.get("id") == req_id:
                    accepted = summary["accepted"] = bool(ev.get("success"))
                    if not accepted:
                        raise PiError(f"pi rejected the prompt: {ev.get('error')}")
                    continue

                if on_event is not None:
                    on_event(ev)

                if etype == "extension_error":
                    summary["errors"].append(ev.get("error") or "extension error")

                if etype == "agent_settled":
                    settled = summary["settled"] = True
                    break
                if etype == "agent_end" and not ev.get("willRetry"):
                    settle_at = time.monotonic() + _SETTLE_GRACE
                elif etype in _WORK_RESUMED:
                    settle_at = None
            else:
                raise PiError(f"pi did not settle within {timeout}s")
        finally:
            timer.cancel()

        if not settled:
            raise PiError(f"pi did not settle within {timeout}s")
        return summary

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


def text_delta(event: dict) -> str | None:
    """The streamed assistant text in a message_update event, if any."""
    if event.get("type") != "message_update":
        return None
    ame = event.get("assistantMessageEvent") or {}
    if ame.get("type") == "text_delta":
        return ame.get("delta") or ""
    return None


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


def tool_finished(event: dict, name: str) -> dict | None:
    """The result of a completed call to `name`, if this event is one."""
    if event.get("type") != "tool_execution_end":
        return None
    if event.get("toolName") != name:
        return None
    return event.get("result") or {}
