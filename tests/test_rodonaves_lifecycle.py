import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _install_playwright_test_stub():
    if "playwright.async_api" in sys.modules:
        return
    try:
        if importlib.util.find_spec("playwright.async_api") is not None:
            return
    except (ImportError, ModuleNotFoundError, ValueError):
        pass
    playwright_module = ModuleType("playwright")
    playwright_module.__path__ = []
    async_api_module = ModuleType("playwright.async_api")

    class PlaywrightTimeoutError(TimeoutError):
        pass

    class _AsyncPlaywrightStub:
        async def start(self):
            return self

        async def stop(self):
            return None

    def async_playwright():
        return _AsyncPlaywrightStub()

    class Page:
        pass

    class Frame:
        pass

    async_api_module.TimeoutError = PlaywrightTimeoutError
    async_api_module.Page = Page
    async_api_module.Frame = Frame
    async_api_module.async_playwright = async_playwright
    playwright_module.async_api = async_api_module
    sys.modules.setdefault("playwright", playwright_module)
    sys.modules["playwright.async_api"] = async_api_module


_install_playwright_test_stub()

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app" / "fretio" / "src"))

from fretio.models import Cotacao
from fretio.providers.rodonaves import RodonavesProvider


class FakePage:
    def __init__(self, url="about:blank", closed=False, goto_error=None):
        self.url = url
        self.closed = closed
        self.goto_error = goto_error
        self.gotos = []

    def is_closed(self):
        return self.closed

    async def goto(self, url, **kwargs):
        self.gotos.append((url, kwargs))
        if self.goto_error is not None:
            error = self.goto_error
            self.goto_error = None
            raise error
        self.url = url

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, pages):
        self.pages = list(pages)
        self.created_pages = []

    async def new_page(self):
        page = FakePage(url="about:blank")
        self.pages.append(page)
        self.created_pages.append(page)
        return page

    async def close(self):
        for page in self.pages:
            page.closed = True


class FakeBrowser:
    def __init__(self, context, connected=True):
        self.contexts = [context]
        self.connected = connected

    def is_connected(self):
        return self.connected

    async def close(self):
        self.connected = False


def _provider():
    return RodonavesProvider(
        dominio="RTE",
        usuario="12345678000190",
        senha="senha_teste",
        cnpj_pagador="12345678000190",
    )


def test_rodonaves_recreates_page_when_closed_before_goto():
    async def run():
        provider = _provider()
        closed_page = FakePage(url="https://cliente.rte.com.br/?showLogin=true", closed=True)
        context = FakeContext([closed_page])
        provider._context = context
        provider._browser = FakeBrowser(context)
        provider._page = closed_page
        provider._passo_atual = "login"

        with pytest.raises(RuntimeError, match="reason=page fechada"):
            await provider._goto_with_lifecycle_guard(
                "https://cliente.rte.com.br/?showLogin=true",
                stage="login",
                wait_until="domcontentloaded",
                timeout=15000,
            )

        assert context.created_pages == [], (
            "Não pode criar uma nova page no mesmo context — "
            "a sessão deve preservar a mesma page"
        )
        assert closed_page.gotos == []
        assert provider._page is closed_page, (
            "A referência à page original deve permanecer inalterada"
        )
        assert "Lifecycle Playwright RODONAVES" in provider.last_error
        assert "target_url=https://cliente.rte.com.br/?showLogin=true" in provider.last_error
        assert "reason=page fechada" in provider.last_error

    asyncio.run(run())


