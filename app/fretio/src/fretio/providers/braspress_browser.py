"""Mixin de browser/sessão/cleanup do provider Braspress (métodos movidos de braspress_playwright.py)."""
import subprocess

from fretio.logging_conf import get_logger

logger = get_logger(__name__)


def _encerrar_chrome_do_perfil(profile_dir: str) -> None:
    """Fecha o Chrome aberto para o captcha. O processo inicial já pode ter saído."""
    if not profile_dir:
        return
    needle = str(profile_dir).replace("'", "''")
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name = 'chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{needle}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except Exception:
        pass


class BraspressBrowserMixin:
    def _headless_efetivo(self) -> bool:
        # Turnstile do login precisa de janela real. `_captcha_visivel` abre essa
        # janela sem alterar `self.headless`, senão a sessão é recriada na próxima cotação.
        return bool(self.headless) and not bool(getattr(self, "_captcha_visivel", False))

    async def _init_browser(self):
        if self._browser:
            if self._browser.is_connected():
                return
            logger.warning("[Braspress] Browser desconectado, reinicializando...")
            await self.cleanup()
        from fretio.providers.base import launch_browser_resilient
        self._browser = await launch_browser_resilient(
            headless=self._headless_efetivo(),
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            locale="pt-BR",
        )
        from fretio.providers.provider_utils import apply_performance_route_filters
        await apply_performance_route_filters(self._context)
        self._page = await self._context.new_page()
        self._page.set_default_timeout(30000)

    async def _mostrar_janela_captcha(self) -> None:
        """Traz o Chrome para a frente só enquanto a pessoa conclui o Turnstile."""
        from fretio.providers._win_taskbar import posicionar_janela_por_pagina

        self._janela_captcha_aberta = True
        try:
            await self._page.evaluate(
                "document.title = 'Fretio — conclua o captcha da Braspress'"
            )
        except Exception:
            pass
        try:
            screen = await self._page.evaluate(
                "() => ({ w: screen.availWidth || screen.width, h: screen.availHeight || screen.height })"
            )
            screen_w = int(screen.get("w") or 1280)
            screen_h = int(screen.get("h") or 800)
        except Exception:
            screen_w, screen_h = 1280, 800
        width, height = 980, 760
        left = max(0, (screen_w - width) // 2)
        top = max(0, (screen_h - height) // 2)
        shown = await posicionar_janela_por_pagina(
            self._page,
            left=left,
            top=top,
            width=width,
            height=height,
            bring_to_front=True,
        )
        if not shown:
            try:
                await self._page.bring_to_front()
            except Exception:
                pass
        try:
            widget = self._page.locator(".cf-turnstile")
            if await widget.count() > 0:
                await widget.first.scroll_into_view_if_needed()
        except Exception:
            pass
        logger.info("[Braspress] Janela aberta para concluir o captcha do login")

    async def _ocultar_janela_captcha(self) -> None:
        if not getattr(self, "_janela_captcha_aberta", False):
            return
        from fretio.providers._win_taskbar import posicionar_janela_por_pagina

        try:
            await posicionar_janela_por_pagina(
                self._page,
                left=-3000,
                top=-3000,
                width=1200,
                height=800,
                bring_to_front=False,
            )
        except Exception:
            pass
        self._janela_captcha_aberta = False

    async def pre_login(self):
        """Inicializa browser e faz login antecipadamente."""
        await self._init_browser()
        await self._login()

    async def cleanup(self):
        """Fecha o browser."""
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
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._browser = None
        self._page = None
        self._context = None
        self._logged_in = False
        proc = getattr(self, "_chrome_proc", None)
        profile = getattr(self, "_profile_dir", None)
        if proc is not None:
            from fretio.browser.process_guard import _kill_proc
            _kill_proc(proc)
            self._chrome_proc = None
        if profile:
            _encerrar_chrome_do_perfil(profile)
            self._profile_dir = None
