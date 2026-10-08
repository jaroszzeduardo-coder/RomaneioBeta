"""
Modulo de rastreamento de entregas via Playwright.

Abre o site de rastreamento da transportadora, verifica status de entrega,
tira screenshot se entregue, e retorna previsao/link se em transito.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from fretio.logging_conf import get_logger
from fretio.providers.base import launch_browser_resilient

logger = get_logger(__name__)

try:
    from remote_permissions import (
        CARRIER_DISABLED_MESSAGE,
        carrier_enabled_or_message,
        normalize_carrier_name,
    )
except Exception:
    CARRIER_DISABLED_MESSAGE = "Esta transportadora foi desabilitada pela configuração da licença."

    def carrier_enabled_or_message(carrier):
        return True, ""

    def normalize_carrier_name(carrier):
        return str(carrier or "").strip().lower()

_TRANSPORTADORAS_IGNORADAS_RASTREIO = {"correios", "azul", "bornelli"}

from rastreamento_common import (
    ResultadoRastreio,
    _download_dir,
    _gerar_path_screenshot,
)
from rastreamento_captura import (
    _braspress_screenshot,
    _capturar_html_fullpage,
    _capturar_ssw_detalhado_fullpage,
    _extrair_url_ssw_detalhado,
    _injetar_base_href,
    _nova_pagina,
    _salvar_screenshot_entrega,
)
from rastreamento_status import (
    _chave_acesso_valida,
    _extrair_etapas_agex,
    _extrair_status_rodonaves,
    _extrair_status_ssw_remetente,
    _linhas_visiveis,
    _montar_status,
)


_TRACKING_URLS: dict[str, str] = {
    "braspress": "https://blue.braspress.com/site/w/tracking/view",
    "alfa": "https://alfatransportes.com.br/",
    "trd": "https://platform.senior.com.br/logistica-tck/tms/tck-frontend/#/login/tracking?tenant=ZEhKa2RISmhibk53YjNKMFpYTT0%3D",
    "agex": "https://cliente.agex.com.br/rastreamento",
    "eucatur": "https://ssw.inf.br/2/rastreamento?sigla_emp=EUC&sc=N&sl=N",
    "coopex": "https://ssw.inf.br/2/rastreamento?sigla_emp=CLD&sc=N&sl=N",
    "viopex": "https://ssw.inf.br/2/rastreamento?",
    "mengue": "https://ssw.inf.br/2/rastreamento?sigla_emp=MEN&sc=N&sl=N",
    "rodonaves": "https://rodonaves.com.br/content/rodonaves/br/rastreio-de-mercadoria.html",
}

# Siglas SSW remanescentes para fluxos especificos de DANFE
_SSW_SIGLAS: dict[str, str] = {
    "mengue": "MEN",
}


def obter_link_rastreio(transportadora: str, numero_nfe: str = "", cnpj_emitente: str = "") -> str:
    return _TRACKING_URLS.get(transportadora, "")


async def rastrear_nfe(
    transportadora: str,
    numero_nfe: str,
    cnpj_emitente: str = "",
    chave_acesso: str = "",
) -> ResultadoRastreio:
    transportadora_normalizada = (transportadora or "").strip().lower()
    resultado = ResultadoRastreio(
        numero_nfe=numero_nfe,
        transportadora=transportadora_normalizada.upper(),
        link_rastreio=obter_link_rastreio(transportadora_normalizada),
    )
    if transportadora_normalizada in _TRANSPORTADORAS_IGNORADAS_RASTREIO:
        await _rastrear_ignorado(resultado, numero_nfe, cnpj_emitente, chave_acesso)
        return resultado

    _handlers = {
        "braspress": _rastrear_braspress,
        "trd": _rastrear_trd,
        "agex": _rastrear_agex,
        "eucatur": _rastrear_eucatur,
        "alfa": _rastrear_alfa,
        "coopex": _rastrear_coopex,
        "viopex": _rastrear_viopex,
        "mengue": _rastrear_mengue,
        "rodonaves": _rastrear_rodonaves,
    }
    handler = _handlers.get(transportadora_normalizada, _rastrear_sem_handler)
    try:
        await handler(resultado, numero_nfe, cnpj_emitente, chave_acesso)
    except Exception as e:
        logger.error(f"Erro ao rastrear NF {numero_nfe} na {transportadora}: {e}")
        resultado.erro = str(e)
        resultado.status_texto = f"Erro ao rastrear: {e}"
    return resultado


async def _rastrear_braspress(
    resultado: ResultadoRastreio,
    numero_nfe: str,
    cnpj_emitente: str,
    chave_acesso: str,
) -> None:
    """Rastreio Braspress via HTTP direto (blue.braspress.com pode bloquear muitos acessos)."""
    cnpj_limpo = re.sub(r'\D', '', cnpj_emitente) if cnpj_emitente else ""
    track_url = f"https://blue.braspress.com/site/w/tracking/find?cpfCnpj={cnpj_limpo}&pedidoNf={numero_nfe}"
    resultado.link_rastreio = track_url

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            follow_redirects=True,
            timeout=15,
        ) as client:
            resp = await client.get(track_url)
            if resp.status_code in (429, 403):
                logger.warning(f"[RASTREIO-BRASPRESS] NF {numero_nfe}: blue.braspress bloqueou ({resp.status_code})")
                resultado.status_texto = "Em trânsito — rastreamento bloqueado, verifique no site"
                return
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # Extrair campos da primeira tabela (table-striped)
        status_campo = ""
        entregue_em = ""
        previsao = ""
        table = soup.find("table", class_="table-striped")
        if table:
            headers = [th.get_text(strip=True).upper() for th in table.find_all("th")]
            row = table.find("tbody")
            cells = row.find_all("td") if row else table.find_all("td")
            cell_texts = [td.get_text(strip=True) for td in cells]
            for i, h in enumerate(headers):
                if i < len(cell_texts):
                    if "STATUS" in h:
                        status_campo = cell_texts[i]
                    elif "ENTREGUE EM" in h:
                        entregue_em = cell_texts[i]
                    elif "PREVIS" in h and "ENTREGA" in h:
                        previsao = cell_texts[i]

        # Fallback: extrair previsao do texto se nao achou na tabela
        if not previsao:
            body_text = soup.get_text(separator="\n", strip=True)
            prev_match = re.search(r'Previs[ãa]o\s+de\s+Entrega\s*[:\n]?\s*(\d{2}/\d{2}/\d{4})', body_text)
            if prev_match:
                previsao = prev_match.group(1)

        # Detectar entregue pelo campo "Entregue em" (preenchido) ou Status contendo "entregue"
        entregue = bool(entregue_em and entregue_em != "-") or "ENTREGUE" in status_campo.upper()

        if entregue:
            resultado.entregue = True
            resultado.status_texto = f"ENTREGUE em {entregue_em}" if entregue_em and entregue_em != "-" else "ENTREGUE"
            logger.info(f"[RASTREIO-BRASPRESS] NF {numero_nfe}: {resultado.status_texto}")
        else:
            resultado.entregue = False
            if previsao:
                resultado.previsao_entrega = previsao
                resultado.status_texto = f"{status_campo} — Previsão: {previsao}" if status_campo else f"Previsão: {previsao}"
            else:
                resultado.status_texto = status_campo or "Em trânsito"
            logger.info(f"[RASTREIO-BRASPRESS] NF {numero_nfe}: {resultado.status_texto}")

        await _braspress_screenshot(resultado, numero_nfe, track_url)

    except Exception as e:
        msg = str(e or "").strip()
        logger.warning(f"[RASTREIO-BRASPRESS] NF {numero_nfe}: erro: {msg}")
        if "ERR_NAME_NOT_RESOLVED" in msg or "getaddrinfo" in msg or "ERR_NETWORK_CHANGED" in msg:
            resultado.status_texto = "Rastreamento indisponível no momento"
        else:
            resultado.status_texto = f"Erro ao rastrear: {msg or 'falha desconhecida'}"


async def _aplicar_resultado_texto(
    resultado: ResultadoRastreio,
    numero_nfe: str,
    body_text: str,
    page=None,
    *,
    not_found_patterns: list[str] | None = None,
    status_patterns: list[str] | None = None,
) -> None:
    body_upper = body_text.upper()
    padroes_nao_encontrado = [
        "NÃO ENCONTRAMOS NENHUM RASTREAMENTO",
        "NAO ENCONTRAMOS NENHUM RASTREAMENTO",
        "NENHUM PEDIDO ENCONTRADO",
        "NENHUMA ENCOMENDA ENCONTRADA",
        "NÃO ENCONTRAMOS",
        "NAO ENCONTRAMOS",
        "NENHUM RESULTADO ENCONTRADO",
    ]
    for padrao in not_found_patterns or []:
        padroes_nao_encontrado.append(padrao.upper())

    if any(padrao in body_upper for padrao in padroes_nao_encontrado):
        resultado.status_texto = "NF não encontrada no sistema de rastreamento"
        return

    previsao = _extrair_previsao(body_text)
    resultado.previsao_entrega = previsao

    status_desc = ""
    for pattern in status_patterns or [
        r'(?:Status|Situa[cç][aã]o)\s*[:\-]?\s*([^\n]+)',
        r'(?:Último\s*evento|Ultimo\s*evento|Evento)\s*[:\-]?\s*([^\n]+)',
        r'(?:Ocorr[eê]ncia(?:\s+atual)?)\s*[:\-]?\s*([^\n]+)',
    ]:
        match = re.search(pattern, body_text, re.IGNORECASE)
        if match:
            status_desc = match.group(1).strip()
            break

    termos_entregue = ["ENTREGUE", "ENTREGA REALIZADA", "ENTREGA EFETUADA", "MERCADORIA ENTREGUE", "DELIVERED"]
    entregue = any(termo in body_upper for termo in termos_entregue)

    if entregue:
        resultado.entregue = True
        resultado.status_texto = _montar_status(status_desc or "ENTREGUE", previsao=previsao)
        if page is not None:
            await _salvar_screenshot_entrega(page, resultado, numero_nfe)
        return

    if status_desc:
        resultado.status_texto = _montar_status(status_desc, previsao=previsao)
    elif previsao:
        resultado.status_texto = f"Previsão: {previsao}"
    else:
        resultado.status_texto = "Consulta realizada, mas o portal não retornou um status legível"
    if page is not None:
        await _salvar_screenshot_entrega(page, resultado, numero_nfe)


async def _rastrear_ssw_remetente_http(
    resultado: ResultadoRastreio,
    numero_nfe: str,
    cnpj_emitente: str,
    *,
    sigla_emp: str,
    tracking_url: str,
    result_url: str = "https://ssw.inf.br/2/resultSSW",
    extra_form_data: dict[str, str] | None = None,
) -> None:
    cnpj_limpo = re.sub(r'\D', '', cnpj_emitente or "")
    resultado.link_rastreio = tracking_url
    if not cnpj_limpo or not numero_nfe:
        resultado.status_texto = "CNPJ ou número de NF ausentes para rastreamento"
        return

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            follow_redirects=True,
            timeout=20,
        ) as client:
            payload = {
                "cnpj": cnpj_limpo,
                "NR": numero_nfe,
                "chave": "",
                "sigla_emp": sigla_emp,
            }
            if extra_form_data:
                payload.update(extra_form_data)
            resp = await client.post(
                result_url,
                data=payload,
            )
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        body_text = soup.get_text(separator="\n", strip=True)
        body_upper = body_text.upper()

        if "RASTREAMENTO PELO REMETENTE" not in body_upper:
            resultado.status_texto = "Portal retornou uma resposta inesperada"
            return

        status, detalhe = _extrair_status_ssw_remetente(body_text)
        if not status and not detalhe:
            if (
                "NENHUM RESULTADO" in body_upper
                or "NAO ENCONTRAMOS" in body_upper
                or "NÃO ENCONTRAMOS" in body_upper
                or "INFORMAÇÃO NÃO DISPONÍVEL" in body_upper
                or "INFORMACAO NAO DISPONIVEL" in body_upper
            ):
                resultado.status_texto = "NF não encontrada no sistema de rastreamento"
            else:
                resultado.status_texto = "Consulta realizada, mas o portal não retornou um status legível"
            return

        detail_url = _extrair_url_ssw_detalhado(resp.text)
        if detail_url:
            resultado.link_rastreio = detail_url

        resultado.entregue = "ENTREGUE" in f"{status} {detalhe}".upper()
        resultado.status_texto = _montar_status(status, detalhe)
        if detail_url:
            resultado.screenshot_path = await _capturar_ssw_detalhado_fullpage(detail_url, numero_nfe)
        else:
            resultado.screenshot_path = await _capturar_html_fullpage(resp.text, result_url, numero_nfe)
    except Exception as e:
        msg = str(e or "").strip()
        logger.warning(f"[RASTREIO-{resultado.transportadora}] NF {numero_nfe}: erro SSW remetente: {msg}")
        if "ERR_NAME_NOT_RESOLVED" in msg or "ERR_NETWORK_CHANGED" in msg or "getaddrinfo" in msg:
            resultado.status_texto = "Rastreamento indisponível no momento"
        else:
            resultado.status_texto = f"Erro ao rastrear: {msg or 'falha desconhecida'}"


async def _rastrear_eucatur(
    resultado: ResultadoRastreio,
    numero_nfe: str,
    cnpj_emitente: str,
    chave_acesso: str,
) -> None:
    await _rastrear_ssw_remetente_http(
        resultado,
        numero_nfe,
        cnpj_emitente,
        sigla_emp="EUC",
        tracking_url=_TRACKING_URLS["eucatur"],
    )


async def _rastrear_viopex(
    resultado: ResultadoRastreio,
    numero_nfe: str,
    cnpj_emitente: str,
    chave_acesso: str,
) -> None:
    await _rastrear_ssw_remetente_http(
        resultado,
        numero_nfe,
        cnpj_emitente,
        sigla_emp="VIO",
        tracking_url=_TRACKING_URLS["viopex"],
    )


async def _rastrear_mengue(
    resultado: ResultadoRastreio,
    numero_nfe: str,
    cnpj_emitente: str,
    chave_acesso: str,
) -> None:
    await _rastrear_ssw(resultado, numero_nfe, cnpj_emitente, chave_acesso)


async def _rastrear_coopex(
    resultado: ResultadoRastreio,
    numero_nfe: str,
    cnpj_emitente: str,
    chave_acesso: str,
) -> None:
    await _rastrear_ssw_remetente_http(
        resultado,
        numero_nfe,
        cnpj_emitente,
        sigla_emp="CLD",
        tracking_url=_TRACKING_URLS["coopex"],
        result_url="https://ssw.inf.br/2/ssw_resultSSW",
        extra_form_data={"urlori": _TRACKING_URLS["coopex"]},
    )


async def _rastrear_trd(
    resultado: ResultadoRastreio,
    numero_nfe: str,
    cnpj_emitente: str,
    chave_acesso: str,
) -> None:
    """Rastreio TRD via API Senior TCK (endpoint público anônimo)."""
    cnpj_limpo = re.sub(r'\D', '', cnpj_emitente or "")
    if not cnpj_limpo or not numero_nfe:
        resultado.status_texto = "CNPJ ou número de NF ausentes para rastreamento"
        return

    _TRD_API = (
        "https://platform.senior.com.br/t/senior.com.br/bridge/1.0"
        "/anonymous/rest/tms/tck/actions/externalTenantConsultaTracking"
    )
    _TRD_HEADERS = {
        "ExternalUser": "true",
        "X-Tenant": "trdtransportes",
        "X-TenantDomain": "trdtransportes.com.br",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                _TRD_API,
                headers=_TRD_HEADERS,
                json={
                    "identificadorCliente": cnpj_limpo,
                    "documento": numero_nfe,
                    "pageRequest": {"offset": 0, "size": 10},
                },
            )
            if resp.status_code == 404:
                resultado.status_texto = "NF não encontrada no sistema TRD"
                logger.info(f"[RASTREIO-TRD] NF {numero_nfe}: não encontrada")
                return
            resp.raise_for_status()
            data = resp.json()

        lista = data.get("listaTracking", [])
        if not lista:
            resultado.status_texto = "NF não encontrada no sistema TRD"
            logger.info(f"[RASTREIO-TRD] NF {numero_nfe}: lista vazia")
            return

        tracking = lista[0].get("tracking", {})
        situacao = tracking.get("situacao", {})
        tipo_sit = situacao.get("tipoSituacao", 0)
        desc_sit = situacao.get("descricao", "")
        data_entrega_raw = tracking.get("dataEntrega") or ""
        data_prev_raw = tracking.get("dataPrevisaoEntrega") or ""

        def _fmt_data(iso: str) -> str:
            """Converte ISO 8601 UTC para DD/MM/AAAA."""
            try:
                dt = datetime.strptime(iso[:10], "%Y-%m-%d")
                return dt.strftime("%d/%m/%Y")
            except Exception:
                return ""

        # tipoSituacao 4 = Encerrado (entregue)
        entregue = tipo_sit == 4 or bool(data_entrega_raw)

        if entregue:
            resultado.entregue = True
            data_fmt = _fmt_data(data_entrega_raw) if data_entrega_raw else ""
            resultado.status_texto = f"ENTREGUE em {data_fmt}" if data_fmt else "ENTREGUE"
            logger.info(f"[RASTREIO-TRD] NF {numero_nfe}: {resultado.status_texto}")
        else:
            resultado.entregue = False
            data_prev_fmt = _fmt_data(data_prev_raw) if data_prev_raw else ""
            if data_prev_fmt:
                resultado.previsao_entrega = data_prev_fmt
                resultado.status_texto = f"{desc_sit} — Previsão: {data_prev_fmt}" if desc_sit else f"Previsão: {data_prev_fmt}"
            else:
                resultado.status_texto = desc_sit or "Em trânsito"
            logger.info(f"[RASTREIO-TRD] NF {numero_nfe}: {resultado.status_texto}")

        await _trd_screenshot(resultado, numero_nfe, tracking.get("codigo", ""))

    except httpx.HTTPStatusError as e:
        logger.warning(f"[RASTREIO-TRD] NF {numero_nfe}: HTTP {e.response.status_code}")
        resultado.status_texto = f"Erro ao rastrear TRD (HTTP {e.response.status_code})"
    except Exception as e:
        msg = str(e or "").strip()
        logger.warning(f"[RASTREIO-TRD] NF {numero_nfe}: erro: {msg}")
        resultado.status_texto = f"Erro ao rastrear: {msg or 'falha desconhecida'}"


async def _trd_screenshot(resultado: ResultadoRastreio, numero_nfe: str, codigo_tracking: str) -> None:
    """Abre o portal TRD, entra o código de rastreio e tira screenshot."""
    if not codigo_tracking:
        return
    _TRD_TRACKING_URL = _TRACKING_URLS["trd"]
    browser = None
    try:
        browser = await launch_browser_resilient(headless=True)
        ctx = await browser.new_context(viewport={"width": 1366, "height": 900})
        page = await ctx.new_page()
        await page.goto(_TRD_TRACKING_URL, wait_until="networkidle", timeout=30000)
        campo = page.locator('input[name="codigoTracking"]').first
        await campo.wait_for(state="visible", timeout=15000)
        await campo.fill(codigo_tracking)
        await page.locator('button.btn-success').first.click()
        await asyncio.sleep(5)

        # Expandir painel "Detalhes" (tabela com todas as etapas)
        try:
            btn_detalhes = page.locator('button:has-text("Detalhes")').first
            await btn_detalhes.wait_for(state="visible", timeout=8000)
            await btn_detalhes.click()
            await asyncio.sleep(2)
        except Exception:
            pass

        screenshot_path = _gerar_path_screenshot(numero_nfe)
        await page.screenshot(path=str(screenshot_path), full_page=True)
        resultado.screenshot_path = str(screenshot_path)
        logger.info(f"[RASTREIO-TRD] Screenshot salvo: {screenshot_path}")
    except Exception as e:
        logger.warning(f"[RASTREIO-TRD] Erro no screenshot: {e}")
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


async def _rastrear_agex(
    resultado: ResultadoRastreio,
    numero_nfe: str,
    cnpj_emitente: str,
    chave_acesso: str,
) -> None:
    cnpj_limpo = re.sub(r'\D', '', cnpj_emitente or "")
    if not cnpj_limpo or not numero_nfe:
        resultado.status_texto = "CNPJ ou número de NF ausentes para rastreamento"
        return

    resultado.link_rastreio = _TRACKING_URLS["agex"]
    browser = None
    try:
        browser = await launch_browser_resilient(headless=True)
        page = await _nova_pagina(browser)

        await page.goto(resultado.link_rastreio, wait_until="domcontentloaded", timeout=30000)
        campo_cnpj = page.locator('#cnpjOrCpf, input[name="cnpjOrCpf"]').first
        await campo_cnpj.wait_for(state="visible", timeout=15000)
        await campo_cnpj.fill(cnpj_limpo)
        await asyncio.sleep(2)

        campo_nf = page.locator('#notaFiscal, input[name="notaFiscal"]').first
        try:
            await campo_nf.wait_for(state="visible", timeout=5000)
        except Exception:
            await campo_cnpj.press("Tab")
            await campo_nf.wait_for(state="visible", timeout=10000)

        await campo_nf.fill(numero_nfe)
        await page.locator('button:has-text("Buscar encomendas")').first.click()
        try:
            await page.locator('button:has-text("Ver detalhes"), text=/Nota fiscal/i').first.wait_for(timeout=15000)
        except Exception:
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
        await asyncio.sleep(2)

        resultado.link_rastreio = page.url
        detalhes_btn = page.locator('button:has-text("Ver detalhes")').first
        if await detalhes_btn.count():
            try:
                await detalhes_btn.click(timeout=5000)
                await asyncio.sleep(2)
            except Exception:
                pass

        body_text = await page.inner_text("body")
        body_upper = body_text.upper()
        if "NENHUMA ENCOMENDA ENCONTRADA" in body_upper or "NENHUM PEDIDO ENCONTRADO" in body_upper:
            resultado.status_texto = "NF não encontrada no sistema de rastreamento"
            return

        etapas = await _extrair_etapas_agex(page)
        previsao = (etapas.get("previsao") or "").strip() or _extrair_previsao(body_text)
        resultado.previsao_entrega = previsao

        rows = etapas.get("rows") or []
        etapas_concluidas = [item["label"] for item in rows if item.get("completed")]
        entregue = any(
            item.get("label") == "Entregue" and item.get("completed")
            for item in rows
        )

        if entregue:
            resultado.entregue = True
            resultado.status_texto = _montar_status("ENTREGUE", previsao=previsao)
            await _salvar_screenshot_entrega(page, resultado, numero_nfe)
            return

        if etapas_concluidas:
            resultado.status_texto = _montar_status(etapas_concluidas[-1], previsao=previsao)
            await _salvar_screenshot_entrega(page, resultado, numero_nfe)
            return

        await _aplicar_resultado_texto(
            resultado,
            numero_nfe,
            body_text,
            page,
            not_found_patterns=["NENHUMA ENCOMENDA ENCONTRADA", "NENHUM PEDIDO ENCONTRADO"],
        )
    except Exception as e:
        msg = str(e or "").strip()
        logger.warning(f"[RASTREIO-AGEX] NF {numero_nfe}: erro: {msg}")
        if "ERR_NAME_NOT_RESOLVED" in msg or "ERR_NETWORK_CHANGED" in msg or "getaddrinfo" in msg:
            resultado.status_texto = "Rastreamento indisponível no momento"
        else:
            resultado.status_texto = f"Erro ao rastrear: {msg or 'falha desconhecida'}"
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


async def _rastrear_rodonaves(
    resultado: ResultadoRastreio,
    numero_nfe: str,
    cnpj_emitente: str,
    chave_acesso: str,
) -> None:
    cnpj_limpo = re.sub(r'\D', '', cnpj_emitente or "")
    query_url = (
        f"{_TRACKING_URLS['rodonaves']}?rastreiemercadoria=2&cpfcnpj={cnpj_limpo}&numnf={numero_nfe}"
        if cnpj_limpo and numero_nfe else _TRACKING_URLS["rodonaves"]
    )
    api_url = (
        f"https://rodonaves.com.br/bin/rodonaves/trackingv3/package"
        f"?TaxIdRegistration={cnpj_limpo}&InvoiceNumber={numero_nfe}"
    )
    resultado.link_rastreio = query_url
    if not cnpj_limpo or not numero_nfe:
        resultado.status_texto = "CNPJ ou número de NF ausentes para rastreamento"
        return

    browser = None
    try:
        detalhe_evento = ""
        entregue_api = False
        termos_entregue = ("ENTREGUE", "ENTREGA REALIZADA", "ENTREGA EFETUADA")
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                follow_redirects=True,
                timeout=20,
            ) as client:
                resp = await client.get(api_url)

            if resp.status_code == 204:
                resultado.status_texto = "NF não encontrada no sistema de rastreamento"
                return

            resp.raise_for_status()
            payload = resp.json()
            eventos = payload.get("Events") or []
            ultimo_evento = next(
                (
                    evento for evento in reversed(eventos)
                    if (evento.get("Reason") or evento.get("Description") or "").strip()
                ),
                {},
            )
            detalhe_evento = (
                (ultimo_evento.get("Reason") or "").strip()
                or (ultimo_evento.get("Description") or "").strip()
            )
            entregue_api = any(
                any(
                    termo in ((evento.get("Reason") or evento.get("Description") or "").upper())
                    for termo in termos_entregue
                )
                for evento in eventos
            )
        except Exception as api_error:
            logger.info(f"[RASTREIO-RODONAVES] NF {numero_nfe}: API auxiliar indisponível ({api_error})")

        browser = await launch_browser_resilient(headless=True)
        page = await _nova_pagina(browser)
        await page.goto(query_url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_function(
                """() => {
                    const text = document.body ? (document.body.innerText || '') : '';
                    return /Previs[aã]o de entrega:/i.test(text) || /(?:^|\\n)Status\\n/i.test(text);
                }""",
                timeout=15000,
            )
        except Exception:
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
        await asyncio.sleep(4)

        resultado.link_rastreio = page.url
        body_text = await page.inner_text("body")
        body_upper = body_text.upper()
        if "PREVISÃO DE ENTREGA:" not in body_upper and "\nSTATUS\n" not in body_upper:
            resultado.status_texto = "NF não encontrada no sistema de rastreamento"
            return

        status_desc = _extrair_status_rodonaves(body_text)
        previsao = _extrair_previsao(body_text)
        resultado.previsao_entrega = previsao

        entregue = entregue_api or any(termo in status_desc.upper() for termo in termos_entregue)
        if entregue:
            resultado.entregue = True
            resultado.status_texto = _montar_status(
                status_desc or "MERCADORIA ENTREGUE",
                detalhe_evento,
                previsao,
            )
            await _salvar_screenshot_entrega(page, resultado, numero_nfe)
            return

        if status_desc:
            resultado.status_texto = _montar_status(status_desc, detalhe_evento, previsao)
        elif detalhe_evento:
            resultado.status_texto = _montar_status(detalhe_evento, previsao=previsao)
        else:
            resultado.status_texto = "Consulta localizada, mas o portal não retornou um status legível"
        await _salvar_screenshot_entrega(page, resultado, numero_nfe)
    except Exception as e:
        msg = str(e or "").strip()
        logger.warning(f"[RASTREIO-RODONAVES] NF {numero_nfe}: erro: {msg}")
        if "ERR_NAME_NOT_RESOLVED" in msg or "ERR_NETWORK_CHANGED" in msg or "getaddrinfo" in msg:
            resultado.status_texto = "Rastreamento indisponível no momento"
        else:
            resultado.status_texto = f"Erro ao rastrear: {msg or 'falha desconhecida'}"
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


async def _rastrear_sem_handler(
    resultado: ResultadoRastreio,
    numero_nfe: str,
    cnpj_emitente: str,
    chave_acesso: str,
) -> None:
    resultado.status_texto = "Transportadora sem handler de rastreamento mapeado"
    logger.info(f"[RASTREIO-{resultado.transportadora}] NF {numero_nfe}: sem handler especifico")



# ---------------------------------------------------------------------------
#  SSW - rastreamento via DANFE para transportadoras com fluxo proprio
# ---------------------------------------------------------------------------

async def _rastrear_ssw(
    resultado: ResultadoRastreio,
    numero_nfe: str,
    cnpj_emitente: str,
    chave_acesso: str,
) -> None:
    """Rastreio via portal SSW usando chave de acesso (DANFE) de 44 digitos."""
    import re as _re
    transp_lower = resultado.transportadora.lower()
    sigla = _SSW_SIGLAS.get(transp_lower, "")

    if not chave_acesso or len(_re.sub(r'\D', '', chave_acesso)) != 44:
        resultado.status_texto = "Chave de acesso (DANFE) nao disponivel para rastreio"
        logger.warning(f"[RASTREIO-{resultado.transportadora}] NF {numero_nfe}: chave_acesso ausente ou invalida")
        return

    chave_limpa = _re.sub(r'\D', '', chave_acesso)
    if sigla:
        candidate_urls = [
            f"https://ssw.inf.br/2/rastreamento_danfe?sigla_emp={sigla}&sc=N",
            f"https://ssw.inf.br/2/rastreamento_danfe?sigla_emp={sigla}",
            f"https://sistema.ssw.inf.br/2/rastreamento_danfe?sigla_emp={sigla}&sc=N",
            f"https://sistema.ssw.inf.br/2/rastreamento_danfe?sigla_emp={sigla}",
        ]
    else:
        candidate_urls = [
            "https://ssw.inf.br/2/rastreamento_danfe?sc=N",
            "https://ssw.inf.br/2/rastreamento_danfe",
        ]
    resultado.link_rastreio = candidate_urls[0]

    browser = None
    try:
        browser = await launch_browser_resilient(headless=True)
        contexts = browser.contexts
        if contexts:
            page = contexts[0].pages[0] if contexts[0].pages else await contexts[0].new_page()
        else:
            ctx = await browser.new_context(viewport={"width": 1366, "height": 900})
            page = await ctx.new_page()

        page_loaded = False
        for base_url in candidate_urls:
            try:
                logger.info(f"[RASTREIO-{resultado.transportadora}] Navegando para {base_url}")
                await page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(1.5)
                if await page.query_selector('#danfe, input[name="danfe"], input[id*="danfe" i]'):
                    resultado.link_rastreio = base_url
                    page_loaded = True
                    break
            except Exception:
                continue

        if not page_loaded:
            resultado.status_texto = "Página de rastreamento indisponível no momento"
            return

        # Preencher campo DANFE (chave de 44 digitos)
        danfe_field = await page.query_selector('#danfe, input[name="danfe"], input[id*="danfe" i]')
        if not danfe_field:
            resultado.status_texto = "Campo DANFE nao encontrado na pagina SSW"
            return

        await danfe_field.fill(chave_limpa)
        await asyncio.sleep(0.5)

        # Clicar RASTREAR e aguardar redirecionamento para SSWDetalhado
        btn = await page.query_selector(
            '#btn_rastrear, a#btn_rastrear, a:has-text("RASTREAR"), button:has-text("RASTREAR")'
        )
        if btn:
            await btn.click()
        else:
            await danfe_field.press("Enter")

        # SSW fluxo: POST -> reload com DOCUMENTID -> JS redireciona para /2/SSWDetalhado?...
        try:
            await page.wait_for_url("**/SSWDetalhado**", timeout=15000)
        except Exception:
            # Sem redirecionamento = DANFE nao encontrada no sistema SSW
            resultado.status_texto = "NF nao encontrada no sistema de rastreamento"
            logger.info(f"[RASTREIO-{resultado.transportadora}] NF {numero_nfe}: nao encontrada no SSW")
            return

        await asyncio.sleep(2)
        resultado.link_rastreio = page.url

        # Extrair dados da pagina SSWDetalhado
        body_text = await page.inner_text("body")
        body_upper = body_text.upper()

        termos_entregue = ["ENTREGUE", "ENTREGA REALIZADA", "ENTREGA EFETUADA"]
        entregue = any(t in body_upper for t in termos_entregue)

        if entregue:
            resultado.entregue = True
            resultado.status_texto = "ENTREGUE"
        else:
            resultado.entregue = False
            previsao = _extrair_previsao(body_text)
            resultado.previsao_entrega = previsao

            # Extrair status descritivo
            status_desc = ""
            for pattern in [
                r'(?:Status|Situa[c\xe7][a\xe3]o)\s*[:\-]\s*([^\n]+)',
                r'(?:[\xdaU]ltimo\s*evento|Evento)\s*[:\-]\s*([^\n]+)',
            ]:
                m = _re.search(pattern, body_text, _re.IGNORECASE)
                if m:
                    status_desc = m.group(1).strip()
                    break

            if status_desc:
                resultado.status_texto = f"{status_desc}" + (f" \u2014 Previs\u00e3o: {previsao}" if previsao else "")
            else:
                resultado.status_texto = "Em tr\u00e2nsito" + (f" \u2014 Previs\u00e3o: {previsao}" if previsao else "")
            logger.info(f"[RASTREIO-{resultado.transportadora}] NF {numero_nfe}: {resultado.status_texto}")

        await _salvar_screenshot_entrega(page, resultado, numero_nfe)

    except Exception as e:
        msg = str(e or "").strip()
        logger.warning(f"[RASTREIO-{resultado.transportadora}] NF {numero_nfe}: erro SSW: {msg}")
        if "ERR_NAME_NOT_RESOLVED" in msg or "ERR_NETWORK_CHANGED" in msg or "getaddrinfo" in msg:
            resultado.status_texto = "Rastreamento indisponível no momento"
        else:
            resultado.status_texto = f"Erro ao rastrear: {msg or 'falha desconhecida'}"
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
#  Transportadoras sem rastreamento disponivel
# ---------------------------------------------------------------------------

async def _rastrear_alfa(
    resultado: ResultadoRastreio,
    numero_nfe: str,
    cnpj_emitente: str,
    chave_acesso: str,
) -> None:
    """Rastreio Alfa Transportes via portal público (sem login necessário)."""
    _ALFA_TRACK_URL = "https://areadocliente.alfatransportes.com.br/rastreio/transbordo-site/"
    _MESES_PT = {
        "janeiro": "01", "fevereiro": "02", "março": "03", "abril": "04",
        "maio": "05", "junho": "06", "julho": "07", "agosto": "08",
        "setembro": "09", "outubro": "10", "novembro": "11", "dezembro": "12",
    }

    cnpj_limpo = re.sub(r'\D', '', cnpj_emitente) if cnpj_emitente else ""
    resultado.link_rastreio = (
        f"{_ALFA_TRACK_URL}?cnpj={cnpj_limpo}&tipo=1&nota={numero_nfe}"
        if cnpj_limpo else _ALFA_TRACK_URL
    )

    if not cnpj_limpo or not numero_nfe:
        resultado.status_texto = "CNPJ ou número de NF ausentes para rastreamento"
        return

    try:
        url = f"{_ALFA_TRACK_URL}?cnpj={cnpj_limpo}&tipo=1&nota={numero_nfe}"
        logger.info(f"[RASTREIO-ALFA] NF {numero_nfe}: consultando {url}")
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            follow_redirects=True,
            timeout=20,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()

        body_text = BeautifulSoup(resp.text, "html.parser").get_text(separator="\n", strip=True)
        body_upper = body_text.upper()

        # NF não encontrada ou dados inválidos
        if "NÃO ENCONTRAMOS NENHUM RASTREAMENTO" in body_upper or (
            "DADOS INVÁLIDOS" in body_upper and "ENCONTRAMOS" in body_upper
        ):
            resultado.status_texto = "NF não encontrada no sistema de rastreamento"
            logger.info(f"[RASTREIO-ALFA] NF {numero_nfe}: não encontrada")
            return

        # Detectar entregue
        termos_entregue = ["ENTREGUE", "ENTREGA REALIZADA", "ENTREGA EFETUADA"]
        entregue = any(t in body_upper for t in termos_entregue)

        # Extrair previsão de entrega no formato "DD de Mês de YYYY"
        previsao = ""
        m_prev = re.search(
            r'Data Prevista para entrega:\s*(\d{1,2}\s+de\s+\w+\s+de\s+\d{4})',
            body_text,
            re.IGNORECASE,
        )
        if m_prev:
            data_str = m_prev.group(1).strip()
            partes = re.match(r'(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})', data_str, re.IGNORECASE)
            if partes:
                dia, mes_nome, ano = partes.groups()
                mes_num = _MESES_PT.get(mes_nome.lower(), "")
                previsao = f"{int(dia):02d}/{mes_num}/{ano}" if mes_num else data_str
        resultado.previsao_entrega = previsao

        # Extrair último evento da tabela de transbordo
        lines = [l.strip() for l in body_text.split('\n') if l.strip()]
        in_table = False
        status_lines: list[str] = []
        for line in lines:
            if re.search(r'Transbordo\s+Data\s+Hora', line, re.IGNORECASE):
                in_table = True
                continue
            if in_table:
                if re.search(
                    r'^(?:Dados Envolvidos|REMETENTE|Em processo|Data Prevista|Voltar)',
                    line,
                    re.IGNORECASE,
                ):
                    break
                # Remove data+hora do final da linha: "DD de Mês de YYYY     HH:MM"
                status_text = re.sub(
                    r'\s+\d{1,2}\s+de\s+\w+\s+de\s+\d{4}\s+\d{1,2}:\d{2}\s*$',
                    '',
                    line,
                ).strip()
                if status_text:
                    status_lines.append(status_text)
        last_status = status_lines[-1] if status_lines else ""

        if entregue:
            resultado.entregue = True
            resultado.status_texto = "ENTREGUE" + (f" — Previsão: {previsao}" if previsao else "")
        else:
            resultado.entregue = False
            if last_status:
                resultado.status_texto = last_status + (f" — Previsão: {previsao}" if previsao else "")
            else:
                resultado.status_texto = "Em trânsito" + (f" — Previsão: {previsao}" if previsao else "")
            logger.info(f"[RASTREIO-ALFA] NF {numero_nfe}: {resultado.status_texto}")

        resultado.screenshot_path = await _capturar_html_fullpage(
            resp.text,
            "https://areadocliente.alfatransportes.com.br/",
            numero_nfe,
        )
        logger.info(f"[RASTREIO-ALFA] NF {numero_nfe}: Screenshot: {resultado.screenshot_path}")

    except Exception as e:
        msg = str(e or "").strip()
        logger.warning(f"[RASTREIO-ALFA] NF {numero_nfe}: erro: {msg}")
        if "ERR_NAME_NOT_RESOLVED" in msg or "ERR_NETWORK_CHANGED" in msg or "getaddrinfo" in msg:
            resultado.status_texto = "Rastreamento indisponível no momento"
        else:
            resultado.status_texto = f"Erro ao rastrear: {msg or 'falha desconhecida'}"


async def _rastrear_ignorado(
    resultado: ResultadoRastreio,
    numero_nfe: str,
    cnpj_emitente: str,
    chave_acesso: str,
) -> None:
    """Ignora transportadoras explicitamente fora do escopo de rastreamento."""
    resultado.status_texto = "Rastreamento ignorado para esta transportadora"
    resultado.link_rastreio = ""
    logger.info(f"[RASTREIO-{resultado.transportadora}] NF {numero_nfe}: rastreamento ignorado")


def _extrair_previsao(texto: str) -> str:
    padroes = [
        # "Previsão de entrega: 22/04/2026" ou "Previsão: 22/04/2026"
        r'(?:previs[aã]o\s*(?:de\s*entrega)?)[:\s]*(\d{2}[/.-]\d{2}[/.-]\d{2,4})',
        # "Entrega prevista: 22/04/2026"
        r'(?:entrega\s*(?:prevista|estimada))[:\s]*(\d{2}[/.-]\d{2}[/.-]\d{2,4})',
        # "Data prevista: 22/04/2026" ou "Data estimada"
        r'(?:data\s*(?:prevista|estimada|de\s*entrega))[:\s]*(\d{2}[/.-]\d{2}[/.-]\d{2,4})',
        # "Estimativa: 22/04/2026" ou "Prazo: 22/04/2026"
        r'(?:estimativa|prazo)[:\s]*(\d{2}[/.-]\d{2}[/.-]\d{2,4})',
        # "Previsão de entrega\n22/04/2026" (quebra de linha entre label e data)
        r'(?:previs[aã]o\s*(?:de\s*entrega)?)\s*\n\s*(\d{2}[/.-]\d{2}[/.-]\d{2,4})',
        # "Prazo de entrega\n5 dias úteis" ou similar
        r'(?:prazo\s*de\s*entrega)[:\s]*(\d+\s*dias?\s*[uú]teis?)',
        # Formato "dd/mm/aaaa" próximo a "previsão" em até 50 chars
        r'previs[aã]o.{0,50}?(\d{2}/\d{2}/\d{2,4})',
    ]
    for padrao in padroes:
        match = re.search(padrao, texto, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


async def rastrear_multiplas(
    notas: list[dict],
    callback=None,
) -> list[ResultadoRastreio]:
    total = len(notas)
    resultados = []
    for i, nota in enumerate(notas):
        transportadora = normalize_carrier_name(nota.get("transportadora", ""))
        allowed, message = carrier_enabled_or_message(transportadora)
        if not allowed:
            resultado = ResultadoRastreio(
                numero_nfe=nota.get("numero_nfe", ""),
                transportadora=(transportadora or "").upper(),
                status_texto=message or CARRIER_DISABLED_MESSAGE,
            )
            logger.info(
                "[RASTREIO-%s] NF %s: %s",
                resultado.transportadora,
                resultado.numero_nfe,
                message or CARRIER_DISABLED_MESSAGE,
            )
            resultados.append(resultado)
            if callback:
                try:
                    callback(i + 1, total, resultado)
                except Exception:
                    pass
            continue
        resultado = await rastrear_nfe(
            transportadora=transportadora,
            numero_nfe=nota.get("numero_nfe", ""),
            cnpj_emitente=nota.get("cnpj_emitente", ""),
            chave_acesso=nota.get("chave_acesso", ""),
        )
        resultados.append(resultado)
        if callback:
            try:
                callback(i + 1, total, resultado)
            except Exception:
                pass
    return resultados
