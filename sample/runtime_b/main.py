"""
Sample Sandbox Runtime — same pure executor as runtime_b_v5, for the MySQL demo.

Two HTTP actions on :8080 (shell / python) returning JSON, plus lifecycle hooks
on :9000. The only difference from runtime_b_v5 is the baked-in image: this one
ships `pymysql` so the model's Python can connect to RDS MySQL directly.

The model decides everything; this runtime just executes. Files persist in
/tmp/workspace across calls within one MicroVM.
"""

import io
import os
import sys
import json
import logging
import shutil
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_PORT = int(os.environ.get("APP_PORT", "8080"))
HOOK_PORT = int(os.environ.get("HOOK_PORT", "9000"))
WORKSPACE = os.environ.get("WORKSPACE_DIR", "/tmp/workspace")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("sample_sandbox")

os.makedirs(WORKSPACE, exist_ok=True)


def _configure_cjk_font():
    try:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib import font_manager as fm
        cjk = [f.fname for f in fm.fontManager.ttflist
               if "CJK" in f.fname or "noto" in f.fname.lower()]
        if cjk:
            fm.fontManager.addfont(cjk[0])
            matplotlib.rcParams["font.sans-serif"] = [fm.FontProperties(fname=cjk[0]).get_name()]
            matplotlib.rcParams["axes.unicode_minus"] = False
            logger.info("matplotlib CJK font configured")
    except Exception as e:
        logger.info(f"CJK font config skipped: {e}")


_configure_cjk_font()


def handle_shell(payload: dict) -> dict:
    import subprocess
    command = payload.get("command", "")
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True,
                           timeout=120, cwd=WORKSPACE)
        return {"status": "success" if r.returncode == 0 else "error",
                "stdout": r.stdout[-5000:], "stderr": r.stderr[-2000:], "exit_code": r.returncode}
    except subprocess.TimeoutExpired:
        return {"status": "error", "stdout": "", "stderr": "timeout (120s)", "exit_code": -1}
    except Exception as e:
        return {"status": "error", "stdout": "", "stderr": str(e), "exit_code": -1}


def handle_python(payload: dict) -> dict:
    code = payload.get("code", "")
    out, err = io.StringIO(), io.StringIO()
    g = {"__builtins__": __builtins__, "WORKSPACE": WORKSPACE}
    so, se = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = out, err
        exec(code, g)
        rc = 0
    except Exception:
        err.write(traceback.format_exc())
        rc = 1
    finally:
        sys.stdout, sys.stderr = so, se
    files = []
    odir = os.path.join(WORKSPACE, "output")
    if os.path.isdir(odir):
        files = [{"name": f, "size": os.path.getsize(os.path.join(odir, f))}
                 for f in os.listdir(odir) if os.path.isfile(os.path.join(odir, f))]
    return {"status": "success" if rc == 0 else "error",
            "stdout": out.getvalue()[-8000:], "stderr": err.getvalue()[-3000:],
            "exit_code": rc, "output_files": files}


def dispatch(payload: dict) -> dict:
    a = payload.get("action", "")
    if a == "shell":
        return handle_shell(payload)
    if a == "python":
        return handle_python(payload)
    return {"status": "error", "error": f"Unknown action: {a}. Supported: shell, python"}


class AppHandler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._json({"status": "ok", "service": "sample_sandbox"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads((self.rfile.read(n) if n else b"{}").decode("utf-8") or "{}")
        except Exception as e:
            self._json({"status": "error", "error": f"bad request: {e}"}, code=400)
            return
        result = dispatch(payload)
        logger.info(f"action={payload.get('action')} -> {result.get('status')}")
        self._json(result)

    def log_message(self, *a):
        pass


def reset_workspace():
    try:
        if os.path.isdir(WORKSPACE):
            shutil.rmtree(WORKSPACE)
        os.makedirs(os.path.join(WORKSPACE, "output"), exist_ok=True)
        logger.info("workspace reset")
    except Exception as e:
        logger.error(f"reset failed: {e}")


class HookHandler(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        path = self.path.rstrip("/")
        n = int(self.headers.get("Content-Length", 0))
        if n:
            self.rfile.read(n)
        if path.endswith("/run"):
            reset_workspace()
        logger.info(f"hook {path} -> 200")
        self._ok()

    def log_message(self, *a):
        pass


def main():
    import threading
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", HOOK_PORT), HookHandler).serve_forever,
                     daemon=True).start()
    logger.info(f"hook server :{HOOK_PORT}")
    srv = ThreadingHTTPServer(("0.0.0.0", APP_PORT), AppHandler)
    logger.info(f"app server :{APP_PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
