"""Mixin de browser/sessão/cleanup do provider TRD (Senior X)."""
from __future__ import annotations
from typing import TYPE_CHECKING, Optional
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
if TYPE_CHECKING:
    from playwright.async_api import Frame
from fretio.logging_conf import get_logger

logger = get_logger(__name__)


class TRDBrowserMixin:
    """Métodos de ciclo de vida do browser/sessão do TRDProvider."""

    async def _init_browser(self):
        """Inicializa browser Playwright."""
        if self._browser:
            if self._browser.is_connected():
                return
            logger.warning(f"[{self.nome}] Browser desconectado, reinicializando...")
            await self.cleanup()

        from fretio.providers.base import launch_browser_resilient
        self._browser = await launch_browser_resilient(
            headless=self.headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--enable-features=NetworkService,NetworkServiceInProcess',
                '--disable-features=IsolateOrigins,site-per-process,TranslateUI',
            ],
        )
        self._context = await self._browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/133.0.0.0 Safari/537.36'
            ),
        )
        from fretio.providers.provider_utils import apply_performance_route_filters
        await apply_performance_route_filters(self._context)
        self._page = await self._context.new_page()
        self._login_frame = None
        await self._page.add_init_script(self._STEALTH_JS)

    @staticmethod
    def _is_logged_in_url(url: Optional[str]) -> bool:
        current_url = str(url or "").lower()
        if not current_url or "platform.senior.com.br" not in current_url:
            return False
        if "/login" in current_url:
            return False
        return any(token in current_url for token in ("senior-x/#/", "/senior-x/", "documentos-frontend/#/"))

    @staticmethod
    def _is_transient_sso_url(url: Optional[str]) -> bool:
        current_url = str(url or "").lower()
        if not current_url:
            return False
        return any(
            token in current_url
            for token in (
                "login-actions/authenticate",
                "login-actions/required-action",
                "sso.senior.com.br/realms/senior-x",
                "platform.senior.com.br/login",
            )
        )

    async def _recreate_page(self) -> None:
        self._login_frame = None
        try:
            if self._page and not self._page.is_closed():
                await self._page.close()
        except Exception:
            pass
        self._page = await self._context.new_page()
        await self._page.add_init_script(self._STEALTH_JS)

    async def _goto_cotacao_tratavel(self) -> None:
        try:
            await self._page.goto(
                self.COTACAO_URL,
                wait_until='domcontentloaded',
                timeout=self.COTACAO_GOTO_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                "TRD: timeout de navegação ao abrir cotação Senior; provável instabilidade do portal/rede"
            ) from exc

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
        self._context = None
        self._page = None
        self._login_frame = None
        self._logged_in = False
