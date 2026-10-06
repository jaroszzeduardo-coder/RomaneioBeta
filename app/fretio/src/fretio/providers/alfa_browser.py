"""Mixin de browser/CDP/janela/cleanup do provider Alfa (métodos movidos de alfa.py)."""
from __future__ import annotations

import os
import base64
import json
import shutil
import socket
import struct
import subprocess
import asyncio
import urllib.parse
import urllib.request
from typing import Any

from playwright.async_api import async_playwright

from fretio.providers.base import _kill_proc, _register_owned_proc
from fretio.logging_conf import get_logger

logger = get_logger(__name__)


class AlfaBrowserMixin:
    """Métodos de ciclo de vida do browser/CDP/janela do AlfaProvider."""

    @staticmethod
    def _user_data_dir() -> str:
        base = os.path.join(os.path.expanduser("~"), ".fretio", "alfa_browser_data")
        os.makedirs(base, exist_ok=True)
        return base

    @staticmethod
    def _find_chrome() -> str | None:
        """Localiza o executável do Chrome instalado no sistema."""
        candidates = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%UserProfile%\AppData\Local\Google\Chrome\Application\chrome.exe"),
        ]
        # Verifica também no registro (caso Chrome tenha sido instalado em path customizado)
        try:
            import winreg
            for root_key in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    key = winreg.OpenKey(
                        root_key,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                    )
                    path, _ = winreg.QueryValueEx(key, "")
                    winreg.CloseKey(key)
                    if path and os.path.isfile(path):
                        candidates.append(path)
                except OSError:
                    pass
        except ImportError:
            pass
        candidates.extend([shutil.which("chrome"), shutil.which("google-chrome")])
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        return None

    @staticmethod
    def _free_port() -> int:
        """Encontra uma porta TCP livre."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @classmethod
    def _is_internal_browser_url(cls, url: str | None) -> bool:
        lowered = str(url or "").strip().lower()
        if not lowered or lowered == "about:blank":
            return True
        return lowered.startswith((
            "about:",
            "chrome://",
            "chrome-extension://",
            "devtools://",
            "edge://",
        ))

    def _score_browser_url(self, url: str | None) -> tuple[int, str]:
        lowered = str(url or "").strip().lower()
        if self.BASE_URL.lower() in lowered or "alfatransportes.com.br" in lowered:
            return (0, lowered)
        if lowered.startswith(("http://", "https://")):
            return (1, lowered)
        if self._is_internal_browser_url(lowered):
            return (3, lowered)
        return (2, lowered)

    def _select_best_debug_target(self, pages: list[dict[str, Any]]) -> dict[str, Any] | None:
        best_page = None
        best_score = None
        for page in pages:
            if page.get("type") != "page":
                continue
            score = self._score_browser_url(page.get("url"))
            if best_score is None or score < best_score:
                best_page = page
                best_score = score
        return best_page

    def _is_debug_port_live(self) -> bool:
        if not self._debug_port:
            return False
        try:
            with socket.create_connection(("127.0.0.1", self._debug_port), timeout=1):
                return True
        except OSError:
            return False

    @staticmethod
    def _fix_preferences(user_data_dir: str) -> None:
        """Marca exit_type como Normal e limpa sessão para evitar 'Restaurar páginas'."""
        default_dir = os.path.join(user_data_dir, "Default")
        os.makedirs(default_dir, exist_ok=True)

        # Remove arquivos de sessão que disparam "Restaurar páginas?"
        for fname in ("Current Session", "Current Tabs", "Last Session", "Last Tabs"):
            fpath = os.path.join(default_dir, fname)
            try:
                if os.path.isfile(fpath):
                    os.remove(fpath)
            except Exception:
                pass
        sessions_dir = os.path.join(default_dir, "Sessions")
        if os.path.isdir(sessions_dir):
            shutil.rmtree(sessions_dir, ignore_errors=True)

        prefs_path = os.path.join(default_dir, "Preferences")
        prefs = {}
        if os.path.isfile(prefs_path):
            try:
                with open(prefs_path, "r", encoding="utf-8") as f:
                    prefs = json.load(f)
            except Exception:
                prefs = {}
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        prefs["profile"]["exited_cleanly"] = True
        prefs.setdefault("session", {})["restore_on_startup"] = 5
        try:
            with open(prefs_path, "w", encoding="utf-8") as f:
                json.dump(prefs, f)
        except Exception:
            pass

    @staticmethod
    def _kill_stale_chrome(user_data_dir: str) -> None:
        """Mata processos Chrome órfãos que usem o mesmo user-data-dir."""
        import sys
        if sys.platform != "win32":
            return
        try:
            # Usa PowerShell Get-CimInstance (wmic foi removido do Windows moderno)
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                 "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"],
                capture_output=True, text=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            norm = user_data_dir.replace("/", "\\").rstrip("\\").lower()
            for line in result.stdout.splitlines():
                line = line.strip()
                if "|" not in line:
                    continue
                pid_str, cmd = line.split("|", 1)
                if norm in cmd.lower():
                    try:
                        pid = int(pid_str.strip())
                        os.kill(pid, 9)
                        logger.debug("[ALFA] Matou Chrome órfão PID=%d", pid)
                    except (ValueError, OSError):
                        pass
        except Exception as e:
            logger.debug("[ALFA] _kill_stale_chrome falhou: %s", e)

    async def _init_browser(self) -> None:
        """Lança Chrome como subprocess SEM conectar Playwright (exceto headless)."""
        if self.headless:
            if self._context:
                return
            from fretio.providers.base import launch_browser_resilient
            self._browser = await launch_browser_resilient(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1920, "height": 1080},
                locale="pt-BR",
            )
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
            self._page.set_default_timeout(45000)
            return

        # Não-headless: lança Chrome real como processo separado
        if self._chrome_proc and self._chrome_proc.poll() is None:
            return  # Chrome já está rodando

        if self._chrome_proc and self._chrome_proc.poll() is not None:
            if self._is_debug_port_live():
                logger.info("[ALFA] Launcher do Chrome saiu, mas CDP segue ativo; reutilizando sessão existente")
                return
            logger.warning("[ALFA] Chrome morreu (exit=%s), reiniciando...", self._chrome_proc.poll())
            await self._disconnect_playwright()
            self._chrome_proc = None

        chrome_path = self._find_chrome()
        if not chrome_path:
            raise RuntimeError("Chrome não encontrado no sistema")

        self._debug_port = self._free_port()
        user_data = self._user_data_dir()

        # Mata processos Chrome órfãos que ainda travem o user-data-dir
        self._kill_stale_chrome(user_data)

        self._fix_preferences(user_data)
        cmd = [
            chrome_path,
            f"--remote-debugging-port={self._debug_port}",
            f"--user-data-dir={user_data}",
            "--no-first-run",
            "--no-default-browser-check",
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
            "--window-position=-3000,-3000",
            "--window-size=900,760",
            self.login_url,
        ]
        logger.info(f"[ALFA] Lançando Chrome real na porta {self._debug_port}")
        self._chrome_proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        _register_owned_proc(self._chrome_proc, source="alfa")
        for _ in range(30):
            await asyncio.sleep(0.5)
            try:
                with socket.create_connection(("127.0.0.1", self._debug_port), timeout=1):
                    break
            except OSError:
                continue
        else:
            logger.warning("[ALFA] Timeout esperando Chrome debug port")

        # Oculta da barra de tarefas imediatamente (Chrome nasce off-screen)
        self._set_taskbar_visible(False)

    async def _connect_playwright(self) -> None:
        """Conecta Playwright ao Chrome já rodando via CDP."""
        if self._context:
            return
        last_err = None
        for _attempt in range(3):
            try:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{self._debug_port}"
                )
                best_context = None
                best_page = None
                best_score = None
                for context in self._browser.contexts:
                    for page in context.pages:
                        score = self._score_browser_url(getattr(page, "url", ""))
                        if best_score is None or score < best_score:
                            best_context = context
                            best_page = page
                            best_score = score
                self._context = best_context or self._browser.contexts[0]
                self._page = best_page or (self._context.pages[0] if self._context.pages else await self._context.new_page())
                self._page.set_default_timeout(30000)

                # Bloqueia widgets pesados e rastreadores (sz.chat, google-analytics, etc.) sem afetar o Turnstile/Alfa
                async def _alfa_route_filter(route):
                    req_url = route.request.url.lower()
                    if "cloudflare" in req_url or "turnstile" in req_url or "alfatransportes" in req_url:
                        await route.continue_()
                    elif any(b in req_url for b in ["sz.chat", "google-analytics", "googletagmanager", "facebook", "doubleclick", "clarity"]):
                        await route.abort()
                    else:
                        await route.continue_()

                try:
                    await self._context.route("**/*", _alfa_route_filter)
                except Exception:
                    pass

                logger.info("[ALFA] Playwright conectado ao Chrome via CDP")
                return
            except Exception as e:
                last_err = e
                logger.warning("[ALFA] CDP tentativa %d/3 falhou: %s", _attempt + 1, e)
                try:
                    if self._playwright:
                        await self._playwright.stop()
                        self._playwright = None
                except Exception:
                    pass
                if _attempt < 2:
                    await asyncio.sleep(1 + _attempt)
        raise RuntimeError(f"[ALFA] Falha ao conectar CDP apos 3 tentativas: {last_err}")

    async def _disconnect_playwright(self) -> None:
        """Desconecta Playwright do Chrome sem fechar o navegador."""
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._context = None
        self._browser = None
        self._playwright = None
        self._page = None
        self._cdp_session = None
        self._window_id = None

    def _get_page_url_sync(self) -> str:
        """Obtém URL da aba ativa via Chrome DevTools HTTP API (sem Playwright)."""
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._debug_port}/json",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                pages = json.loads(resp.read())
            best_page = self._select_best_debug_target(pages)
            if best_page:
                url = str(best_page.get("url", "") or "")
                if url and not self._is_internal_browser_url(url):
                    return url
        except Exception:
            pass
        return ""

    async def _cdp_eval_raw(self, expression: str):
        """Executa JavaScript via CDP WebSocket direto (sem Playwright).

        Conecta, executa Runtime.evaluate e retorna o resultado confirmado.
        """
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._debug_port}/json",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                pages = json.loads(resp.read())
            best_page = self._select_best_debug_target(pages)
            ws_url = best_page.get("webSocketDebuggerUrl") if best_page else None
            if not ws_url:
                return

            parsed = urllib.parse.urlparse(ws_url)
            host = parsed.hostname
            port = parsed.port
            path = parsed.path

            key = base64.b64encode(os.urandom(16)).decode()
            reader, writer = await asyncio.open_connection(host, port)
            handshake = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"\r\n"
            )
            writer.write(handshake.encode())
            await writer.drain()

            response = b""
            while b"\r\n\r\n" not in response:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=5)
                if not chunk:
                    break
                response += chunk
            if b"101" not in response:
                writer.close()
                return

            msg = json.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {"expression": expression, "returnByValue": True},
            })
            await self._ws_send_text(writer, msg)
            value = None
            try:
                async with asyncio.timeout(5):
                    while True:
                        header = await reader.readexactly(2)
                        opcode = header[0] & 15
                        length = header[1] & 127
                        if length == 126:
                            length = struct.unpack(">H", await reader.readexactly(2))[0]
                        elif length == 127:
                            length = struct.unpack(">Q", await reader.readexactly(8))[0]
                        mask = await reader.readexactly(4) if header[1] & 128 else None
                        payload = await reader.readexactly(length)
                        if mask:
                            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                        if opcode == 8:
                            break
                        if opcode != 1:
                            continue
                        reply = json.loads(payload)
                        if reply.get("id") == 1:
                            result = reply.get("result", {})
                            if "error" not in reply and "exceptionDetails" not in result:
                                value = result.get("result", {}).get("value")
                            break
            finally:
                writer.close()
                await writer.wait_closed()
            return value

        except Exception as e:
            logger.debug("[ALFA] CDP eval raw falhou: %s", type(e).__name__)

    @staticmethod
    async def _ws_send_text(writer, text: str) -> None:
        """Envia um frame de texto WebSocket."""
        data = text.encode("utf-8")
        mask_key = os.urandom(4)
        frame = bytearray()
        frame.append(0x81)  # FIN + opcode text
        length = len(data)
        if length <= 125:
            frame.append(0x80 | length)
        elif length <= 65535:
            frame.append(0x80 | 126)
            frame.extend(struct.pack(">H", length))
        else:
            frame.append(0x80 | 127)
            frame.extend(struct.pack(">Q", length))
        frame.extend(mask_key)
        masked = bytearray(len(data))
        for i in range(len(data)):
            masked[i] = data[i] ^ mask_key[i % 4]
        frame.extend(masked)
        writer.write(bytes(frame))
        await writer.drain()

    def _set_taskbar_visible(self, visible: bool) -> None:
        """Mostra/oculta a janela do Chrome na barra de tarefas (Windows)."""
        import sys
        if sys.platform != "win32" or not self._chrome_proc:
            return
        try:
            import ctypes
            import ctypes.wintypes as wt

            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000
            SW_HIDE = 0
            SW_SHOWNOACTIVATE = 4

            root_pid = self._chrome_proc.pid
            pids = {root_pid}
            # Busca TODOS os descendentes (multi-nível) do processo Chrome
            try:
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"function Get-Desc($p){{ Get-CimInstance Win32_Process -Filter \"ParentProcessId=$p\" | "
                     f"ForEach-Object {{ $_.ProcessId; Get-Desc $_.ProcessId }} }}; Get-Desc {root_pid}"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                for line in result.stdout.splitlines():
                    stripped = line.strip()
                    if stripped.isdigit():
                        pids.add(int(stripped))
            except Exception:
                pass

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
            count = [0]

            def _enum_cb(hwnd, _lparam):
                # Verifica todas as janelas (visíveis E ocultas que possam reaparecer)
                tid = wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(tid))
                if tid.value in pids:
                    # Só processa janelas top-level com texto (não Chrome internals)
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0 or user32.IsWindowVisible(hwnd):
                        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                        if visible:
                            nex = (ex | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
                        else:
                            nex = (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
                        if nex != ex:
                            user32.ShowWindow(hwnd, SW_HIDE)
                            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, nex)
                            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                            count[0] += 1
                return True

            user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
            logger.debug("[ALFA] Taskbar visibility=%s (%d janelas)", visible, count[0])
        except Exception as e:
            logger.debug(f"[ALFA] _set_taskbar_visible falhou: {e}")

    def _ensure_chrome_visible(self) -> None:
        """Garante que a janela do Chrome esteja visível para o usuário (Win32)."""
        import sys
        if sys.platform != "win32" or not self._chrome_proc:
            return
        try:
            import ctypes
            import ctypes.wintypes as wt

            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000
            SW_RESTORE = 9
            SWP_SHOWWINDOW = 0x0040

            root_pid = self._chrome_proc.pid
            pids = {root_pid}
            try:
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"function Get-Desc($p){{ Get-CimInstance Win32_Process -Filter \"ParentProcessId=$p\" | "
                     f"ForEach-Object {{ $_.ProcessId; Get-Desc $_.ProcessId }} }}; Get-Desc {root_pid}"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                for line in result.stdout.splitlines():
                    stripped = line.strip()
                    if stripped.isdigit():
                        pids.add(int(stripped))
            except Exception:
                pass

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
            found = []

            def _enum_cb(hwnd, _lparam):
                tid = wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(tid))
                if tid.value in pids:
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        # Filtra janelas pequenas (popups/dialogs como "Restaurar páginas?")
                        rect = wt.RECT()
                        user32.GetWindowRect(hwnd, ctypes.byref(rect))
                        w = rect.right - rect.left
                        h = rect.bottom - rect.top
                        if w >= 500 and h >= 400:
                            found.append(hwnd)
                        elif user32.IsWindowVisible(hwnd):
                            # Esconde popups/dialogs do Chrome
                            user32.ShowWindow(hwnd, 0)  # SW_HIDE
                            logger.debug("[ALFA] Ocultou popup Chrome %dx%d", w, h)
                return True

            user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)

            KEYEVENTF_EXTENDEDKEY = 0x0001
            KEYEVENTF_KEYUP = 0x0002
            VK_MENU = 0x12
            kernel32 = ctypes.windll.kernel32

            for hwnd in found:
                ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                nex = (ex | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
                if nex != ex:
                    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, nex)
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.SetWindowPos(hwnd, 0, 200, 100, 900, 760, SWP_SHOWWINDOW)
                # AttachThreadInput + keybd_event trick para Win10/11
                fg_hwnd = user32.GetForegroundWindow()
                fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None)
                our_tid = kernel32.GetCurrentThreadId()
                attached = False
                if fg_tid != our_tid:
                    attached = bool(user32.AttachThreadInput(our_tid, fg_tid, True))
                user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY, 0)
                user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                if attached:
                    user32.AttachThreadInput(our_tid, fg_tid, False)

            logger.debug("[ALFA] Chrome janela posicionada para visivel (%d janelas)", len(found))
        except Exception as e:
            logger.debug(f"[ALFA] _ensure_chrome_visible falhou: {e}")

    def _move_window_win32(self, left: int, top: int, width: int, height: int) -> bool:
        """Move a janela do Chrome via Win32 API (fallback quando CDP falha)."""
        import sys
        if sys.platform != "win32" or not self._chrome_proc:
            return False
        try:
            import ctypes
            import ctypes.wintypes as wt

            user32 = ctypes.windll.user32
            SWP_NOACTIVATE = 0x0010

            root_pid = self._chrome_proc.pid
            pids = {root_pid}
            try:
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"function Get-Desc($p){{ Get-CimInstance Win32_Process -Filter \"ParentProcessId=$p\" | "
                     f"ForEach-Object {{ $_.ProcessId; Get-Desc $_.ProcessId }} }}; Get-Desc {root_pid}"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                for line in result.stdout.splitlines():
                    stripped = line.strip()
                    if stripped.isdigit():
                        pids.add(int(stripped))
            except Exception:
                pass

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
            moved = [0]

            def _enum_cb(hwnd, _lparam):
                tid = wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(tid))
                if tid.value in pids:
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        rect = wt.RECT()
                        user32.GetWindowRect(hwnd, ctypes.byref(rect))
                        w = rect.right - rect.left
                        h = rect.bottom - rect.top
                        if w >= 400 and h >= 300:
                            user32.SetWindowPos(hwnd, 0, left, top, width, height, SWP_NOACTIVATE)
                            moved[0] += 1
                return True

            user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
            return moved[0] > 0
        except Exception as e:
            logger.debug(f"[ALFA] _move_window_win32 falhou: {e}")
            return False

    async def _ocultar_janela(self) -> None:
        if self.headless:
            return
        cdp_ok = False
        try:
            if not self._cdp_session:
                self._cdp_session = await self._context.new_cdp_session(self._page)
            if not self._window_id:
                resp = await self._cdp_session.send("Browser.getWindowForTarget")
                self._window_id = resp.get("windowId")
            if self._window_id:
                await self._cdp_session.send(
                    "Browser.setWindowBounds",
                    {"windowId": self._window_id, "bounds": {"windowState": "normal"}},
                )
                await self._cdp_session.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": self._window_id,
                        "bounds": {"left": -3000, "top": -3000, "width": 1920, "height": 1080},
                    },
                )
                cdp_ok = True
                logger.debug("[ALFA] Janela movida off-screen via CDP")
        except Exception as e:
            logger.debug(f"[ALFA] CDP ocultar falhou (esperado em modo subprocess): {e}")
        if not cdp_ok:
            if self._move_window_win32(-3000, -3000, 1920, 1080):
                logger.debug("[ALFA] Janela movida off-screen via Win32")
        self._set_taskbar_visible(False)

    async def _mostrar_janela(self) -> None:
        if self.headless:
            return
        w, h = 900, 760
        left = max((1920 - w) // 2, 0)
        top = max((1080 - h) // 2, 0)
        cdp_ok = False
        try:
            if not self._cdp_session:
                self._cdp_session = await self._context.new_cdp_session(self._page)
            if not self._window_id:
                resp = await self._cdp_session.send("Browser.getWindowForTarget")
                self._window_id = resp.get("windowId")
            if self._window_id:
                await self._cdp_session.send(
                    "Browser.setWindowBounds",
                    {"windowId": self._window_id, "bounds": {"windowState": "normal"}},
                )
                await self._cdp_session.send(
                    "Browser.setWindowBounds",
                    {"windowId": self._window_id, "bounds": {"left": left, "top": top, "width": w, "height": h}},
                )
                await self._page.bring_to_front()
                cdp_ok = True
                logger.debug("[ALFA] Janela visivel para login via CDP")
        except Exception as e:
            logger.debug(f"[ALFA] CDP mostrar falhou (esperado em modo subprocess): {e}")
        if not cdp_ok:
            if self._move_window_win32(left, top, w, h):
                logger.debug("[ALFA] Janela visivel para login via Win32")
        self._set_taskbar_visible(True)
        self._ensure_chrome_visible()

    async def cleanup(self) -> None:
        try:
            if self._page and not self._page.is_closed():
                await self._page.close()
        except Exception:
            pass
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser and self._browser.is_connected():
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        # Encerra o Chrome lançado como subprocess de forma limpa
        if self._chrome_proc:
            # Tenta fechar via CDP (graceful) para evitar "Restaurar páginas"
            if self._debug_port:
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{self._debug_port}/json/version",
                        headers={"Accept": "application/json"},
                    )
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        data = json.loads(resp.read())
                    ws_url = data.get("webSocketDebuggerUrl")
                    if ws_url:
                        # Envia Browser.close via CDP
                        parsed = urllib.parse.urlparse(ws_url)
                        key = base64.b64encode(os.urandom(16)).decode()
                        reader, writer = await asyncio.open_connection(
                            parsed.hostname, parsed.port
                        )
                        handshake = (
                            f"GET {parsed.path} HTTP/1.1\r\n"
                            f"Host: {parsed.hostname}:{parsed.port}\r\n"
                            f"Upgrade: websocket\r\n"
                            f"Connection: Upgrade\r\n"
                            f"Sec-WebSocket-Key: {key}\r\n"
                            f"Sec-WebSocket-Version: 13\r\n"
                            f"\r\n"
                        )
                        writer.write(handshake.encode())
                        await writer.drain()
                        response = b""
                        while b"\r\n\r\n" not in response:
                            chunk = await asyncio.wait_for(reader.read(4096), timeout=3)
                            if not chunk:
                                break
                            response += chunk
                        if b"101" in response:
                            msg = json.dumps({"id": 1, "method": "Browser.close", "params": {}})
                            await self._ws_send_text(writer, msg)
                            await asyncio.sleep(1)
                        writer.close()
                        try:
                            await writer.wait_closed()
                        except Exception:
                            pass
                except Exception:
                    pass
            # Garante encerramento
            _kill_proc(self._chrome_proc)
            # Marca como encerrado limpo
            try:
                self._fix_preferences(self._user_data_dir())
            except Exception:
                pass
        self._context = None
        self._browser = None
        self._playwright = None
        self._page = None
        self._logged_in = False
        self._cdp_session = None
        self._window_id = None
        self._chrome_proc = None
