"""Captura de screenshot/HTML fullpage para rastreamento."""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path
from urllib.parse import urljoin

from fretio.logging_conf import get_logger
from fretio.providers.base import launch_browser_resilient

from rastreamento_common import ResultadoRastreio, _gerar_path_screenshot

logger = get_logger(__name__)


async def _braspress_screenshot(resultado: ResultadoRastreio, numero_nfe: str, track_url: str) -> None:
    """Abre pagina de tracking no browser e tira screenshot com todos os detalhes expandidos."""
    browser = None
    try:
        browser = await launch_browser_resilient(headless=True)
        ctx = await browser.new_context(viewport={"width": 1366, "height": 900})
        page = await ctx.new_page()

        await page.goto(track_url, wait_until="networkidle", timeout=20000)
        await asyncio.sleep(3)

        # 1. Clicar em "Detalhes do Rastreamento" (desktop) via JS para expandir o log detalhado
        await page.evaluate("""
            () => {
                const spans = document.querySelectorAll('span');
                const match = Array.from(spans).find(s =>
                    s.textContent.trim() === 'Detalhes do Rastreamento' &&
                    !s.closest('[id^="mobTimeline"]')
                );
                if (match) match.click();
            }
        """)
        await asyncio.sleep(2)

        # 2. Clicar em "Mais Detalhes" via JS para carregar ocorrências (inclusive elementos ocultos)
        await page.evaluate("""
            () => {
                document.querySelectorAll('a').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    if (href.includes('openDetalhesPend')) {
                        a.click();
                    }
                });
            }
        """)
        await asyncio.sleep(3)

        screenshot_path = _gerar_path_screenshot(numero_nfe)
        await page.screenshot(path=str(screenshot_path), full_page=True)
        resultado.screenshot_path = str(screenshot_path)
        logger.info(f"[RASTREIO-BRASPRESS] Screenshot salvo: {screenshot_path}")
    except Exception as e:
        logger.warning(f"[RASTREIO-BRASPRESS] Erro no screenshot: {e}")
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


async def _salvar_screenshot_entrega(page, resultado: ResultadoRastreio, numero_nfe: str) -> None:
    """Salva a tela da consulta independentemente do status da entrega."""
    try:
        screenshot_path = _gerar_path_screenshot(numero_nfe)
        await page.screenshot(path=str(screenshot_path), full_page=True)
        resultado.screenshot_path = str(screenshot_path)
    except Exception as exc:
        logger.warning("Falha ao salvar screenshot do rastreamento da NF %s: %s", numero_nfe, exc)


def _injetar_base_href(html: str, base_url: str) -> str:
    if not html:
        return html
    if re.search(r"<base\s+href=", html, re.IGNORECASE):
        return html
    base_tag = f'<base href="{base_url}">'
    if re.search(r"<head[^>]*>", html, re.IGNORECASE):
        return re.sub(r"(<head[^>]*>)", rf"\1{base_tag}", html, count=1, flags=re.IGNORECASE)
    return f"<head>{base_tag}</head>{html}"


async def _capturar_html_fullpage(html: str, base_url: str, numero_nfe: str) -> str:
    browser = None
    temp_html_path = None
    try:
        browser = await launch_browser_resilient(headless=True)
        contexts = browser.contexts
        if contexts:
            page = contexts[0].pages[0] if contexts[0].pages else await contexts[0].new_page()
        else:
            ctx = await browser.new_context(viewport={"width": 1366, "height": 900})
            page = await ctx.new_page()
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as tmp:
            tmp.write(_injetar_base_href(html, base_url))
            temp_html_path = tmp.name
        await page.goto(Path(temp_html_path).resolve().as_uri(), wait_until="load")
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(1)
        screenshot_path = _gerar_path_screenshot(numero_nfe)
        await page.screenshot(path=str(screenshot_path), full_page=True)
        return str(screenshot_path)
    finally:
        if temp_html_path:
            try:
                Path(temp_html_path).unlink(missing_ok=True)
            except Exception:
                pass
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


def _extrair_url_ssw_detalhado(html: str, base_url: str = "https://ssw.inf.br") -> str:
    if not html:
        return ""
    match = re.search(r"opx\('([^']*SSWDetalhado[^']*)'\)", html, re.IGNORECASE)
    if not match:
        return ""
    return urljoin(base_url, match.group(1))


async def _capturar_ssw_detalhado_fullpage(detail_url: str, numero_nfe: str) -> str:
    if not detail_url:
        return ""

    browser = None
    try:
        browser = await launch_browser_resilient(headless=True)
        page = await _nova_pagina(browser)
        await page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(2)
        screenshot_path = _gerar_path_screenshot(numero_nfe)
        await page.screenshot(path=str(screenshot_path), full_page=True)
        return str(screenshot_path)
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


async def _nova_pagina(browser):
    contexts = browser.contexts
    if contexts:
        return contexts[0].pages[0] if contexts[0].pages else await contexts[0].new_page()
    ctx = await browser.new_context(viewport={"width": 1366, "height": 900})
    return await ctx.new_page()
