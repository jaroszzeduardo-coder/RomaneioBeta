"""Mixin de browser/sessão/cleanup do provider Braspress (métodos movidos de braspress_playwright.py)."""
from fretio.logging_conf import get_logger

logger = get_logger(__name__)


class BraspressBrowserMixin:
    async def _init_browser(self):
        if self._browser:
            if self._browser.is_connected():
                return
            logger.warning("[Braspress] Browser desconectado, reinicializando...")
            await self.cleanup()
        from fretio.providers.base import launch_browser_resilient
        self._browser = await launch_browser_resilient(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
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
