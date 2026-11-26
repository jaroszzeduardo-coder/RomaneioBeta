import asyncio
import shutil
import socket
import subprocess
import tempfile

from playwright.async_api import async_playwright

from fretio.browser.chrome_locator import find_chrome
from fretio.browser.process_guard import (
    _find_free_port,
    _kill_proc,
    _register_owned_proc,
    browser_shutdown_requested,
)
from fretio.logging_conf import get_logger

_logger = get_logger(__name__)


class _ChromeBrowser:
    """Proxy que delega tudo ao browser Playwright, mas ao fechar tambem encerra o subprocess.

    Se *owned_playwright* for passado, o close() tambem encerra o driver Node.js.
    """

    def __init__(self, browser, process, profile_dir, owned_playwright=None):
        self._inner = browser
        self._process = process
        self._profile_dir = profile_dir
        self._owned_pw = owned_playwright

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __del__(self):
        # Garante que o Chrome seja morto mesmo que close() nunca seja chamado
        # (ex: quando o GC roda apos o event loop fechar)
        try:
            _kill_proc(self._process)
            self._process = None
        except Exception:
            pass

    def debug_port(self) -> int | None:
        args = getattr(self._process, "args", None) or []
        if isinstance(args, str):
            args = [args]
        for arg in args:
            text = str(arg)
            if text.startswith("--remote-debugging-port="):
                try:
                    return int(text.split("=", 1)[1])
                except ValueError:
                    return None
        return None

    async def detach(self):
        """Desconecta o Playwright e deixa o Chrome aberto para interação humana."""
        proc = self._process
        profile = self._profile_dir
        port = self.debug_port()
        self._process = None
        try:
            await self._inner.close()
        except Exception:
            pass
        if self._owned_pw:
            try:
                await self._owned_pw.stop()
            except Exception:
                pass
            self._owned_pw = None
        return proc, profile, port

    async def close(self):
        try:
            await self._inner.close()
        except Exception:
            pass
        _kill_proc(self._process)
        self._process = None
        if self._owned_pw:
            try:
                await self._owned_pw.stop()
            except Exception:
                pass
            self._owned_pw = None
        try:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
        except Exception:
            pass


def with_low_memory_chrome_args(launch_args: list[str]) -> list[str]:
    """Flags leves só quando o PC não aguenta vários Chromes. Sem isso, devolve a lista igual."""
    try:
        from cotacao.memory_budget import apply_low_memory_chrome_args
    except Exception:
        return launch_args
    return apply_low_memory_chrome_args(launch_args)


def launch_human_chrome(url: str, *, offscreen: bool = False):
    """Abre o Chrome real, sem Playwright, para o Turnstile concluir sozinho.

    A janela nasce em tamanho real. Fora da tela quando *offscreen* é verdadeiro.
    Flags de automação ficam de fora: o visto fica vermelho quando o Chrome
    nasce controlado.
    """
    if browser_shutdown_requested():
        raise RuntimeError("Encerramento do aplicativo em andamento; novos browsers estao bloqueados")
    chrome_path = find_chrome()
    port = _find_free_port()
    profile_dir = tempfile.mkdtemp(prefix="fretio_braspress_")
    # Sem --do-not-de-elevate o Chrome deste processo reabre o Chrome do
    # usuário, descarta o perfil novo e a porta de depuração nunca sobe.
    # O job do app também fica de fora: ele quebra o sandbox e o visto volta vermelho.
    if offscreen:
        window = ["--window-position=-3000,-3000", "--window-size=900,760"]
    else:
        window = ["--window-position=200,80", "--window-size=980,760"]
    proc = subprocess.Popen(
        [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--do-not-de-elevate",
            *window,
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, port, profile_dir


async def launch_browser_resilient(playwright=None, *, headless: bool = True, args: list[str] | None = None):
    """Lanca Chrome local como subprocess e conecta via CDP.

    Nunca usa Chromium embutido do Playwright.
    Se *playwright* for None, cria e gerencia o driver Node.js internamente
    com ate 3 tentativas (resiliente a crash do driver).
    """
    if browser_shutdown_requested():
        raise RuntimeError("Encerramento do aplicativo em andamento; novos browsers estao bloqueados")
    chrome_path = find_chrome()
    manage_pw = playwright is None
    last_error = None

    for attempt in range(3 if manage_pw else 1):
        pw = None
        port = _find_free_port()
        profile_dir = tempfile.mkdtemp(prefix="fretio_chrome_")
        proc = None

        try:
            pw = await async_playwright().start() if manage_pw else playwright

            launch_args = [
                chrome_path,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-dev-shm-usage",
                "--renderer-process-limit=2",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-client-side-phishing-detection",
                "--disable-default-apps",
                "--disable-hang-monitor",
                "--disable-popup-blocking",
                "--disable-prompt-on-repost",
                "--disable-sync",
                "--metrics-recording-only",
                "--mute-audio",
                "--disable-session-crashed-bubble",
                "--disable-features=InfiniteSessionRestore,Translate,OptimizationHints,MediaRouter",
                "--hide-crash-restore-bubble",
                "--noerrdialogs",
                "--do-not-de-elevate",
                "--disable-blink-features=AutomationControlled",
            ]
            if headless:
                launch_args.append("--headless=new")
            else:
                launch_args.extend(["--window-position=-3000,-3000", "--window-size=1,1"])

            if args:
                existing_keys = {a.split("=")[0] for a in launch_args}
                for a in args:
                    if a.split("=")[0] not in existing_keys:
                        launch_args.append(a)

            launch_args = with_low_memory_chrome_args(launch_args)

            proc = subprocess.Popen(
                launch_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            _register_owned_proc(proc, source="launch_browser_resilient")

            for _ in range(50):
                await asyncio.sleep(0.1)
                if proc.poll() is not None:
                    raise RuntimeError(f"Chrome encerrou inesperadamente (exit code {proc.returncode})")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                        break
                except (ConnectionRefusedError, OSError):
                    continue
            else:
                raise RuntimeError(f"Chrome nao respondeu na porta {port} em 5s")

            browser = await asyncio.wait_for(
                pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}"),
                timeout=15,
            )
            _logger.info("Chrome conectado via CDP porta %d (headless=%s)", port, headless)

            owned_pw = pw if manage_pw else None
            return _ChromeBrowser(browser, proc, profile_dir, owned_pw)

        except Exception as e:
            last_error = e
            _kill_proc(proc)
            shutil.rmtree(profile_dir, ignore_errors=True)
            if manage_pw and pw:
                try:
                    await pw.stop()
                except Exception:
                    pass
            if not manage_pw:
                raise
            if attempt < 2:
                _logger.warning(
                    "launch_browser_resilient tentativa %d/3 falhou: %s", attempt + 1, e
                )
                await asyncio.sleep(1 + attempt)

    raise last_error
