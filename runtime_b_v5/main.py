"""
Runtime B V5 — Pure Executor on AWS Lambda MicroVMs (No Decision-Making)

Difference from V3:
  - V3 ran on AgentCore Runtime (@app.entrypoint, invoked via invoke_agent_runtime).
  - V5 runs inside a Lambda MicroVM: a plain HTTP server that Lambda's proxy
    forwards requests to. Runtime A reaches it over the MicroVM endpoint URL.

Two capabilities, both commanded by Agent A (Runtime A) — report rendering
moved to Runtime A in V5, so Runtime B is a pure code executor:
  1. shell:   Execute shell commands (aws cli, head, wc, sort, etc.)
  2. python:  Execute Python code (pandas, numpy, matplotlib)

Ports:
  - 8080: application traffic (shell/python actions). Lambda routes inbound
          endpoint traffic here by default.
  - 9000: lifecycle hooks (/ready, /run, /resume, /suspend, /terminate).
          Configured via the MicroVM image `hooks.port`.

Snapshot uniqueness (Firecracker snapshot-restore):
  The MicroVM image is built by starting this server and snapshotting it.
  Every MicroVM restores from that one snapshot, so anything created BEFORE
  the snapshot is shared across all MicroVMs. We therefore:
    - Do NOT create long-lived boto3 clients at module load. The shell action
      shells out to the aws CLI (fresh creds per process) and the python
      action runs user code in a fresh namespace, so there is no persistent
      network state to corrupt.
    - Reset the workspace in the /run hook so each MicroVM starts clean.

Filesystem persists within the same MicroVM (same session = same microVM)
across multiple shell/python calls, exactly like V3's same-session behavior.
"""

import io
import os
import sys
import json
import logging
import shutil
import subprocess
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------- Config ----------
APP_PORT = int(os.environ.get("APP_PORT", "8080"))
HOOK_PORT = int(os.environ.get("HOOK_PORT", "9000"))
WORKSPACE = os.environ.get("WORKSPACE_DIR", "/tmp/workspace")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("runtime_b_v5")

os.makedirs(WORKSPACE, exist_ok=True)


def _configure_cjk_font():
    """Make matplotlib default to a bundled CJK font so Chinese chart labels
    render instead of boxes. Best-effort: skipped if matplotlib/fonts absent."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib import font_manager as fm
        cjk = [f.fname for f in fm.fontManager.ttflist
               if "CJK" in f.fname or "noto" in f.fname.lower()]
        if cjk:
            fm.fontManager.addfont(cjk[0])
            name = fm.FontProperties(fname=cjk[0]).get_name()
            matplotlib.rcParams["font.sans-serif"] = [name]
            matplotlib.rcParams["axes.unicode_minus"] = False
            logger.info(f"matplotlib CJK font set: {name}")
    except Exception as e:
        logger.info(f"CJK font config skipped: {e}")


_configure_cjk_font()


# ---------- Action: Shell ----------

def handle_shell(payload: dict) -> dict:
    command = payload.get("command", "")
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=120, cwd=WORKSPACE,
        )
        return {
            "status": "success" if result.returncode == 0 else "error",
            "stdout": result.stdout[-5000:] if len(result.stdout) > 5000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "stdout": "", "stderr": "Command timed out (120s)", "exit_code": -1}
    except Exception as e:
        return {"status": "error", "stdout": "", "stderr": str(e), "exit_code": -1}


# ---------- Action: Python ----------

def handle_python(payload: dict) -> dict:
    code = payload.get("code", "")

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    exec_globals = {"__builtins__": __builtins__, "WORKSPACE": WORKSPACE}

    old_stdout, old_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout = stdout_buf
        sys.stderr = stderr_buf
        exec(code, exec_globals)
        exit_code = 0
    except Exception:
        stderr_buf.write(traceback.format_exc())
        exit_code = 1
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    stdout_str = stdout_buf.getvalue()
    stderr_str = stderr_buf.getvalue()

    # List output files if any were created
    output_files = []
    output_dir = os.path.join(WORKSPACE, "output")
    if os.path.isdir(output_dir):
        for f in os.listdir(output_dir):
            fp = os.path.join(output_dir, f)
            if os.path.isfile(fp):
                output_files.append({"name": f, "size": os.path.getsize(fp)})

    return {
        "status": "success" if exit_code == 0 else "error",
        "stdout": stdout_str[-8000:] if len(stdout_str) > 8000 else stdout_str,
        "stderr": stderr_str[-3000:] if len(stderr_str) > 3000 else stderr_str,
        "exit_code": exit_code,
        "output_files": output_files,
    }


def dispatch(payload: dict) -> dict:
    action = payload.get("action", "")
    if action == "shell":
        return handle_shell(payload)
    elif action == "python":
        return handle_python(payload)
    else:
        return {"status": "error", "error": f"Unknown action: {action}. Supported: shell, python"}


# ---------- App HTTP server (port 8080) ----------

class AppHandler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Simple health endpoint
        self._send_json({"status": "ok", "service": "runtime_b_v5"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:
            self._send_json({"status": "error", "error": f"bad request: {e}"}, code=400)
            return

        action = payload.get("action", "")
        result = dispatch(payload)
        logger.info(f"action={action} -> status={result.get('status')} exit={result.get('exit_code')}")
        self._send_json(result)

    def log_message(self, *args):
        pass  # silence default access logging


# ---------- Lifecycle hook server (port 9000) ----------

HOOK_BASE = "/aws/lambda-microvms/runtime/v1"


def reset_workspace():
    """Each MicroVM starts from the same snapshot; wipe workspace for a clean slate."""
    try:
        if os.path.isdir(WORKSPACE):
            shutil.rmtree(WORKSPACE)
        os.makedirs(os.path.join(WORKSPACE, "output"), exist_ok=True)
        logger.info("workspace reset")
    except Exception as e:
        logger.error(f"workspace reset failed: {e}")


class HookHandler(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        path = self.path.rstrip("/")
        # drain body if any
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)

        if path.endswith("/ready"):
            # Image build: signal the app initialized and is snapshot-ready.
            logger.info("hook /ready -> 200")
            self._ok()
        elif path.endswith("/run"):
            # Fresh MicroVM started from snapshot — clean per-MicroVM state.
            reset_workspace()
            logger.info("hook /run -> 200")
            self._ok()
        elif path.endswith("/resume"):
            # Resumed from suspend — no persistent network state to rebuild
            # (no long-lived boto3 clients). Nothing to do.
            logger.info("hook /resume -> 200")
            self._ok()
        elif path.endswith("/suspend"):
            logger.info("hook /suspend -> 200")
            self._ok()
        elif path.endswith("/terminate"):
            logger.info("hook /terminate -> 200")
            self._ok()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):
        pass


# ---------- Entrypoint ----------

def main():
    # Hook server in a background thread, app server in the foreground.
    import threading
    hook_srv = ThreadingHTTPServer(("0.0.0.0", HOOK_PORT), HookHandler)
    threading.Thread(target=hook_srv.serve_forever, daemon=True).start()
    logger.info(f"hook server listening on :{HOOK_PORT} ({HOOK_BASE}/*)")

    app_srv = ThreadingHTTPServer(("0.0.0.0", APP_PORT), AppHandler)
    logger.info(f"app server listening on :{APP_PORT} (POST shell/python)")
    app_srv.serve_forever()


if __name__ == "__main__":
    main()