def test_rodonaves_restarts_session_when_browser_closes_during_goto(monkeypatch):
    async def run():
        provider = _provider()
        first_page = FakePage(
            url="about:blank",
            goto_error=RuntimeError("Page.goto: Target page, context or browser has been closed"),
        )
        first_context = FakeContext([first_page])
        provider._context = first_context
        provider._browser = FakeBrowser(first_context)
        provider._page = first_page
        provider._passo_atual = "login"

        cleanup_calls = []
        init_calls = []

        async def fake_cleanup():
            cleanup_calls.append(True)

        async def fake_init_browser():
            init_calls.append(True)

        monkeypatch.setattr(provider, "cleanup", fake_cleanup)
        monkeypatch.setattr(provider, "_init_browser", fake_init_browser)

        with pytest.raises(RuntimeError, match="Target page, context or browser has been closed"):
            await provider._goto_with_lifecycle_guard(
                "https://cliente.rte.com.br/?showLogin=true",
                stage="login",
                wait_until="domcontentloaded",
                timeout=15000,
            )

        assert cleanup_calls == [], (
            "cleanup() não pode ser chamado em erro de lifecycle goto — "
            "a sessão deve ser preservada"
        )
        assert init_calls == [], (
            "_init_browser() não pode ser chamado em erro de lifecycle goto — "
            "a sessão deve ser preservada"
        )
        assert first_page.gotos[0][0] == "https://cliente.rte.com.br/?showLogin=true"
        assert "falha de lifecycle durante goto" in provider.last_error
        assert "sessão preservada" in provider.last_error
        assert "stage=login" in provider.last_error

    asyncio.run(run())


class FakeDiagnosticPage:
    url = "https://cliente.rte.com.br/Quotation"

    def is_closed(self):
        return False

    async def title(self):
        return "Cotação RTE"

    async def evaluate(self, _script):
        return {
            "bodyText": "Erro para CNPJ 12345678000190 no CEP 99999-000 sem valor",
            "alertText": "CNPJ 12345678000190 inválido",
            "formPresent": True,
            "calculateButtonPresent": True,
            "calculateButtonVisible": True,
            "resultPresent": False,
            "recaptchaFrames": 1,
            "captchaTokenLen": 0,
        }


def test_rodonaves_safe_diagnostic_snapshot_redacts_documents():
    async def run():
        provider = _provider()
        provider._page = FakeDiagnosticPage()
        provider._passo_atual = "ler_resultado"

        snapshot = await provider._capture_safe_diagnostic_snapshot(
            reason="valor_frete_nao_encontrado",
            stage="ler_resultado",
            api_result={"text": "CNPJ 12345678000190 valor ausente", "url": "https://cliente.rte.com.br/Quotation"},
        )

        assert snapshot["form_present"] is True
        assert snapshot["recaptcha_frames"] == 1
        assert "12345678000190" not in snapshot["body_excerpt"]
        assert "12345678000190" not in snapshot["alert_excerpt"]
        assert "12345678000190" not in snapshot["api_excerpt"]
        assert "***" in snapshot["body_excerpt"]
        assert provider._diagnostic_context["rodonaves_snapshot"] is snapshot

    asyncio.run(run())


def test_rodonaves_does_not_retry_visible_when_headless_recaptcha_blocks(monkeypatch):
    async def run():
        provider = _provider()
        provider._effective_headless = True
        calls = []

        async def fake_init_browser():
            calls.append("init")

        async def fake_login():
            calls.append("login")

        async def fake_navegar():
            calls.append("navegar")

        async def fake_preencher(**_kwargs):
            calls.append("preencher")

        async def fake_submeter():
            calls.append("submeter")
            provider.last_error = "Rodonaves: reCAPTCHA não resolvido ou bloqueio antifraude impediu a cotação"
            return None

        monkeypatch.setattr(provider, "_init_browser", fake_init_browser)
        monkeypatch.setattr(provider, "_login", fake_login)
        monkeypatch.setattr(provider, "_navegar_cotacao", fake_navegar)
        monkeypatch.setattr(provider, "_preencher_cotacao", fake_preencher)
        monkeypatch.setattr(provider, "_submeter_e_extrair", fake_submeter)

        result = await provider.coteir(
            origem="01001000",
            destino="02002000",
            peso=100,
            valor=1500.0,
            volumes=23,
            comprimento_cm=0,
            largura_cm=0,
            altura_cm=0,
            cnpj_destinatario="12345678000190",
            cubagens=[
                {"quantidade": 10, "comprimento_cm": 40, "largura_cm": 30, "altura_cm": 20, "peso_por_volume_kg": 4.3},
                {"quantidade": 13, "comprimento_cm": 50, "largura_cm": 35, "altura_cm": 25, "peso_por_volume_kg": 4.4},
            ],
            preencher_cep_origem=True,
        )

        assert result is None
        assert calls == ["init", "login", "navegar", "preencher", "submeter"]
        assert provider.last_error == "Rodonaves: reCAPTCHA não resolvido ou bloqueio antifraude impediu a cotação"

    asyncio.run(run())


