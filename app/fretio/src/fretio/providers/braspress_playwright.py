"""Provider Braspress - Versão com Playwright para contornar antibot."""
from datetime import datetime
from typing import Optional
import asyncio
import json
import re

from playwright.async_api import async_playwright

from fretio.providers.base import ProviderBase
from fretio.providers.braspress_browser import BraspressBrowserMixin
from fretio.providers.provider_utils import (
    _digits, _fmt_decimal, _parse_decimal_any, _parse_int_any
)
from fretio.models import Cotacao
from fretio.quotation_contract import QuoteRequest, QuoteResponse
from fretio.logging_conf import get_logger

logger = get_logger(__name__)


class BraspressPlaywrightProvider(BraspressBrowserMixin, ProviderBase):
    """Provider Braspress via Playwright para contornar antibot."""

    LOGIN_URL = "https://www.braspress.com.br/w/cliente/view"
    COTACAO_URL = "https://www.braspress.com.br/w/cotacao/view"
    _TURNSTILE_AUTO_S = 8
    _TURNSTILE_USUARIO_S = 180
    _digits = staticmethod(_digits)
    _fmt_decimal = staticmethod(_fmt_decimal)
    _parse_decimal_any = staticmethod(_parse_decimal_any)
    _parse_int_any = staticmethod(_parse_int_any)

    def __init__(self, cnpj: str, senha: str, headless: bool = True):
        super().__init__(nome="Braspress")
        self.cnpj = _digits(cnpj)
        self.senha = senha
        self.headless = headless
        self.last_error: str | None = None
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._logged_in = False
        self._captcha_visivel = False
        self._janela_captcha_aberta = False
        self._login_lock = asyncio.Lock()
        self._passo_atual = "inicio"

    @staticmethod
    def _normalizar_cubagens_cm(cubagens: Optional[list[dict]]) -> list[dict]:
        # Consolida por dimensão para só abrir nova linha quando houver tamanho diferente.
        agrupadas: dict[tuple[int, int, int], int] = {}
        if not isinstance(cubagens, list):
            return []
        for row in cubagens:
            if not isinstance(row, dict):
                continue
            try:
                qtd = int(row.get("quantidade", 0) or 0)
                comp = int(row.get("comprimento_cm", 0) or 0)
                larg = int(row.get("largura_cm", 0) or 0)
                alt = int(row.get("altura_cm", 0) or 0)
            except Exception:
                continue
            if qtd <= 0 or comp <= 0 or larg <= 0 or alt <= 0:
                continue
            key = (comp, larg, alt)
            agrupadas[key] = agrupadas.get(key, 0) + qtd

        return [
            {
                "quantidade": int(qtd),
                "comprimento_cm": int(comp),
                "largura_cm": int(larg),
                "altura_cm": int(alt),
            }
            for (comp, larg, alt), qtd in agrupadas.items()
        ]

    async def _fechar_modais_genericos(self, page):
        """Remove modais promocionais (ex: upload CTE / 'otimizar emissão') que bloqueiam o resultado."""
        try:
            removed = await page.evaluate("""() => {
                let count = 0;
                const modals = document.querySelectorAll('.modal.in, .modal[style*="block"]');
                for (const m of modals) {
                    const txt = (m.innerText || '').toLowerCase();
                    if (
                        txt.includes('otimizar') ||
                        txt.includes('cotação online') ||
                        txt.includes('conhecimento de embarque') ||
                        txt.includes('visando otimizar')
                    ) {
                        m.remove();
                        count++;
                    }
                }
                if (count > 0) {
                    document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
                    document.body.classList.remove('modal-open');
                    document.body.style.removeProperty('overflow');
                    document.body.style.removeProperty('padding-right');
                }
                return count;
            }""")
            if removed:
                logger.info(f"[Braspress] Removeu {removed} modal(is) promocional(is)")
                await page.wait_for_timeout(200)
        except Exception:
            pass

    async def _fechar_alert_xml_modal(self, page):
        """Remove o modal #alertXmlModal e backdrop do DOM (incondicional)."""
        try:
            removed = await page.evaluate("""() => {
                const m = document.getElementById('alertXmlModal');
                if (!m) return false;
                m.remove();
                document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
                document.body.classList.remove('modal-open');
                document.body.style.removeProperty('overflow');
                document.body.style.removeProperty('padding-right');
                return true;
            }""")
            if removed:
                logger.info("[Braspress] Modal #alertXmlModal removido do DOM")
                await page.wait_for_timeout(200)
        except Exception:
            pass

    async def _abrir_formulario_login(self) -> None:
        self._passo_atual = "login"
        await self._page.goto(self.LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        await self._page.locator("#inputLogin").wait_for(state="visible", timeout=15000)
        await self._page.locator("#inputLogin").fill(self.cnpj)
        await self._page.locator('input[name="pass"]').fill(self.senha)

    async def _ler_turnstile(self) -> dict:
        try:
            state = await self._page.evaluate(
                """() => {
                    const widget = document.querySelector('.cf-turnstile, iframe[src*="challenges.cloudflare.com"]');
                    const field = document.querySelector('[name="cf-turnstile-response"]');
                    const token = field && field.value ? String(field.value) : '';
                    return { present: !!(widget || field), token: token };
                }"""
            )
        except Exception:
            return {"present": False, "token": ""}
        if not isinstance(state, dict):
            return {"present": False, "token": ""}
        return {
            "present": bool(state.get("present")),
            "token": str(state.get("token") or ""),
        }

    async def _aguardar_token_turnstile(self, seconds: float) -> str:
        """Espera o Cloudflare gravar o token. Widget ausente não libera o envio."""
        loops = max(1, int(seconds * 2))
        for _ in range(loops):
            state = await self._ler_turnstile()
            token = state["token"].strip()
            if token:
                return token
            await self._page.wait_for_timeout(500)
        return ""

    def _js_preencher_login(self) -> str:
        cnpj = json.dumps(self.cnpj)
        senha = json.dumps(self.senha)
        return (
            "(function(){"
            "var u=document.querySelector('#inputLogin');"
            "var p=document.querySelector('input[name=\"pass\"]');"
            f"if(u){{u.value={cnpj};u.dispatchEvent(new Event('input',{{bubbles:true}}));}}"
            f"if(p){{p.value={senha};p.dispatchEvent(new Event('input',{{bubbles:true}}));}}"
            "return !!(u&&p&&u.value&&p.value);"
            "})()"
        )

    async def _soltar_chrome_para_captcha(self) -> bool:
        # O Turnstile recusa o clique enquanto o Playwright está ligado na página.
        detach = getattr(self._browser, "detach", None) if self._browser else None
        if not callable(detach):
            return False
        proc, profile, port = await detach()
        self._browser = None
        self._context = None
        self._page = None
        self._chrome_proc = proc
        self._profile_dir = profile
        self._debug_port = port
        return bool(port)

    async def _reconectar_chrome(self) -> None:
        port = getattr(self, "_debug_port", None)
        proc = getattr(self, "_chrome_proc", None)
        if not port or proc is None:
            return
        from playwright.async_api import async_playwright

        from fretio.browser.launcher import _ChromeBrowser
        from fretio.providers.provider_utils import apply_performance_route_filters

        pw = await async_playwright().start()
        inner = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        self._browser = _ChromeBrowser(inner, proc, getattr(self, "_profile_dir", None), pw)
        self._chrome_proc = None
        self._context = inner.contexts[0]
        await apply_performance_route_filters(self._context)
        pages = list(self._context.pages)
        self._page = next((p for p in pages if "braspress.com" in (p.url or "")), None)
        if self._page is None:
            self._page = pages[0] if pages else await self._context.new_page()
        self._page.set_default_timeout(30000)

    async def _aguardar_captcha_sem_playwright(self) -> bool:
        from fretio.browser.cdp_raw import cdp_eval

        port = int(self._debug_port)
        hint = "braspress.com"
        await cdp_eval(port, f"location.href = {json.dumps(self.LOGIN_URL)}", host_hint=hint)
        ready = False
        for _ in range(20):
            await asyncio.sleep(0.5)
            if await cdp_eval(port, "!!document.querySelector('#inputLogin')", host_hint=hint) is True:
                ready = True
                break
        if not ready:
            return False
        if await cdp_eval(port, self._js_preencher_login(), host_hint=hint) is not True:
            return False
        await cdp_eval(
            port,
            "document.title = 'Fretio — conclua o captcha da Braspress'",
            host_hint=hint,
        )
        estado_js = """(() => {
            const field = document.querySelector('[name="cf-turnstile-response"]');
            const body = (document.body && document.body.innerText) || '';
            const errEl = document.querySelector('.login-error');
            const err = errEl ? (errEl.innerText || '').replace(/\\s+/g, ' ').trim() : '';
            return {
                tokenLen: field && field.value ? field.value.length : 0,
                loggedIn: body.toLowerCase().includes('sair'),
                loginError: err.slice(0, 180)
            };
        })()"""
        clique_js = """(() => {
            const b = document.querySelector('input[type="submit"][value="Acessar"]');
            if (!b) return false;
            b.click();
            return true;
        })()"""
        clicou = False
        for _ in range(self._TURNSTILE_USUARIO_S):
            await asyncio.sleep(1)
            state = await cdp_eval(port, estado_js, host_hint=hint)
            if not isinstance(state, dict):
                continue
            if state.get("loggedIn"):
                return True
            if int(state.get("tokenLen") or 0) > 20 and not clicou:
                await cdp_eval(port, clique_js, host_hint=hint)
                clicou = True
                logger.info("[Braspress] Captcha concluído, entrando no portal")
            erro = str(state.get("loginError") or "").strip()
            if clicou and erro:
                self.last_error = f"Login falhou: {erro}"
        return clicou and not self.last_error

    async def _abrir_chrome_limpo_para_captcha(self) -> None:
        import socket

        from fretio.browser.cdp_raw import list_debug_targets, pick_page_target
        from fretio.browser.launcher import launch_human_chrome

        proc, port, profile = launch_human_chrome(self.LOGIN_URL)
        self._chrome_proc = proc
        self._debug_port = port
        self._profile_dir = profile
        # O chrome.exe inicial fecha depois de criar o processo da janela.
        # A porta de depuração é que indica que a janela ficou aberta.
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Chrome da Braspress não respondeu para o captcha")
        for _ in range(40):
            try:
                target = pick_page_target(list_debug_targets(port), host_hint="braspress.com")
            except Exception:
                target = None
            if target and "braspress.com" in str(target.get("url") or ""):
                logger.info("[Braspress] Chrome normal aberto para o captcha")
                return
            await asyncio.sleep(0.25)
        raise RuntimeError("Página de login da Braspress não abriu")

    async def _aguardar_captcha_humano(self) -> bool:
        """Espera o clique humano sem chamar o depurador enquanto a caixinha está na tela."""
        from fretio.browser.cdp_raw import cdp_eval, list_debug_targets, pick_page_target

        port = int(self._debug_port)
        hint = "braspress.com"
        # Uma única ligação, só para preencher a senha. O socket fecha em seguida.
        await asyncio.sleep(1.5)
        preenchido = await cdp_eval(port, self._js_preparar_captcha_humano(), host_hint=hint)
        if preenchido is not True:
            self.last_error = "Login falhou: não foi possível preencher o login da Braspress"
            return False
        clicou = False
        for _ in range(self._TURNSTILE_USUARIO_S):
            await asyncio.sleep(1)
            try:
                target = pick_page_target(list_debug_targets(port), host_hint=hint) or {}
            except Exception:
                target = {}
            title = str(target.get("title") or "")
            if "LOGADO" in title:
                return True
            if "CAPTCHA_OK" in title and not clicou:
                await cdp_eval(
                    port,
                    """(() => {
                        const b = document.querySelector('input[type="submit"][value="Acessar"]');
                        if (!b) return false;
                        b.click();
                        return true;
                    })()""",
                    host_hint=hint,
                )
                clicou = True
                logger.info("[Braspress] Captcha concluído, entrando no portal")
            if clicou:
                logged = await cdp_eval(
                    port,
                    "((document.body && document.body.innerText) || '').toLowerCase().includes('sair')",
                    host_hint=hint,
                )
                if logged is True:
                    return True
        return False

    def _js_preparar_captcha_humano(self) -> str:
        cnpj = json.dumps(self.cnpj)
        senha = json.dumps(self.senha)
        return (
            "(function(){"
            "var u=document.querySelector('#inputLogin');"
            "var p=document.querySelector('input[name=\"pass\"]');"
            f"if(u) u.value={cnpj};"
            f"if(p) p.value={senha};"
            "if(!window.__fretioCaptchaWatch){"
            "window.__fretioCaptchaWatch=setInterval(function(){"
            "var field=document.querySelector('[name=\"cf-turnstile-response\"]');"
            "var body=((document.body&&document.body.innerText)||'').toLowerCase();"
            "if(body.indexOf('sair')>=0){document.title='Fretio LOGADO';return;}"
            "if(field&&field.value&&field.value.length>20) document.title='Fretio CAPTCHA_OK';"
            "},400);}"
            "if(document.title.indexOf('Fretio')!==0) document.title='Fretio — confirme que é humano';"
            "return !!(u&&p&&u.value&&p.value);"
            "})()"
        )

    async def _reabrir_para_captcha(self) -> None:
        # O Chrome headless não deixa a pessoa marcar o Turnstile. A janela
        # abre antes do novo carregamento: o widget não renderiza num viewport 1x1.
        # `headless` configurado permanece intacto para a sessão não ser recriada.
        if self.headless and not self._captcha_visivel:
            self._captcha_visivel = True
            await self.cleanup()
            await self._init_browser()
        await self._mostrar_janela_captcha()
        await self._abrir_formulario_login()

    async def _resolver_turnstile_se_necessario(self) -> bool:
        state = await self._ler_turnstile()
        if not state["present"]:
            return True
        token = await self._aguardar_token_turnstile(self._TURNSTILE_AUTO_S)
        if token:
            return True
        self._passo_atual = "aguardando_captcha"
        logger.info("[Braspress] Captcha do login exige confirmação na janela do Chrome")
        if self._browser is not None and callable(getattr(self._browser, "detach", None)):
            await self.cleanup()
            self._captcha_visivel = True
            await self._abrir_chrome_limpo_para_captcha()
            ok = await self._aguardar_captcha_humano()
            if ok:
                await self._reconectar_chrome()
            if ok:
                logger.info("[Braspress] Captcha do login concluído")
                self._captcha_ja_enviado = True
                return True
            if not self.last_error:
                self.last_error = (
                    "Login falhou: captcha da Braspress não resolvido. "
                    "Conclua o desafio na janela do Chrome e tente a cotação de novo."
                )
            logger.error("[Braspress] %s", self.last_error)
            return False
        await self._reabrir_para_captcha()
        token = await self._aguardar_token_turnstile(self._TURNSTILE_USUARIO_S)
        if token:
            logger.info("[Braspress] Captcha do login concluído")
            return True
        self.last_error = (
            "Login falhou: captcha da Braspress não resolvido. "
            "Conclua o desafio na janela do Chrome e tente a cotação de novo."
        )
        logger.error("[Braspress] %s", self.last_error)
        return False

    async def _clicar_acessar(self) -> None:
        # O portal recusa o POST sem o token do Turnstile. O clique só ocorre
        # depois que o widget já gravou esse token, ou quando não há captcha.
        await self._page.locator('input[type="submit"][value="Acessar"]').click()

    async def _mensagem_erro_login(self) -> str:
        try:
            texto = await self._page.locator(".login-error").inner_text()
        except Exception:
            return ""
        texto = " ".join(str(texto or "").split())
        return texto[:180]

    async def _confirmar_login(self) -> bool:
        for _ in range(80):
            await self._page.wait_for_timeout(100)
            try:
                body = await self._page.inner_text("body")
                if "sair" in body.lower():
                    return True
            except Exception:
                continue
            erro = await self._mensagem_erro_login()
            if erro:
                self.last_error = f"Login falhou: {erro}"
                return False
        return False

    async def _login(self) -> bool:
        async with self._login_lock:
            if self._logged_in:
                return True
            logger.info("[Braspress] Fazendo login...")
            self.last_error = None
            try:
                await self._abrir_formulario_login()
                if not await self._resolver_turnstile_se_necessario():
                    return False
                if not getattr(self, "_captcha_ja_enviado", False):
                    await self._clicar_acessar()
                if await self._confirmar_login():
                    self._logged_in = True
                    self._passo_atual = "login_ok"
                    await self._ocultar_janela_captcha()
                    logger.info("[Braspress] Login OK")
                    return True
            except Exception as exc:
                self.last_error = "Login falhou"
                logger.error("[Braspress] Login falhou: %s", exc)
                return False
            if not self.last_error:
                self.last_error = "Login falhou"
            logger.error("[Braspress] %s", self.last_error)
            return False

    async def _cotacao_ainda_no_login(self, page) -> bool:
        try:
            campo = page.locator("#inputLogin")
            return await campo.count() > 0 and await campo.is_visible()
        except Exception:
            return False

    async def _entrar_na_cotacao(self, page) -> bool:
        # /w/cotacao/view mostra ENTRAR mesmo depois do login em /w/cliente/view.
        # Nessa janela o visto do Turnstile já pode estar verde.
        if not await self._cotacao_ainda_no_login(page):
            return True
        logger.info("[Braspress] A cotação pediu o login de novo")
        await page.locator("#inputLogin").fill(self.cnpj)
        await page.locator('input[name="pass"]').fill(self.senha)
        token = ""
        for _ in range(40):
            token = str(
                await page.evaluate(
                    "() => (document.querySelector('[name=\"cf-turnstile-response\"]')?.value || '')"
                )
                or ""
            )
            if len(token) > 20:
                break
            await page.wait_for_timeout(500)
        if len(token) <= 20:
            self.last_error = (
                "Login falhou: captcha da Braspress não resolvido. "
                "Conclua o desafio na janela do Chrome e tente a cotação de novo."
            )
            logger.error("[Braspress] %s", self.last_error)
            return False
        await page.locator('input[type="submit"][value="Acessar"]').click()
        for _ in range(40):
            if not await self._cotacao_ainda_no_login(page):
                return True
            await page.wait_for_timeout(250)
        self.last_error = "Login falhou: a cotação continuou na tela de entrar"
        logger.error("[Braspress] %s", self.last_error)
        return False

    async def _coteir_legacy(
        self,
        origem: str,
        destino: str,
        peso: float,
        valor: float,
        cnpj_destinatario: str | None = None,
        volumes: int = 1,
        comprimento_cm: int = 0,
        largura_cm: int = 0,
        altura_cm: int = 0,
        cubagens: Optional[list[dict]] = None,
        cnpj_remetente: str | None = None,
        tipo_frete: str | None = None,
    ) -> Optional[Cotacao]:
        """Realiza cotação via Playwright."""
        try:
            cubagens_validas = self._normalizar_cubagens_cm(cubagens)
            if not cubagens_validas:
                if volumes > 0 and comprimento_cm > 0 and largura_cm > 0 and altura_cm > 0:
                    cubagens_validas = [
                        {
                            "quantidade": int(volumes),
                            "comprimento_cm": int(comprimento_cm),
                            "largura_cm": int(largura_cm),
                            "altura_cm": int(altura_cm),
                        }
                    ]
                else:
                    self.last_error = "Cubagem inválida: dimensões ausentes no romaneio"
                    logger.error(f"[Braspress] {self.last_error}")
                    return None

            volumes_total = sum(int(c["quantidade"]) for c in cubagens_validas)

            await self._init_browser()
            if not await self._login():
                return None

            # A mesma aba segue para a cotação. Uma aba nova cai de novo
            # na tela ENTRAR de /w/cotacao/view, sem a sessão que acabou de entrar.
            page = self._page
            if page is None or page.is_closed():
                self._page = await self._context.new_page()
                self._page.set_default_timeout(30000)
                page = self._page

            # ── PÁGINA DE COTAÇÃO ──────────────────────────────────
            await page.goto(self.COTACAO_URL, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(500)

            # Verificar se sessão expirou (redirecionou para login)
            current_url = page.url.lower()
            if "cliente/view" in current_url or "login" in current_url:
                logger.warning("[Braspress] Sessão expirada, refazendo login...")
                self._logged_in = False
                if not await self._login():
                    return None
                await page.goto(self.COTACAO_URL, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(500)

            if not await self._entrar_na_cotacao(page):
                return None

            # ── FECHAR MODAL/POPUP ─────────────────────────────────
            for _ in range(3):
                try:
                    btn = page.locator("text=Fechar").first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click()
                        await page.wait_for_timeout(400)
                    else:
                        break
                except Exception:
                    break

            # Fechar modal #alertXmlModal se estiver visível (bloqueia cliques)
            await self._fechar_alert_xml_modal(page)

            await page.wait_for_timeout(300)

            # ── 4. PREENCHER FORMULÁRIO ───────────────────────────────
            logger.info("[Braspress] Preenchendo formulário...")

            # Aguarda formulário ficar interativo antes de preencher
            try:
                await page.locator("#btnCalcular").wait_for(state="visible", timeout=10000)
            except Exception:
                logger.warning("[Braspress] Formulário não visível, aguardando networkidle...")
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass

            # Fechar #alertXmlModal se apareceu após carregamento
            await self._fechar_alert_xml_modal(page)

            # #modal e #tipoFrete só existem no frete fornecedor (FOB)
            if cnpj_remetente:
                # Aguarda select #modal ficar disponível antes de interagir
                try:
                    await page.locator("#modal").wait_for(state="attached", timeout=10000)
                except Exception:
                    logger.warning("[Braspress] Select #modal não encontrado, tentando reload...")
                    await page.goto(self.COTACAO_URL, wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(500)
                    # Fechar modais/popups pós-reload
                    for _ in range(3):
                        try:
                            btn = page.locator("text=Fechar").first
                            if await btn.count() > 0 and await btn.is_visible():
                                await btn.click()
                                await page.wait_for_timeout(400)
                            else:
                                break
                        except Exception:
                            break
                    try:
                        await page.locator("#modal").wait_for(state="attached", timeout=10000)
                    except Exception:
                        logger.warning("[Braspress] #modal ainda não encontrado, aguardando networkidle...")
                        try:
                            await page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            pass
                        await page.locator("#modal").wait_for(state="attached", timeout=5000)

                # Selects (frete fornecedor)
                await page.select_option("#modal", "R")
                tipo_frete_val = tipo_frete if tipo_frete else "2"
                await page.select_option("#tipoFrete", tipo_frete_val)

            # Remover #alertXmlModal antes de interagir com o formulário
            await self._fechar_alert_xml_modal(page)

            # CNPJ Remetente (preencher quando fornecido, ex: modo FOB fornecedor)
            cnpj_rem = self._digits(cnpj_remetente) if cnpj_remetente else ""
            if cnpj_rem:
                await page.locator("#cnpjRemetente").click()
                await page.locator("#cnpjRemetente").press("Control+a")
                await page.locator("#cnpjRemetente").type(cnpj_rem, delay=50)
                await page.keyboard.press("Tab")
                await page.wait_for_timeout(500)

            # CNPJ Destinatário
            if cnpj_rem:
                cnpj_dest = self.cnpj
            else:
                cnpj_dest = self._digits(cnpj_destinatario) if cnpj_destinatario else ""
            if cnpj_dest:
                try:
                    await page.locator("#cnpjDestinatario").click(timeout=10000)
                    await page.locator("#cnpjDestinatario").press("Control+a")
                    await page.locator("#cnpjDestinatario").type(cnpj_dest, delay=50)
                    await page.keyboard.press("Tab")
                except Exception:
                    logger.warning("[Braspress] Click em #cnpjDestinatario falhou, usando JS")
                    await page.evaluate(f"""(cnpj) => {{
                        const el = document.getElementById('cnpjDestinatario');
                        if (!el) return;
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                        if (setter) setter.call(el, cnpj);
                        else el.value = cnpj;
                        el.dispatchEvent(new Event('input', {{bubbles: true}}));
                        el.dispatchEvent(new Event('change', {{bubbles: true}}));
                        el.dispatchEvent(new Event('blur', {{bubbles: true}}));
                    }}""", cnpj_dest)
                await page.wait_for_timeout(500)

            # CEPs: aguardar auto-preenchimento, preencher manualmente se necessário
            cep_orig_auto = ""
            cep_dest_auto = ""
            for _ in range(10):
                ceps_auto = await page.evaluate(
                    """() => ({
                        origem: (document.getElementById('cepOrigem')?.value || '').trim(),
                        destino: (document.getElementById('cepDestino')?.value || '').trim(),
                    })"""
                )
                cep_orig_auto = self._digits(str(ceps_auto.get("origem", "")))
                cep_dest_auto = self._digits(str(ceps_auto.get("destino", "")))
                if len(cep_orig_auto) == 8 and len(cep_dest_auto) == 8:
                    break
                await page.wait_for_timeout(400)

            # Preencher CEPs manualmente se não foram auto-preenchidos
            if len(cep_orig_auto) != 8:
                logger.info("[Braspress] CEP Origem não auto-preenchido, preenchendo manualmente...")
                try:
                    await page.locator("#cepOrigem").click(timeout=10000)
                    await page.locator("#cepOrigem").fill(self._digits(origem))
                    await page.keyboard.press("Tab")
                except Exception:
                    logger.warning("[Braspress] Click em #cepOrigem falhou, usando JS")
                    await page.evaluate("""(cep) => {
                        const el = document.getElementById('cepOrigem');
                        if (!el) return;
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                        if (setter) setter.call(el, cep);
                        else el.value = cep;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('blur', {bubbles: true}));
                    }""", self._digits(origem))
                await page.wait_for_timeout(500)
            if len(cep_dest_auto) != 8:
                logger.info("[Braspress] CEP Destino não auto-preenchido, preenchendo manualmente...")
                try:
                    await page.locator("#cepDestino").click(timeout=10000)
                    await page.locator("#cepDestino").fill(self._digits(destino))
                    await page.keyboard.press("Tab")
                except Exception:
                    logger.warning("[Braspress] Click em #cepDestino falhou, usando JS")
                    await page.evaluate("""(cep) => {
                        const el = document.getElementById('cepDestino');
                        if (!el) return;
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                        if (setter) setter.call(el, cep);
                        else el.value = cep;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('blur', {bubbles: true}));
                    }""", self._digits(destino))
                await page.wait_for_timeout(500)

            # Peso (formato BR: vírgula decimal)
            peso_str = self._fmt_decimal(peso)
            await page.locator("#peso").click()
            await page.locator("#peso").press("Control+a")
            await page.locator("#peso").type(peso_str, delay=100)
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(300)

            # Volumes (type=number – usar nativeInputValueSetter)
            vol_str = str(volumes_total)
            await page.evaluate(
                """(vol) => {
                    const el = document.getElementById('volumes');
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    setter.call(el, vol);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new KeyboardEvent('keyup', {key: vol, bubbles: true}));
                }""",
                vol_str,
            )
            await page.wait_for_timeout(300)

            # Valor mercadoria
            valor_str = self._fmt_decimal(valor)
            await page.locator("#vlrMercadoria").click()
            await page.locator("#vlrMercadoria").press("Control+a")
            await page.locator("#vlrMercadoria").type(valor_str, delay=100)
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(300)

            # Cubagens reais do romaneio (múltiplos tipos de volume).
            for idx, cub in enumerate(cubagens_validas):
                if idx > 0:
                    await page.locator("#btnAdd").click()
                    await page.wait_for_timeout(400)
                    final_count = await page.evaluate(
                        "() => document.querySelectorAll(\"input[id^='cubagem'][id$='comprimento']\").length"
                    )
                    if int(final_count or 0) < idx + 1:
                        self.last_error = f"Não foi possível adicionar linha de cubagem #{idx + 1} na Braspress"
                        logger.error(f"[Braspress] {self.last_error}")
                        return None

                cubagem_fields = {
                    f"cubagem{idx}comprimento": self._fmt_decimal(int(cub["comprimento_cm"]) / 100.0),
                    f"cubagem{idx}largura": self._fmt_decimal(int(cub["largura_cm"]) / 100.0),
                    f"cubagem{idx}altura": self._fmt_decimal(int(cub["altura_cm"]) / 100.0),
                    f"cubagem{idx}volumes": str(int(cub["quantidade"])),
                }
                for fid, val in cubagem_fields.items():
                    try:
                        loc = page.locator(f"#{fid}")
                        if await loc.count() > 0 and await loc.is_visible():
                            await loc.click()
                            await loc.press("Control+a")
                            await loc.type(val, delay=50)
                            await page.keyboard.press("Tab")
                            await page.wait_for_timeout(180)
                            continue
                    except Exception:
                        pass
                    await page.evaluate(
                        """({ fieldId, fieldValue }) => {
                            let el = document.getElementById(fieldId);
                            if (!el) {
                                const m = fieldId.match(/^cubagem(\\d+)(comprimento|largura|altura|volumes)$/);
                                if (m) {
                                    const rowIndex = Number(m[1]);
                                    const suffix = m[2];
                                    const candidatos = Array.from(
                                        document.querySelectorAll(`input[id^='cubagem'][id$='${suffix}']`)
                                    );
                                    if (candidatos.length > rowIndex) {
                                        el = candidatos[rowIndex];
                                    }
                                }
                            }
                            if (!el) return;
                            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                            if (setter) setter.call(el, fieldValue);
                            else el.value = fieldValue;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            el.dispatchEvent(new Event('blur', { bubbles: true }));
                        }""",
                        {"fieldId": fid, "fieldValue": val},
                    )
                    await page.wait_for_timeout(180)

            # Log de verificação dos campos preenchidos
            form_values = await page.evaluate("""() => {
                const get = (id) => { const el = document.getElementById(id); return el ? el.value : 'N/A'; };
                return {
                    modal: get('modal'), tipoFrete: get('tipoFrete'),
                    cepOrigem: get('cepOrigem'), cepDestino: get('cepDestino'),
                    peso: get('peso'), volumes: get('volumes'), vlrMercadoria: get('vlrMercadoria'),
                    cubComp: get('cubagem0comprimento'), cubLarg: get('cubagem0largura'),
                    cubAlt: get('cubagem0altura'), cubVol: get('cubagem0volumes'),
                    linhasCubagem: document.querySelectorAll("input[id^='cubagem'][id$='comprimento']").length,
                };
            }""")
            logger.info(f"[Braspress] Campos do formulário: {form_values}")

            # ── 5. CALCULAR ───────────────────────────────────────────
            logger.info("[Braspress] Calculando...")

            # Verificar se os campos obrigatórios estão preenchidos
            form_check = await page.evaluate("""() => ({
                modal: document.getElementById('modal')?.value,
                cepOrig: document.getElementById('cepOrigem')?.value,
                cepDest: document.getElementById('cepDestino')?.value,
                peso: document.getElementById('peso')?.value,
            })""")
            if not form_check.get("peso"):
                self.last_error = "Campos do formulário não preenchidos (peso ausente)"
                logger.error(f"[Braspress] {self.last_error}: {form_check}")
                return None

            # O clique no CNPJ estoura enquanto a página ainda abre e o
            # fallback em JS se perde. Sem esse campo o Calcular fica desligado.
            if cnpj_dest:
                cnpj_now = await page.evaluate(
                    "() => (document.getElementById('cnpjDestinatario')?.value || '')"
                )
                if len(self._digits(str(cnpj_now))) < 11:
                    logger.warning("[Braspress] CNPJ do destinatário vazio antes de calcular, re-preenchendo...")
                    try:
                        loc_cnpj = page.locator("#cnpjDestinatario")
                        await loc_cnpj.click(timeout=5000)
                        await loc_cnpj.press("Control+a")
                        await loc_cnpj.type(self._digits(cnpj_dest), delay=40)
                        await page.keyboard.press("Tab")
                        await page.wait_for_timeout(800)
                    except Exception:
                        await page.evaluate(
                            """(cnpj) => {
                                const el = document.getElementById('cnpjDestinatario');
                                if (!el) return;
                                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                                if (setter) setter.call(el, cnpj);
                                else el.value = cnpj;
                                el.dispatchEvent(new Event('input', {bubbles: true}));
                                el.dispatchEvent(new Event('change', {bubbles: true}));
                                el.dispatchEvent(new Event('blur', {bubbles: true}));
                            }""",
                            self._digits(cnpj_dest),
                        )
                        await page.wait_for_timeout(800)

            # Verificar e corrigir CEP Destino antes de calcular
            cep_dest_check = self._digits(str(form_check.get("cepDest", "")))
            if len(cep_dest_check) != 8:
                logger.warning(f"[Braspress] CEP Destino vazio/incorreto antes de calcular: '{form_check.get('cepDest')}', re-preenchendo...")
                dest_digits = self._digits(destino)
                try:
                    loc_dest = page.locator("#cepDestino")
                    await loc_dest.click(timeout=5000)
                    await loc_dest.press("Control+a")
                    await loc_dest.type(dest_digits, delay=50)
                    await page.keyboard.press("Tab")
                    await page.wait_for_timeout(500)
                except Exception:
                    await page.evaluate("""(cep) => {
                        const el = document.getElementById('cepDestino');
                        if (!el) return;
                        el.value = '';
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                        if (setter) setter.call(el, cep);
                        else el.value = cep;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('blur', {bubbles: true}));
                    }""", dest_digits)
                    await page.wait_for_timeout(500)
                # Verificar novamente
                cep_dest_recheck = await page.evaluate("() => document.getElementById('cepDestino')?.value || ''")
                if len(self._digits(str(cep_dest_recheck))) != 8:
                    self.last_error = f"CEP Destino não preenchido após retry: '{cep_dest_recheck}'"
                    logger.error(f"[Braspress] {self.last_error}")
                    return None

            # Verificar e corrigir CEP Origem
            cep_orig_check = self._digits(str(form_check.get("cepOrig", "")))
            if len(cep_orig_check) != 8:
                logger.warning(f"[Braspress] CEP Origem vazio/incorreto antes de calcular: '{form_check.get('cepOrig')}', re-preenchendo...")
                orig_digits = self._digits(origem)
                try:
                    loc_orig = page.locator("#cepOrigem")
                    await loc_orig.click(timeout=5000)
                    await loc_orig.press("Control+a")
                    await loc_orig.type(orig_digits, delay=50)
                    await page.keyboard.press("Tab")
                    await page.wait_for_timeout(500)
                except Exception:
                    await page.evaluate("""(cep) => {
                        const el = document.getElementById('cepOrigem');
                        if (!el) return;
                        el.value = '';
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                        if (setter) setter.call(el, cep);
                        else el.value = cep;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('blur', {bubbles: true}));
                    }""", orig_digits)
                    await page.wait_for_timeout(500)

            await page.locator("#btnCalcular").click()
            # Polling robusto: aguardar resultado aparecer em qualquer bloco da página.
            # Alguns cenários retornam tabela fora de #step4Result.
            resultado_pronto = False
            ultimo_status: dict | None = None
            for _poll in range(30):  # até ~40s
                await page.wait_for_timeout(800)
                # Fecha modal promocional que pode bloquear o resultado
                await self._fechar_modais_genericos(page)
                status = await page.evaluate(
                    """() => {
                        const clean = (v) => (v || '').replace(/\\s+/g, ' ').trim();
                        const bodyText = clean(document.body?.innerText || '');
                        const hasTable = Array.from(document.querySelectorAll('table')).some((t) => {
                            const headers = Array.from(t.querySelectorAll('th'))
                                .map((th) => clean(th.innerText || th.textContent).toLowerCase());
                            const hasValor = headers.some(
                                (h) => h.includes('valor total frete') || (h.includes('valor') && h.includes('frete'))
                            );
                            const hasDias = headers.some((h) => h.includes('dias'));
                            return hasValor && hasDias;
                        });
                        const badges = Array.from(document.querySelectorAll('.badge, .label'))
                            .map((el) => clean(el.innerText || el.textContent))
                            .filter(Boolean);
                        const hasValorBadge = badges.some((b) => /\\d+[\\.,]\\d{2}/.test(b) || /^R\\$\\s*\\d/.test(b));
                        const hasSucesso = /cota[çc][aã]o realizada com sucesso/i.test(bodyText);
                        const hasResultadoTitulo = /resultado da cota[çc][aã]o/i.test(bodyText);
                        // Detectar erros visíveis na página
                        const errs = Array.from(document.querySelectorAll(
                            '.has-error .help-block, .error-message, .alert-danger, .alert-warning, .modal.in .modal-body'
                        )).map(e => clean(e.innerText)).filter(t => t.length > 0);
                        return {
                            stepLen: clean(document.getElementById('step4Result')?.innerText || '').length,
                            hasTable,
                            hasValorBadge,
                            hasSucesso,
                            hasResultadoTitulo,
                            errs,
                        };
                    }"""
                )
                ultimo_status = status if isinstance(status, dict) else None
                if isinstance(status, dict):
                    # Detectar erros da página cedo para não esperar 135s em vão
                    page_errs = status.get("errs") or []
                    # Filtrar mensagens de status/processamento (não são erros reais)
                    page_errs = [
                        e for e in page_errs
                        if not re.search(r'\b(VERIFICANDO|CARREGANDO|PROCESSANDO|AGUARD)\b', e, re.IGNORECASE)
                    ]
                    if page_errs and _poll >= 3:
                        self.last_error = f"Erro na página Braspress: {'; '.join(page_errs[:3])}"
                        logger.error(f"[Braspress] {self.last_error}")
                        return None
                    if (
                        bool(status.get("hasTable"))
                        or bool(status.get("hasValorBadge"))
                        or bool(status.get("hasSucesso"))
                        or bool(status.get("hasResultadoTitulo"))
                        or int(status.get("stepLen", 0) or 0) > 0
                    ):
                        resultado_pronto = True
                        break

            # Fecha modal promocional antes de tentar extrair (pode ter aparecido após calcular)
            await self._fechar_modais_genericos(page)
            await self._fechar_alert_xml_modal(page)

            if not resultado_pronto:
                # Log estado atual para debug
                alerts = await page.evaluate("""() => {
                    const errs = document.querySelectorAll('.has-error .help-block, .error-message, .alert-danger, .alert-warning');
                    return Array.from(errs).map(e => e.innerText.trim()).filter(t => t.length > 0);
                }""")
                if alerts:
                    logger.warning(f"[Braspress] Alertas na página: {alerts}")
                if ultimo_status:
                    logger.warning(f"[Braspress] Resultado não detectado no polling: {ultimo_status}")

            # ── 6. EXTRAIR RESULTADO ──────────────────────────────────            # Via .label-result (ordem: previsão, dias, valor, data, nº, status)
            labels = await page.evaluate("""() => {
                const els = document.querySelectorAll('#step4Result .label-result');
                return Array.from(els).map(e => (e.innerText || e.textContent || '').trim());
            }""")
            logger.info(f"[Braspress] Labels do resultado: {labels}")

            if isinstance(labels, list) and len(labels) >= 3:
                _prazo = self._parse_int_any(labels[1])
                _valor = self._parse_decimal_any(labels[2])
                if _valor is not None:
                    logger.info(f"[Braspress] Cotação: R$ {_valor:.2f} - {_prazo} dias")
                    return Cotacao(
                        transportadora=self.nome,
                        prazo_dias=_prazo,
                        valor_frete=round(_valor, 2),
                        restricoes="Cotação portal Braspress",
                        timestamp=datetime.now(),
                    )

            # Fallback: extração por texto completo
            result_text = await page.evaluate(
                "() => { const el = document.getElementById('step4Result'); return el ? el.innerText : ''; }"
            )
            result_dom = await page.evaluate(
                """() => {
                    const clean = (v) => (v || '').replace(/\\s+/g, ' ').trim();
                    const lower = (v) => clean(v).toLowerCase();

                    const tableInfo = (table) => {
                        if (!table) return { headers: [], rows: [] };
                        const headers = Array.from(table.querySelectorAll('th'))
                            .map((th) => lower(th.innerText || th.textContent));
                        const rows = Array.from(table.querySelectorAll('tr'))
                            .map((tr) => Array.from(tr.querySelectorAll('td')).map((td) => clean(td.innerText || td.textContent)))
                            .filter((row) => row.length > 0);
                        return { headers, rows };
                    };

                    // 1) Tenta bloco tradicional
                    const rootStep4 = document.getElementById('step4Result');
                    let table = rootStep4 ? rootStep4.querySelector('table') : null;
                    let root = rootStep4;

                    // 2) Fallback: procura tabela de resultado em toda a página
                    if (!table) {
                        const allTables = Array.from(document.querySelectorAll('table'));
                        for (const t of allTables) {
                            const info = tableInfo(t);
                            const hasValor = info.headers.some(
                                (h) => h.includes('valor total frete') || (h.includes('valor') && h.includes('frete'))
                            );
                            const hasDias = info.headers.some((h) => h.includes('dias'));
                            if (hasValor && hasDias) {
                                table = t;
                                break;
                            }
                        }
                    }

                    if (!root && table) {
                        root =
                            table.closest('section') ||
                            table.closest('.panel') ||
                            table.closest('.panel-body') ||
                            table.closest('.container') ||
                            table.closest('form') ||
                            table.parentElement ||
                            document.body;
                    }
                    if (!root) root = document.body;

                    const info = tableInfo(table);
                    let valorText = '';
                    let prazoText = '';

                    if (info.headers.length && info.rows.length) {
                        const row = info.rows.find((r) => r.some((c) => /\\d/.test(c))) || info.rows[0];
                        const idxValor = info.headers.findIndex(
                            (h) => h.includes('valor total frete') || (h.includes('valor') && h.includes('frete'))
                        );
                        const idxPrazo = info.headers.findIndex((h) => h.includes('dias'));
                        if (idxValor >= 0 && idxValor < row.length) valorText = row[idxValor] || '';
                        if (idxPrazo >= 0 && idxPrazo < row.length) prazoText = row[idxPrazo] || '';
                    }

                    if (!valorText || !prazoText) {
                        const scope = table ? (table.parentElement || root) : root;
                        const badges = Array.from(scope.querySelectorAll('.badge, .label'))
                            .map((el) => clean(el.innerText || el.textContent))
                            .filter(Boolean);
                        if (!prazoText) {
                            const p = badges.find((b) => /^\\d{1,2}$/.test(b));
                            if (p) prazoText = p;
                        }
                        if (!valorText) {
                            const v = badges.find((b) => /\\d+[\\.,]\\d{2}/.test(b) || /^R\\$\\s*\\d/.test(b));
                            if (v) valorText = v;
                        }
                    }

                    return {
                        valorText,
                        prazoText,
                        text: clean((table ? table.innerText : root.innerText) || ''),
                        hasTable: !!table,
                    };
                }"""
            )
            if not result_text.strip():
                if isinstance(result_dom, dict):
                    result_text = str(result_dom.get("text", "") or "").strip()
                if not result_text:
                    result_text = await page.inner_text("body")

            logger.info(f"[Braspress] Resultado bruto (500 chars): {result_text[:500]}")
            if isinstance(result_dom, dict):
                logger.info(
                    "[Braspress] Resultado DOM: hasTable=%s valorText=%s prazoText=%s",
                    bool(result_dom.get("hasTable")),
                    str(result_dom.get("valorText", "") or ""),
                    str(result_dom.get("prazoText", "") or ""),
                )

            # Verificar sucesso
            if "sucesso" not in result_text.lower() and "valor" not in result_text.lower():
                erros_el = page.locator(".has-error .help-block, .error-message, .alert-danger")
                cnt = await erros_el.count()
                erros = []
                for i in range(min(cnt, 5)):
                    txt = await erros_el.nth(i).text_content()
                    if txt and txt.strip():
                        erros.append(txt.strip()[:100])
                self.last_error = "; ".join(erros) if erros else "Sem resultado"
                logger.error(f"[Braspress] {self.last_error}")
                return None

            # Extrair valor/prazo priorizando a tabela de resultado do step4.
            valor_frete = None
            prazo_dias = 0

            if isinstance(result_dom, dict):
                valor_frete = self._parse_decimal_any(str(result_dom.get("valorText", "") or ""))
                prazo_dias = self._parse_int_any(str(result_dom.get("prazoText", "") or ""))

            if valor_frete is None:
                # Fallback por rótulos e sequência numérica
                valor_label = re.search(r"valor\s*total\s*frete[^\d]*([\d.,]+)", result_text, re.IGNORECASE)
                if valor_label:
                    valor_frete = self._parse_decimal_any(valor_label.group(1))

            if prazo_dias <= 0:
                prazo_label = re.search(r"dias?\s*(?:úteis?)?[^\d]*(\d{1,3})", result_text, re.IGNORECASE)
                if prazo_label:
                    prazo_dias = int(prazo_label.group(1))

            if valor_frete is None:
                # Fallback antigo (sequência de colunas)
                valor_match = re.search(
                    r"(\d+)\s+([\d.,]+)\s+\d{2}/\d{2}/\d{4}\s+\d+\s+\w+",
                    result_text,
                )
                if valor_match:
                    prazo_dias = prazo_dias or int(valor_match.group(1))
                    valor_frete = self._parse_decimal_any(valor_match.group(2))

            if valor_frete is None:
                # Último fallback: qualquer decimal plausível no bloco de resultado.
                vals = re.findall(r"(\d[\d.,]*[.,]\d{2})", result_text)
                for v in vals:
                    vf = self._parse_decimal_any(v)
                    if vf is not None and 1 < vf < 100000:
                        valor_frete = vf
                        break

            if valor_frete is None:
                trecho = re.sub(r"\s+", " ", result_text).strip()[:220]
                dom_diag = ""
                if isinstance(result_dom, dict):
                    dom_diag = (
                        f" hasTable={bool(result_dom.get('hasTable'))}"
                        f" valorText={str(result_dom.get('valorText', '') or '')!r}"
                        f" prazoText={str(result_dom.get('prazoText', '') or '')!r}"
                    )
                self.last_error = "Valor do frete não encontrado no resultado"
                logger.error(f"[Braspress] {self.last_error} ({dom_diag} trecho: {trecho})")
                return None

            logger.info(f"[Braspress] Cotação: R$ {valor_frete:.2f} - {prazo_dias} dias")
            return Cotacao(
                transportadora=self.nome,
                prazo_dias=prazo_dias,
                valor_frete=round(valor_frete, 2),
                restricoes="Cotação portal Braspress",
                timestamp=datetime.now(),
            )

        except Exception as e:
            self.last_error = str(e)
            logger.error(f"[Braspress] Erro: {e}")
            return None

    async def cotar(self, request: QuoteRequest) -> QuoteResponse:
        return await super().cotar(request)

    async def coteir(
        self,
        origem: str,
        destino: str,
        peso: float,
        valor: float,
        cnpj_destinatario: str | None = None,
        volumes: int = 1,
        comprimento_cm: int = 0,
        largura_cm: int = 0,
        altura_cm: int = 0,
        cubagens: Optional[list[dict]] = None,
        cnpj_remetente: str | None = None,
        tipo_frete: str | None = None,
    ) -> Optional[Cotacao]:
        return await self._coteir_legacy(
            origem=origem,
            destino=destino,
            peso=peso,
            valor=valor,
            cnpj_destinatario=cnpj_destinatario,
            volumes=volumes,
            comprimento_cm=comprimento_cm,
            largura_cm=largura_cm,
            altura_cm=altura_cm,
            cubagens=cubagens,
            cnpj_remetente=cnpj_remetente,
            tipo_frete=tipo_frete,
        )
