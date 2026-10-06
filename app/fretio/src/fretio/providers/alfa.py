"""Provider Alfa Transportes via Playwright com login manual (Turnstile)."""
from __future__ import annotations

from datetime import datetime
import os
import re
import base64
import json
import shutil
import socket
import struct
import subprocess
import asyncio
import urllib.parse
import urllib.request
from typing import Optional, Any

from playwright.async_api import async_playwright

from fretio.providers.base import ProviderBase, _kill_proc, _register_owned_proc
from fretio.providers.provider_utils import (
    _digits, _fmt_decimal, _parse_decimal_any, _parse_int_any
)
from fretio.providers.alfa_browser import AlfaBrowserMixin
from fretio.models import Cotacao
from fretio.quotation_contract import QuoteRequest, QuoteResponse
from fretio.logging_conf import get_logger

logger = get_logger(__name__)


class AlfaProvider(AlfaBrowserMixin, ProviderBase):
    """Provider Alfa com login manual e cotacao automatizada."""

    BASE_URL = "https://areadocliente.alfatransportes.com.br"
    LOGIN_URL = "https://areadocliente.alfatransportes.com.br/login/"
    COTACAO_URL = "https://areadocliente.alfatransportes.com.br/cotacao/"
    COTACAO_API_URL = "https://areadocliente.alfatransportes.com.br/cotacao/api/"
    LOGIN_MAX_WAIT_S = 120
    _digits = staticmethod(_digits)
    _fmt_decimal = staticmethod(_fmt_decimal)
    _parse_decimal_any = staticmethod(_parse_decimal_any)
    _parse_int_any = staticmethod(_parse_int_any)

    def __init__(
        self,
        login: str,
        senha: str,
        login_url: str = "",
        cotacao_url: str = "",
        headless: bool = False,
    ) -> None:
        super().__init__(nome="ALFA")
        self.login = str(login or "").strip()
        self.senha = str(senha or "").strip()
        self.headless = bool(headless)

        login_url = str(login_url or "").strip()
        if "arearestrita.alfatransportes.com.br" in login_url:
            login_url = login_url.replace("arearestrita.alfatransportes.com.br", "areadocliente.alfatransportes.com.br")
        self.login_url = login_url or self.LOGIN_URL

        cotacao_url = str(cotacao_url or "").strip()
        if "arearestrita.alfatransportes.com.br" in cotacao_url:
            cotacao_url = cotacao_url.replace("arearestrita.alfatransportes.com.br", "areadocliente.alfatransportes.com.br")

        if cotacao_url and "/api/" in str(cotacao_url):
            self.cotacao_api_url = str(cotacao_url).strip()
            self.cotacao_url = self.COTACAO_URL
        else:
            self.cotacao_url = str(cotacao_url or self.COTACAO_URL).strip()
            self.cotacao_api_url = self.COTACAO_API_URL

        self.last_error: str | None = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._logged_in = False
        self._cdp_session = None
        self._window_id = None
        self._chrome_proc = None
        self._debug_port = 0

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _format_doc(value: str) -> str:
        digits = re.sub(r"\D", "", str(value or ""))
        if len(digits) == 11:
            return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
        if len(digits) == 14:
            return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"
        return digits

    @staticmethod
    def _calc_cubagem_m3(cubagens: Optional[list[dict]]) -> float:
        total = 0.0
        if not isinstance(cubagens, list):
            return total
        for row in cubagens:
            if not isinstance(row, dict):
                continue
            try:
                qtd = int(row.get("quantidade", 0) or 0)
                comp = float(row.get("comprimento_cm", 0) or 0)
                larg = float(row.get("largura_cm", 0) or 0)
                alt = float(row.get("altura_cm", 0) or 0)
            except Exception:
                continue
            if qtd <= 0 or comp <= 0 or larg <= 0 or alt <= 0:
                continue
            total += (comp * larg * alt / 1_000_000.0) * qtd
        return total

    @staticmethod
    def _sum_volumes(cubagens: Optional[list[dict]], fallback: int) -> int:
        if not isinstance(cubagens, list):
            return int(fallback or 0)
        total = 0
        for row in cubagens:
            if not isinstance(row, dict):
                continue
            try:
                qtd = int(row.get("quantidade", 0) or 0)
            except Exception:
                qtd = 0
            total += max(qtd, 0)
        return total if total > 0 else int(fallback or 0)

    @staticmethod
    def _today_str() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    # ── login ─────────────────────────────────────────────────────────

    _DEBUG_SCREENSHOT_RETENTION = 20

    async def _save_debug_screenshot(self, suffix: str = "") -> None:
        """Salva screenshot para diagnóstico em ~/.fretio/alfa_debug/ (só com FRETIO_DEBUG_DUMP)."""
        # Screenshot full-page expõe CNPJ/endereço; só captura sob flag explícita.
        if not os.environ.get("FRETIO_DEBUG_DUMP"):
            return
        try:
            debug_dir = os.path.join(os.path.expanduser("~"), ".fretio", "alfa_debug")
            os.makedirs(debug_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = f"alfa_{ts}_{suffix}.png" if suffix else f"alfa_{ts}.png"
            fpath = os.path.join(debug_dir, fname)
            if self._page:
                await self._page.screenshot(path=fpath, full_page=True)
                logger.info("[ALFA] Screenshot salvo: %s", fpath)
                self._prune_debug_screenshots(debug_dir)
        except Exception as e:
            logger.debug("[ALFA] Falha ao salvar screenshot: %s", e)

    @classmethod
    def _prune_debug_screenshots(cls, debug_dir: str) -> None:
        try:
            arquivos = [
                os.path.join(debug_dir, n)
                for n in os.listdir(debug_dir)
                if n.startswith("alfa_") and n.endswith(".png")
            ]
            arquivos.sort(key=os.path.getmtime, reverse=True)
            for antigo in arquivos[cls._DEBUG_SCREENSHOT_RETENTION:]:
                try:
                    os.remove(antigo)
                except OSError:
                    pass
        except OSError:
            pass

    async def _form_is_ready(self, page: Any) -> bool:
        """Verifica se o formulário de cotação da Alfa está renderizado e pronto."""
        try:
            url_low = (getattr(page, "url", "") or "").lower()
            if "cotacao" not in url_low:
                return False
            return bool(await page.evaluate("""() => {
                const required = ['#tipoPagador', '#pesoMercadoria', '#valorMercadoria', '#cnpjRemetente'];
                return required.every(s => {
                    const el = document.querySelector(s);
                    return el && (el.offsetParent !== null || el.offsetWidth > 0 || el.offsetHeight > 0);
                });
            }"""))
        except Exception:
            return False

    async def _wait_for_form(self, page: Any, timeout_ms: int = 10000) -> bool:
        loops = max(1, timeout_ms // 250)
        for _ in range(loops):
            if await self._form_is_ready(page):
                return True
            await page.wait_for_timeout(250)
        return False

    async def _navegar_para_cotacao(self) -> bool:
        """Navega para o formulário de cotação: direto por URL ou via menu/sidebar."""
        page = self._page
        try:
            current = page.url.lower()
            logger.info("[ALFA] _navegar_para_cotacao — URL atual: %s", page.url)

            # Se já está na página com o formulário pronto
            if "login" not in current and await self._form_is_ready(page):
                logger.info("[ALFA] Já estava na página de cotação com formulário pronto")
                return True

            # Após cotação anterior, pode ter botão "Fazer outra Cotação"
            try:
                btn_outra = page.locator("a[href*='/cotacao/api/'], a[href*='cotacao'], button:has-text('outra'), a:has-text('outra'), button:has-text('Nova'), a:has-text('Nova')").first
                if await btn_outra.is_visible(timeout=1000):
                    await btn_outra.click()
                    logger.info("[ALFA] Clicou em botão de nova cotação")
                    if await self._wait_for_form(page, 5000):
                        return True
            except Exception:
                pass

            # Tentativas de navegação direta por URLs conhecidas
            candidate_urls = [
                self.cotacao_api_url,
                self.cotacao_url,
                f"{self.BASE_URL}/cotacao/api/",
                f"{self.BASE_URL}/cotacao/",
                f"{self.BASE_URL}/cotacoes/",
            ]
            seen_urls = set()
            urls_to_try = []
            for u in candidate_urls:
                u_clean = str(u or "").strip()
                if u_clean and u_clean not in seen_urls:
                    seen_urls.add(u_clean)
                    urls_to_try.append(u_clean)

            for target_url in urls_to_try:
                try:
                    logger.info("[ALFA] Tentando navegação direta para %s", target_url)
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
                    if await self._wait_for_form(page, 8000):
                        logger.info("[ALFA] Formulário pronto via navegação direta (%s)", target_url)
                        return True
                except Exception as e:
                    logger.debug("[ALFA] Navegação direta para %s falhou: %s", target_url, e)

            # Fallback: volta à página base e navega via menu / sidebar / cards
            await page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1000)

            # Clicar no menu lateral (#appSidebar) ou cards
            card_clicked = await page.evaluate("""() => {
                // Abre grupos da sidebar recolhidos
                document.querySelectorAll('.app-nav-group, details').forEach(d => { d.open = true; });

                const isMatch = (txt) => {
                    const t = (txt || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
                    return t.includes('cota') || t.includes('painel de cota') || t.includes('solicitar cota') || t.includes('frete');
                };

                const directLinks = document.querySelectorAll('a[href*="cotacao"], a[href*="cotac"], a[href*="cota"]');
                for (const a of directLinks) {
                    if (a.offsetParent !== null || a.offsetWidth > 0 || a.offsetHeight > 0) {
                        a.click();
                        return true;
                    }
                }

                const elements = document.querySelectorAll('a.app-nav-link, div.opcoes, div.card, div.col, .nav-link, .dropdown-item, button, a, div[onclick]');
                for (const el of elements) {
                    if (isMatch(el.textContent)) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }""")

            if not card_clicked:
                logger.warning("[ALFA] Não encontrou link/menu de cotação")
                await self._save_debug_screenshot("card_nao_encontrado")
            else:
                logger.info("[ALFA] Clicou no link/menu de cotação")
                if await self._wait_for_form(page, 5000):
                    return True

            # Clicar em "Nova Cotação" se estiver no painel intermediário
            nova_clicked = await page.evaluate("""() => {
                const isMatchNova = (txt) => {
                    const t = (txt || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
                    return (t.includes('nova') && t.includes('cota')) || t.includes('solicitar') || t.includes('fazer cota') || t.includes('criar cota') || t.includes('+ cota');
                };

                const links = document.querySelectorAll('a[href*="/cotacao/api"], a[href*="novo"], a[href*="nova"], a, button');
                for (const el of links) {
                    if (isMatchNova(el.textContent) || (el.getAttribute('href') || '').includes('/cotacao/api')) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }""")

            if nova_clicked:
                logger.info("[ALFA] Clicou em 'Nova Cotação'")

            if await self._wait_for_form(page, 10000):
                return True

            logger.warning("[ALFA] Formulário não renderizou após navegação por menu")
            await self._save_debug_screenshot("formulario_nao_renderizou")
            return False
        except Exception as e:
            logger.warning("[ALFA] _navegar_para_cotacao falhou com exceção: %s", e)
            return False

    async def _is_logged_in(self) -> bool:
        """Verifica se está autenticado navegando até cotação via menu."""
        page = self._page
        try:
            current_url = page.url.lower()
            logger.info("[ALFA] _is_logged_in — URL atual: %s", page.url)
            if "login" in current_url:
                logger.info("[ALFA] Ainda na página de login")
                return False
            return await self._navegar_para_cotacao()
        except Exception as e:
            logger.warning("[ALFA] _is_logged_in check falhou: %s", e)
            return False

    async def _login(self) -> bool:
        if self._logged_in:
            return True

        await self._init_browser()

        # ── Headless: login direto via Playwright (Turnstile não funciona) ──
        if self.headless:
            page = self._page
            await page.goto(self.login_url, wait_until="domcontentloaded", timeout=60000)
            await page.locator("#username").fill(self.login)
            await page.locator("#password").fill(self.senha)
            try:
                await page.locator("#btn-enviar").click(timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(5000)
            if "login" not in page.url.lower():
                self._logged_in = True
                return True
            self.last_error = "Login Alfa falhou (headless, Turnstile bloqueou)"
            return False

        # ── Não-headless ──
        # Verifica se já está logado (sessão persistente do user-data-dir)
        current_url = self._get_page_url_sync()
        if current_url and "alfatransportes.com.br" in current_url.lower() and "login" not in current_url.lower():
            await self._connect_playwright()
            if await self._is_logged_in():
                self._logged_in = True
                await self._ocultar_janela()
                self._set_taskbar_visible(False)
                return True

        # Turnstile necessário: desconecta Playwright
        await self._disconnect_playwright()

        # Aguarda os campos e confirma o preenchimento via CDP direto.
        fill_js = (
            "(function(){"
            f"var u=document.querySelector('#username');"
            f"var p=document.querySelector('#password');"
            f"if(u){{u.value={json.dumps(self.login)};u.dispatchEvent(new Event('input',{{bubbles:true}}));u.dispatchEvent(new Event('change',{{bubbles:true}}));}}"
            f"if(p){{p.value={json.dumps(self.senha)};p.dispatchEvent(new Event('input',{{bubbles:true}}));p.dispatchEvent(new Event('change',{{bubbles:true}}));}}"
            f"return !!u && !!p && u.value==={json.dumps(self.login)} && p.value==={json.dumps(self.senha)};}})();"
        )
        filled = False
        for _ in range(40):
            if await self._cdp_eval_raw(fill_js) is True:
                filled = True
                break
            await asyncio.sleep(0.25)
        if not filled:
            self.last_error = "Login Alfa: campos de acesso não ficaram disponíveis para preenchimento"
            return False
        logger.info("[ALFA] Credenciais preenchidas via CDP direto (sem Playwright)")

        # Script para submeter quando o botão estiver habilitado pelo Turnstile
        try_submit_js = """(function(){
            var b = document.querySelector('#btn-enviar');
            if (b && !b.disabled && document.querySelector('#username')?.value && document.querySelector('#password')?.value) {
                b.click();
                return true;
            }
            return false;
        })();"""

        # Tenta submeter imediatamente
        await self._cdp_eval_raw(try_submit_js)

        # Aguarda até 15s por auto-pass do Turnstile (janela fica oculta)
        logger.info("[ALFA] Tentando auto-login (sem janela)...")
        auto_pass = False
        for _ in range(30):
            await asyncio.sleep(0.5)
            # Tenta clicar caso o Turnstile tenha acabado de validar
            await self._cdp_eval_raw(try_submit_js)
            url = self._get_page_url_sync()
            if url and "alfatransportes.com.br" in url.lower() and "login" not in url.lower():
                auto_pass = True
                break

        if not auto_pass:
            # Auto-pass falhou — mostra janela para resolução manual do Turnstile
            self._ensure_chrome_visible()
            self._set_taskbar_visible(True)
            logger.info("[ALFA] Aguardando usuario resolver Turnstile e clicar Continuar...")

            for _ in range(self.LOGIN_MAX_WAIT_S):
                await asyncio.sleep(0.5)
                # Se o Turnstile resolver enquanto a janela estiver aberta, tenta clicar
                await self._cdp_eval_raw(try_submit_js)
                url = self._get_page_url_sync()
                if url and "alfatransportes.com.br" in url.lower() and "login" not in url.lower():
                    break
            else:
                self.last_error = "Login Alfa timeout (aguardando login manual)"
                logger.error(f"[ALFA] {self.last_error}")
                return False

        # Login OK — espera a página estabilizar antes de conectar Playwright
        logger.info("[ALFA] Login detectado! Aguardando página estabilizar...")
        await asyncio.sleep(1)

        # Conecta Playwright (Turnstile já passou)
        await self._connect_playwright()
        # Oculta janela e taskbar após login
        await self._ocultar_janela()
        self._set_taskbar_visible(False)

        # Verifica se o formulário de cotação renderizou (com retry)
        for attempt in range(3):
            if await self._is_logged_in():
                self._logged_in = True
                logger.info("[ALFA] Login OK — Playwright conectado após Turnstile")
                return True
            logger.info(f"[ALFA] Formulário não renderizou (tentativa {attempt+1}/3), aguardando...")
            await asyncio.sleep(1.5)

        self._logged_in = False
        self.last_error = "Login realizado mas formulário não renderizou após 3 tentativas"
        logger.error(f"[ALFA] {self.last_error}")
        return False

    async def pre_login(self) -> None:
        try:
            await self._login()
        except Exception as e:
            logger.warning(f"[ALFA] Pre-login falhou: {e}")

    # ── cotacao ───────────────────────────────────────────────────────

    async def _preencher_formulario(
        self,
        *,
        cnpj_remetente: str,
        cnpj_destinatario: str,
        cep_remetente: str,
        cep_destinatario: str,
        peso: float,
        valor: float,
        volumes: int,
        cubagem_m3: float,
        tipo_pagador: str = "1",
    ) -> None:
        page = self._page

        # Navega para cotação via menu (não por URL direta)
        logger.info("[ALFA] Iniciando navegação para formulário de cotação...")
        if not await self._navegar_para_cotacao():
            # Se caiu na tela de login, precisa relogar
            if "login" in page.url.lower():
                logger.info("[ALFA] Sessão expirou, refazendo login...")
                self._logged_in = False
                if not await self._login():
                    raise RuntimeError("Login Alfa falhou")
                # Garante janela oculta após re-login por sessão expirada
                if not self.headless:
                    await self._ocultar_janela()
                    self._set_taskbar_visible(False)

            if not await self._navegar_para_cotacao():
                logger.error("[ALFA] Navegação para cotação falhou")
                await self._save_debug_screenshot("navegacao_falhou")
                raise RuntimeError("Não conseguiu navegar para cotação")

        # Espera o formulário renderizar
        if not await self._wait_for_form(page, 10000):
            raise RuntimeError("Formulário de cotação não carregou")

        try:
            await page.select_option("#tipoPagador", str(tipo_pagador))
        except Exception:
            pass
        try:
            await page.select_option("#tipoCarga", "0")
        except Exception:
            pass
        try:
            await page.select_option("#tipoZona", "0")
        except Exception:
            pass
        await page.wait_for_timeout(200)

        # Preenche todos os campos via JS de uma vez (mais rápido que fills individuais)
        await page.evaluate(
            """(data) => {
                function setVal(selectors, val) {
                    const list = Array.isArray(selectors) ? selectors : [selectors];
                    for (const sel of list) {
                        const el = document.querySelector(sel);
                        if (!el) continue;
                        el.value = val;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        break;
                    }
                }
                function setSelect(selectors, val) {
                    const list = Array.isArray(selectors) ? selectors : [selectors];
                    for (const sel of list) {
                        const el = document.querySelector(sel);
                        if (!el) continue;
                        el.value = val;
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        break;
                    }
                }
                setSelect(['#tipoPagador', 'select[name="tipoPagador"]', 'select[name*="pagador"]'], data.tipoPagador);
                setVal(['#pesoMercadoria', 'input[name="pesoMercadoria"]', 'input[name*="peso"]'], data.peso);
                setVal(['#valorMercadoria', 'input[name="valorMercadoria"]', 'input[name*="valor"]'], data.valor);
                setVal(['#dataInicialColeta', 'input[name="dataInicialColeta"]', 'input[name*="data"]'], data.data);
                setVal(['#totalVolumes', 'input[name="totalVolumes"]', 'input[name*="volume"]'], data.volumes);
                setVal(['#totalCubagem', 'input[name="totalCubagem"]', 'input[name*="cubagem"]'], data.cubagem);
                setSelect(['#tipoCarga', 'select[name="tipoCarga"]'], '0');
                setSelect(['#tipoZona', 'select[name="tipoZona"]'], '0');
            }""",
            {
                "tipoPagador": tipo_pagador,
                "peso": self._fmt_decimal(peso, 3, comma=True),
                "valor": self._fmt_decimal(valor, 2, comma=True),
                "data": self._today_str(),
                "volumes": str(int(volumes or 0)),
                "cubagem": self._fmt_decimal(cubagem_m3, 3, comma=False),
            },
        )

        # Preenche CNPJs e CEPs por último para não ser sobrescrito pelo framework da página
        await page.wait_for_timeout(300)
        await page.evaluate(
            """(data) => {
                function setVal(selectors, val) {
                    const list = Array.isArray(selectors) ? selectors : [selectors];
                    for (const sel of list) {
                        const el = document.querySelector(sel);
                        if (!el) continue;
                        el.value = val;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('blur', {bubbles: true}));
                        break;
                    }
                }
                setVal(['#cnpjRemetente', 'input[name="cnpjRemetente"]', 'input[name*="cnpjRem"]'], data.cnpjRem);
                setVal(['#cepRemetente', 'input[name="cepRemetente"]', 'input[name*="cepRem"]'], data.cepRem);
                setVal(['#cnpjDestinatario', 'input[name="cnpjDestinatario"]', 'input[name*="cnpjDest"]'], data.cnpjDest);
                setVal(['#cepDestinatario', 'input[name="cepDestinatario"]', 'input[name*="cepDest"]'], data.cepDest);
            }""",
            {
                "cnpjRem": self._format_doc(cnpj_remetente),
                "cepRem": self._digits(cep_remetente),
                "cnpjDest": self._format_doc(cnpj_destinatario),
                "cepDest": self._digits(cep_destinatario),
            },
        )

    async def _do_submit_click(self, submit_btn) -> None:
        """Clica no botão submit com fallbacks."""
        try:
            if submit_btn and await submit_btn.is_visible(timeout=1500):
                await submit_btn.click(timeout=6000)
                return
        except Exception:
            pass
        logger.info("[ALFA] Tentando submit via seletores DOM alternativos...")
        clicked = await self._page.evaluate("""() => {
            const selectors = [
                "button[type='submit']", "input[type='submit']",
                "button.btn-alfa", "button.btn-primary",
                "#btnCalcular", "#btn-cotar"
            ];
            const form = document.querySelector('#pesoMercadoria')?.closest('form');
            if (!form) return false;
            for (const s of selectors) {
                const el = form.querySelector(s);
                if (el) { el.click(); return true; }
            }
            const buttons = form.querySelectorAll('button');
            for (const b of buttons) {
                const t = (b.textContent || '').toLowerCase();
                if (t.includes('calcular') || t.includes('cotar') || t.includes('enviar') || t.includes('continuar')) {
                    b.click();
                    return true;
                }
            }
            return false;
        }""")
        if not clicked:
            logger.warning("[ALFA] Não encontrou botão de submit")

    async def _extrair_resultado(self, api_response=None) -> Optional[Cotacao]:
        page = self._page

        valor_frete = None
        prazo_dias = 0

        # Tenta extrair da response da API (já capturada durante o click)
        if api_response is not None and asyncio.iscoroutine(api_response):
            api_response = await api_response
        if api_response is not None and api_response.ok:
            try:
                data = await api_response.json()
                valor_frete = self._find_json_value(data, ["frete", "valor", "total"])
                prazo_dias = int(self._find_json_value(data, ["prazo", "dia", "dias"]) or 0)
            except Exception:
                valor_frete = None
                prazo_dias = 0

        # Fallback: extrai do DOM (resultado já está renderizado)
        if valor_frete is None:
            await page.wait_for_timeout(500)
            body_txt = await page.inner_text("body")
            body_txt = (body_txt or "").replace("\xa0", " ")
            m_val = re.search(r"R\$\s*([\d.]+,\d{2})", body_txt)
            if m_val:
                valor_frete = self._parse_decimal_any(m_val.group(1))
            m_prazo = re.search(r"(\d+)\s*dias?", body_txt, re.IGNORECASE)
            if m_prazo:
                prazo_dias = int(m_prazo.group(1))

        if valor_frete is None:
            self.last_error = "ALFA: valor de frete nao encontrado"
            return None

        return Cotacao(
            transportadora=self.nome,
            prazo_dias=int(prazo_dias or 0),
            valor_frete=round(float(valor_frete), 2),
            restricoes="Cotacao via portal Alfa",
            timestamp=datetime.now(),
        )

    def _find_json_value(self, data: Any, keys: list[str]) -> float | None:
        if isinstance(data, dict):
            for k, v in data.items():
                k_low = str(k).lower()
                if any(key in k_low for key in keys):
                    parsed = self._parse_decimal_any(v)
                    if parsed is not None:
                        return parsed
                nested = self._find_json_value(v, keys)
                if nested is not None:
                    return nested
        elif isinstance(data, list):
            for item in data:
                nested = self._find_json_value(item, keys)
                if nested is not None:
                    return nested
        return None

    async def coteir(
        self,
        origem: str,
        destino: str,
        peso: float,
        valor: float,
        volumes: int = 1,
        cubagem_m3: float = 0.0,
        comprimento_cm: int = 0,
        largura_cm: int = 0,
        altura_cm: int = 0,
        cnpj_remetente: str = "",
        cnpj_destinatario: str = "",
        cubagens: Optional[list[dict]] = None,
        tipo_pagador: str = "1",
    ) -> Optional[Cotacao]:
        try:
            self.last_error = None
            if not await self._login():
                return None

            # Garante que a janela está oculta após login (mesmo que Turnstile tenha sido resolvido)
            if not self.headless:
                await self._ocultar_janela()
                self._set_taskbar_visible(False)

            vol_total = self._sum_volumes(cubagens, volumes)
            cub_total = self._calc_cubagem_m3(cubagens)
            if cub_total <= 0:
                if cubagem_m3 and float(cubagem_m3) > 0:
                    cub_total = float(cubagem_m3)
                elif comprimento_cm > 0 and largura_cm > 0 and altura_cm > 0 and vol_total > 0:
                    cub_total = (float(comprimento_cm) * float(largura_cm) * float(altura_cm) / 1_000_000.0) * vol_total

            await self._preencher_formulario(
                cnpj_remetente=cnpj_remetente,
                cnpj_destinatario=cnpj_destinatario,
                cep_remetente=origem,
                cep_destinatario=destino,
                peso=peso,
                valor=valor,
                volumes=vol_total,
                cubagem_m3=cub_total,
                tipo_pagador=tipo_pagador,
            )

            submit_btn = self._page.locator("form:has(#pesoMercadoria) button[type='submit'], form:has(#pesoMercadoria) input[type='submit']").first
            try:
                await submit_btn.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass

            # Captura a response da API durante o click para não perder
            # respostas rápidas (causa raiz do delay de 30s)
            api_response = None
            try:
                async with self._page.expect_response(
                    lambda r: (self.cotacao_api_url in r.url or "cotacao" in r.url.lower() or "calcul" in r.url.lower()) and r.request.method.upper() in {"POST", "GET"},
                    timeout=15000,
                ) as response_info:
                    await self._do_submit_click(submit_btn)
                api_response = response_info.value
                if asyncio.iscoroutine(api_response) or asyncio.isfuture(api_response):
                    api_response = await api_response
            except Exception:
                # Se expect_response falhar (timeout ou click falhou antes),
                # continua sem response — fallback DOM será usado
                pass

            return await self._extrair_resultado(api_response)

        except Exception as e:
            self.last_error = str(e)
            logger.error(f"[ALFA] Erro na cotacao: {e}")
            return None

    async def cotar(self, request: QuoteRequest) -> QuoteResponse:
        return await super().cotar(request)