def test_rodonaves_valid_quote_forces_coherent_login_status(monkeypatch):
    async def run():
        provider = _provider()
        calls = []

        async def fake_init_browser():
            calls.append("init")

        async def fake_login():
            calls.append("login")
            provider._set_login_status("login_ok", True)

        async def fake_navegar():
            calls.append("navegar")

        async def fake_preencher(**_kwargs):
            calls.append("preencher")

        async def fake_submeter():
            calls.append("submeter")
            provider._set_login_status("aguardando_captcha", True)
            provider._set_login_status("captcha_resolvido", True)
            provider._logged_in = False  # simula sincronização antiga incoerente após resultado válido
            provider._set_login_status("login_falhou", True)
            return Cotacao(transportadora="RODONAVES", prazo_dias=3, valor_frete=925.56)

        monkeypatch.setattr(provider, "_init_browser", fake_init_browser)
        monkeypatch.setattr(provider, "_login", fake_login)
        monkeypatch.setattr(provider, "_navegar_cotacao", fake_navegar)
        monkeypatch.setattr(provider, "_preencher_cotacao", fake_preencher)
        monkeypatch.setattr(provider, "_submeter_e_extrair", fake_submeter)

        result = await provider.coteir(
            origem="01001000",
            destino="02002000",
            peso=100,
            valor=1500.0,
            volumes=1,
            cnpj_destinatario="12345678000190",
            cubagens=[{"quantidade": 1, "comprimento_cm": 40, "largura_cm": 30, "altura_cm": 20}],
        )

        assert result is not None
        assert result.valor_frete == 925.56
        assert provider._logged_in is True
        assert provider.login_status["login_ok"] is True
        assert provider.login_status["aguardando_captcha"] is True
        assert provider.login_status["captcha_resolvido"] is True
        assert provider.login_status["cotacao_ok"] is True
        assert provider.login_status["login_falhou"] is False
        assert calls == ["init", "login", "navegar", "preencher", "submeter"]

    asyncio.run(run())


def test_rodonaves_window_context_manager_guarantees_hide_after_show():
    async def run():
        provider = _provider()
        events: list[str] = []

        async def fake_show():
            events.append("show")
            return True

        async def fake_hide():
            events.append("hide")

        provider._mostrar_janela = fake_show
        provider._ocultar_janela = fake_hide

        async with provider._janela_visivel_para_captcha() as shown:
            events.append("body")
            assert shown is True
            assert provider._window_visible_for_captcha is True

        assert events == ["show", "body", "hide"], (
            "hide deve ser chamado mesmo no caminho feliz do context manager"
        )
        assert provider._window_visible_for_captcha is False

    asyncio.run(run())


def test_rodonaves_window_context_manager_guarantees_hide_after_body_exception():
    async def run():
        provider = _provider()
        events: list[str] = []

        async def fake_show():
            events.append("show")
            return True

        async def fake_hide():
            events.append("hide")

        provider._mostrar_janela = fake_show
        provider._ocultar_janela = fake_hide

        with pytest.raises(RuntimeError, match="boom"):
            async with provider._janela_visivel_para_captcha():
                events.append("body-raise")
                raise RuntimeError("boom")

        assert events == ["show", "body-raise", "hide"], (
            "hide deve ser chamado mesmo quando o corpo do context manager levanta"
        )
        assert provider._window_visible_for_captcha is False

    asyncio.run(run())


def test_rodonaves_window_context_manager_keeps_hide_idempotent():
    async def run():
        provider = _provider()
        events: list[str] = []

        async def fake_show():
            events.append("show-raise")
            raise RuntimeError("CDP off-screen falhou")

        async def fake_hide():
            events.append("hide")

        provider._mostrar_janela = fake_show
        provider._ocultar_janela = fake_hide

        async with provider._janela_visivel_para_captcha() as shown:
            events.append("body")
            assert shown is False

        assert events == ["show-raise", "body", "hide"]
        assert provider._window_visible_for_captcha is False

    asyncio.run(run())


