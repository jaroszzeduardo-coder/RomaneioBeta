import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "fretio" / "src"))

from cotacao.common import ResultadoCotacao, carrier_login_indicator_from_progress_payload
from fretio.providers.coopex import CoopexProvider
from fretio.providers.eucatur import EucaturProvider
from fretio.providers.factory import _build_rodonaves
from fretio.providers.rodonaves import RodonavesProvider
from fretio.providers.trd import TRDProvider
from fretio.providers.translovato import TranslovatoProvider


class _FakePage:
    async def wait_for_timeout(self, _ms):
        return None


async def _run_translovato_auto_address(
    *, detected_zip="01301100", detected_city="SAO PAULO", detected_uf="SP", expected_city="São Paulo", expected_uf="SP"
):
    provider = TranslovatoProvider(cnpj="12345678000190", usuario="user", senha="senha")
    provider._page = _FakePage()

    async def read_zip():
        return detected_zip

    async def read_city_uf():
        raw = f"{detected_city}/{detected_uf}" if detected_city or detected_uf else ""
        return raw, detected_city, detected_uf

    async def validate_receiver(_expected, *, context):
        return None

    provider._read_delivery_zip_digits = read_zip
    provider._read_delivery_city_uf = read_city_uf
    provider._validate_receiver_cnpj = validate_receiver
    await provider._aguardar_e_validar_autopreenchimento_destino(
        expected_receiver="12345678000190",
        expected_cep="01415001",
        expected_city=expected_city,
        expected_uf=expected_uf,
    )


def test_translovato_blocks_different_auto_cep_when_cnpj_is_valid():
    with pytest.raises(ValueError, match="CEP de entrega diverge"):
        asyncio.run(_run_translovato_auto_address())


def test_translovato_does_not_block_when_city_uf_are_not_detected():
    asyncio.run(_run_translovato_auto_address(detected_zip="01415001", detected_city="", detected_uf=""))


def test_translovato_blocks_clear_city_uf_divergence():
    with pytest.raises(ValueError, match="Cidade de entrega"):
        asyncio.run(_run_translovato_auto_address(detected_zip="01415001", detected_city="CAMPINAS", detected_uf="SP"))


def test_translovato_still_blocks_divergent_receiver_cnpj():
    provider = TranslovatoProvider(cnpj="12345678000190", usuario="user", senha="senha")

    async def read_receiver():
        return "00000000000000"

    async def diagnostic(**_kwargs):
        return {}

    provider._read_receiver_cnpj_digits = read_receiver
    provider._receiver_divergence_diagnostic = diagnostic
    with pytest.raises(ValueError, match="CNPJ destinatário no portal diverge"):
        asyncio.run(provider._validate_receiver_cnpj("12345678000190", context="teste"))


def test_rodonaves_valid_quote_neutralizes_prelogin_timeout_status():
    provider = RodonavesProvider("dom", "user", "senha", "12345678000190")
    provider.last_error = "Pre-login rodonaves timeout (60s) — login continuará na cotação"
    provider._set_login_status("login_falhou", True)

    provider._mark_valid_quote()

    assert provider.login_status["login_ok"] is True
    assert provider.login_status["cotacao_ok"] is True
    assert provider.login_status["login_falhou"] is False
    assert provider.last_error is None


def test_rodonaves_forces_headful_session_even_when_requested_headless():
    provider = RodonavesProvider("dom", "user", "senha", "12345678000190", headless=True)
    assert provider.headless is True
    assert provider._effective_headless is False
    original_makedirs = os.makedirs
    try:
        os.makedirs = lambda *_args, **_kwargs: None
        assert Path(provider._user_data_dir()).parts[-2:] == (".fretio", "rodonaves_browser_data")
    finally:
        os.makedirs = original_makedirs


def test_factory_build_rodonaves_respects_headless_true():
    kwargs = _build_rodonaves(
        {
            "dominio": "RTE",
            "usuario": "user",
            "senha": "senha",
            "cnpj_pagador": "12345678000190",
            "headless": True,
        }
    )

    assert kwargs is not None
    assert kwargs["headless"] is True


