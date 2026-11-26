"""RAM baixa serializa as cotações sem mudar o preenchimento dos portais."""

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "fretio" / "src"))

import cotacao_transportadoras as ct
from cotacao import deps
from cotacao import memory_budget
from cotacao import orchestrator as cotacao_orchestrator
from cotacao.common import QuoteResponse
from cotacao.memory_budget import LOW_MEMORY_USER_MESSAGE
from cotacao.session_manager import TransportadoraSession
from fretio.browser import launcher


class _Status:
    def __init__(self, total_mb, free_mb):
        self.ullTotalPhys = total_mb * 1024 * 1024
        self.ullAvailPhys = free_mb * 1024 * 1024


def _fixar_ram(monkeypatch, total_mb, free_mb):
    status = None if total_mb is None else _Status(total_mb, free_mb if free_mb is not None else 0)

    def _status():
        return status

    monkeypatch.setattr(memory_budget, "_memory_status", _status)


def test_dois_gb_abre_um_chrome(monkeypatch):
    _fixar_ram(monkeypatch, 2048, 700)
    assert memory_budget.single_chrome_mode() is True
    assert memory_budget.chrome_concurrency_limit(5) == 1
    assert "--renderer-process-limit=1" in memory_budget.lean_chrome_args()


def test_quatro_gb_limita_a_dois_sem_passar_da_configuracao(monkeypatch):
    _fixar_ram(monkeypatch, 4096, 2000)
    assert memory_budget.single_chrome_mode() is False
    assert memory_budget.chrome_concurrency_limit(5) == 2
    assert memory_budget.chrome_concurrency_limit(1) == 1
    assert memory_budget.lean_chrome_args() == []


def test_ram_alta_mantem_o_paralelo_configurado(monkeypatch):
    _fixar_ram(monkeypatch, 16384, 8000)
    assert memory_budget.single_chrome_mode() is False
    assert memory_budget.chrome_concurrency_limit(3) == 3
    assert memory_budget.chrome_concurrency_limit(7) == 7


def test_ram_livre_abaixo_de_600_mb_abre_um_chrome(monkeypatch):
    _fixar_ram(monkeypatch, 16384, 400)
    assert memory_budget.single_chrome_mode() is True
    assert memory_budget.chrome_concurrency_limit(4) == 1


def test_falha_ao_ler_ram_nao_reduz_pc_grande(monkeypatch):
    monkeypatch.setattr(memory_budget, "_memory_status", lambda: None)
    assert memory_budget.physical_ram_mb() is None
    assert memory_budget.available_ram_mb() is None
    assert memory_budget.single_chrome_mode() is False
    assert memory_budget.chrome_concurrency_limit(3) == 3


def test_flags_leves_ficam_antes_da_url_e_substituem_o_limite_de_renderer(monkeypatch):
    _fixar_ram(monkeypatch, 2048, 400)
    args = memory_budget.apply_low_memory_chrome_args(
        ["chrome", "--renderer-process-limit=2", "--disable-gpu", "https://portal.example/login"]
    )
    assert args.count("--renderer-process-limit=1") == 1
    assert "--renderer-process-limit=2" not in args
    assert args[-1] == "https://portal.example/login"
    assert "--disable-extensions" in args


def test_flags_leves_somem_quando_ha_ram(monkeypatch):
    _fixar_ram(monkeypatch, 16384, 8000)
    original = ["chrome", "--renderer-process-limit=2"]
    assert memory_budget.apply_low_memory_chrome_args(original) == original


def test_leitura_real_de_ram_no_windows():
    total = memory_budget.physical_ram_mb()
    livre = memory_budget.available_ram_mb()
    if sys.platform != "win32":
        assert total is None
        assert livre is None
        return
    assert isinstance(total, int) and total > 0
    assert isinstance(livre, int) and livre >= 0