def test_rodonaves_show_window_brings_to_front_for_captcha(monkeypatch):
    async def run():
        provider = _provider()
        calls: list[tuple[str, object]] = []

        class Locator:
            @property
            def first(self):
                return self

            async def scroll_into_view_if_needed(self, **_kwargs):
                return None

            async def count(self):
                return 0

        class Page:
            def locator(self, _selector):
                return Locator()

            async def evaluate(self, _script):
                return {"w": 1920, "h": 1080}

            async def bring_to_front(self):
                raise AssertionError("Rodonaves não deve usar page.bring_to_front() — usa Win32 via posicionar_janela_por_pagina")

        class Cdp:
            async def send(self, method, payload=None):
                calls.append((method, payload))
                if method == "Browser.getWindowForTarget":
                    return {"windowId": 321}
                return {}

        class Context:
            async def new_cdp_session(self, _page):
                return Cdp()

        async def noop(*_args, **_kwargs):
            return None

        async def fake_position(*_args, **kwargs):
            calls.append(("position", kwargs.get("bring_to_front")))
            return True

        provider._page = Page()
        provider._context = Context()
        provider._sync_active_page = noop
        monkeypatch.setattr("fretio.providers.rodonaves_browser.ocultar_taskbar_por_pagina", noop)
        monkeypatch.setattr("fretio.providers.rodonaves_browser.posicionar_janela_por_pagina", fake_position)

        shown = await provider._mostrar_janela()

        assert shown is True
        assert ("position", True) in calls, (
            "A janela do CAPTCHA deve vir à frente (bring_to_front=True via Win32)"
        )

    asyncio.run(run())


class _BootstrapCdp:
    def __init__(self, *, window_id=999, get_bounds=None):
        self.window_id = window_id
        self.get_bounds = get_bounds or {"left": -32000, "top": -32000, "width": 1920, "height": 1080}
        self.sent: list[tuple[str, dict]] = []

    async def send(self, method, payload=None):
        self.sent.append((method, payload))
        if method == "Browser.getWindowForTarget":
            return {"windowId": self.window_id}
        if method == "Browser.getWindowBounds":
            return {"bounds": self.get_bounds}
        return {}


class _BootstrapPage:
    def __init__(self, cdp):
        self._cdp = cdp

    async def evaluate(self, _script):
        return None


class _BootstrapContext:
    def __init__(self, cdp):
        self._cdp = cdp

    async def new_cdp_session(self, _page):
        return self._cdp


def test_rodonaves_bootstrap_helper_returns_true_when_off_screen_confirmed(monkeypatch):
    provider = _provider()
    cdp = _BootstrapCdp()
    provider._context = _BootstrapContext(cdp)
    provider._page = _BootstrapPage(cdp)
    provider._sync_active_page = lambda: asyncio.sleep(0, result=None)
    monkeypatch.setattr("fretio.providers.rodonaves.ocultar_taskbar_por_pagina", lambda _p: asyncio.sleep(0, result=True))

    result = asyncio.run(provider._definir_janela_offscreen_inicial())

    assert result is True
    methods = [m for m, _ in cdp.sent]
    assert "Browser.setWindowBounds" in methods
    assert "Browser.getWindowBounds" in methods, (
        "helper deve LER os bounds de volta para confirmar off-screen"
    )


def test_rodonaves_bootstrap_helper_returns_false_when_windowid_missing(monkeypatch):
    provider = _provider()
    cdp = _BootstrapCdp(window_id=0)
    provider._context = _BootstrapContext(cdp)
    provider._page = _BootstrapPage(cdp)
    provider._sync_active_page = lambda: asyncio.sleep(0, result=None)
    monkeypatch.setattr("fretio.providers.rodonaves.ocultar_taskbar_por_pagina", lambda _p: asyncio.sleep(0, result=True))

    result = asyncio.run(provider._definir_janela_offscreen_inicial())

    assert result is False, "sem windowId não há como garantir off-screen"