def test_rodonaves_hide_window_stays_offscreen_without_visible_restore(monkeypatch):
    provider = RodonavesProvider("dom", "user", "senha", "12345678000190")
    calls = []

    class CdpSession:
        async def send(self, method, payload=None):
            calls.append((method, payload))
            if method == "Browser.getWindowForTarget":
                return {"windowId": 123}
            return {}

    class Context:
        async def new_cdp_session(self, _page):
            return CdpSession()

    async def fail_win32(**_kwargs):
        raise AssertionError("off-screen hide must not use Win32 show path")

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("fretio.providers.rodonaves.ocultar_taskbar_por_pagina", noop)
    provider._context = Context()
    provider._page = object()
    provider._sync_active_page = noop
    provider._reposicionar_janela_win32 = fail_win32

    asyncio.run(provider._ocultar_janela())

    expected = {
        "windowId": 123,
        "bounds": {"left": -32000, "top": -32000, "width": 1920, "height": 1080},
    }
    assert ("Browser.setWindowBounds", expected) in calls
    assert all(
        (payload or {}).get("bounds", {}).get("windowState") != "normal"
        for _, payload in calls
    )


def test_rodonaves_prelogin_does_not_recreate_session_after_failure():
    provider = RodonavesProvider("dom", "user", "senha", "12345678000190")
    calls = []

    async def init_browser():
        calls.append("init")

    async def login():
        calls.append("login")
        raise RuntimeError("login failed")

    async def cleanup():
        calls.append("cleanup")

    provider._init_browser = init_browser
    provider._login = login
    provider.cleanup = cleanup

    assert asyncio.run(provider.pre_login()) is False
    assert calls == ["init", "login"]
    assert provider.last_error == "login failed"


def test_rodonaves_accepts_form_after_goto_commit_timeout():
    class Locator:
        def __init__(self):
            self.timeouts: list[int] = []

        async def wait_for(self, timeout):
            self.timeouts.append(timeout)
            if len(self.timeouts) < 6:
                raise RuntimeError("form not visible yet")
            return None

    class Page:
        def __init__(self):
            self.url = "https://cliente.rte.com.br/?showLogin=true"
            self._locator = Locator()

        def locator(self, _selector):
            return self._locator

    provider = RodonavesProvider("dom", "user", "senha", "12345678000190")
    page = Page()

    async def ensure_live_page_for_navigation(**_kwargs):
        return page

    async def goto_with_lifecycle_guard(*_args, **_kwargs):
        raise PlaywrightTimeoutError("Timeout 12000ms exceeded")

    provider._ensure_live_page_for_navigation = ensure_live_page_for_navigation
    provider._goto_with_lifecycle_guard = goto_with_lifecycle_guard

    asyncio.run(provider._navegar_cotacao(_from_login=True))

    assert 250 in page._locator.timeouts
    assert len(page._locator.timeouts) >= 6


def test_rodonaves_open_portal_entrypoint_never_starts_at_quotation():
    provider = RodonavesProvider("dom", "user", "senha", "12345678000190")
    page = None
    visited_urls = []

    class HiddenLocator:
        async def count(self):
            return 0

        async def wait_for(self, timeout):
            raise RuntimeError(f"not visible in {timeout}")

    class LoginLocator(HiddenLocator):
        async def count(self):
            return 1

        async def wait_for(self, timeout):
            return None

    class Page:
        def __init__(self):
            self.url = "about:blank"

        def locator(self, selector):
            if selector == "#cpfcnp":
                return LoginLocator()
            return HiddenLocator()

        async def wait_for_timeout(self, _ms):
            return None

    page = Page()

    async def ensure_live_page_for_navigation(**_kwargs):
        return page

    async def goto_with_lifecycle_guard(target_url, **_kwargs):
        visited_urls.append(target_url)
        page.url = target_url
        return page

    provider._ensure_live_page_for_navigation = ensure_live_page_for_navigation
    provider._goto_with_lifecycle_guard = goto_with_lifecycle_guard

    result_page = asyncio.run(provider._open_portal_entrypoint())

    assert result_page is page
    assert visited_urls == [provider.portal_entry_url]


