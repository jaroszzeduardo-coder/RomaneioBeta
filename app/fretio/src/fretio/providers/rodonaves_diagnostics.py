"""Mixin de diagnóstico/lifecycle do provider Rodonaves (métodos movidos de rodonaves.py)."""
from typing import Any
import re
from fretio.logging_conf import get_logger

logger = get_logger(__name__)


class RodonavesDiagnosticsMixin:
    @staticmethod
    def _is_retryable_navigation_error(error: BaseException | str) -> bool:
        text = str(error or "")
        if not text:
            return False
        upper = text.upper()
        if "TIMEOUT" in upper or "TIMED OUT" in upper:
            return True
        return any(
            token in upper
            for token in (
                "ERR_ABORTED",
                "ERR_CONNECTION_RESET",
                "ERR_CONNECTION_CLOSED",
                "ERR_CONNECTION_TIMED_OUT",
                "ERR_NAME_NOT_RESOLVED",
                "ERR_NETWORK_CHANGED",
                "ERR_INTERNET_DISCONNECTED",
                "ERR_ADDRESS_UNREACHABLE",
                "ERR_HTTP2_PROTOCOL_ERROR",
            )
        )


    @staticmethod
    def _is_playwright_lifecycle_error(error: BaseException | str) -> bool:
        text = str(error or "").lower()
        return any(
            token in text
            for token in (
                "target page, context or browser has been closed",
                "target closed",
                "browser has been closed",
                "context has been closed",
                "page has been closed",
                "browser closed",
            )
        )

    @staticmethod
    def _is_page_level_error(error: BaseException) -> bool:
        """Erros transitórios de frame/rede que afetam só a page, não o browser."""
        text = str(error or "").lower()
        return any(token in text for token in (
            "frame was detached",
            "net::err_aborted",
            "net::err_connection",
            "net::err_timed_out",
        ))

    def _safe_current_url(self) -> str:
        page = self._page
        if page is None:
            return "(sem page)"
        try:
            if page.is_closed():
                return "(page fechada)"
        except Exception:
            return "(page sem status)"
        try:
            return str(getattr(page, "url", "") or "(sem URL)")
        except Exception:
            return "(URL indisponivel)"

    def _browser_is_connected(self) -> bool:
        try:
            return bool(self._browser and self._browser.is_connected())
        except Exception:
            return False

    def _page_is_closed(self, page: Any | None = None) -> bool:
        page = self._page if page is None else page
        if page is None:
            return True
        try:
            return bool(page.is_closed())
        except Exception:
            return True

    def _lifecycle_closed_reason(self) -> str | None:
        if not self._browser:
            return "browser ausente"
        if not self._browser_is_connected():
            return "browser desconectado"
        if self._context is None:
            return "context ausente"
        if self._page is None:
            return "page ausente"
        if self._page_is_closed():
            return "page fechada"
        return None

    def _record_lifecycle_diagnostic(
        self,
        *,
        stage: str,
        target_url: str,
        reason: str,
        previous_stage: str | None = None,
        error: BaseException | None = None,
    ) -> None:
        detail = (
            "Lifecycle Playwright RODONAVES: "
            f"stage={stage}; previous_stage={previous_stage or self._passo_atual}; "
            f"target_url={target_url}; current_url={self._safe_current_url()}; "
            f"reason={reason}; headless={self._effective_headless}"
        )
        self._diagnostic_context.update({
            "rodonaves_stage": stage,
            "rodonaves_previous_stage": previous_stage or self._passo_atual,
            "rodonaves_target_url": target_url,
            "rodonaves_current_url": self._safe_current_url(),
            "rodonaves_close_reason": reason,
            "rodonaves_effective_headless": self._effective_headless,
        })
        if error is not None:
            detail = f"{detail}; error={error}"
        self.last_error = detail
        logger.warning(f"[{self.nome}] {detail}")

    @staticmethod
    def _safe_diagnostic_excerpt(value: Any, *, limit: int = 900) -> str:
        text = str(value or "")
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
        text = re.sub(r"(?is)<[^>]+>", " ", text)
        text = re.sub(r"\b\d{14}\b", "***", text)
        text = re.sub(r"\b\d{11}\b", "***", text)
        text = re.sub(r"\b\d{5}-?\d{3}\b", "***", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > limit:
            return text[:limit].rstrip() + "..."
        return text

    def _set_last_error_with_diagnostic(self, message: str, *, stage: str | None = None) -> None:
        stage_name = stage or self._passo_atual
        snapshot = self._diagnostic_context.get("rodonaves_snapshot")
        suffix = ""
        if isinstance(snapshot, dict):
            flags = []
            # body_excerpt/alert_excerpt podem conter nome/endereço do destinatário
            # (texto cru do portal) e last_error vai para o servidor; ficam só no
            # snapshot local. Aqui só metadados estruturais seguros.
            for key in (
                "url",
                "title",
                "recaptcha_frames",
                "captcha_token_len",
                "form_present",
                "result_present",
                "road_option_present",
                "road_price_present",
                "road_deadline_present",
            ):
                value = snapshot.get(key)
                if value not in (None, "", []):
                    flags.append(f"{key}={value}")
            if flags:
                suffix = " | diagnostico: " + "; ".join(flags)
        self.last_error = f"{message} (stage={stage_name}; headless={self._effective_headless}){suffix}"

    async def _capture_safe_diagnostic_snapshot(
        self,
        *,
        reason: str,
        stage: str | None = None,
        api_result: dict | None = None,
    ) -> dict[str, Any]:
        page = self._page
        snapshot: dict[str, Any] = {
            "reason": str(reason or "")[:120],
            "stage": stage or self._passo_atual,
            "headless": self.headless,
            "effective_headless": self._effective_headless,
            "logged_in": self._logged_in,
        }
        if page is None:
            snapshot["page_state"] = "ausente"
            self._diagnostic_context["rodonaves_snapshot"] = snapshot
            return snapshot
        try:
            snapshot["page_closed"] = bool(page.is_closed())
        except Exception:
            snapshot["page_closed"] = None
        try:
            snapshot["url"] = str(getattr(page, "url", "") or "")[:240]
        except Exception:
            snapshot["url"] = ""
        if snapshot.get("page_closed"):
            self._diagnostic_context["rodonaves_snapshot"] = snapshot
            return snapshot
        try:
            snapshot["title"] = self._safe_diagnostic_excerpt(await page.title(), limit=120)
        except Exception:
            pass
        try:
            dom_state = await page.evaluate(r"""() => {
                const text = (document.body && document.body.innerText || '').replace(/\s+/g, ' ').trim();
                const alertText = Array.from(document.querySelectorAll('.alert, .validation-summary-errors, [role="alert"], .field-validation-error'))
                    .map(el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim())
                    .filter(Boolean)
                    .join(' | ');
                const visible = (selector) => {
                    const el = document.querySelector(selector);
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                };
                const captchaToken = document.querySelector('textarea[name="g-recaptcha-response"]');
                const roadSection = (text.match(/Rodovi[áa]rio([\s\S]*?)(?=Rodoa[ée]reo|$)/i) || [])[1] || '';
                return {
                    alertPresent: !!alertText,
                    formPresent: !!document.querySelector('#ReceiverTaxId, #senderTaxId'),
                    calculateButtonPresent: !!document.querySelector('#calculateQuotationBtn') || Array.from(document.querySelectorAll('button')).some(el => el.textContent && el.textContent.trim() === 'Cotar frete'),
                    calculateButtonVisible: visible('#calculateQuotationBtn') || Array.from(document.querySelectorAll('button')).some(el => el.textContent && el.textContent.trim() === 'Cotar frete' && el.getBoundingClientRect().width > 0),
                    resultPresent: !!document.querySelector('td.col-result, #quotationResult') || /Composi[çc][ãa]o do frete/.test(text),
                    roadOptionPresent: /Rodovi[áa]rio/.test(text),
                    roadPricePresent: /R\$\s*[\d.]+,\d{2}/.test(roadSection),
                    roadDeadlinePresent: /Prazo:\s*at[ée]\s+\d+\s+dias?/i.test(roadSection),
                    recaptchaFrames: document.querySelectorAll('iframe[title*="reCAPTCHA"], iframe[src*="recaptcha"]').length,
                    captchaTokenLen: captchaToken && captchaToken.value ? captchaToken.value.length : 0,
                };
            }""")
            if isinstance(dom_state, dict):
                snapshot.update({
                    "form_present": bool(dom_state.get("formPresent")),
                    "calculate_button_present": bool(dom_state.get("calculateButtonPresent")),
                    "calculate_button_visible": bool(dom_state.get("calculateButtonVisible")),
                    "result_present": bool(dom_state.get("resultPresent")),
                    "recaptcha_frames": int(dom_state.get("recaptchaFrames") or 0),
                    "captcha_token_len": int(dom_state.get("captchaTokenLen") or 0),
                    "alert_present": bool(dom_state.get("alertPresent", dom_state.get("alertText"))),
                    "road_option_present": bool(dom_state.get("roadOptionPresent")),
                    "road_price_present": bool(dom_state.get("roadPricePresent")),
                    "road_deadline_present": bool(dom_state.get("roadDeadlinePresent")),
                })
        except Exception as exc:
            snapshot["snapshot_error"] = self._safe_diagnostic_excerpt(exc, limit=180)
        if api_result:
            snapshot["api_url"] = str(api_result.get("url") or "").split("?", 1)[0][:240]
            if "json" in api_result:
                snapshot["api_kind"] = "json"
                data = api_result.get("json")
                snapshot["api_key_envelope"] = isinstance(data, dict) and set(data) == {"key"}
                snapshot["api_field_count"] = len(data) if isinstance(data, dict) else 0
            elif "text" in api_result:
                snapshot["api_kind"] = "text"
        self._diagnostic_context["rodonaves_snapshot"] = snapshot
        logger.info(f"[{self.nome}] Diagnóstico seguro Rodonaves ({reason}): {snapshot}")
        return snapshot