def test_rodonaves_bootstrap_helper_returns_false_when_bounds_not_confirmed(monkeypatch):
    provider = _provider()
    cdp = _BootstrapCdp(get_bounds={"left": 0, "top": 0, "width": 1920, "height": 1080})
    provider._context = _BootstrapContext(cdp)
    provider._page = _BootstrapPage(cdp)
    provider._sync_active_page = lambda: asyncio.sleep(0, result=None)
    monkeypatch.setattr("fretio.providers.rodonaves.ocultar_taskbar_por_pagina", lambda _p: asyncio.sleep(0, result=True))

    result = asyncio.run(provider._definir_janela_offscreen_inicial())

    assert result is False, (
        "se o CDP reportar bounds >= 0 a janela está visível — "
        "o helper deve recusar e a inicialização deve falhar"
    )


def test_rodonaves_init_browser_inner_fails_fast_when_offscreen_not_confirmed(monkeypatch):
    async def run():
        provider = _provider()
        cdp = _BootstrapCdp(get_bounds={"left": 100, "top": 100, "width": 1920, "height": 1080})
        provider._context = _BootstrapContext(cdp)
        provider._page = _BootstrapPage(cdp)

        async def fake_launch_chrome(_self):
            provider._chrome_proc = None
            provider._browser = FakeBrowser(_BootstrapContext(cdp))
            provider._playwright = object()

        async def fake_find_free_port():
            return 0

        async def fake_subprocess_popen(*_args, **_kwargs):
            return None

        async def fake_sleep(_s):
            return None

        monkeypatch.setattr(
            "fretio.providers.rodonaves.RodonavesProvider._init_browser_inner",
            lambda *_a, **_k: asyncio.sleep(0),
            raising=False,
        )
        monkeypatch.setattr(provider, "_definir_janela_offscreen_inicial", lambda: asyncio.sleep(0, result=False))
        monkeypatch.setattr("fretio.providers.rodonaves._kill_proc", lambda _p: None)
        provider._sync_active_page = lambda: asyncio.sleep(0, result=None)
        monkeypatch.setattr("fretio.providers.rodonaves.ocultar_taskbar_por_pagina", lambda _p: asyncio.sleep(0, result=True))

        from fretio.providers import rodonaves as rod_module

        async def fake_inner(self):
            self._context = provider._context
            self._page = provider._page
            if not await self._definir_janela_offscreen_inicial():
                rod_module._kill_proc(self._chrome_proc)
                self._chrome_proc = None
                raise RuntimeError(
                    f"{self.nome}: bootstrap falhou — não foi possível confirmar a janela off-screen via CDP"
                )

        monkeypatch.setattr(rod_module.RodonavesProvider, "_init_browser_inner", fake_inner)

        with pytest.raises(RuntimeError, match="off-screen"):
            await rod_module.RodonavesProvider._init_browser_inner(provider)

    asyncio.run(run())


def test_rodonaves_preserves_same_page_context_browser_across_submit(monkeypatch):
    class Response:
        url = "https://cliente.rte.com.br/Quotation/Calculate"
        status = 200
        headers = {"content-type": "application/json"}

        async def json(self):
            return {"TotalFreight": 450.0, "Prazo": 4}

    class Mouse:
        async def move(self, *_args, **_kwargs):
            return None

        async def wheel(self, *_args, **_kwargs):
            return None

    class Locator:
        def __init__(self, role):
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

    class FrameLocator:
        def __init__(self):
            self.checkbox = Locator(role="checkbox")

        def get_by_role(self, *_args, **_kwargs):
            return self.checkbox

    class FakePage:
        def __init__(self):
            self.handlers = {}
            self.mouse = Mouse()
            self.calculate = Locator(role="calculate")
            self.captcha = Locator(role="captcha-frame")
            self.frame = FrameLocator()
            self.wait_n = 0

        def on(self, event, handler):
            self.handlers[event] = handler

        def remove_listener(self, event, handler):
            if self.handlers.get(event) is handler:
                self.handlers.pop(event, None)

        def locator(self, selector):
            if selector == "#calculateQuotationBtn":
                return self.calculate
            if selector.startswith("iframe["):
                return self.captcha
            return Locator(selector)

        def frame_locator(self, _selector):
            return self.frame

        async def wait_for_timeout(self, _ms):
            self.wait_n += 1
            if self.wait_n == 2:
                self.handlers["response"](Response())
                await asyncio.sleep(0)
            return None

        async def evaluate(self, script):
            if "g-recaptcha-response" in script:
                return "token"
            if "col-result" in script:
                return bool(self.wait_n >= 2)
            if "const fields = {};" in script:
                return {"captcha_token_len": 5}
            if "const texts = [];" in script:
                return []
            return None

    class FakeBrowser:
        def is_connected(self):
            return True

    class FakeContext:
        pass

    async def run():
        provider = _provider()
        page = FakePage()
        browser = FakeBrowser()
        context = FakeContext()
        provider._page = page
        provider._browser = browser
        provider._context = context
        provider._capture_safe_diagnostic_snapshot = lambda **_kwargs: asyncio.sleep(0, result={})

        page_id = id(page)
        browser_id = id(browser)
        context_id = id(context)

        result = await provider._submeter_e_extrair()

        assert result is not None
        assert result.valor_frete == 450.0
        assert provider._page is page, "page deve ser a mesma do início do submit"
        assert provider._browser is browser, "browser deve ser o mesmo do início do submit"
        assert provider._context is context, "context deve ser o mesmo do início do submit"
        assert id(provider._page) == page_id
        assert id(provider._browser) == browser_id
        assert id(provider._context) == context_id
        assert provider._window_visible_for_captcha is False, (
            "context manager deve garantir que a janela está oculta após o submit"
        )

    asyncio.run(run())