def test_rodonaves_login_retries_once_via_entrypoint_when_post_login_navigation_fails():
    provider = RodonavesProvider("dom", "user", "senha", "12345678000190")
    page = object()
    open_calls = []
    login_calls = []
    quotation_calls = []

    async def open_portal_entrypoint():
        open_calls.append("open")
        return page

    async def wait_for_quotation_form(_page, *, timeout, success_message):
        return False

    async def perform_ajax_login(_page):
        login_calls.append("login")

    async def go_to_quotation_after_login():
        quotation_calls.append("quote")
        if len(quotation_calls) == 1:
            raise RuntimeError("Cotação Rodonaves voltou para login/entrypoint sem formulário")
        return page

    provider._open_portal_entrypoint = open_portal_entrypoint
    provider._wait_for_quotation_form = wait_for_quotation_form
    provider._perform_ajax_login = perform_ajax_login
    provider._go_to_quotation_after_login = go_to_quotation_after_login

    asyncio.run(provider._login())

    assert open_calls == ["open", "open"]
    assert login_calls == ["login", "login"]
    assert quotation_calls == ["quote", "quote"]
    assert provider.login_status["login_ok"] is True


def test_rodonaves_does_not_expose_headless_retry_helper():
    assert hasattr(RodonavesProvider, "_retry_visible_after_headless_captcha") is False


def test_rodonaves_js_calculate_click_uses_single_native_click():
    provider = RodonavesProvider("dom", "user", "senha", "12345678000190")

    class Page:
        def __init__(self):
            self.script = ""

        async def evaluate(self, script):
            self.script = script
            return True

    page = Page()

    assert asyncio.run(provider._click_calcular_via_js(page)) is True
    assert "el.click();" in page.script
    assert ".trigger(" not in page.script


def test_rodonaves_js_calculate_click_reports_missing_button():
    provider = RodonavesProvider("dom", "user", "senha", "12345678000190")

    class Page:
        async def evaluate(self, _script):
            return False

    assert asyncio.run(provider._click_calcular_via_js(Page())) is False


def test_successful_quote_promotes_login_indicator_to_ok():
    resultado = ResultadoCotacao(
        transportadora="RODONAVES",
        status="ok",
        valor_frete=925.56,
        prazo_dias=3,
    )

    assert carrier_login_indicator_from_progress_payload({"resultado": resultado}) == ("RODONAVES", "ok")


class _FakeInputLocator:
    def __init__(self):
        self.fill_calls: list[str] = []
        self.dispatched: list[str] = []
        self.press_calls: list[str] = []

    @property
    def first(self):
        return self

    async def wait_for(self, **_kwargs):
        return None

    async def fill(self, value):
        self.fill_calls.append(value)

    async def dispatch_event(self, name):
        self.dispatched.append(name)

    async def press(self, key):
        self.press_calls.append(key)


class _FakeReceiverPage:
    def __init__(self, locator):
        self._locator = locator

    def locator(self, _selector):
        return self._locator

    async def evaluate(self, _script):
        return False

    async def wait_for_timeout(self, _ms):
        return None


def test_translovato_preserves_already_correct_receiver_cnpj():
    provider = TranslovatoProvider(cnpj="12345678000190", usuario="user", senha="senha")
    locator = _FakeInputLocator()
    provider._page = _FakeReceiverPage(locator)

    async def read_receiver():
        return "12345678000190"

    validated_contexts: list[str] = []

    async def validate_receiver(_expected, *, context):
        validated_contexts.append(context)
        return None

    provider._read_receiver_cnpj_digits = read_receiver
    provider._validate_receiver_cnpj = validate_receiver

    asyncio.run(provider._preencher_cnpj_destinatario("12345678000190"))

    assert locator.fill_calls == []
    assert locator.press_calls == []
    assert validated_contexts == ["valor já preenchido"]


def test_translovato_does_not_tab_again_when_blur_already_resolves_receiver():
    provider = TranslovatoProvider(cnpj="12345678000190", usuario="user", senha="senha")
    locator = _FakeInputLocator()
    provider._page = _FakeReceiverPage(locator)

    read_values = iter(["", "12345678000190", "12345678000190", "12345678000190"])

    async def read_receiver():
        return next(read_values, "12345678000190")

    validated_contexts: list[str] = []

    async def validate_receiver(_expected, *, context):
        validated_contexts.append(context)
        return None

    provider._read_receiver_cnpj_digits = read_receiver
    provider._validate_receiver_cnpj = validate_receiver

    asyncio.run(provider._preencher_cnpj_destinatario("12345678000190"))

    assert locator.fill_calls == ["12345678000190"]
    assert locator.press_calls == []
    assert validated_contexts == ["blur", "tabulação"]


