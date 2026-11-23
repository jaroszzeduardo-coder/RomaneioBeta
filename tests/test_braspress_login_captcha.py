import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app" / "fretio" / "src"))

from fretio.providers.braspress_playwright import BraspressPlaywrightProvider


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    async def wait_for(self, **kwargs):
        return None

    async def fill(self, value):
        self.page.filled[self.selector] = value

    async def click(self):
        self.page.clicks.append(self.selector)
        if 'value="Acessar"' in self.selector:
            self.page.body = "Menu Sair"

    async def inner_text(self):
        if self.selector == ".login-error":
            return self.page.login_error
        return ""

    async def count(self):
        return 0

    async def scroll_into_view_if_needed(self):
        return None

    @property
    def first(self):
        return self


class FakePage:
    def __init__(self):
        self.present = False
        self.token = ""
        self.body = "ENTRAR"
        self.login_error = ""
        self.clicks = []
        self.filled = {}
        self.gotos = 0
        self.closed = False

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def goto(self, url, **kwargs):
        self.gotos += 1

    async def wait_for_timeout(self, ms):
        return None

    async def evaluate(self, script):
        if "cf-turnstile" in script or "turnstile" in script:
            return {"present": self.present, "token": self.token}
        return ""

    async def inner_text(self, selector):
        return self.body

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True

    async def bring_to_front(self):
        return None


def _provider(page, *, headless=True):
    provider = BraspressPlaywrightProvider(cnpj="12.345.678/0001-90", senha="segredo-local", headless=headless)
    provider._page = page
    provider._TURNSTILE_AUTO_S = 1
    provider._TURNSTILE_USUARIO_S = 1

    async def mostrar():
        provider._janela_captcha_aberta = True

    async def ocultar():
        provider._janela_captcha_aberta = False

    provider._mostrar_janela_captcha = mostrar
    provider._ocultar_janela_captcha = ocultar
    return provider


def test_login_sem_captcha_clica_acessar():
    async def run():
        page = FakePage()
        provider = _provider(page)
        assert await provider._login() is True
        assert page.clicks == ['input[type="submit"][value="Acessar"]']
        assert provider._logged_in is True
        assert provider._captcha_visivel is False
        assert "segredo-local" not in (provider.last_error or "")

    asyncio.run(run())


def test_login_com_token_automatico_nao_abre_janela():
    async def run():
        page = FakePage()
        page.present = True
        page.token = "token-do-widget"
        provider = _provider(page)
        assert await provider._login() is True
        assert provider._janela_captcha_aberta is False
        assert page.clicks == ['input[type="submit"][value="Acessar"]']

    asyncio.run(run())


def test_login_headless_abre_janela_e_preserva_headless():
    async def run():
        page = FakePage()
        page.present = True
        provider = _provider(page, headless=True)
        reinicios = []

        async def init_browser():
            reinicios.append(provider._captcha_visivel)
            provider._page = page
            provider._browser = None
            page.token = "token-depois-da-janela"
            page.closed = False

        provider._init_browser = init_browser
        assert await provider._login() is True
        assert reinicios == [True]
        assert provider.headless is True
        assert provider._captcha_visivel is True
        assert page.clicks == ['input[type="submit"][value="Acessar"]']
        assert provider._janela_captcha_aberta is False

    asyncio.run(run())


def test_login_captcha_nao_resolvido_nao_envia_o_formulario():
    async def run():
        page = FakePage()
        page.present = True
        provider = _provider(page, headless=False)
        assert await provider._login() is False
        assert page.clicks == []
        assert "captcha da Braspress não resolvido" in (provider.last_error or "")
        assert "segredo-local" not in provider.last_error
        assert provider._logged_in is False

    asyncio.run(run())