def test_rodonaves_coteir_error_does_not_recreate_page(monkeypatch):
    async def run():
        provider = _provider()
        page = FakePage(url="about:blank")
        context = FakeContext([page])
        provider._context = context
        provider._browser = FakeBrowser(context)
        provider._page = page

        calls: list[str] = []
        recreated_pages: list[int] = []

        original_context = type(context)

        class TrackedContext(original_context):
            def __init__(self_inner, pages):
                super().__init__(pages)

            async def new_page(self_inner):
                recreated_pages.append(1)
                raise AssertionError(
                    "coteir não pode chamar _context.new_page() em handler de erro"
                )

        provider._context = TrackedContext([page])

        async def fake_init_browser():
            calls.append("init")

        async def fake_login():
            calls.append("login")
            raise RuntimeError("falha simulada de login")

        async def fake_cleanup():
            calls.append("cleanup")

        async def fake_preencher(*_args, **_kwargs):
            calls.append("preencher")

        async def fake_submeter():
            calls.append("submeter")

        monkeypatch.setattr(provider, "_init_browser", fake_init_browser)
        monkeypatch.setattr(provider, "_login", fake_login)
        monkeypatch.setattr(provider, "_navegar_cotacao", lambda: calls.append("navegar"))
        monkeypatch.setattr(provider, "_preencher_cotacao", fake_preencher)
        monkeypatch.setattr(provider, "_submeter_e_extrair", fake_submeter)
        monkeypatch.setattr(provider, "cleanup", fake_cleanup)

        page_id = id(page)
        result = await provider.coteir(
            origem="01001000",
            destino="02002000",
            peso=1,
            valor=10.0,
            volumes=1,
            cnpj_destinatario="12345678000190",
            cubagens=[{"quantidade": 1, "comprimento_cm": 10, "largura_cm": 10, "altura_cm": 10}],
        )

        assert result is None
        assert calls == ["init", "login"], (
            "login falhou — não pode chegar a preencher/submeter; "
            "e principalmente não pode chamar cleanup() só por erro de login"
        )
        assert recreated_pages == [], (
            "nenhuma page pode ser recriada em qualquer caminho do handler de erro"
        )
        assert provider._page is page
        assert id(provider._page) == page_id