def test_translovato_wait_for_logged_in_state_accepts_logged_body_without_url_change():
    provider = TranslovatoProvider(cnpj="12345678000190", usuario="user", senha="senha")

    class BodyLocator:
        async def inner_text(self, timeout=None):
            return "Portal do cliente\nMinhas Cotações\nSair"

    class Page:
        url = "https://www.translovato.com.br/portal-do-cliente"

        def locator(self, _selector):
            return BodyLocator()

        async def wait_for_timeout(self, _ms):
            return None

    provider._page = Page()

    assert asyncio.run(provider._wait_for_logged_in_state(timeout_ms=500)) is True


def test_translovato_wait_for_logged_in_state_accepts_visible_quote_form():
    provider = TranslovatoProvider(cnpj="12345678000190", usuario="user", senha="senha")

    class FieldLocator:
        def __init__(self, visible):
            self.visible = visible

        @property
        def first(self):
            return self

        async def is_visible(self, timeout=None):
            return self.visible

        async def inner_text(self, timeout=None):
            return "Portal do cliente"

    class Page:
        url = "https://www.translovato.com.br/portal-do-cliente"

        def locator(self, selector):
            if selector in (provider.SENDER_CNPJ_SELECTOR, provider.RECEIVER_CNPJ_SELECTOR):
                return FieldLocator(True)
            return FieldLocator(False)

        async def wait_for_timeout(self, _ms):
            return None

    provider._page = Page()

    assert asyncio.run(provider._wait_for_logged_in_state(timeout_ms=500)) is True


def test_rodonaves_fill_field_uses_short_timeout_in_headless_and_js_fallback():
    provider = RodonavesProvider("dom", "user", "senha", "12345678000190", headless=True)
    provider._effective_headless = True

    class Locator:
        def __init__(self):
            self.timeouts = []

        async def fill(self, _value, timeout=None):
            self.timeouts.append(timeout)
            raise RuntimeError("overlay")

    class Page:
        def __init__(self):
            self.loc = Locator()
            self.js_calls = []

        def locator(self, _selector):
            return self.loc

        async def evaluate(self, _script, payload):
            self.js_calls.append(payload)
            return None

    page = Page()

    asyncio.run(provider._fill_field(page, "amountPacks2", "1"))

    assert page.loc.timeouts == [1200]
    assert page.js_calls == [{"fieldId": "amountPacks2", "val": "1"}]


def test_rodonaves_submit_keeps_same_session_when_manual_captcha_submission_happens():
    provider = RodonavesProvider("dom", "user", "senha", "12345678000190")
    events: list[str] = []

    class Response:
        url = "https://cliente.rte.com.br/Quotation/Calculate"
        status = 200
        headers = {"content-type": "application/json"}

        async def json(self):
            return {"TotalFreight": 321.45, "Prazo": 2}

    class Mouse:
        async def move(self, *_args, **_kwargs):
            return None

    class FakeLocator:
        def __init__(self, role=None):
            self.role = role
            self.clicks = 0

        @property
        def first(self):
            return self

        async def scroll_into_view_if_needed(self, **_kwargs):
            return None

        async def click(self, **_kwargs):
            self.clicks += 1
            if self.role == "checkbox":
                return None
            raise AssertionError("manual submit should not auto-click Calcular")

        async def wait_for(self, **_kwargs):
            return None

        async def count(self):
            return 1

        async def bounding_box(self):
            return {"x": 10, "y": 10, "width": 20, "height": 20}

    class FakeFrameLocator:
        def __init__(self):
            self.checkbox = FakeLocator(role="checkbox")

        def get_by_role(self, *_args, **_kwargs):
            return self.checkbox

    class FakePage:
        def __init__(self):
            self.handlers = {}
            self.mouse = Mouse()
            self.calculate_locator = FakeLocator(role="calculate")
            self.captcha_locator = FakeLocator(role="captcha-frame")
            self.frame = FakeFrameLocator()
            self.wait_calls = 0

        def on(self, event, handler):
            self.handlers[event] = handler

        def remove_listener(self, event, handler):
            if self.handlers.get(event) is handler:
                self.handlers.pop(event, None)

        def locator(self, selector):
            if selector == "#calculateQuotationBtn":
                return self.calculate_locator
            if selector == "iframe[title*='reCAPTCHA'], iframe[src*='recaptcha']":
                return self.captcha_locator
            return FakeLocator(role=selector)

        def frame_locator(self, _selector):
            return self.frame

        async def wait_for_timeout(self, _ms):
            self.wait_calls += 1
            if self.wait_calls == 2:
                self.handlers["response"](Response())
                await asyncio.sleep(0)
            return None

        async def evaluate(self, script):
            if "g-recaptcha-response" in script:
                return ""
            if "document.querySelectorAll('td.col-result').length > 0" in script:
                return bool(self.wait_calls >= 2)
            if "const fields = {};" in script:
                return {"captcha_token_len": 0}
            if "const texts = [];" in script:
                return []
            return None

        async def inner_text(self, _selector):
            return "Resultado manual já apareceu"

    page = FakePage()
    browser = object()
    context = object()
    provider._page = page
    provider._browser = browser
    provider._context = context
    provider._capture_safe_diagnostic_snapshot = lambda **_kwargs: asyncio.sleep(0, result={})
    provider._simular_interacao_humana = lambda _page: asyncio.sleep(0)
    provider._mostrar_janela = lambda: asyncio.sleep(0, result=events.append("show") or True)
    provider._ocultar_janela = lambda: asyncio.sleep(0, result=events.append("hide"))

    result = asyncio.run(provider._submeter_e_extrair())

    assert result is not None
    assert result.valor_frete == 321.45
    assert events == ["show", "hide"]
    assert provider._page is page
    assert provider._browser is browser
    assert provider._context is context
    assert page.calculate_locator.clicks == 0


