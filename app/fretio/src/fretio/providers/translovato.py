"""Provider Translovato via portal do cliente."""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from typing import Any

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from fretio.logging_conf import get_logger
from fretio.models import Cotacao
from fretio.providers.base import ProviderBase
from fretio.providers.provider_utils import _digits, _parse_decimal_any
from fretio.providers.translovato_browser import TranslovatoBrowserMixin

logger = get_logger(__name__)


class TranslovatoProvider(TranslovatoBrowserMixin, ProviderBase):
    """Provider Translovato com cotação automatizada via Playwright."""

    BASE_URL = "https://www.translovato.com.br"
    LOGIN_URL = "https://www.translovato.com.br/fale-conosco/solicitacao-de-cotacao#portal-do-cliente"
    DEFAULT_COTACAO_URL = "https://www.translovato.com.br/fale-conosco/solicitacao-de-cotacao"
    MINHAS_COTACOES_URL = "https://www.translovato.com.br/portal-do-cliente/minhas-cotacoes"

    LOGIN_CNPJ_SELECTOR = "#cnpj"
    LOGIN_USER_SELECTOR = "#user"
    LOGIN_PASSWORD_SELECTOR = 'input[name="password"]'
    SENDER_CNPJ_SELECTOR = "#sender_cpnj"
    RECEIVER_CNPJ_SELECTOR = "#receiver_c"
    DELIVERY_ZIP_SELECTOR = "#cep_entrega"
    VALUE_SELECTOR = 'input[name="value[volume_nf]"]'
    WEIGHT_SELECTOR = 'input[name="value[volume_weigth]"]'
    QUANTITY_SELECTOR = 'input[name="cubing_qnt[]"]'
    HEIGHT_SELECTOR = 'input[name="cubing_height[]"]'
    LENGTH_SELECTOR = 'input[name="cubing_length[]"]'
    DEPTH_SELECTOR = 'input[name="cubing_depth[]"]'
    CUBING_SELECTOR = 'input[name="cubing[]"]'
    CUBING_WEIGHT_SELECTOR = 'input[name="cubing_weigth[]"]'
    CUBING_TOTAL_SELECTOR = 'input[name="cubing_total"]'
    CUBING_WEIGHT_TOTAL_SELECTOR = 'input[name="cubing_weigth_total"]'

    # Recursos puramente visuais que o portal carrega mas a cotação nunca usa.
    # Mantemos stylesheet/script/xhr/fetch: layout (is_visible/getComputedStyle),
    # autocomplete do destinatário e autopreenchimento de endereço dependem deles.
    BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})

    MAIN_SELECTORS = (
        LOGIN_CNPJ_SELECTOR,
        LOGIN_USER_SELECTOR,
        LOGIN_PASSWORD_SELECTOR,
        SENDER_CNPJ_SELECTOR,
        RECEIVER_CNPJ_SELECTOR,
        DELIVERY_ZIP_SELECTOR,
        VALUE_SELECTOR,
        WEIGHT_SELECTOR,
        QUANTITY_SELECTOR,
        HEIGHT_SELECTOR,
        LENGTH_SELECTOR,
        DEPTH_SELECTOR,
    )

    def __init__(
        self,
        cnpj: str,
        usuario: str,
        senha: str,
        cnpj_remetente: str = "",
        produto: str = "CONFECCAO",
        cotacao_url: str = "",
        headless: bool = True,
    ) -> None:
        super().__init__(nome="TRANSLOVATO")
        self.cnpj = _digits(cnpj)
        self.usuario = str(usuario or "").strip()
        self.senha = str(senha or "").strip()
        self.cnpj_remetente = _digits(cnpj_remetente)
        self.produto = str(produto or "CONFECCAO").strip() or "CONFECCAO"
        self.cotacao_url = str(cotacao_url or self.DEFAULT_COTACAO_URL).strip()
        if self.cotacao_url.rstrip("/").endswith(
            "/fale-conosco/solicitacao-de-cotacao#portal-do-cliente"
        ):
            self.cotacao_url = self.DEFAULT_COTACAO_URL
        self.headless = bool(headless)
        self.last_error: str | None = None
        self._passo_atual: str | None = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._logged_in = False
        self._last_receiver_diagnostic: dict[str, Any] = {}
        self._stage_started_at: float | None = None

    @staticmethod
    def _mask_doc(value: str) -> str:
        digits = _digits(value)
        if len(digits) <= 6:
            return "***"
        return f"{digits[:4]}***{digits[-2:]}"

    def _start_stage(self, stage: str) -> float:
        self._passo_atual = stage
        started_at = time.monotonic()
        self._stage_started_at = started_at
        logger.info("[TRANSLOVATO] Etapa iniciada: %s", stage)
        return started_at

    def _finish_stage(self, stage: str, started_at: float, *, details: str = "") -> None:
        elapsed = time.monotonic() - started_at
        suffix = f" ({details})" if details else ""
        logger.info("[TRANSLOVATO] Etapa concluída: %s em %.2fs%s", stage, elapsed, suffix)


    @staticmethod
    def _normalizar_texto_comparacao(value: str) -> str:
        text = unicodedata.normalize("NFKD", str(value or ""))
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = re.sub(r"[^A-Za-z0-9]+", " ", text).strip().upper()
        return re.sub(r"\s+", " ", text)

    @classmethod
    def _parse_cidade_uf(cls, value: str) -> tuple[str, str]:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if not text:
            return "", ""
        uf = ""
        city = text
        match = re.search(r"(.+?)(?:\s*/\s*|\s+-\s+|\s+)([A-Za-z]{2})$", text)
        if match:
            city = str(match.group(1) or "").strip(" -/")
            uf = str(match.group(2) or "").strip().upper()
        return cls._normalizar_texto_comparacao(city), uf

    @staticmethod
    def _format_cnpj(value: str) -> str:
        digits = _digits(value)
        if len(digits) != 14:
            return str(value or "").strip()
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"

    @staticmethod
    def _format_decimal_br(value: float, decimals: int = 2) -> str:
        return f"{float(value):.{decimals}f}".replace(".", ",")

    @classmethod
    def _cm_to_m_br(cls, value_cm: Any) -> str:
        try:
            meters = float(value_cm or 0) / 100.0
        except Exception:
            meters = 0.0
        text = cls._format_decimal_br(meters, 2)
        return re.sub(r",?0+$", "", text) if "," in text else text

    @classmethod
    def _calcular_resumo_cubagem(
        cls,
        cubagens: list[dict[str, int]],
        *,
        fator_produto: float = 300.0,
    ) -> dict[str, Any]:
        fator = float(fator_produto or 0.0)
        if fator <= 0:
            fator = 300.0

        linhas: list[dict[str, str]] = []
        total_cubagem = 0.0
        total_peso_cubado = 0.0
        for cub in cubagens:
            qtd = int(cub.get("quantidade", 0) or 0)
            altura_m = float(cub.get("altura_cm", 0) or 0) / 100.0
            comprimento_m = float(cub.get("comprimento_cm", 0) or 0) / 100.0
            largura_m = float(cub.get("largura_cm", 0) or 0) / 100.0
            cubagem_linha = max(0.0, float(qtd) * altura_m * comprimento_m * largura_m)
            peso_cubado_linha = max(0.0, cubagem_linha * fator)
            total_cubagem += cubagem_linha
            total_peso_cubado += peso_cubado_linha
            linhas.append(
                {
                    "cubagem": cls._format_decimal_br(cubagem_linha, 4),
                    "peso_cubado": cls._format_decimal_br(peso_cubado_linha, 2),
                }
            )

        return {
            "linhas": linhas,
            "total_cubagem": cls._format_decimal_br(total_cubagem, 4),
            "total_peso_cubado": cls._format_decimal_br(total_peso_cubado, 2),
            "fator_produto": fator,
        }

    @staticmethod
    def _normalizar_cubagens_cm(cubagens: Any, *, volumes: int = 1) -> list[dict[str, int]]:
        validas: list[dict[str, int]] = []
        if isinstance(cubagens, list):
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
                if qtd > 0 and comp > 0 and larg > 0 and alt > 0:
                    validas.append(
                        {
                            "quantidade": qtd,
                            "comprimento_cm": comp,
                            "largura_cm": larg,
                            "altura_cm": alt,
                        }
                    )
        return validas

    @staticmethod
    def _extract_valor(text: str) -> float | None:
        matches = re.findall(r"R\$\s*\d{1,3}(?:\.\d{3})*,\d{2}|R\$\s*\d+,\d{2}", str(text or ""))
        for raw in matches:
            value = _parse_decimal_any(raw)
            if value is not None and value > 0:
                return float(value)
        return None

    @staticmethod
    def _extract_prazo(text: str) -> int | None:
        patterns = (
            r"prazo(?:\s+de\s+entrega)?\D{0,30}(\d{1,3})\s*dias?",
            r"entrega\D{0,30}(\d{1,3})\s*dias?",
            r"(\d{1,3})\s*dias?\s+(?:úteis|uteis|corridos)?",
        )
        lowered = str(text or "").lower()
        for pattern in patterns:
            match = re.search(pattern, lowered, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    async def _click_if_present(self, selector_or_text: str, *, by_text: bool = False, timeout: int = 2500) -> bool:
        page = self._page
        try:
            loc = page.get_by_text(selector_or_text, exact=True) if by_text else page.locator(selector_or_text)
            if await loc.count() == 0:
                return False
            first = loc.first
            if await first.is_visible(timeout=timeout):
                await first.click(timeout=timeout)
                return True
        except Exception:
            return False
        return False

    async def _aceitar_cookies(self) -> None:
        clicked = await self._click_if_present("Ok, entendi!", by_text=True, timeout=1500)
        if clicked:
            try:
                await self._page.wait_for_load_state("domcontentloaded", timeout=1000)
            except Exception:
                pass

    async def _quote_form_visible(self) -> bool:
        try:
            sender = self._page.locator(self.SENDER_CNPJ_SELECTOR).first
            receiver = self._page.locator(self.RECEIVER_CNPJ_SELECTOR).first
            return bool(
                await sender.is_visible(timeout=400)
                and await receiver.is_visible(timeout=400)
            )
        except Exception:
            return False

    async def _wait_for_logged_in_state(self, *, timeout_ms: int = 12000) -> bool:
        page = self._page
        started_at = time.monotonic()
        while (time.monotonic() - started_at) * 1000 < timeout_ms:
            current_url = page.url.lower()
            try:
                body_text = await page.locator("body").inner_text(timeout=1500)
            except Exception:
                body_text = ""
            lowered = body_text.lower()
            if (
                "minhas cotações" in lowered
                or "minhas cotacoes" in lowered
                or "solicitar nova cotação" in lowered
                or "solicitar nova cotacao" in lowered
                or "sair" in lowered
                or "minhas-cotacoes" in current_url
                or await self._quote_form_visible()
            ):
                return True
            await page.wait_for_timeout(250)
        return False

    async def _receiver_autocomplete_visible(self) -> bool:
        try:
            return bool(await self._page.evaluate(r"""() => {
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                };
                return Array.from(document.querySelectorAll(
                    '[role="listbox"], [role="option"], .autocomplete, .ui-autocomplete, .select2-results, .dropdown-menu'
                )).some(visible);
            }"""))
        except Exception:
            return False

    async def _wait_for_receiver_resolution(
        self,
        receiver: str,
        *,
        context: str,
        timeout_ms: int = 5000,
        allow_autocomplete_click: bool = True,
    ) -> str:
        started_at = time.monotonic()
        clicked_autocomplete = False
        while (time.monotonic() - started_at) * 1000 < timeout_ms:
            current = await self._read_receiver_cnpj_digits()
            if current == receiver and not await self._receiver_autocomplete_visible():
                return current
            if allow_autocomplete_click and not clicked_autocomplete and await self._receiver_autocomplete_visible():
                clicked_autocomplete = await self._select_receiver_autocomplete_match(receiver)
                if clicked_autocomplete:
                    logger.info(
                        "[TRANSLOVATO] Autocomplete do destinatário confirmado após %s para %s",
                        context,
                        self._mask_doc(receiver),
                    )
            await self._page.wait_for_timeout(200)
        return await self._read_receiver_cnpj_digits()

    async def _wait_for_form_ready(self, *, timeout_ms: int = 15000) -> None:
        await self._page.locator(self.SENDER_CNPJ_SELECTOR).first.wait_for(state="visible", timeout=timeout_ms)

    async def _wait_for_quote_result(self, *, timeout_ms: int = 30000) -> tuple[str, float | None, int | None]:
        page = self._page
        started_at = time.monotonic()
        warned_slow_portal = False
        last_text = ""
        while (time.monotonic() - started_at) * 1000 < timeout_ms:
            try:
                last_text = await page.locator("body").inner_text(timeout=3000)
            except Exception:
                last_text = ""
            valor = self._extract_valor(last_text)
            prazo = self._extract_prazo(last_text)
            if valor is not None and prazo is not None:
                return last_text, valor, prazo
            if not warned_slow_portal and time.monotonic() - started_at >= 8:
                warned_slow_portal = True
                logger.info("[TRANSLOVATO] Portal externo ainda processando a cotação após %.2fs", time.monotonic() - started_at)
            await page.wait_for_timeout(500)
        return last_text, None, None

    async def _login(self) -> bool:
        if self._logged_in:
            return True
        started_at = self._start_stage("login")
        page = self._page
        logger.info("[TRANSLOVATO] Fazendo login")

        await page.goto(self.LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        await self._aceitar_cookies()

        form = page.locator("#login-portal")
        document = form.locator(self.LOGIN_CNPJ_SELECTOR)
        await document.fill("")
        await document.press_sequentially(_digits(self.cnpj), delay=50)
        await document.press("Tab")
        await form.locator(self.LOGIN_USER_SELECTOR).fill(self.usuario)
        await form.locator(self.LOGIN_PASSWORD_SELECTOR).fill(self.senha)
        await form.locator(self.LOGIN_PASSWORD_SELECTOR).press("Tab")
        login_response: dict[str, Any] = {}

        async def capture_login_response(response) -> None:
            try:
                response_url = response.url.lower()
                if not any(
                    path in response_url
                    for path in ("/portal/comum/login", "/portal-do-cliente/login")
                ):
                    return
                login_response["status_code"] = int(response.status)
                if "json" in response.headers.get("content-type", "").lower():
                    payload = await response.json()
                    if isinstance(payload, dict):
                        login_response["payload"] = payload
            except Exception:
                pass

        response_handler = lambda response: asyncio.ensure_future(capture_login_response(response))
        page.on("response", response_handler)
        try:
            button = page.locator("#login-portal").get_by_role("button", name=re.compile("entrar", re.I))
            await button.click()
            logged_in = await self._wait_for_logged_in_state(timeout_ms=3000)
            payload = login_response.get("payload")
            if not logged_in and isinstance(payload, dict) and payload.get("status") is True:
                logged_in = await self._wait_for_logged_in_state(timeout_ms=12000)
            if not logged_in and await button.is_visible():
                try:
                    await button.click(timeout=3000)
                except PlaywrightTimeoutError:
                    if not await self._wait_for_logged_in_state(timeout_ms=12000):
                        raise
                logged_in = await self._wait_for_logged_in_state(timeout_ms=12000)
        finally:
            page.remove_listener("response", response_handler)

        if not logged_in:
            payload = login_response.get("payload")
            message = ""
            if isinstance(payload, dict):
                for key in ("message", "mensagem", "error", "erro", "title"):
                    value = payload.get(key)
                    if isinstance(value, str) and value.strip():
                        message = value.strip()
                        break
            status_code = login_response.get("status_code")
            suffix = f" (HTTP {status_code})" if status_code else ""
            self.last_error = (message or "Login falhou ou portal não confirmou acesso") + suffix
            logger.warning("[TRANSLOVATO] %s", self.last_error)
            return False

        self._logged_in = True
        self._finish_stage("login", started_at)
        logger.info("[TRANSLOVATO] Login OK")
        return True

    async def pre_login(self):
        await self._init_browser()
        ok = await self._login()
        return ok

    async def _abrir_nova_cotacao(self) -> None:
        started_at = self._start_stage("abrindo_nova_cotacao")
        page = self._page
        await self._goto_com_retry(self.MINHAS_COTACOES_URL)
        await self._aceitar_cookies()
        link = page.get_by_role("link", name=re.compile(r"solicitar nova cota[çc][aã]o", re.I))
        try:
            await link.click(timeout=15000)
        except Exception:
            await page.get_by_text(re.compile(r"solicitar nova cota[çc][aã]o", re.I)).first.click(timeout=15000)
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
        await self._wait_for_form_ready(timeout_ms=15000)
        self._finish_stage("abrindo_nova_cotacao", started_at)

    async def _fill_input(self, selector: str, value: str, *, index: int = 0, timeout: int = 12000) -> None:
        loc = self._page.locator(selector).nth(index)
        await loc.wait_for(state="visible", timeout=timeout)
        if selector == self.DELIVERY_ZIP_SELECTOR:
            await loc.click()
            try:
                await self._page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                pass
            await loc.fill("")
            await loc.press_sequentially(_digits(value), delay=40)
            await loc.press("Tab")
            return
        await loc.fill(str(value))
        await loc.dispatch_event("input")
        await loc.dispatch_event("change")
        await loc.evaluate(
            """(el) => {
                el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Tab' }));
            }"""
        )
        await loc.dispatch_event("blur")

    async def _read_receiver_cnpj_digits(self) -> str:
        try:
            return _digits(await self._page.locator(self.RECEIVER_CNPJ_SELECTOR).first.input_value(timeout=1500))
        except Exception:
            return ""

    async def _receiver_divergence_diagnostic(
        self,
        *,
        stage: str,
        expected: str,
        found: str,
        selector: str | None = None,
    ) -> dict[str, Any]:
        raw_city = ""
        city = ""
        uf = ""
        zip_digits = ""
        autocomplete_present = False
        try:
            raw_city, city, uf = await self._read_delivery_city_uf()
        except Exception:
            pass
        try:
            zip_digits = await self._read_delivery_zip_digits()
        except Exception:
            pass
        autocomplete_present = await self._receiver_autocomplete_visible()
        diag = {
            "stage": str(stage or "")[:80],
            "expected_masked": self._mask_doc(expected),
            "found_masked": self._mask_doc(found),
            "selector": selector or self.RECEIVER_CNPJ_SELECTOR,
            "autocomplete_present": autocomplete_present,
            "cep_detected_masked": f"{zip_digits[:5]}-***" if len(zip_digits) == 8 else "",
            "cidade_detectada": raw_city[:80],
            "cidade_norm": city[:80],
            "uf_detectada": uf[:2],
        }
        self._last_receiver_diagnostic = diag
        logger.warning(
            "[TRANSLOVATO] Diagnóstico divergência CNPJ destinatário: etapa=%s esperado=%s encontrado=%s seletor=%s autocomplete=%s cep=%s cidade_uf=%s",
            diag["stage"],
            diag["expected_masked"],
            diag["found_masked"],
            diag["selector"],
            diag["autocomplete_present"],
            diag["cep_detected_masked"] or "?",
            diag["cidade_detectada"] or "?",
        )
        return diag

    def _receiver_divergence_message(self, *, stage: str, expected: str, found: str) -> str:
        return (
            "CNPJ destinatário no portal diverge do romaneio após "
            f"{stage}: esperado {self._mask_doc(expected)}, encontrado {self._mask_doc(found)}. "
            "O portal alterou o CNPJ destinatário após blur; cotação bloqueada para evitar destinatário incorreto."
        )

    async def _validate_receiver_cnpj(self, expected: str, *, context: str) -> None:
        found = await self._read_receiver_cnpj_digits()
        logger.info(
            "[TRANSLOVATO] Validação CNPJ destinatário (%s): esperado=%s encontrado=%s",
            context,
            self._mask_doc(expected),
            self._mask_doc(found),
        )
        if found != expected:
            await self._receiver_divergence_diagnostic(
                stage=context,
                expected=expected,
                found=found,
                selector=self.RECEIVER_CNPJ_SELECTOR,
            )
            raise ValueError(self._receiver_divergence_message(stage=context, expected=expected, found=found))

    async def _select_receiver_autocomplete_match(self, receiver: str) -> bool:
        try:
            return bool(await self._page.evaluate(r"""(expected) => {
                const digits = (value) => String(value || '').replace(/\D+/g, '');
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                };
                const nodes = Array.from(document.querySelectorAll(
                    '[role="option"], [role="listbox"] *, .autocomplete *, .ui-autocomplete *, .select2-results__option, .dropdown-menu *, ul li'
                ));
                const match = nodes.find((el) => visible(el) && digits(el.innerText || el.textContent).includes(expected));
                if (!match) return false;
                match.click();
                return true;
            }""", receiver))
        except Exception:
            return False

    async def _rewrite_receiver_cnpj_fallback(self, receiver: str, *, reason_context: str) -> None:
        loc = self._page.locator(self.RECEIVER_CNPJ_SELECTOR).first
        logger.warning(
            "[TRANSLOVATO] CNPJ destinatário divergente após %s; limpando e preenchendo novamente %s",
            reason_context,
            self._mask_doc(receiver),
        )
        await loc.fill("")
        await loc.dispatch_event("input")
        await loc.dispatch_event("change")
        await loc.fill(receiver)
        await loc.dispatch_event("input")
        await loc.dispatch_event("change")
        await loc.dispatch_event("blur")
        await self._wait_for_receiver_resolution(
            receiver,
            context=f"fallback seguro após {reason_context}",
            timeout_ms=2500,
            allow_autocomplete_click=True,
        )
        await self._validate_receiver_cnpj(receiver, context=f"fallback seguro após {reason_context}")

    async def _preencher_cnpj_destinatario(self, receiver: str) -> None:
        loc = self._page.locator(self.RECEIVER_CNPJ_SELECTOR).first
        await loc.wait_for(state="visible", timeout=12000)
        if await self._read_receiver_cnpj_digits() == receiver:
            logger.info(
                "[TRANSLOVATO] CNPJ destinatário já estava correto no portal; preservando valor automático %s",
                self._mask_doc(receiver),
            )
            await self._validate_receiver_cnpj(receiver, context="valor já preenchido")
            return
        await loc.fill(receiver)
        await loc.dispatch_event("input")
        await loc.dispatch_event("change")
        await loc.dispatch_event("blur")
        resolved_after_blur = await self._wait_for_receiver_resolution(
            receiver,
            context="blur",
            timeout_ms=2500,
            allow_autocomplete_click=True,
        )
        try:
            await self._validate_receiver_cnpj(receiver, context="blur")
        except ValueError:
            await self._rewrite_receiver_cnpj_fallback(receiver, reason_context="blur")
        if resolved_after_blur != receiver or await self._receiver_autocomplete_visible():
            await loc.press("Tab")
            await self._wait_for_receiver_resolution(
                receiver,
                context="tabulação",
                timeout_ms=2000,
                allow_autocomplete_click=False,
            )
        await self._validate_receiver_cnpj(receiver, context="tabulação")

    async def _read_delivery_zip_digits(self) -> str:
        try:
            loc = self._page.locator(self.DELIVERY_ZIP_SELECTOR)
            if await loc.count() == 0:
                return ""
            return _digits(await loc.first.input_value(timeout=1500))
        except Exception:
            return ""

    async def _read_delivery_city_uf(self) -> tuple[str, str, str]:
        script = r"""() => {
            const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width >= 0 && rect.height >= 0;
            };
            const fields = Array.from(document.querySelectorAll('input, select, textarea'));
            const labelFor = (el) => {
                const id = el.getAttribute('id') || '';
                const direct = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                const parent = el.closest('label');
                return [direct && direct.innerText, parent && parent.innerText].filter(Boolean).join(' ');
            };
            const textOf = (el) => [
                el.getAttribute('id'),
                el.getAttribute('name'),
                el.getAttribute('placeholder'),
                el.getAttribute('aria-label'),
                labelFor(el),
            ].filter(Boolean).join(' ').toLowerCase();
            const valueOf = (el) => {
                if (el.tagName === 'SELECT') {
                    return (el.options[el.selectedIndex]?.text || el.value || '').trim();
                }
                return (el.value || '').trim();
            };
            const candidates = fields
                .filter(visible)
                .map((el) => ({ el, haystack: textOf(el), value: valueOf(el) }))
                .filter((item) => item.value);
            const cityUf = candidates.find((item) =>
                /(cidade|municipio|munic[ií]pio).*(uf|estado)|cidade.*estado|cep.*cidade|entrega.*cidade|destino.*cidade/.test(item.haystack)
            );
            if (cityUf) return cityUf.value;
            const city = candidates.find((item) => /(cidade|municipio|munic[ií]pio)/.test(item.haystack));
            const uf = candidates.find((item) => /(^|[^a-z])(uf|estado)([^a-z]|$)/.test(item.haystack));
            return [city && city.value, uf && uf.value].filter(Boolean).join('/');
        }"""
        try:
            raw = str(await self._page.evaluate(script) or "").strip()
        except Exception:
            raw = ""
        city, uf = self._parse_cidade_uf(raw)
        return raw, city, uf

    async def _aguardar_e_validar_autopreenchimento_destino(
        self,
        *,
        expected_receiver: str,
        expected_cep: str,
        expected_city: str = "",
        expected_uf: str = "",
    ) -> None:
        expected_city_norm = self._normalizar_texto_comparacao(expected_city)
        expected_uf_norm = str(expected_uf or "").strip().upper()
        detected_zip = ""
        detected_raw = ""
        detected_city = ""
        detected_uf = ""

        started_at = time.monotonic()
        for _ in range(24):
            detected_zip, (detected_raw, detected_city, detected_uf) = await asyncio.gather(
                self._read_delivery_zip_digits(),
                self._read_delivery_city_uf(),
            )
            if (not expected_cep or detected_zip == expected_cep) and (detected_city or detected_uf):
                break
            await self._page.wait_for_timeout(250)

        await self._validate_receiver_cnpj(expected_receiver, context="autopreenchimento do endereço")
        logger.info(
            "[TRANSLOVATO] Diagnóstico endereço destino: CNPJ esperado=%s CNPJ campo=%s cidade/UF esperada=%s/%s cidade/UF detectada=%s",
            self._mask_doc(expected_receiver),
            self._mask_doc(await self._read_receiver_cnpj_digits()),
            expected_city_norm or "?",
            expected_uf_norm or "?",
            detected_raw or "?",
        )

        if expected_cep and not detected_zip:
            logger.warning(
                "[TRANSLOVATO] Portal não expôs CEP de entrega após CNPJ válido; seguindo com endereço automático. CNPJ=%s",
                self._mask_doc(expected_receiver),
            )
        elif expected_cep and detected_zip != expected_cep:
            raise ValueError("CEP de entrega diverge do romaneio; cotacao bloqueada")
        if not detected_city and not detected_uf:
            logger.warning(
                "[TRANSLOVATO] Cidade/UF do destino não detectadas com segurança; seguindo com endereço automático do portal. CNPJ=%s",
                self._mask_doc(expected_receiver),
            )
            logger.info("[TRANSLOVATO] Autopreenchimento do portal estabilizado em %.2fs", time.monotonic() - started_at)
            return
        if expected_uf_norm and detected_uf and detected_uf != expected_uf_norm:
            raise ValueError(
                "UF de entrega preenchida pelo portal diverge do romaneio: "
                f"esperada {expected_uf_norm}, detectada {detected_uf}."
            )
        if expected_city_norm and detected_city and detected_city != expected_city_norm:
            raise ValueError(
                "Cidade de entrega preenchida pelo portal diverge do romaneio: "
                f"esperada {expected_city_norm}, detectada {detected_city}."
            )
        logger.info("[TRANSLOVATO] Autopreenchimento do portal estabilizado em %.2fs", time.monotonic() - started_at)

    async def _ensure_cubagem_rows(self, desired_rows: int) -> int:
        page = self._page
        desired_rows = max(1, int(desired_rows or 1))
        add_names = re.compile(r"adicionar\s+(linha|item|volume)|incluir\s+(linha|item|volume)", re.I)
        for _ in range(desired_rows - 1):
            current = await page.locator(self.QUANTITY_SELECTOR).count()
            if current >= desired_rows:
                break
            clicked = False
            for getter in (
                lambda: page.get_by_role("button", name=add_names),
                lambda: page.get_by_role("link", name=add_names),
                lambda: page.get_by_text(add_names),
            ):
                try:
                    loc = getter().first
                    if await loc.is_visible(timeout=1000):
                        await loc.click(timeout=1500)
                        await page.wait_for_timeout(500)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                break
        return max(1, await page.locator(self.QUANTITY_SELECTOR).count())

    async def _preencher_cubagens(self, cubagens: list[dict[str, int]]) -> str:
        supported_rows = await self._ensure_cubagem_rows(len(cubagens))
        detalhes = ""
        if len(cubagens) > supported_rows:
            detalhes = "Múltiplas cubagens parcialmente suportadas pelo portal; enviada a primeira linha disponível."
        for index, cub in enumerate(cubagens[:supported_rows]):
            await self._fill_input(self.QUANTITY_SELECTOR, str(int(cub["quantidade"])), index=index)
            await self._fill_input(self.HEIGHT_SELECTOR, self._cm_to_m_br(cub["altura_cm"]), index=index)
            await self._fill_input(self.LENGTH_SELECTOR, self._cm_to_m_br(cub["comprimento_cm"]), index=index)
            await self._fill_input(self.DEPTH_SELECTOR, self._cm_to_m_br(cub["largura_cm"]), index=index)
        return detalhes

    async def _read_cubagem_resumo_portal(self) -> dict[str, str]:
        page = self._page
        script = r"""() => {
            const valueOf = (selector, index = 0) => {
                const nodes = Array.from(document.querySelectorAll(selector));
                const el = nodes[index];
                return el ? String(el.value || '').trim() : '';
            };
            return {
                cubagem_linha: valueOf('input[name="cubing[]"]'),
                peso_cubado_linha: valueOf('input[name="cubing_weigth[]"]'),
                cubagem_total: valueOf('input[name="cubing_total"]'),
                peso_cubado_total: valueOf('input[name="cubing_weigth_total"]'),
            };
        }"""
        return dict(await page.evaluate(script) or {})

    async def _fallback_preencher_resumo_cubagem(self, cubagens: list[dict[str, int]]) -> dict[str, Any]:
        page = self._page
        factor_script = r"""() => {
            const factor = document.querySelector('#product-factor');
            return factor ? String(factor.value || '').trim() : '';
        }"""
        factor_raw = str(await page.evaluate(factor_script) or "").replace(",", ".").strip()
        try:
            factor = float(factor_raw or 0.0)
        except Exception:
            factor = 0.0
        resumo = self._calcular_resumo_cubagem(cubagens, fator_produto=factor or 300.0)
        payload = {
            "linhas": resumo["linhas"],
            "totalCubagem": resumo["total_cubagem"],
            "totalPeso": resumo["total_peso_cubado"],
        }
        await page.evaluate(
            r"""(data) => {
                const setValue = (el, value, key = 'Tab') => {
                    if (!el) return;
                    el.value = String(value || '');
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                };
                const cubagemInputs = Array.from(document.querySelectorAll('input[name="cubing[]"]'));
                const pesoInputs = Array.from(document.querySelectorAll('input[name="cubing_weigth[]"]'));
                data.linhas.forEach((linha, index) => {
                    setValue(cubagemInputs[index], linha.cubagem);
                    setValue(pesoInputs[index], linha.peso_cubado);
                });
                setValue(document.querySelector('input[name="cubing_total"]'), data.totalCubagem);
                setValue(document.querySelector('input[name="cubing_weigth_total"]'), data.totalPeso);
            }""",
            payload,
        )
        return resumo

    async def _garantir_resumo_cubagem_calculado(self, cubagens: list[dict[str, int]]) -> None:
        resumo = {}
        started_at = time.monotonic()
        while time.monotonic() - started_at < 3:
            resumo = await self._read_cubagem_resumo_portal()
            if any(str(resumo.get(key) or "").strip() not in {"", "0", "0,0", "0,00", "0,000", "0,0000"} for key in ("cubagem_total", "peso_cubado_total")):
                logger.info(
                    "[TRANSLOVATO] Portal calculou cubagem automaticamente: cubagem_total=%s peso_cubado_total=%s",
                    resumo.get("cubagem_total") or "?",
                    resumo.get("peso_cubado_total") or "?",
                )
                return
            await self._page.wait_for_timeout(200)
        if any(str(resumo.get(key) or "").strip() not in {"", "0", "0,0", "0,00", "0,000", "0,0000"} for key in ("cubagem_total", "peso_cubado_total")):
            logger.info(
                "[TRANSLOVATO] Portal calculou cubagem automaticamente: cubagem_total=%s peso_cubado_total=%s",
                resumo.get("cubagem_total") or "?",
                resumo.get("peso_cubado_total") or "?",
            )
            return

        fallback = await self._fallback_preencher_resumo_cubagem(cubagens)
        await self._page.wait_for_timeout(200)
        resumo_fim = await self._read_cubagem_resumo_portal()
        logger.warning(
            "[TRANSLOVATO] Portal não calculou cubagem automaticamente; fallback aplicado. cubagem_total=%s peso_cubado_total=%s fator=%s",
            resumo_fim.get("cubagem_total") or fallback.get("total_cubagem") or "?",
            resumo_fim.get("peso_cubado_total") or fallback.get("total_peso_cubado") or "?",
            fallback.get("fator_produto") or "?",
        )

    async def _preencher_formulario(
        self,
        *,
        origem: str,
        destino: str,
        peso: float,
        valor: float,
        volumes: int,
        cubagens: list[dict[str, int]],
        cnpj_destinatario: str,
        cnpj_remetente: str,
        cidade_destino: str = "",
        uf_destino: str = "",
    ) -> str:
        self._passo_atual = "preenchendo_formulario"
        sender = _digits(cnpj_remetente or self.cnpj_remetente or self.cnpj)
        receiver = _digits(cnpj_destinatario)
        if len(sender) != 14:
            raise ValueError("CNPJ remetente inválido para TRANSLOVATO")
        if len(receiver) != 14:
            raise ValueError("CNPJ destinatário inválido para TRANSLOVATO")

        logger.info(
            "[TRANSLOVATO] Preenchendo cotação remetente=%s destinatario=%s",
            self._mask_doc(sender),
            self._mask_doc(receiver),
        )
        await self._fill_input(self.SENDER_CNPJ_SELECTOR, sender)
        await self._preencher_cnpj_destinatario(receiver)
        try:
            await self._page.wait_for_load_state("networkidle", timeout=5000)
        except PlaywrightTimeoutError:
            pass
        if _digits(destino):
            await self._fill_input(self.DELIVERY_ZIP_SELECTOR, _digits(destino))
            try:
                await self._page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                pass
        await self._aguardar_e_validar_autopreenchimento_destino(
            expected_receiver=receiver,
            expected_cep=_digits(destino),
            expected_city=cidade_destino,
            expected_uf=uf_destino,
        )
        await self._fill_input(self.VALUE_SELECTOR, self._format_decimal_br(valor, 2))
        await self._fill_input(self.WEIGHT_SELECTOR, self._format_decimal_br(peso, 3))
        detalhes = await self._preencher_cubagens(cubagens)
        await self._garantir_resumo_cubagem_calculado(cubagens)
        await self._validate_receiver_cnpj(receiver, context="preenchimento final da cotação")
        return detalhes

    async def _ler_sweet_alert(self) -> str:
        """Retorna título+mensagem de um SweetAlert visível (ex.: 'Oops!'), ou ''."""
        try:
            return await self._page.evaluate(
                """() => {
                    const el = document.querySelector(
                        '.sweet-alert.visible, .sweet-alert.showSweetAlert'
                    );
                    if (!el) return '';
                    // SweetAlert usa position:fixed → offsetParent é sempre null
                    // mesmo visível; use display/visibility computados.
                    const cs = getComputedStyle(el);
                    if (cs.display === 'none' || cs.visibility === 'hidden') return '';
                    const h2 = el.querySelector('h2');
                    const p = el.querySelector('p');
                    const titulo = h2 ? (h2.textContent || '').trim() : '';
                    const msg = p ? (p.textContent || '').trim() : '';
                    return [titulo, msg].filter(Boolean).join(' - ');
                }"""
            )
        except Exception:
            return ""

    async def _fechar_sweet_alert(self) -> None:
        """Fecha o SweetAlert clicando no botão de confirmação/cancelamento."""
        try:
            await self._page.evaluate(
                """() => {
                    const el = document.querySelector(
                        '.sweet-alert.visible, .sweet-alert.showSweetAlert'
                    );
                    if (!el) return;
                    const btn = el.querySelector('button.confirm, button.cancel, button');
                    if (btn) btn.click();
                }"""
            )
            await self._page.wait_for_timeout(300)
        except Exception:
            pass

    async def _simular_e_extrair(self, detalhes_extra: str = "") -> Cotacao | None:
        started_at = self._start_stage("simulando_cotacao")
        page = self._page
        botao = page.get_by_role("button", name=re.compile(r"simular\s+cota[çc][aã]o", re.I))
        # Um SweetAlert "Oops!" (erro de validação do portal) sobrepõe o botão e
        # intercepta o clique — antes isso gerava 20s de retry cego seguidos de
        # TimeoutError enganoso. Detecta o diálogo e falha rápido com a mensagem
        # real do portal.
        alerta = await self._ler_sweet_alert()
        if alerta:
            self.last_error = f"Portal exibiu erro na simulação: {alerta}"
            logger.warning("[TRANSLOVATO] %s", self.last_error)
            await self._fechar_sweet_alert()
            return None
        try:
            await botao.click(timeout=20000)
        except PlaywrightTimeoutError:
            alerta = await self._ler_sweet_alert()
            if alerta:
                self.last_error = f"Portal exibiu erro na simulação: {alerta}"
                logger.warning("[TRANSLOVATO] %s", self.last_error)
                await self._fechar_sweet_alert()
                return None
            raise
        # Espera curta só para a requisição da cotação partir; o resultado em si é
        # detectado por _wait_for_quote_result (poll de valor+prazo). Trackers de
        # terceiros podem impedir o networkidle de disparar, então não bloqueamos
        # os 15s antigos aqui — era tempo morto antes da extração.
        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except PlaywrightTimeoutError:
            pass

        self._passo_atual = "extraindo_resultado"
        text, valor, prazo = await self._wait_for_quote_result(timeout_ms=30000)
        if valor is None or prazo is None:
            # Frase alinhada a _SEM_COTACAO_PATTERNS (base.py) para classificar como
            # sem_cotacao, não erro técnico: portal simplesmente não devolveu cotação.
            self.last_error = "Portal respondeu sem cotação (sem valor ou prazo)"
            logger.warning("[TRANSLOVATO] %s", self.last_error)
            return None
        detalhes = detalhes_extra or None
        self._finish_stage("simulando_cotacao", started_at, details=f"valor={valor:.2f} prazo={prazo}d")
        return Cotacao(
            transportadora="TRANSLOVATO",
            prazo_dias=int(prazo),
            valor_frete=float(valor),
            restricoes=detalhes,
        )

    async def coteir(
        self,
        origem: str,
        destino: str,
        peso: float,
        valor: float,
        volumes: int = 1,
        cubagem_m3: float = 0.0,
        cubagens: list[dict[str, Any]] | None = None,
        cnpj_destinatario: str = "",
        cnpj_remetente: str = "",
        cep_origem: str = "",
        cep_destino: str = "",
        uf_destino: str = "",
        cidade_destino: str = "",
        **_kwargs: Any,
    ) -> Cotacao | None:
        try:
            self.last_error = None
            cubagens_validas = self._normalizar_cubagens_cm(cubagens, volumes=volumes)
            if not cubagens_validas:
                self.last_error = "Cubagem inválida: dimensões ausentes no romaneio"
                logger.warning("[TRANSLOVATO] %s", self.last_error)
                return None

            destino_cep = _digits(cep_destino or destino)
            origem_cep = _digits(cep_origem or origem)
            if len(destino_cep) != 8:
                raise ValueError("CEP de destino inválido para TRANSLOVATO")
            if origem_cep and len(origem_cep) != 8:
                raise ValueError("CEP de origem inválido para TRANSLOVATO")

            stage_start = self._start_stage("init_browser")
            await self._init_browser()
            self._finish_stage("init_browser", stage_start)
            if not await self._login():
                return None
            await self._abrir_nova_cotacao()
            stage_start = self._start_stage("preenchendo_formulario")
            detalhes = await self._preencher_formulario(
                origem=origem_cep,
                destino=destino_cep,
                peso=float(peso or 0.0),
                valor=float(valor or 0.0),
                volumes=int(volumes or 1),
                cubagens=cubagens_validas,
                cnpj_destinatario=cnpj_destinatario,
                cnpj_remetente=cnpj_remetente,
                cidade_destino=cidade_destino,
                uf_destino=uf_destino,
            )
            self._finish_stage("preenchendo_formulario", stage_start, details=f"linhas_cubagem={len(cubagens_validas)}")
            return await self._simular_e_extrair(detalhes)
        except Exception as exc:
            self.last_error = f"Falha na cotação TRANSLOVATO ({self._passo_atual or 'desconhecido'}): {exc}"
            logger.warning("[TRANSLOVATO] %s", self.last_error)
            raise
