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


def test_login_sem_token_resolve_no_chrome_limpo_sem_janela():
    async def run():
        page = FakePage()
        page.present = True
        provider = _provider(page, headless=True)
        chamadas = []

        async def automatico():
            chamadas.append("chrome_limpo")
            provider._captcha_ja_enviado = True
            provider._page = page
            page.body = "Menu Sair"
            page.closed = False
            return True

        provider._resolver_turnstile_no_chrome_limpo = automatico
        assert await provider._login() is True
        assert chamadas == ["chrome_limpo"]
        assert provider.headless is True
        assert provider._captcha_visivel is False
        assert provider._janela_captcha_aberta is False
        assert page.clicks == []
        assert provider._logged_in is True

    asyncio.run(run())


def test_login_captcha_nao_resolvido_nao_envia_o_formulario():
    async def run():
        page = FakePage()
        page.present = True
        provider = _provider(page, headless=False)

        async def automatico():
            provider.last_error = "Login falhou: captcha da Braspress não resolvido automaticamente."
            return False

        provider._resolver_turnstile_no_chrome_limpo = automatico
        assert await provider._login() is False
        assert page.clicks == []
        assert "captcha da Braspress não resolvido" in (provider.last_error or "")
        assert "segredo-local" not in provider.last_error
        assert provider._logged_in is False
        assert provider._janela_captcha_aberta is False

    asyncio.run(run())


def test_turnstile_automatico_clica_quando_o_token_aparece(monkeypatch):
    import fretio.providers.braspress_playwright as braspress_mod
    import fretio.browser.cdp_raw as cdp_raw

    provider = BraspressPlaywrightProvider(cnpj="12345678000190", senha="segredo-local", headless=True)
    provider._debug_port = 9
    provider._TURNSTILE_AUTO_CHROME_S = 4
    titulos = iter(["Login", "Fretio CAPTCHA_OK", "Fretio LOGADO"])
    cliques = []

    async def fake_eval(port, expression, host_hint=""):
        if "Acessar" in expression:
            cliques.append(port)
            return True
        if "sair" in expression:
            return True
        return True

    def fake_list(_port):
        return [{
            "type": "page",
            "url": "https://www.braspress.com.br/w/cliente/view",
            "title": next(titulos, "Fretio LOGADO"),
        }]

    def fake_pick(pages, host_hint=""):
        return pages[0]

    async def instant(*_args, **_kwargs):
        return None

    monkeypatch.setattr(braspress_mod.asyncio, "sleep", instant)
    monkeypatch.setattr(cdp_raw, "cdp_eval", fake_eval)
    monkeypatch.setattr(cdp_raw, "list_debug_targets", fake_list)
    monkeypatch.setattr(cdp_raw, "pick_page_target", fake_pick)

    assert asyncio.run(provider._aguardar_captcha_automatico()) is True
    assert cliques == [9]
    assert provider._janela_captcha_aberta is False


def test_chrome_do_turnstile_abre_fora_da_tela(monkeypatch):
    import fretio.browser.launcher as launcher

    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = list(args)

        class _Proc:
            pass

        return _Proc()

    monkeypatch.setattr(launcher, "find_chrome", lambda: r"C:\Chrome\chrome.exe")
    monkeypatch.setattr(launcher, "_find_free_port", lambda: 9)
    monkeypatch.setattr(launcher.tempfile, "mkdtemp", lambda prefix="": r"C:\perfil")
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launcher, "browser_shutdown_requested", lambda: False)

    launcher.launch_human_chrome("https://www.braspress.com.br/w/cliente/view", offscreen=True)

    args = captured["args"]
    joined = " ".join(args).lower()
    assert "--window-position=-3000,-3000" in args
    assert "--window-size=900,760" in args
    assert "--headless" not in joined
    assert "--enable-automation" not in joined
    assert args[-1] == "https://www.braspress.com.br/w/cliente/view"
