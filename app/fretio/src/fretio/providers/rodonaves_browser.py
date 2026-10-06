"""Mixin de browser/CDP/janela/captcha/cleanup do provider Rodonaves (métodos movidos de rodonaves.py)."""
from contextlib import asynccontextmanager
import asyncio
import json
import os
import socket
import subprocess
from playwright.async_api import async_playwright
from fretio.providers.base import find_chrome, _find_free_port, _kill_proc, _register_owned_proc
from fretio.providers._win_taskbar import (
    ocultar_taskbar_por_pagina,
    posicionar_janela_por_pagina,
    posicionar_janela_por_pid,
)
from fretio.providers.provider_utils import get_stealth_script
from fretio.logging_conf import get_logger

logger = get_logger(__name__)

# User-Agent completo de Chrome real (evita sinal de bot no reCAPTCHA)
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)

# Script de stealth injetado antes de qualquer página carregar.
# Remove sinais de automação que o reCAPTCHA usa para escalar dificuldade.
_STEALTH_JS = get_stealth_script()


class RodonavesBrowserMixin:
    @staticmethod
    def _user_data_dir() -> str:
        """Diretório persistente exclusivo da Rodonaves para a sessão CDP."""
        base = os.path.join(os.path.expanduser("~"), ".fretio", "rodonaves_browser_data")
        os.makedirs(base, exist_ok=True)
        return base

    @staticmethod
    def _launcher_exit_can_still_spawn_browser(exit_code: int | None) -> bool:
        return exit_code == 0

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

    def _score_page_url(self, url: str | None) -> tuple[int, str]:
        lowered = str(url or "").strip().lower()
        if f"{self.BASE_URL.lower()}/cotacao" in lowered:
            return (0, lowered)
        if self.BASE_URL.lower() in lowered:
            return (1, lowered)
        if lowered.startswith(("http://", "https://")):
            return (2, lowered)
        if self._is_internal_browser_url(lowered):
            return (4, lowered)
        return (3, lowered)

    async def _select_best_context_page(self) -> tuple[object | None, object | None]:
        best_context = None
        best_page = None
        best_score = None
        for context in getattr(self._browser, "contexts", []) or []:
            for page in getattr(context, "pages", []) or []:
                score = self._score_page_url(getattr(page, "url", ""))
                if best_score is None or score < best_score:
                    best_context = context
                    best_page = page
                    best_score = score
        return best_context, best_page

    async def _sync_active_page(self) -> None:
        if not self._browser:
            return
        best_context, best_page = await self._select_best_context_page()
        if best_context is not None:
            self._context = best_context
        elif self._context is None:
            self._context = self._browser.contexts[0] if self._browser.contexts else None
        if best_page is not None:
            self._page = best_page
        elif self._context is not None and self._page is None:
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

    @staticmethod
    def _captcha_window_bounds(screen_w: int, screen_h: int) -> tuple[int, int, int, int]:
        w, h = 820, 720
        left = max(0, (int(screen_w) - w) // 2)
        top = max(0, (int(screen_h) - h) // 2)
        return left, top, w, h

    async def _reposicionar_janela_win32(
        self,
        *,
        left: int,
        top: int,
        width: int,
        height: int,
        bring_to_front: bool,
    ) -> bool:
        moved = await posicionar_janela_por_pagina(
            self._page,
            left=left,
            top=top,
            width=width,
            height=height,
            bring_to_front=bring_to_front,
        )
        if moved:
            return True
        for pid in self._candidate_window_pids():
            if posicionar_janela_por_pid(
                pid,
                left=left,
                top=top,
                width=width,
                height=height,
                bring_to_front=bring_to_front,
            ):
                logger.debug(f"[{self.nome}] Janela reposicionada via PID %d", pid)
                return True
        return False

    def _candidate_window_pids(self) -> list[int]:
        pids: list[int] = []
        pid_launcher = getattr(self._chrome_proc, "pid", 0) or 0
        if pid_launcher > 0:
            pids.append(int(pid_launcher))
        for pid in self._listar_pids_chrome_por_user_data_dir(self._active_user_data_dir):
            if pid > 0 and pid not in pids:
                pids.append(pid)
        return pids

    @staticmethod
    def _listar_pids_chrome_por_user_data_dir(user_data_dir: str) -> list[int]:
        if os.name != "nt":
            return []
        norm = str(user_data_dir or "").replace("/", "\\").rstrip("\\").lower()
        if not norm:
            return []
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                    "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            logger.debug(f"[RodonavesProvider] Falha ao listar PIDs do Chrome: {e}")
            return []

        pids: list[int] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if "|" not in line:
                continue
            pid_str, cmd = line.split("|", 1)
            if norm not in cmd.lower():
                continue
            try:
                pid = int(pid_str.strip())
            except ValueError:
                continue
            if pid > 0 and pid not in pids:
                pids.append(pid)
        return pids

    def _has_live_browser_session(self) -> bool:
        try:
            return bool(self._context and self._browser and self._browser.is_connected())
        except Exception:
            return False

    @staticmethod
    def _fix_preferences(user_data_dir: str) -> None:
        """Marca exit_type como Normal e limpa sessão para evitar 'Restaurar páginas'."""
        import shutil as _shutil
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
            _shutil.rmtree(sessions_dir, ignore_errors=True)

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
        import sys as _sys
        import subprocess as _subprocess
        if _sys.platform != "win32":
            return
        try:
            result = _subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                 "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"],
                capture_output=True, text=True, timeout=15,
                creationflags=_subprocess.CREATE_NO_WINDOW,
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
                        logger.debug("[RODONAVES] Matou Chrome órfão PID=%d", pid)
                    except (ValueError, OSError):
                        pass
        except Exception as e:
            logger.debug("[RODONAVES] _kill_stale_chrome falhou: %s", e)

    async def _init_browser(self):
        if self._context:
            # Verifica se o processo Chrome ainda está vivo
            if self._chrome_proc and self._chrome_proc.poll() is not None:
                if self._has_live_browser_session():
                    logger.info(f"[{self.nome}] Launcher saiu, mas sessão CDP segue ativa; reutilizando browser")
                    await self._ocultar_janela()
                    return
                logger.warning(f"[{self.nome}] Chrome process morreu (exit={self._chrome_proc.returncode}), reinicializando...")
                await self.cleanup()
            elif self._browser and not self._browser.is_connected():
                logger.warning(f"[{self.nome}] Browser desconectado, reinicializando...")
                await self.cleanup()
            else:
                await self._ocultar_janela()
                return
        await self._init_browser_inner()

    async def _init_browser_inner(self):
        """Lança Chrome via CDP, em headless quando configurado."""
        import shutil as _shutil
        chrome_path = find_chrome()

        for launch_attempt in range(2):
            port = _find_free_port()
            udd = self._user_data_dir()
            self._kill_stale_chrome(udd)
            self._fix_preferences(udd)
            self._profile_tmp = None

            launch_args = [
                chrome_path,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={udd}",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
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
                "--window-position=-32000,-32000",
                "--window-size=1920,1080",
                "--disable-session-crashed-bubble",
                "--disable-features=InfiniteSessionRestore,Translate,OptimizationHints,MediaRouter,IsolateOrigins,site-per-process,TranslateUI",
                "--hide-crash-restore-bubble",
                "--noerrdialogs",
                "--disable-infobars",
                "--enable-features=NetworkService,NetworkServiceInProcess",
                "--disable-component-extensions-with-background-pages",
            ]
            if self.headless:
                launch_args.append("--headless=new")

            self._chrome_proc = subprocess.Popen(
                launch_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            _register_owned_proc(self._chrome_proc, source="rodonaves")

            chrome_ok = False
            for _ in range(50):
                await asyncio.sleep(0.1)
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                        chrome_ok = True
                        break
                except (ConnectionRefusedError, OSError):
                    exit_code = self._chrome_proc.poll()
                    if exit_code is None:
                        continue
                    if self._launcher_exit_can_still_spawn_browser(exit_code):
                        continue
                    break

            if chrome_ok:
                self._effective_headless = self.headless
                self._active_user_data_dir = udd
                break

            exit_code = self._chrome_proc.returncode if self._chrome_proc.poll() is not None else None
            _kill_proc(self._chrome_proc)
            self._chrome_proc = None

            if launch_attempt == 0:
                logger.warning(
                    f"[{self.nome}] Chrome crashou (exit {exit_code}), "
                    f"limpando perfil e tentando novamente..."
                )
                try:
                    _shutil.rmtree(udd, ignore_errors=True)
                except Exception:
                    pass
                await asyncio.sleep(0.5)
                continue

            raise RuntimeError(
                f"Chrome (Rodonaves) encerrou inesperadamente (exit {exit_code})"
            )

        # Conecta Playwright via CDP com retry (driver Node.js pode crashar)
        last_err = None
        for _attempt in range(3):
            try:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                logger.info(f"[{self.nome}] Chrome conectado via CDP porta {port} (headless={self._effective_headless})")
                break
            except Exception as e:
                last_err = e
                logger.warning(f"[{self.nome}] CDP tentativa {_attempt+1}/3 falhou: {e}")
                try:
                    if self._playwright:
                        await self._playwright.stop()
                        self._playwright = None
                except Exception:
                    pass
                if _attempt < 2:
                    await asyncio.sleep(1 + _attempt)
        else:
            _kill_proc(self._chrome_proc)
            raise RuntimeError(f"Falha ao conectar CDP apos 3 tentativas: {last_err}")

        self._context = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=_CHROME_UA,
            locale="pt-BR",
        )
        await self._sync_active_page()
        if self._page is None:
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

        if not self._effective_headless and not await self._definir_janela_offscreen_inicial():
            _kill_proc(self._chrome_proc)
            self._chrome_proc = None
            await self.cleanup()
            raise RuntimeError(
                f"{self.nome}: bootstrap falhou — não foi possível confirmar a janela off-screen via CDP"
            )

        await self._context.add_init_script(_STEALTH_JS)
        try:
            await self._page.evaluate(_STEALTH_JS)
        except Exception:
            pass

    async def _ocultar_janela(self):
        """Move a janela para coordenadas fora da tela (invisível)."""
        if self._effective_headless:
            return
        try:
            await self._sync_active_page()
            if not self._cdp_session:
                self._cdp_session = await self._context.new_cdp_session(self._page)
            if not self._window_id:
                resp = await self._cdp_session.send("Browser.getWindowForTarget")
                self._window_id = resp.get("windowId")
            if self._window_id:
                await self._cdp_session.send("Browser.setWindowBounds", {
                    "windowId": self._window_id,
                    "bounds": {"left": -32000, "top": -32000, "width": 1920, "height": 1080},
                })
                logger.debug(f"[{self.nome}] Janela movida off-screen")
            await ocultar_taskbar_por_pagina(self._page)
        except Exception as e:
            logger.warning(f"[{self.nome}] Falha ao ocultar janela: {e}")

    async def _definir_janela_offscreen_inicial(self) -> bool:
        try:
            await self._sync_active_page()
            if not self._context or not self._page:
                return False
            if not self._cdp_session:
                self._cdp_session = await self._context.new_cdp_session(self._page)
            if not self._window_id:
                resp = await self._cdp_session.send("Browser.getWindowForTarget")
                self._window_id = resp.get("windowId")
            if not self._window_id:
                logger.warning(
                    f"[{self.nome}] Bootstrap off-screen falhou: sem windowId do CDP"
                )
                return False
            await self._cdp_session.send("Browser.setWindowBounds", {
                "windowId": self._window_id,
                "bounds": {"left": -32000, "top": -32000, "width": 1920, "height": 1080},
            })
            bounds = await self._cdp_session.send("Browser.getWindowBounds", {
                "windowId": self._window_id,
            })
            bounds_block = (bounds or {}).get("bounds", {}) if isinstance(bounds, dict) else {}
            if int(bounds_block.get("left", 0)) >= 0 or int(bounds_block.get("top", 0)) >= 0:
                logger.warning(
                    f"[{self.nome}] Bootstrap off-screen não foi confirmado pelo CDP: {bounds_block}"
                )
                return False
            await ocultar_taskbar_por_pagina(self._page)
            return True
        except Exception as exc:
            logger.warning(f"[{self.nome}] Falha ao definir off-screen inicial: {exc}")
            return False

    @asynccontextmanager
    async def _janela_visivel_para_captcha(self):
        # Regra de negócio: o Chrome real/headful só pode ficar visível
        # enquanto houver interação humana (CAPTCHA). O ``finally`` garante
        # o re-hide mesmo se o bloco levantar exceção.
        if self._effective_headless:
            yield False
            return

        self._window_visible_for_captcha = True
        try:
            try:
                shown = await self._mostrar_janela()
            except Exception as exc:
                logger.warning(f"[{self.nome}] Falha ao mostrar janela para CAPTCHA: {exc}")
                shown = False
            yield shown
        finally:
            self._window_visible_for_captcha = False
            try:
                await self._ocultar_janela()
            except Exception as exc:
                logger.warning(f"[{self.nome}] Falha ao ocultar janela após CAPTCHA: {exc}")

    async def _mostrar_janela(self):
        """Mostra janela compacta centralizada na tela, só para o CAPTCHA."""
        await self._sync_active_page()
        shown = False
        try:
            if not self._cdp_session:
                self._cdp_session = await self._context.new_cdp_session(self._page)
            if not self._window_id:
                resp = await self._cdp_session.send("Browser.getWindowForTarget")
                self._window_id = resp.get("windowId")
            if self._window_id:
                try:
                    screen_info = await self._page.evaluate(
                        "() => ({ w: screen.width, h: screen.height })"
                    )
                    screen_w = int(screen_info.get("w", 1920))
                    screen_h = int(screen_info.get("h", 1080))
                except Exception:
                    screen_w, screen_h = 1920, 1080

                left, top, w, h = self._captcha_window_bounds(screen_w, screen_h)
                await self._cdp_session.send("Browser.setWindowBounds", {
                    "windowId": self._window_id,
                    "bounds": {"windowState": "normal"},
                })
                await self._cdp_session.send("Browser.setWindowBounds", {
                    "windowId": self._window_id,
                    "bounds": {"left": left, "top": top, "width": w, "height": h},
                })
                await ocultar_taskbar_por_pagina(self._page)
                await posicionar_janela_por_pagina(
                    self._page,
                    left=left,
                    top=top,
                    width=w,
                    height=h,
                    bring_to_front=True,
                )
                logger.debug(
                    f"[{self.nome}] Janela compacta (CAPTCHA) visível em ({left},{top}) tela {screen_w}x{screen_h}"
                )
                shown = True

            try:
                await self._page.locator("#calculateQuotationBtn").scroll_into_view_if_needed()
            except Exception:
                pass
            try:
                captcha_iframe = self._page.locator("iframe[title*='reCAPTCHA'], iframe[src*='recaptcha']")
                if await captcha_iframe.count() > 0:
                    await captcha_iframe.first.scroll_into_view_if_needed()
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"[{self.nome}] Falha ao mostrar janela via CDP: {e}")

        if shown:
            return True

        try:
            screen_info = await self._page.evaluate("() => ({ w: screen.width, h: screen.height })")
            screen_w = int(screen_info.get("w", 1920))
            screen_h = int(screen_info.get("h", 1080))
        except Exception:
            screen_w, screen_h = 1920, 1080

        left, top, w, h = self._captcha_window_bounds(screen_w, screen_h)
        shown = await self._reposicionar_janela_win32(
            left=left,
            top=top,
            width=w,
            height=h,
            bring_to_front=True,
        )
        if shown:
            await ocultar_taskbar_por_pagina(self._page)
            logger.debug(f"[{self.nome}] Janela compacta (CAPTCHA) visível via Win32 em ({left},{top}) tela {screen_w}x{screen_h}")
            return True
        return False

    async def cleanup(self):
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
        _kill_proc(self._chrome_proc)
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        # Marca sessão como limpa para evitar "Restaurar páginas?" na próxima abertura
        try:
            profile_dir = self._active_user_data_dir or self._user_data_dir()
            self._fix_preferences(profile_dir)
        except Exception:
            pass
        if getattr(self, "_profile_tmp", None):
            import shutil as _shutil
            _shutil.rmtree(self._profile_tmp, ignore_errors=True)
            self._profile_tmp = None
        self._context = None
        self._browser = None
        self._page = None
        self._playwright = None
        self._logged_in = False
        self._cdp_session = None
        self._window_id = None
        self._chrome_proc = None
        self._active_user_data_dir = ""
