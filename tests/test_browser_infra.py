import asyncio
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app" / "fretio" / "src"))

from fretio.browser import chrome_locator, launcher, process_guard


def test_find_chrome_prefers_existing_standard_path(monkeypatch):
    expected = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

    def fake_expandvars(value):
        return expected if value.startswith("%ProgramFiles%") else value

    monkeypatch.setattr(chrome_locator.os.path, "expandvars", fake_expandvars)
    monkeypatch.setattr(chrome_locator.os.path, "isfile", lambda path: path == expected)
    monkeypatch.setattr(chrome_locator.shutil, "which", lambda name: None)

    assert chrome_locator.find_chrome() == expected


def test_find_chrome_uses_registry_candidate_when_present(monkeypatch):
    registry_path = r"C:\ChromeFromRegistry\chrome.exe"
    fake_winreg = types.SimpleNamespace(
        HKEY_LOCAL_MACHINE=object(),
        HKEY_CURRENT_USER=object(),
        OpenKey=lambda root, path: (root, path),
        QueryValueEx=lambda key, name: (registry_path, None),
        CloseKey=lambda key: None,
    )

    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(chrome_locator.os.path, "expandvars", lambda value: value)
    monkeypatch.setattr(chrome_locator.os.path, "isfile", lambda path: path == registry_path)
    monkeypatch.setattr(chrome_locator.shutil, "which", lambda name: None)

    assert chrome_locator.find_chrome() == registry_path


def test_find_chrome_raises_clear_error_when_missing(monkeypatch):
    monkeypatch.setattr(chrome_locator.os.path, "expandvars", lambda value: value)
    monkeypatch.setattr(chrome_locator.os.path, "isfile", lambda path: False)
    monkeypatch.setattr(chrome_locator.shutil, "which", lambda name: None)

    with pytest.raises(FileNotFoundError, match="Google Chrome nao encontrado"):
        chrome_locator.find_chrome()


def test_kill_pid_tree_ignores_invalid_pid(monkeypatch):
    calls = []
    monkeypatch.setattr(process_guard.os, "kill", lambda *args: calls.append(args))

    process_guard._kill_pid_tree(0)

    assert calls == []


def test_kill_pid_tree_uses_taskkill_for_windows(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(process_guard, "os", types.SimpleNamespace(name="nt", kill=lambda *args: None))
    monkeypatch.setattr(process_guard.subprocess, "run", fake_run)

    process_guard._kill_pid_tree(1234)

    assert calls[0][0] == ["taskkill", "/F", "/T", "/PID", "1234"]
    assert calls[0][1]["timeout"] == 10


def test_cleanup_fallback_owned_procs_kills_registered_pids_without_real_processes(monkeypatch):
    killed = []
    monkeypatch.setattr(process_guard, "_kill_pid_tree", lambda pid: killed.append(pid))

    with process_guard._WINDOWS_JOB_LOCK:
        process_guard._FALLBACK_OWNED_PIDS.clear()
        process_guard._FALLBACK_OWNED_PIDS.update({101, 202})

    process_guard._cleanup_fallback_owned_procs()

    assert sorted(killed) == [101, 202]
    assert process_guard._FALLBACK_OWNED_PIDS == set()


def test_register_owned_proc_falls_back_to_pid_when_job_object_fails(monkeypatch):
    class FakeProc:
        pid = 777

        def poll(self):
            return None

    monkeypatch.setattr(process_guard, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        process_guard,
        "_get_windows_job_handle",
        lambda: (_ for _ in ()).throw(RuntimeError("job unavailable")),
    )

    with process_guard._WINDOWS_JOB_LOCK:
        process_guard._FALLBACK_OWNED_PIDS.clear()

    process_guard._register_owned_proc(FakeProc(), source="test")

    assert process_guard._FALLBACK_OWNED_PIDS == {777}
    process_guard._FALLBACK_OWNED_PIDS.clear()


def test_launch_browser_resilient_connects_via_cdp_without_opening_real_chrome(monkeypatch, tmp_path):
    popen_calls = []
    registered = []
    killed = []

    class FakeProc:
        pid = 456
        returncode = None

        def poll(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeChromium:
        async def connect_over_cdp(self, endpoint):
            self.endpoint = endpoint
            return types.SimpleNamespace(closed=False, close=self.close_browser)

        async def close_browser(self):
            return None

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()

    def fake_popen(args, **kwargs):
        popen_calls.append((args, kwargs))
        return FakeProc()

    async def fake_sleep(delay):
        return None

    monkeypatch.setattr(launcher, "find_chrome", lambda: "/fake/chrome")
    monkeypatch.setattr(launcher, "_find_free_port", lambda: 9222)
    monkeypatch.setattr(launcher.tempfile, "mkdtemp", lambda prefix: str(tmp_path / "profile"))
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launcher, "_register_owned_proc", lambda proc, source: registered.append((proc.pid, source)))
    monkeypatch.setattr(launcher, "_kill_proc", lambda proc: killed.append(getattr(proc, "pid", None)))
    monkeypatch.setattr(launcher.socket, "create_connection", lambda *args, **kwargs: FakeConnection())
    monkeypatch.setattr(launcher.asyncio, "sleep", fake_sleep)

    browser = asyncio.run(
        launcher.launch_browser_resilient(
            FakePlaywright(),
            headless=False,
            args=["--disable-gpu", "--custom-flag=1"],
        )
    )

    assert isinstance(browser, launcher._ChromeBrowser)
    launch_args = popen_calls[0][0]
    assert launch_args[0] == "/fake/chrome"
    assert "--headless=new" not in launch_args
    assert "--window-position=-3000,-3000" in launch_args
    assert launch_args.count("--disable-gpu") == 1
    assert "--custom-flag=1" in launch_args
    assert registered == [(456, "launch_browser_resilient")]
    assert killed == []

    asyncio.run(browser.close())
    assert killed == [456]


@pytest.mark.parametrize("platform,sandbox_present", [("win32", False), ("linux", True)])
def test_no_sandbox_only_added_off_windows(monkeypatch, tmp_path, platform, sandbox_present):
    """--no-sandbox só é injetado fora do Windows; no desktop Windows o sandbox
    do Chromium fica ativo (CWE-693)."""
    popen_calls = []

    class FakeProc:
        pid = 789
        returncode = None

        def poll(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeChromium:
        async def connect_over_cdp(self, endpoint):
            return types.SimpleNamespace(closed=False, close=self._close)

        async def _close(self):
            return None

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(launcher, "find_chrome", lambda: "/fake/chrome")
    monkeypatch.setattr(launcher, "_find_free_port", lambda: 9222)
    monkeypatch.setattr(launcher.tempfile, "mkdtemp", lambda prefix: str(tmp_path / "profile"))
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda args, **k: popen_calls.append(args) or FakeProc())
    monkeypatch.setattr(launcher, "_register_owned_proc", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "_kill_proc", lambda p: None)
    monkeypatch.setattr(launcher.socket, "create_connection", lambda *a, **k: FakeConnection())
    monkeypatch.setattr(launcher.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(launcher.sys, "platform", platform)

    asyncio.run(
        launcher.launch_browser_resilient(
            FakePlaywright(),
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
    )
    launch_args = popen_calls[0]
    assert ("--no-sandbox" in launch_args) is sandbox_present


def test_providers_base_reexports_browser_helpers():
    import fretio.providers.base as base

    assert base.find_chrome is chrome_locator.find_chrome
    assert base.launch_browser_resilient is launcher.launch_browser_resilient
    assert base._kill_proc is process_guard._kill_proc