def test_rodonaves_error_handler_resets_page_when_closed_but_browser_alive(monkeypatch):
    """Bug 1: page fechada, browser vivo → handler de erro deve recriar só a page."""
    async def run():
        provider = _provider()
        closed_page = FakePage(url="about:blank", closed=True)
        context = FakeContext([closed_page])

        context_new_pages: list = []
        original_new_page = context.new_page

        async def tracked_new_page():
            p = await original_new_page()
            context_new_pages.append(p)
            return p

        context.new_page = tracked_new_page

        provider._context = context
        provider._browser = FakeBrowser(context, connected=True)
        provider._page = closed_page

        async def fake_init_browser():
            pass

        async def fake_login():
            raise RuntimeError("target page, context or browser has been closed")

        monkeypatch.setattr(provider, "_init_browser", fake_init_browser)
        monkeypatch.setattr(provider, "_login", fake_login)

        result = await provider.coteir(
            origem="01001000",
            destino="02002000",
            peso=1.0,
            valor=100.0,
            volumes=1,
            cnpj_destinatario="12345678000190",
            cubagens=[{"quantidade": 1, "comprimento_cm": 10, "largura_cm": 10, "altura_cm": 10}],
        )

        assert result is None
        assert len(context_new_pages) == 1, (
            "Uma nova page deve ser criada quando a page estava fechada mas o browser sobreviveu"
        )
        assert provider._page is context_new_pages[0], (
            "self._page deve apontar para a nova page após o reset"
        )
        assert provider._logged_in is False

    asyncio.run(run())


def test_rodonaves_error_handler_resets_page_on_frame_detached(monkeypatch):
    """Bug 2: frame was detached (transitório) → handler deve recriar só a page."""
    async def run():
        provider = _provider()
        page = FakePage(url="about:blank", closed=False)
        context = FakeContext([page])

        context_new_pages: list = []
        original_new_page = context.new_page

        async def tracked_new_page():
            p = await original_new_page()
            context_new_pages.append(p)
            return p

        context.new_page = tracked_new_page

        provider._context = context
        provider._browser = FakeBrowser(context, connected=True)
        provider._page = page

        async def fake_init_browser():
            pass

        async def fake_login():
            pass

        async def fake_navegar():
            pass

        async def fake_preencher(*_args, **_kwargs):
            raise RuntimeError("frame was detached")

        monkeypatch.setattr(provider, "_init_browser", fake_init_browser)
        monkeypatch.setattr(provider, "_login", fake_login)
        monkeypatch.setattr(provider, "_navegar_cotacao", fake_navegar)
        monkeypatch.setattr(provider, "_preencher_cotacao", fake_preencher)

        result = await provider.coteir(
            origem="01001000",
            destino="02002000",
            peso=1.0,
            valor=100.0,
            volumes=1,
            cnpj_destinatario="12345678000190",
            cubagens=[{"quantidade": 1, "comprimento_cm": 10, "largura_cm": 10, "altura_cm": 10}],
        )

        assert result is None
        assert len(context_new_pages) == 1, (
            "Uma nova page deve ser criada quando houve 'frame was detached' com browser vivo"
        )
        assert provider._page is context_new_pages[0]
        assert provider._logged_in is False

    asyncio.run(run())


def test_rodonaves_ignores_legacy_ssw_cotacao_url():
    """Config antiga apontando para o SSW legado deve cair no portal moderno."""
    provider = RodonavesProvider(
        dominio="RTE",
        usuario="12345678000190",
        senha="senha_teste",
        cnpj_pagador="12345678000190",
        cotacao_url="https://sistema.rte.com.br/bin/ssw1608",
        login_url="https://sistema.rte.com.br/bin/ssw0422",
    )

    assert provider.quotation_url == RodonavesProvider.PORTAL_URL
    assert provider.portal_entry_url == RodonavesProvider.LOGIN_URL


def test_rodonaves_honors_modern_portal_override():
    """URLs válidas do portal rodonaves.com.br continuam respeitadas."""
    provider = RodonavesProvider(
        dominio="RTE",
        usuario="12345678000190",
        senha="senha_teste",
        cnpj_pagador="12345678000190",
        cotacao_url="https://rodonaves.com.br/cotacao/nova",
        login_url="https://rodonaves.com.br/portaldocliente/acesso",
    )

    assert provider.quotation_url == "https://rodonaves.com.br/cotacao/nova"
    assert provider.portal_entry_url == "https://rodonaves.com.br/portaldocliente/acesso"


def test_rodonaves_uses_defaults_when_urls_empty():
    provider = RodonavesProvider(
        dominio="RTE",
        usuario="12345678000190",
        senha="senha_teste",
        cnpj_pagador="12345678000190",
    )

    assert provider.quotation_url == RodonavesProvider.PORTAL_URL
    assert provider.portal_entry_url == RodonavesProvider.LOGIN_URL
