import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


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

from fretio.providers.translovato import TranslovatoProvider


EXPECTED = "03770000000175"
WRONG = "03770000000101"


class FakeReceiverLocator:
    def __init__(self, page):
        self.page = page
        self.actions = []

    @property
    def first(self):
        return self

    async def wait_for(self, **kwargs):
        self.actions.append(("wait_for", kwargs))

    async def fill(self, value):
        self.actions.append(("fill", value))
        self.page.receiver_value = value
        if value == "":
            self.page.after_clear = True

    async def dispatch_event(self, event):
        self.actions.append(("event", event))
        if event == "blur" and self.page.receiver_value == EXPECTED:
            if self.page.always_wrong or not self.page.after_clear:
                self.page.receiver_value = WRONG

    async def press(self, key):
        self.actions.append(("press", key))

    async def input_value(self, timeout=1500):
        return self.page.receiver_value


class FakeLocatorCollection:
    def __init__(self, locator):
        self._locator = locator

    @property
    def first(self):
        return self._locator


class FakePage:
    def __init__(self, *, always_wrong=False):
        self.receiver_value = ""
        self.after_clear = False
        self.always_wrong = always_wrong
        self.receiver_locator = FakeReceiverLocator(self)
        self.waits = []
        self.evaluations = []

    def locator(self, selector):
        assert selector == TranslovatoProvider.RECEIVER_CNPJ_SELECTOR
        return FakeLocatorCollection(self.receiver_locator)

    async def wait_for_timeout(self, ms):
        self.waits.append(ms)

    async def evaluate(self, script, *args):
        self.evaluations.append((script, args))
        return False


async def _empty_city_uf():
    return "", "", ""


async def _empty_zip():
    return ""


def _provider(page):
    provider = TranslovatoProvider(cnpj="12345678000190", usuario="usuario", senha="senha")
    provider._page = page
    provider._read_delivery_city_uf = _empty_city_uf
    provider._read_delivery_zip_digits = _empty_zip
    return provider


def test_translovato_login_submits_again_when_first_click_does_not_log_in():
    from unittest.mock import AsyncMock, MagicMock

    async def run():
        document = MagicMock(fill=AsyncMock(), press_sequentially=AsyncMock(), press=AsyncMock())
        user = MagicMock(fill=AsyncMock())
        password = MagicMock(fill=AsyncMock(), press=AsyncMock())
        button = MagicMock(click=AsyncMock(), is_visible=AsyncMock(return_value=True))
        form = MagicMock()
        form.locator.side_effect = lambda selector: {
            TranslovatoProvider.LOGIN_CNPJ_SELECTOR: document,
            TranslovatoProvider.LOGIN_USER_SELECTOR: user,
            TranslovatoProvider.LOGIN_PASSWORD_SELECTOR: password,
        }[selector]
        form.get_by_role.return_value = button
        page = MagicMock(goto=AsyncMock())
        page.locator.side_effect = lambda selector: form if selector == "#login-portal" else password
        provider = _provider(page)
        provider._aceitar_cookies = AsyncMock()
        provider._wait_for_logged_in_state = AsyncMock(side_effect=[False, True])

        assert await provider._login() is True
        assert button.click.await_count == 2
        document.press_sequentially.assert_awaited_once_with("12345678000190", delay=50)
        user.fill.assert_awaited_once_with("usuario")
        password.fill.assert_awaited_once_with("senha")
        assert provider._logged_in is True

    asyncio.run(run())


def test_translovato_rewrites_receiver_cnpj_after_blur_divergence():
    async def run():
        page = FakePage(always_wrong=False)
        provider = _provider(page)

        await provider._preencher_cnpj_destinatario(EXPECTED)

        assert page.receiver_value == EXPECTED
        assert ("fill", "") in page.receiver_locator.actions
        assert page.receiver_locator.actions.count(("event", "input")) >= 2
        assert page.receiver_locator.actions.count(("event", "change")) >= 2
        assert page.receiver_locator.actions.count(("event", "blur")) >= 2
        assert page.receiver_locator.actions[-1] == ("press", "Tab")
        assert provider._last_receiver_diagnostic["expected_masked"] == "0377***75"
        assert provider._last_receiver_diagnostic["found_masked"] == "0377***01"

    asyncio.run(run())


def test_translovato_blocks_quote_when_fallback_keeps_divergent_cnpj():
    async def run():
        page = FakePage(always_wrong=True)
        provider = _provider(page)

        try:
            await provider._preencher_cnpj_destinatario(EXPECTED)
        except ValueError as exc:
            message = str(exc)
        else:
            raise AssertionError("CNPJ divergente deveria bloquear a cotação")

        assert "portal alterou o CNPJ destinatário após blur" in message
        assert "cotação bloqueada" in message
        assert "0377***75" in message
        assert "0377***01" in message
        assert EXPECTED not in message
        assert WRONG not in message
        assert page.receiver_value == WRONG

    asyncio.run(run())


def test_translovato_blocks_quote_with_wrong_delivery_zip():
    from unittest.mock import AsyncMock, MagicMock
    import pytest

    async def run():
        page = MagicMock(wait_for_timeout=AsyncMock())
        provider = _provider(page)
        provider._read_delivery_zip_digits = AsyncMock(return_value="90010000")
        provider._read_delivery_city_uf = AsyncMock(return_value=("", "", ""))
        provider._validate_receiver_cnpj = AsyncMock()
        provider._read_receiver_cnpj_digits = AsyncMock(return_value=EXPECTED)
        with pytest.raises(ValueError, match="CEP de entrega diverge"):
            await provider._aguardar_e_validar_autopreenchimento_destino(expected_receiver=EXPECTED, expected_cep="90020000")

    asyncio.run(run())