def test_rodonaves_token_after_manual_submit_does_not_click_calcular_again():
    provider = RodonavesProvider("dom", "user", "senha", "12345678000190")

    class Response:
        url = "https://cliente.rte.com.br/Quotation/Calculate"
        status = 200
        headers = {"content-type": "application/json"}

        async def json(self):
            return {"TotalFreight": 321.45, "Prazo": 2}

    class Mouse:
        async def move(self, *_args, **_kwargs):
            return None

    class FakeLocator:
        def __init__(self, role=None):
            self.role = role
            self.clicks = 0

        @property
        def first(self):
            return self

        async def scroll_into_view_if_needed(self, **_kwargs):
            return None

        async def click(self, **_kwargs):
            self.clicks += 1
            if self.role == "checkbox":
                return None
            raise AssertionError("manual submit should not auto-click Calcular")

        async def wait_for(self, **_kwargs):
            return None

        async def count(self):
            return 1

        async def bounding_box(self):
            return {"x": 10, "y": 10, "width": 20, "height": 20}

    class FakeFrameLocator:
        def __init__(self):
            self.checkbox = FakeLocator(role="checkbox")

        def get_by_role(self, *_args, **_kwargs):
            return self.checkbox

    class FakePage:
        def __init__(self):
            self.handlers = {}
            self.mouse = Mouse()
            self.calculate_locator = FakeLocator(role="calculate")
            self.captcha_locator = FakeLocator(role="captcha-frame")
            self.frame = FakeFrameLocator()
            self.wait_calls = 0

        def on(self, event, handler):
            self.handlers[event] = handler

        def remove_listener(self, event, handler):
            if self.handlers.get(event) is handler:
                self.handlers.pop(event, None)

        def locator(self, selector):
            if selector == "#calculateQuotationBtn":
                return self.calculate_locator
            if selector == "iframe[title*='reCAPTCHA'], iframe[src*='recaptcha']":
                return self.captcha_locator
            return FakeLocator(role=selector)

        def frame_locator(self, _selector):
            return self.frame

        async def wait_for_timeout(self, _ms):
            self.wait_calls += 1
            if self.wait_calls == 2:
                self.handlers["response"](Response())
                await asyncio.sleep(0)
            return None

        async def evaluate(self, script):
            if "g-recaptcha-response" in script:
                return "resolved-token"
            if "document.querySelectorAll('td.col-result').length > 0" in script:
                return bool(self.wait_calls >= 2)
            if "const fields = {};" in script:
                return {"captcha_token_len": 14}
            if "const texts = [];" in script:
                return []
            return None

        async def inner_text(self, _selector):
            return "Resultado manual já apareceu"

    page = FakePage()
    provider._page = page
    provider._browser = object()
    provider._context = object()
    provider._capture_safe_diagnostic_snapshot = lambda **_kwargs: asyncio.sleep(0, result={})
    provider._simular_interacao_humana = lambda _page: asyncio.sleep(0)
    provider._mostrar_janela = lambda: asyncio.sleep(0, result=True)
    provider._ocultar_janela = lambda: asyncio.sleep(0)

    result = asyncio.run(provider._submeter_e_extrair())

    assert result is not None
    assert result.valor_frete == 321.45
    assert page.calculate_locator.clicks == 0


