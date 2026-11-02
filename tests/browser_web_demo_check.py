"""Real-browser acceptance check for the /demo workbench.

This is intentionally not part of ``unittest discover`` because it launches a
real Uvicorn process, a local Chrome/Edge instance and a temporary SQLite
database.  It never reads a provider key and never makes a paid model request.

Run from the repository root:

    python tests/browser_web_demo_check.py

The script uses Chrome DevTools Protocol directly (no Playwright/Selenium).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

REPO = Path(__file__).resolve().parents[1]
PROJECT = "browser-demo-project"
QUESTION_ONE = "研发部署方案使用什么技术？"
QUESTION_TWO = "部署方案是否使用 SQLite？"
KEY_NAMES = (
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
    "SILICONFLOW_API_KEY", "SILICONFLOW_BASE_URL", "SILICONFLOW_MODEL",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "MODEL_NAME",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
)
BROWSER_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def request_json(url, *, method="GET", timeout=20):
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else None


def request_json_url(base, path, *, method="GET", payload=None, timeout=20):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers,
                                     method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else None


class CDP:
    def __init__(self, websocket_url):
        self.ws = connect(websocket_url, max_size=20_000_000,
                          proxy=None, open_timeout=20)
        self.next_id = 0

    def call(self, method, params=None, timeout=40):
        self.next_id += 1
        message_id = self.next_id
        self.ws.send(json.dumps({"id": message_id, "method": method,
                                 "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"CDP timeout waiting for {method}")
            raw = self.ws.recv(timeout=remaining)
            data = json.loads(raw)
            if data.get("id") == message_id:
                if "error" in data:
                    raise RuntimeError(f"CDP {method} failed: {data['error']}")
                return data.get("result", {})
            # Other messages are events from the page; this acceptance flow
            # polls DOM state instead of maintaining an event queue.

    def evaluate(self, expression, *, await_promise=False, timeout=40):
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": True,
        }, timeout=timeout)
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            raise RuntimeError(f"page evaluation failed: {details}")
        return result.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def wait_js(cdp, expression, description, timeout=30, interval=0.15):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = cdp.evaluate(expression, timeout=5)
        except Exception as error:  # page navigation can transiently invalidate
            last = f"evaluate-error: {error}"
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {description}; last={last!r}")


def wait_health(base, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = request_json(base + "/health", timeout=1)
            if payload.get("status") == "ok":
                return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.15)
    raise AssertionError("uvicorn did not become healthy")


def wait_browser(browser_port, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return request_json(
                f"http://127.0.0.1:{browser_port}/json/version", timeout=1)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.1)
    raise AssertionError("browser debugging endpoint did not become ready")


def find_browser():
    for candidate in BROWSER_CANDIDATES:
        if candidate.exists():
            return candidate
    found = shutil.which("chrome") or shutil.which("msedge")
    return Path(found) if found else None


def prepare_environment(root):
    corpus = root / "corpus"
    corpus.mkdir()
    document = corpus / "deployment.md"
    document.write_text(
        "# 研发部署方案\n\n"
        "研发部署方案使用 FastAPI 提供接口，使用 SQLite 保存 Evidence 和 Session。\n"
        "合成安全标记：<img src=x onerror=\"window.__xss=1\">\n",
        encoding="utf-8",
    )
    db_path = root / "evidence.sqlite3"
    index_root = root / "live_index"

    for name in KEY_NAMES:
        os.environ.pop(name, None)
    os.environ["EVIDENCE_DB_PATH"] = str(db_path)
    os.environ["LIVE_INDEX_ROOT"] = str(index_root)
    os.environ["LIVE_RETRIEVAL_MODE"] = "bm25"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    sys.path.insert(0, str(REPO))
    from src.evidence import EvidenceScope
    from src.evidence_store import EvidenceStore
    from src.sync.models import SourceSpec
    from src.sync.service import SyncService

    scope = EvidenceScope(project_id=PROJECT, dataset_kind="live")
    sync = SyncService(db_path)
    source = sync.registry.register(SourceSpec(
        project_id=PROJECT,
        dataset_kind="live",
        connector_type="directory",
        source_uri=corpus.as_uri(),
        config={"root_path": str(corpus)},
        display_name="browser acceptance corpus",
    ))
    report = sync.sync(source)
    evidence = EvidenceStore(db_path).list(scope)
    assert_true(report.status == "succeeded", f"sync failed: {report.as_dict()}")
    assert_true(len(evidence) == 1, f"unexpected evidence count: {len(evidence)}")
    return db_path, index_root, evidence[0]


def build_server_environment(db_path, index_root):
    env = {k: v for k, v in os.environ.items() if k not in KEY_NAMES}
    env.update({
        "EVIDENCE_DB_PATH": str(db_path),
        "LIVE_INDEX_ROOT": str(index_root),
        "LIVE_RETRIEVAL_MODE": "bm25",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    })
    return env


def launch_server(port, env, log_path):
    log_handle = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=REPO, env=env, stdout=log_handle, stderr=subprocess.STDOUT,
        text=True,
    )
    return proc, log_handle


def launch_browser(root, url):
    executable = find_browser()
    if executable is None:
        raise RuntimeError("Chrome/Edge was not found; manual browser acceptance is required")
    port = free_port()
    profile = root / "browser-profile"
    log_path = root / "browser.log"
    log_handle = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(executable), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--no-first-run", "--no-default-browser-check", "--disable-extensions",
         "--window-size=1440,1000", f"--user-data-dir={profile}",
         f"--remote-debugging-port={port}", "--remote-allow-origins=*",
         "--disable-dev-shm-usage", "about:blank"],
        stdout=log_handle, stderr=subprocess.STDOUT, text=True,
    )
    wait_browser(port)
    # Create a page explicitly so the script does not depend on browser startup
    # tab ordering.  Chrome requires PUT for /json/new.
    target = request_json(
        f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe='')}",
        method="PUT", timeout=20)
    websocket_url = target["webSocketDebuggerUrl"]
    return proc, log_handle, port, websocket_url, log_path


def main():
    root = Path(tempfile.mkdtemp(prefix="cma-browser-check-"))
    db_path, index_root, evidence = prepare_environment(root)
    server_env = build_server_environment(db_path, index_root)

    preflight = subprocess.run(
        [sys.executable, "-c",
         "import api; from src.llm_config import resolve_api_key, resolve_base_url; "
         "print('KEY_EMPTY=' + str(resolve_api_key(resolve_base_url()) == '')); "
         "print('TITLE=' + api.app.title)"],
        cwd=REPO, env=server_env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert_true(preflight.returncode == 0, f"preflight failed: {preflight.stderr}")
    assert_true("KEY_EMPTY=True" in preflight.stdout,
                f"provider key unexpectedly resolved: {preflight.stdout}")

    server_port = free_port()
    base = f"http://127.0.0.1:{server_port}"
    server_log = root / "server.log"
    server = server_handle = None
    server, server_handle = launch_server(server_port, server_env, server_log)
    browser = None
    browser_handle = None
    cdp = None
    summary = {"status": "ok"}
    try:
        wait_health(base)
        browser, browser_handle, browser_port, websocket_url, browser_log = launch_browser(
            root, base + "/demo/")
        cdp = CDP(websocket_url)
        cdp.call("Runtime.enable")
        wait_js(cdp, "document.readyState === 'complete' && !!document.getElementById('project-id')",
                "workbench DOM")
        assert_true("研发调查工作台" in cdp.evaluate("document.title"),
                    "unexpected page title")
        assert_true(cdp.evaluate("document.querySelectorAll('.workspace > .panel').length") == 3,
                    "three-column workbench panels missing")

        cdp.evaluate(
            "window.__pageErrors = [];"
            "window.addEventListener('error', function (event) {"
            "  if (event.filename && event.filename.indexOf('app.js') >= 0) {"
            "    window.__pageErrors.push('error:' + event.message);"
            "  }"
            "});"
            "window.addEventListener('unhandledrejection', function (event) {"
            "  window.__pageErrors.push('rejection:' + String(event.reason));"
            "}); true;"
        )

        # 1) Create a Session through the visible left-column control.
        cdp.evaluate(
            "(function () {"
            f"const p=document.getElementById('project-id'); p.value={json.dumps(PROJECT)};"
            "p.dispatchEvent(new Event('change', {bubbles:true}));"
            "const d=document.getElementById('dataset-kind'); d.value='live';"
            "d.dispatchEvent(new Event('change', {bubbles:true}));"
            "document.getElementById('create-session-btn').click();"
            "return true;"
            "})()"
        )
        session_id = wait_js(
            cdp,
            "document.getElementById('session-id').value",
            "created session_id", timeout=20,
        )
        assert_true(session_id, "Session created without session_id")
        wait_js(cdp,
                "document.getElementById('session-status').textContent.trim() === '进行中'",
                "open Session status")

        # 2) Submit the first investigation question.
        cdp.evaluate(
            "(function () {"
            "const q=document.getElementById('question');"
            f"q.value={json.dumps(QUESTION_ONE)};"
            "q.dispatchEvent(new Event('input', {bubbles:true}));"
            "document.getElementById('run-form').requestSubmit(); return true;"
            "})()"
        )
        run_id = wait_js(
            cdp,
            "(function () {"
            "const value=document.getElementById('run-id-label').textContent"
            ".replace('run_id: ', '').trim();"
            "return value && value !== '—' ? value : '';"
            "})()",
            "run_id immediately after submit", timeout=20,
        )
        assert_true(run_id and run_id != "—", f"missing run_id: {run_id!r}")
        terminal_text = wait_js(
            cdp,
            "(function () {"
            "const t=document.getElementById('run-status-badge').textContent.trim();"
            "return ['已完成','证据不足','存在冲突','失败','已超时','已取消','已中断'].includes(t) ? t : '';"
            "})()",
            "terminal run badge", timeout=60,
        )
        assert_true(terminal_text == "证据不足",
                    f"offline fallback should end insufficient, got {terminal_text!r}")
        assert_true(cdp.evaluate(
            "document.querySelector('.agent-card') && "
            "document.querySelector('.agent-card').textContent.indexOf('不是成功找到答案') >= 0"),
            "insufficient warning missing from Agent card")
        wait_js(cdp, "document.querySelectorAll('#evidence-list .card').length > 0",
                "Evidence cards", timeout=20)

        # 3) Evidence is read through the scoped backend endpoint and rendered
        # as text; the synthetic <img> must never become an element.
        evidence_text = cdp.evaluate("document.getElementById('evidence-list').textContent")
        assert_true(evidence.evidence_id in evidence_text, "evidence id missing in UI")
        assert_true(evidence.content_hash in evidence_text, "evidence hash missing in UI")
        assert_true("FastAPI" in evidence_text or "SQLite" in evidence_text,
                    "Evidence content missing in UI")
        assert_true(cdp.evaluate("window.__xss") is None,
                    "untrusted Evidence markup executed")
        assert_true(cdp.evaluate("document.querySelectorAll('#evidence-list img').length") == 0,
                    "untrusted Evidence created an <img> element")
        assert_true("<img" in evidence_text,
                    "safe-rendering probe did not reach the Evidence text")

        # 4) Tool Calls and Trace tabs use the persisted API data.
        cdp.evaluate("document.querySelector('[data-tab=\"tools\"]').click(); true;")
        wait_js(cdp, "!document.getElementById('tab-tools').hidden", "Tool Calls tab")
        wait_js(cdp, "document.querySelectorAll('#tool-call-list .card').length > 0",
                "Tool Call cards", timeout=20)
        tool_text = cdp.evaluate("document.getElementById('tool-call-list').textContent")
        assert_true("search_memory" in tool_text or "tool" in tool_text.lower(),
                    "tool call content missing")

        cdp.evaluate("document.querySelector('[data-tab=\"trace\"]').click(); true;")
        wait_js(cdp, "!document.getElementById('tab-trace').hidden", "Trace tab")
        wait_js(cdp, "document.querySelectorAll('#trace-events li').length > 0",
                "Trace events", timeout=20)
        trace_text = cdp.evaluate("document.getElementById('trace-events').textContent")
        assert_true("run_created" in trace_text and "run_finished" in trace_text,
                    "lifecycle events missing from Trace")
        trace_summary = cdp.evaluate("document.getElementById('trace-summary').textContent")
        assert_true("insufficient" in trace_summary.lower() or "证据不足" in trace_summary,
                    "terminal reason missing from Trace summary")

        # 5) Final HTTP result agrees with the browser state.
        run_row = request_json_url(
            base, f"/runs/{run_id}?{urllib.parse.urlencode({'project_id': PROJECT})}")
        assert_true(run_row["run"]["status"] == "insufficient",
                    f"run row not insufficient: {run_row}")
        result = request_json_url(
            base, f"/runs/{run_id}/result?{urllib.parse.urlencode({'project_id': PROJECT})}")
        assert_true(result["result_available"] is True, "result not persisted")
        assert_true(bool(result.get("answer")), "empty persisted answer")
        refs = request_json_url(
            base, f"/runs/{run_id}/evidence-refs?{urllib.parse.urlencode({'project_id': PROJECT})}")
        assert_true(len(refs["evidence_refs"]) >= 1, "no persisted Evidence references")

        # 6) Same Session, second Run: multi-turn history and latest run.
        cdp.evaluate(
            "(function () {"
            "const q=document.getElementById('question');"
            f"q.value={json.dumps(QUESTION_TWO)};"
            "q.dispatchEvent(new Event('input', {bubbles:true}));"
            "document.getElementById('run-form').requestSubmit(); return true;"
            "})()"
        )
        wait_js(cdp,
                "(function () {"
                "const value=document.getElementById('run-id-label').textContent"
                ".replace('run_id: ', '').trim();"
                "return value && value !== '—' && value !== " + json.dumps(run_id) + ";"
                "})()",
                "second run_id", timeout=20)
        wait_js(cdp,
                "['已完成','证据不足','存在冲突','失败','已超时','已取消','已中断']"
                ".includes(document.getElementById('run-status-badge').textContent.trim())",
                "second terminal run", timeout=60)
        wait_js(cdp, "document.querySelectorAll('#run-history .run-item').length >= 2",
                "two-run history", timeout=20)
        second_run_id = cdp.evaluate(
            "document.getElementById('run-id-label').textContent.replace('run_id: ', '').trim()")
        assert_true(second_run_id and second_run_id != run_id,
                    "second run did not create a new run_id")

        # 7) Reload: URL recovery restores the same Session/Run and does not
        # send a cancellation request for the background Run.
        assert_true(cdp.evaluate("window.__pageErrors.length") == 0,
                    "page raised JavaScript errors before reload")
        cdp.call("Page.reload", {"ignoreCache": True}, timeout=20)
        wait_js(cdp, "document.readyState === 'complete' && !!document.getElementById('project-id')",
                "reloaded workbench", timeout=30)
        wait_js(cdp,
                "document.getElementById('session-id').value === " + json.dumps(session_id),
                "session recovery after reload", timeout=30)
        wait_js(cdp,
                "document.getElementById('run-id-label').textContent.indexOf(" + json.dumps(second_run_id) + ") >= 0",
                "run recovery after reload", timeout=30)
        wait_js(cdp, "document.querySelectorAll('#evidence-list .card').length > 0",
                "Evidence recovery after reload", timeout=30)
        assert_true(cdp.evaluate(
            "window.__pageErrors ? window.__pageErrors.length : 0") == 0,
            "page raised JavaScript errors after reload")
        after_reload = request_json_url(
            base, f"/runs/{second_run_id}?{urllib.parse.urlencode({'project_id': PROJECT})}")
        assert_true(after_reload["run"]["status"] in {
            "insufficient", "succeeded", "conflict", "failed", "timed_out",
            "cancelled", "interrupted",
        }, f"recovered run not terminal: {after_reload}")

        summary.update({
            "browser": str(find_browser()),
            "project_id": PROJECT,
            "session_id": session_id,
            "first_run_id": run_id,
            "first_status": run_row["run"]["status"],
            "second_run_id": second_run_id,
            "second_status": after_reload["run"]["status"],
            "evidence_refs": len(refs["evidence_refs"]),
            "evidence_rendered_as_text": True,
            "xss_executed": False,
            "page_errors": 0,
            "tested_url": base + "/demo/",
            "server_entry": "python -m uvicorn api:app",
            "provider_key_resolved": False,
        })
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        if cdp is not None:
            cdp.close()
        if browser is not None and browser.poll() is None:
            browser.terminate()
            try:
                browser.wait(timeout=10)
            except subprocess.TimeoutExpired:
                browser.kill()
        if browser_handle is not None:
            browser_handle.close()
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
        if server_handle is not None:
            server_handle.close()
        # Keep artifacts available for diagnosis, but avoid failing the test on
        # Windows profile locks.  The caller prints the path on failure.
        print(f"artifacts: {root}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