def test_launcher_aplica_flags_leves_so_com_pouca_ram(monkeypatch, tmp_path):
    popen_calls = []

    class FakeProc:
        pid = 321
        returncode = None

        def poll(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeChromium:
        async def connect_over_cdp(self, endpoint):
            return types.SimpleNamespace(close=self.close_browser)

        async def close_browser(self):
            return None

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()

    monkeypatch.setattr(launcher, "find_chrome", lambda: "/fake/chrome")
    monkeypatch.setattr(launcher, "_find_free_port", lambda: 9333)
    monkeypatch.setattr(launcher.tempfile, "mkdtemp", lambda prefix: str(tmp_path / "profile"))
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda args, **kwargs: popen_calls.append(args) or FakeProc())
    monkeypatch.setattr(launcher, "_register_owned_proc", lambda proc, source: None)
    monkeypatch.setattr(launcher, "_kill_proc", lambda proc: None)
    monkeypatch.setattr(launcher.socket, "create_connection", lambda *args, **kwargs: FakeConnection())
    monkeypatch.setattr(launcher.asyncio, "sleep", lambda delay: _done())

    _fixar_ram(monkeypatch, 2048, 400)
    asyncio.run(launcher.launch_browser_resilient(FakePlaywright(), headless=True))
    baixa = popen_calls[-1]
    assert baixa.count("--renderer-process-limit=1") == 1
    assert "--renderer-process-limit=2" not in baixa
    assert "--disable-extensions" in baixa

    popen_calls.clear()
    _fixar_ram(monkeypatch, 16384, 8000)
    asyncio.run(launcher.launch_browser_resilient(FakePlaywright(), headless=True))
    alta = popen_calls[-1]
    assert "--renderer-process-limit=2" in alta
    assert "--disable-extensions" not in alta


async def _done():
    return None


def test_prelogin_nao_abre_chrome_com_pouca_ram(monkeypatch):
    created = []
    messages = []

    class FakeFactory:
        def __init__(self, config):
            pass

        def preload(self):
            pass

        def create(self, nome, **kwargs):
            created.append(nome)
            return object()

    monkeypatch.setattr(memory_budget, "physical_ram_mb", lambda: 2048)
    monkeypatch.setattr(memory_budget, "available_ram_mb", lambda: 400)
    monkeypatch.setattr("cotacao.session_manager._carregar_config", lambda config_path=None: {})
    monkeypatch.setattr("cotacao.deps.ProviderFactory", FakeFactory)
    monkeypatch.setattr("cotacao.session_manager._kill_orphan_Fretio_chromes", lambda: None)
    monkeypatch.setattr("fretio.providers.base.find_chrome", lambda: r"C:\chrome.exe")

    session = TransportadoraSession()

    async def run():
        await session.inicializar(callback=messages.append)
        await session.inicializar(callback=messages.append)
        pronto = session.pronto
        await session.cleanup()
        return pronto

    pronto = asyncio.run(run())

    assert created == []
    assert pronto is True
    assert messages == [LOW_MEMORY_USER_MESSAGE]


def test_prelogin_preguicoso_espera_a_cotacao_com_pouca_ram(monkeypatch):
    monkeypatch.setattr("cotacao.session_manager._carregar_config", lambda config_path=None: {})
    monkeypatch.setattr(memory_budget, "physical_ram_mb", lambda: 2048)
    monkeypatch.setattr(memory_budget, "available_ram_mb", lambda: 400)
    session = TransportadoraSession()
    chamadas = []

    class Provider:
        _logged_in = False

        async def pre_login(self):
            chamadas.append("pre_login")
            return True

    asyncio.run(session._executar_lazy_prelogin("trd", Provider()))
    assert chamadas == []


class _TrackingProvider:
    def __init__(self, nome):
        self.nome = nome
        self.headless = True
        self.last_error = None
        self._passo_atual = "inicio"
        self._logged_in = False

    async def cotar(self, request):
        _ACTIVE.add(self.nome)
        _PEAKS.append(len(_ACTIVE))
        try:
            await asyncio.sleep(0.05)
        finally:
            _ACTIVE.discard(self.nome)
        return QuoteResponse.ok(provider=self.nome, valor_frete=10.0, prazo_dias=2)

    async def cleanup(self):
        _CLEANUPS.append((self.nome, len(_ACTIVE)))


_ACTIVE: set[str] = set()
_PEAKS: list[int] = []
_CLEANUPS: list[tuple[str, int]] = []