def test_rodonaves_cleanup_uses_active_user_data_dir_and_own_process_only(monkeypatch):
    provider = RodonavesProvider("dom", "user", "senha", "12345678000190")
    calls: list[tuple[str, object]] = []

    class Page:
        def is_closed(self):
            return False

        async def close(self):
            calls.append(("page", None))

    class Context:
        async def close(self):
            calls.append(("context", None))

    class Browser:
        def is_connected(self):
            return True

        async def close(self):
            calls.append(("browser", None))

    class Playwright:
        async def stop(self):
            calls.append(("playwright", None))

    class Proc:
        pid = 999

    monkeypatch.setattr("fretio.providers.rodonaves_browser._kill_proc", lambda proc: calls.append(("proc", proc.pid if proc else None)))
    monkeypatch.setattr(RodonavesProvider, "_fix_preferences", staticmethod(lambda path: calls.append(("prefs", path))))

    provider._page = Page()
    provider._context = Context()
    provider._browser = Browser()
    provider._playwright = Playwright()
    provider._chrome_proc = Proc()
    provider._active_user_data_dir = "C:/Users/test/.fretio/rodonaves_browser_data"

    asyncio.run(provider.cleanup())

    assert ("proc", 999) in calls
    assert ("prefs", "C:/Users/test/.fretio/rodonaves_browser_data") in calls
    assert provider._chrome_proc is None
    assert provider._active_user_data_dir == ""


def test_rodonaves_kill_stale_chrome_filters_by_exclusive_user_data_dir(monkeypatch):
    killed: list[int] = []

    class Result:
        stdout = "\n".join(
            [
                r"111|chrome.exe --user-data-dir=C:\Users\eduardo\.fretio\rodonaves_browser_data",
                r"222|chrome.exe --user-data-dir=C:\Users\eduardo\.fretio\other_profile",
            ]
        )

    monkeypatch.setattr("fretio.providers.rodonaves.os.kill", lambda pid, _sig: killed.append(pid))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)

    RodonavesProvider._kill_stale_chrome(r"C:\Users\eduardo\.fretio\rodonaves_browser_data")

    assert killed == [111]


def test_translovato_calcula_resumo_cubagem_com_fator_padrao():
    resumo = TranslovatoProvider._calcular_resumo_cubagem(
        [
            {
                "quantidade": 2,
                "comprimento_cm": 100,
                "largura_cm": 50,
                "altura_cm": 40,
            }
        ],
        fator_produto=0,
    )

    assert resumo["linhas"] == [{"cubagem": "0,4000", "peso_cubado": "120,00"}]
    assert resumo["total_cubagem"] == "0,4000"
    assert resumo["total_peso_cubado"] == "120,00"
    assert resumo["fator_produto"] == 300.0


class _CloseRaises:
    def __init__(self, exc):
        self.exc = exc

    def is_closed(self):
        return False

    async def close(self):
        raise self.exc


def test_eucatur_cleanup_ignores_cancelled_and_timeout_errors():
    async def run_cleanup():
        provider = EucaturProvider("dom", "user", "senha")
        provider._page = _CloseRaises(asyncio.CancelledError("cancelado"))
        provider._context = _CloseRaises(TimeoutError("travado"))
        provider._browser = _CloseRaises(PlaywrightTimeoutError("timeout"))
        await provider.cleanup()
        assert provider._page is None
        assert provider._context is None
        assert provider._browser is None

    asyncio.run(run_cleanup())


