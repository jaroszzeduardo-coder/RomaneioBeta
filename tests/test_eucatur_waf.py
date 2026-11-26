"""Eucatur no TMS: URL nova e verificação de robô automática, sem clique humano."""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "fretio" / "src"))

from fretio.providers.eucatur import EucaturProvider
import fretio.providers.eucatur as eucatur_mod


def _provider(*, headless: bool = True) -> EucaturProvider:
    return EucaturProvider(
        dominio="EUC",
        usuario="usr",
        senha="secret",
        cnpj_pagador="00000000000191",
        headless=headless,
    )


def test_eucatur_usa_portal_tms():
    assert EucaturProvider.LOGIN_URL == "https://tms.ssw.inf.br/bin/ssw0422"
    assert EucaturProvider.COTACAO_URL == "https://tms.ssw.inf.br/bin/ssw1608"


def test_chrome_da_verificacao_nao_nasce_headless_nem_automatizado():
    offscreen = EucaturProvider._chrome_launch_args(
        r"C:\Chrome\chrome.exe",
        9222,
        r"C:\perfil",
        EucaturProvider.LOGIN_URL,
        offscreen=True,
    )
    visivel = EucaturProvider._chrome_launch_args(
        r"C:\Chrome\chrome.exe",
        9222,
        r"C:\perfil",
        EucaturProvider.LOGIN_URL,
        offscreen=False,
    )
    joined = " ".join(offscreen + visivel).lower()
    assert "--headless" not in joined
    assert "--enable-automation" not in joined
    assert "--disable-blink-features=automationcontrolled" not in joined
    assert "--window-position=-3000,-3000" in offscreen
    assert "--window-position=200,80" in visivel
    assert offscreen[-1] == EucaturProvider.LOGIN_URL
    assert "--do-not-de-elevate" in offscreen


def test_verificacao_de_robo_conclui_sozinha(monkeypatch):
    provider = _provider()
    estados = iter([
        {"ready": False, "challenge": True, "url": EucaturProvider.LOGIN_URL},
        {"ready": True, "challenge": False, "url": EucaturProvider.LOGIN_URL},
    ])
    vistos = []

    async def fake_eval(port, expression, host_hint=""):
        vistos.append((port, host_hint, "input[name=\"f1\"]" in expression))
        return next(estados)

    async def instant_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(eucatur_mod.asyncio, "sleep", instant_sleep)

    ok = asyncio.run(
        provider._aguardar_portal_liberado(
            9222,
            timeout_s=5,
            pausa_inicial=0,
            cdp_eval=fake_eval,
        )
    )

    assert ok is True
    assert vistos[0][0] == 9222
    assert vistos[0][1] == "ssw.inf.br"


def test_verificacao_de_robo_nao_abre_janela_para_humano(monkeypatch):
    provider = _provider()

    async def preso(*_args, **_kwargs):
        return {"ready": False, "challenge": True, "url": EucaturProvider.LOGIN_URL}

    async def instant_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(eucatur_mod.asyncio, "sleep", instant_sleep)

    ok = asyncio.run(
        provider._aguardar_portal_liberado(
            9222,
            timeout_s=1.2,
            pausa_inicial=0,
            cdp_eval=preso,
        )
    )

    assert ok is False


def test_desafio_no_meio_do_login_solta_o_playwright_e_nao_pede_clique(monkeypatch):
    provider = _provider()
    provider._debug_port = 9333
    chamadas = []

    class Page:
        async def evaluate(self, _script):
            return True

    class Browser:
        async def detach(self):
            chamadas.append("detach")
            return object(), r"C:\perfil-eucatur", 9333

    async def falso_aguardar(port, **kwargs):
        chamadas.append(("aguardar", port, kwargs.get("pausa_inicial")))
        return True

    async def falso_conectar():
        chamadas.append("conectar")
        provider._page = Page()

    provider._page = Page()
    provider._browser = Browser()
    monkeypatch.setattr(provider, "_aguardar_portal_liberado", falso_aguardar)
    monkeypatch.setattr(provider, "_conectar_playwright", falso_conectar)

    asyncio.run(provider._garantir_sem_desafio())

    assert chamadas == ["detach", ("aguardar", 9333, 0.5), "conectar"]


def test_cleanup_encerra_chrome_orfao_da_verificacao(monkeypatch, tmp_path):
    provider = _provider()
    profile = tmp_path / "perfil"
    profile.mkdir()
    (profile / "marker.txt").write_text("x", encoding="utf-8")
    proc = object()
    killed = []

    def fake_kill(target):
        killed.append(target)

    monkeypatch.setattr("fretio.browser.process_guard._kill_proc", fake_kill)
    provider._chrome_proc = proc
    provider._profile_dir = str(profile)
    provider._debug_port = 9444

    asyncio.run(provider.cleanup())

    assert killed == [proc]
    assert not profile.exists()
    assert provider._chrome_proc is None
    assert provider._profile_dir is None
    assert provider._browser is None
    assert provider._page is None


class _Ancora:
    def __init__(self, *, presente: bool, marcado: str = "false", token_depois: bool = False):
        self.presente = presente
        self.marcado = marcado
        self.token_depois = token_depois
        self.cliques = 0

    async def count(self):
        return 1 if self.presente else 0

    async def get_attribute(self, nome):
        assert nome == "aria-checked"
        return self.marcado

    async def click(self, timeout=8000):
        self.cliques += 1
        if self.token_depois:
            self.marcado = "true"


class _PaginaRecaptcha:
    def __init__(self, ancora: _Ancora, token_inicial: str = ""):
        self.ancora = ancora
        self.token = token_inicial
        self.esperas = 0
        self.seletor_iframe = ""

    def frame_locator(self, seletor):
        self.seletor_iframe = seletor
        return self

    def locator(self, seletor):
        assert seletor == "#recaptcha-anchor"
        return self.ancora

    async def evaluate(self, _script):
        if self.ancora.cliques and self.ancora.token_depois:
            self.token = "t" * 40
        return self.token

    async def wait_for_timeout(self, _ms):
        self.esperas += 1


def test_recaptcha_ausente_segue_o_login():
    provider = _provider()
    provider._page = _PaginaRecaptcha(_Ancora(presente=False))

    assert asyncio.run(provider._aguardar_recaptcha()) is True
    assert provider._page.ancora.cliques == 0


def test_recaptcha_do_login_e_marcado_e_espera_o_token():
    provider = _provider()
    provider._page = _PaginaRecaptcha(_Ancora(presente=True, token_depois=True))

    ok = asyncio.run(provider._aguardar_recaptcha())

    assert ok is True
    assert provider._page.ancora.cliques == 1
    assert "api2/anchor" in provider._page.seletor_iframe


def test_recaptcha_ja_marcado_nao_clica_de_novo():
    provider = _provider()
    provider._page = _PaginaRecaptcha(
        _Ancora(presente=True, marcado="true"),
        token_inicial="t" * 40,
    )

    ok = asyncio.run(provider._aguardar_recaptcha())

    assert ok is True
    assert provider._page.ancora.cliques == 0
    assert provider._page.esperas == 0


def test_recaptcha_sem_token_falha_sozinho(monkeypatch):
    provider = _provider()
    provider._RECAPTCHA_TIMEOUT_S = 0.5
    provider._page = _PaginaRecaptcha(_Ancora(presente=True, token_depois=False))

    async def instant_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(provider._page, "wait_for_timeout", instant_sleep)

    assert asyncio.run(provider._aguardar_recaptcha()) is False
