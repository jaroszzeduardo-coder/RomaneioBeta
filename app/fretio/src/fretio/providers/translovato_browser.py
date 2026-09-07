"""Mixin de browser/sessão/cleanup do provider Translovato."""

from __future__ import annotations

import asyncio

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from fretio.logging_conf import get_logger

logger = get_logger(__name__)


class TranslovatoBrowserMixin:
    """Métodos de ciclo de vida do browser/sessão/navegação do TranslovatoProvider."""

    async def _init_browser(self) -> None:
        if self._browser and self._browser.is_connected():
            return
        if self._browser:
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
        await self._context.route("**/*", self._route_block_heavy_resources)
        self._page = await self._context.new_page()
        self._page.set_default_timeout(30000)

    async def _route_block_heavy_resources(self, route) -> None:
        try:
            url = route.request.url.lower()
            if route.request.resource_type in self.BLOCKED_RESOURCE_TYPES:
                await route.abort()
                return
            from fretio.providers.provider_utils import BLOCKED_PERFORMANCE_PATTERNS
            if any(pat in url for pat in BLOCKED_PERFORMANCE_PATTERNS):
                await route.abort()
                return
            await route.continue_()
        except Exception:
            # Requisição já resolvida/cancelada (ex.: navegação abortada); ignorar.
            pass

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
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._logged_in = False

    async def _goto_com_retry(self, url: str, *, tentativas: int = 3, timeout: int = 60000) -> None:
        """Navega para `url` com retry em erros transitórios de navegação.

        O portal por vezes aborta a navegação (net::ERR_ABORTED) quando um
        redirecionamento interno do SPA dispara durante o goto. Nesses casos um
        novo goto após uma pequena espera costuma ter sucesso.
        """
        ultimo_erro: Exception | None = None
        for tentativa in range(1, tentativas + 1):
            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                return
            except Exception as exc:  # noqa: BLE001 - reclassificado abaixo
                msg = str(exc)
                transitorio = "ERR_ABORTED" in msg or "ERR_NETWORK_CHANGED" in msg or isinstance(exc, PlaywrightTimeoutError)
                if not transitorio or tentativa >= tentativas:
                    raise
                ultimo_erro = exc
                logger.warning(
                    "[TRANSLOVATO] Navegação para %s falhou (tentativa %d/%d): %s — retry",
                    url,
                    tentativa,
                    tentativas,
                    msg.splitlines()[0] if msg else exc,
                )
                await asyncio.sleep(1.5 * tentativa)
        if ultimo_erro is not None:  # pragma: no cover - salvaguarda
            raise ultimo_erro