def test_trd_goto_timeout_is_treated_as_portal_instability():
    class Page:
        async def goto(self, *args, **kwargs):
            raise PlaywrightTimeoutError("Timeout 30000ms exceeded")

    async def run_goto():
        provider = TRDProvider("email@example.com", "senha")
        provider._page = Page()
        with pytest.raises(RuntimeError, match="instabilidade do portal/rede"):
            await provider._goto_cotacao_tratavel()

    asyncio.run(run_goto())


def test_coopex_classifies_name_not_resolved_as_temporary_network_error():
    assert CoopexProvider._is_temporary_network_error("net::ERR_NAME_NOT_RESOLVED") is True


def test_alfa_navegar_para_cotacao_detects_ready_form_without_navigation():
    from fretio.providers.alfa import AlfaProvider

    class FakeAlfaPage:
        def __init__(self):
            self.url = "https://areadocliente.alfatransportes.com.br/cotacao/"
            self.gotos = []

        async def evaluate(self, script, *args):
            return True

        async def wait_for_timeout(self, _ms):
            return None

    async def run_test():
        provider = AlfaProvider("user", "senha")
        provider._page = FakeAlfaPage()
        ok = await provider._navegar_para_cotacao()
        assert ok is True
        assert provider._page.gotos == []

    asyncio.run(run_test())


def test_alfa_navegar_para_cotacao_direct_url_success():
    from fretio.providers.alfa import AlfaProvider

    class FakeAlfaPage:
        def __init__(self):
            self.url = "https://areadocliente.alfatransportes.com.br/painel/"
            self.gotos = []
            self._eval_count = 0

        async def goto(self, url, **kwargs):
            self.gotos.append(url)
            self.url = url

        async def evaluate(self, script, *args):
            self._eval_count += 1
            return len(self.gotos) >= 1

        async def wait_for_timeout(self, _ms):
            return None

        def locator(self, _selector):
            class FakeLocator:
                async def is_visible(self, **kwargs):
                    return False
            return FakeLocator()

    async def run_test():
        provider = AlfaProvider("user", "senha")
        provider._page = FakeAlfaPage()
        ok = await provider._navegar_para_cotacao()
        assert ok is True
        assert len(provider._page.gotos) >= 1

    asyncio.run(run_test())


def test_alfa_legacy_url_auto_migration():
    from fretio.providers.alfa import AlfaProvider

    provider = AlfaProvider(
        "user",
        "senha",
        login_url="https://arearestrita.alfatransportes.com.br/login/",
        cotacao_url="https://arearestrita.alfatransportes.com.br/cotacao/api/",
    )
    assert provider.login_url == "https://areadocliente.alfatransportes.com.br/login/"
    assert provider.cotacao_api_url == "https://areadocliente.alfatransportes.com.br/cotacao/api/"
    assert provider.BASE_URL == "https://areadocliente.alfatransportes.com.br"


def test_rodonaves_ajax_login_failure_raises_and_marks_status():
    provider = RodonavesProvider("dom", "user", "senha", "12345678000190")

    class FakeLocator:
        def __init__(self, name=""):
            self.name = name

        async def is_visible(self, **kwargs):
            return True

        async def wait_for(self, **kwargs):
            return None

        async def fill(self, _val, **kwargs):
            return None

        async def click(self, **kwargs):
            return None

        async def count(self):
            return 0

        async def is_checked(self):
            return True

        async def check(self, **kwargs):
            return None

    class FakePage:
        def __init__(self):
            self.url = "https://cliente.rte.com.br/?showLogin=true"
            self.listeners = {}

        def locator(self, selector):
            return FakeLocator(selector)

        def on(self, event, handler):
            self.listeners[event] = handler

        def remove_listener(self, event, handler):
            self.listeners.pop(event, None)

        async def evaluate(self, script, *args):
            if "errorMessage" in script or "toast-message" in script:
                return "Usuário ou senha inválidos"
            return False

        async def wait_for_timeout(self, _ms):
            return None

    async def run_test():
        page = FakePage()
        with pytest.raises(RuntimeError, match="Login Rodonaves falhou — Usuário ou senha inválidos"):
            await provider._perform_ajax_login(page)
        assert provider.login_status["login_falhou"] is True
        assert provider.login_status["login_ok"] is False

    asyncio.run(run_test())