class _MemSessao:
    def __init__(self, factory):
        self.factory = factory
        self.providers: dict[str, _TrackingProvider] = {}

    async def obter_provider(self, nome):
        return self.providers.get(nome)

    async def assegurar_provider(self, nome, factory):
        provider = self.providers.get(nome)
        if provider is None:
            provider = factory()
            self.providers[nome] = provider
        return provider

    async def fechar_provider(self, nome, expected=None, **kwargs):
        atual = self.providers.get(nome)
        if atual is None:
            return False
        if expected is not None and atual is not expected:
            return False
        self.providers.pop(nome, None)
        await atual.cleanup()
        return True

    async def fechar_providers_exceto(self, nomes, contexto=""):
        manter = {str(nome).strip().lower() for nome in nomes}
        for nome in list(self.providers):
            if nome not in manter:
                await self.fechar_provider(nome)

    def record_quote_success(self, nome):
        return None

    def record_quote_failure(self, nome):
        return None


def _dados():
    return {
        "destino_cep": "90010123",
        "uf_destino": "RS",
        "cnpj_destinatario": "12345678000190",
        "peso": 3.3,
        "valor": 150.0,
        "volumes": 1,
        "cubagem_m3": 0.044,
        "cubagens": [
            {"quantidade": 1, "comprimento_cm": 45, "largura_cm": 31, "altura_cm": 31}
        ],
        "descricoes_itens": [],
    }


def _factory(habilitadas):
    class FakeFactory:
        def __init__(self, config):
            self.config = config

        def is_available(self, nome):
            return nome in habilitadas

        def get_provider_config(self, nome):
            if nome not in habilitadas:
                return {"habilitado": False}
            if nome == "trd":
                return {
                    "habilitado": True,
                    "email": "a@b.com",
                    "senha": "pw",
                    "ufs_atendidas": ["RS"],
                    "headless": True,
                }
            if nome == "braspress":
                return {
                    "habilitado": True,
                    "cnpj": "12345678000190",
                    "senha": "pw",
                    "ufs_atendidas": ["RS"],
                    "headless": True,
                }
            if nome == "alfa":
                return {
                    "habilitado": True,
                    "login": "user",
                    "senha": "pw",
                    "cnpj_remetente": "12345678000190",
                    "ufs_atendidas": ["RS"],
                    "headless": True,
                }
            return {"habilitado": False}

        def create(self, nome, **kwargs):
            provider = _TrackingProvider(nome.upper())
            provider.headless = bool(kwargs.get("headless", True))
            return provider

    return FakeFactory


def _rodar(monkeypatch, *, total_mb, free_mb, habilitadas, max_paralelo):
    _ACTIVE.clear()
    _PEAKS.clear()
    _CLEANUPS.clear()
    _fixar_ram(monkeypatch, total_mb, free_mb)
    monkeypatch.setattr(deps, "carrier_enabled_or_message", lambda carrier: (True, ""))
    monkeypatch.setattr(deps, "ProviderFactory", _factory(habilitadas))
    monkeypatch.setattr(cotacao_orchestrator, "report_provider_error", lambda *args, **kwargs: None)
    sessao = _MemSessao(_factory(habilitadas))
    resultados = asyncio.run(
        ct._executar_cotacoes_com_dados(
            config={
                "fretio": {"max_paralelo": max_paralelo},
                "romaneio": {"cep_origem": "90000000"},
                "transportadoras": {nome: {"habilitado": True} for nome in habilitadas},
            },
            dados=_dados(),
            cep_origem="90000000",
            sessao=sessao,
        )
    )
    nomes = {r.transportadora for r in resultados if r.status == "ok"}
    return nomes


def test_pouca_ram_nao_cota_duas_transportadoras_juntas(monkeypatch):
    nomes = _rodar(
        monkeypatch,
        total_mb=2048,
        free_mb=400,
        habilitadas={"trd", "alfa"},
        max_paralelo=3,
    )
    assert nomes == {"TRD", "ALFA"}
    assert _PEAKS
    assert max(_PEAKS) == 1
    assert any(nome == "TRD" and ativos == 0 for nome, ativos in _CLEANUPS)
    assert any(nome == "ALFA" and ativos == 0 for nome, ativos in _CLEANUPS)


def test_ram_alta_mantem_cotacoes_paralelas(monkeypatch):
    nomes = _rodar(
        monkeypatch,
        total_mb=16384,
        free_mb=8000,
        habilitadas={"trd", "braspress"},
        max_paralelo=3,
    )
    assert nomes == {"TRD", "BRASPRESS"}
    assert max(_PEAKS) == 2
    assert _CLEANUPS == []