def test_rodonaves_extrair_de_json_handles_strings_and_prioritizes_freight():
    # Testa parsing de strings monetárias e prioridade de frete sobre nota fiscal
    data = {
        "EletronicInvoiceValue": 2500.0,
        "TotalFreight": "R$ 185,50",
        "Prazo": "4 dias úteis",
    }
    valor, prazo = RodonavesProvider._extrair_de_json(data)
    assert valor == 185.50
    assert prazo == 4

    # Testa dados de endereço (não deve extrair frete)
    address_data = {"ZipCode": "95000-000", "City": "Caxias do Sul", "State": "RS"}
    valor_addr, prazo_addr = RodonavesProvider._extrair_de_json(address_data)
    assert valor_addr is None
    assert prazo_addr == 0


def test_rodonaves_submeter_ignores_auxiliary_cep_lookup_and_clicks_calcular():
    provider = RodonavesProvider("dom", "user", "senha", "12345678000190")

    class ResponseZip:
        url = "https://cliente.rte.com.br/Quotation/GetAddressByZipCode"
        status = 200
        headers = {"content-type": "application/json"}

        async def json(self):
            return {"City": "Caxias do Sul", "State": "RS"}

    class ResponseCalc:
        url = "https://cliente.rte.com.br/Quotation/Calculate"
        status = 200
        headers = {"content-type": "application/json"}

        async def json(self):
            return {"TotalFreight": 142.30, "Prazo": 3}

    class FakeLocator:
        def __init__(self, role=None):
            self.role = role
            self.clicks = 0

        @property
        def first(self):
            return self

        async def scroll_into_view_if_needed(self, **_kwargs):
            return None

        async def click(self, **_kwargs):
            self.clicks += 1
            return None

        async def wait_for(self, **_kwargs):
            return None

        async def count(self):
            return 1

        async def bounding_box(self):
            return {"x": 10, "y": 10, "width": 20, "height": 20}

    class FakeFrameLocator:
        def __init__(self):
            self.checkbox = FakeLocator(role="checkbox")

        def get_by_role(self, *_args, **_kwargs):
            return self.checkbox

    class FakePage:
        def __init__(self):
            self.handlers = {}
            self.calculate_locator = FakeLocator(role="calculate")
            self.captcha_locator = FakeLocator(role="captcha-frame")
            self.frame = FakeFrameLocator()
            self.wait_calls = 0

        def on(self, event, handler):
            self.handlers[event] = handler

        def remove_listener(self, event, handler):
            self.handlers.pop(event, None)

        def locator(self, selector):
            if selector == "#calculateQuotationBtn":
                return self.calculate_locator
            if selector == "iframe[title*='reCAPTCHA'], iframe[src*='recaptcha']":
                return self.captcha_locator
            return FakeLocator(role=selector)

        def frame_locator(self, _selector):
            return self.frame

        async def wait_for_timeout(self, _ms):
            self.wait_calls += 1
            # Simula que o CEP lookup chegou durante a espera do captcha
            if self.wait_calls == 1:
                self.handlers["response"](ResponseZip())
                await asyncio.sleep(0)
            # Simula que a resposta de cálculo chegou depois que o botão foi clicado
            elif self.wait_calls == 4:
                self.handlers["response"](ResponseCalc())
                await asyncio.sleep(0)
            return None

        async def evaluate(self, script, *args):
            if "g-recaptcha-response" in script:
                return "token123"
            if "td.col-result" in script or "quotationResult" in script:
                return False
            if "fields" in script:
                return {"captcha_token_len": 8}
            if "texts" in script:
                return []
            return None

        async def inner_text(self, _selector):
            return ""

    page = FakePage()
    provider._page = page
    provider._browser = object()
    provider._context = object()
    provider._capture_safe_diagnostic_snapshot = lambda **_kwargs: asyncio.sleep(0, result={})
    provider._simular_interacao_humana = lambda _page: asyncio.sleep(0)
    provider._mostrar_janela = lambda: asyncio.sleep(0, result=True)
    provider._ocultar_janela = lambda: asyncio.sleep(0)

    result = asyncio.run(provider._submeter_e_extrair())

    assert result is not None
    assert result.valor_frete == 142.30
    assert result.prazo_dias == 3
    # Confirma que o botão Calcular FOI clicado (não foi pulado por causa do CEP lookup)
    assert page.calculate_locator.clicks >= 1


