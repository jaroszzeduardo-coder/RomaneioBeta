"""Gerenciamento de sessões persistentes dos providers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import asyncio
import os
import sys
import time

from fretio.providers.factory import slowness_priority_for_provider

from . import deps
from .common import (
    KNOWN_CARRIERS,
    MODO_FOCO_TRANSPORTADORA,
    _log_diag,
    _logger,
    _remote_disabled_results_for_config,
    _trd_headless_config_value,
)
from .config import _carregar_config
from .validation import _uf_atendida
from .error_context import is_expected_prelogin_failure, report_provider_error
from .circuit_breaker import ProviderCircuitBreaker

CHROME_DOWNLOAD_URL = "https://www.google.com/chrome/"
CHROME_MISSING_USER_MESSAGE = (
    "O Fretio precisa do Google Chrome instalado para fazer cotações e rastreios automáticos. "
    "Instale o Chrome e abra o Fretio novamente."
)


def _is_chrome_missing_error(exc: BaseException | str) -> bool:
    text = str(exc)
    return "Google Chrome" in text or "chrome nao encontrado" in text.lower() or "chrome não encontrado" in text.lower()


def _kill_orphan_Fretio_chromes() -> None:
    """Mata processos Chrome órfãos de sessões anteriores do Fretio.

    Procura por processos chrome.exe cujo command-line contenha
    o diretório .Fretio (user-data-dir dos providers Alfa e Rodonaves).
    Tenta wmic primeiro (rápido); se falhar usa Get-CimInstance (Windows 11+).
    """
    if sys.platform != "win32":
        return
    import subprocess as _sp
    fretio_marker = os.path.join(os.path.expanduser("~"), ".fretio").replace("/", "\\").lower()
    fretio_temp_marker = "fretio_chrome_"

    def _kill_pids_from_lines(lines: list[str]) -> None:
        pid = None
        cmd = ""
        for line in lines:
            line = line.strip()
            if not line:
                if pid is not None and (fretio_marker in cmd.lower() or fretio_temp_marker in cmd.lower()):
                    try:
                        _sp.run(
                            ["taskkill", "/F", "/T", "/PID", str(pid)],
                            capture_output=True, timeout=10,
                            creationflags=_sp.CREATE_NO_WINDOW,
                        )
                        _log_diag(f"Matou Chrome órfão do Fretio PID={pid} (tree kill)")
                    except Exception:
                        try:
                            os.kill(pid, 9)
                            _log_diag(f"Matou Chrome órfão do Fretio PID={pid} (os.kill)")
                        except OSError:
                            pass
                pid = None
                cmd = ""
                continue
            if line.startswith("CommandLine="):
                cmd = line[len("CommandLine="):]
            elif line.startswith("ProcessId="):
                try:
                    pid = int(line[len("ProcessId="):])
                except ValueError:
                    pid = None
        if pid is not None and (fretio_marker in cmd.lower() or fretio_temp_marker in cmd.lower()):
            try:
                os.kill(pid, 9)
                _log_diag(f"Matou Chrome órfão do Fretio PID={pid}")
            except OSError:
                pass

    # Tenta wmic (rápido, disponível em Windows 10 e versões antigas do 11)
    try:
        result = _sp.run(
            ["wmic", "process", "where", "Name='chrome.exe'", "get",
             "ProcessId,CommandLine", "/FORMAT:LIST"],
            capture_output=True, text=True, timeout=10,
            creationflags=_sp.CREATE_NO_WINDOW,
        )
        if result.returncode == 0:
            _kill_pids_from_lines(result.stdout.splitlines())
            return
    except Exception:
        pass

    # Fallback: PowerShell Get-CimInstance (Windows 11 22H2+)
    try:
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" "
            "| ForEach-Object { \"CommandLine=$($_.CommandLine)\"; \"ProcessId=$($_.ProcessId)\"; \"\" }"
        )
        result = _sp.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15,
            creationflags=_sp.CREATE_NO_WINDOW,
        )
        _kill_pids_from_lines(result.stdout.splitlines())
    except Exception as e:
        _log_diag(f"_kill_orphan_Fretio_chromes falhou: {e}")


# Prioridade de lentidão: maior = mais lento (baseado em testes reais).
# Usado para iniciar os mais lentos primeiro e para ordenar resultados.
# Derivado do registro único (ProviderSpec.slowness_priority em factory.py).
_PRIORIDADE_LENTIDAO: dict[str, int] = {
    nome.upper(): slowness_priority_for_provider(nome) for nome in KNOWN_CARRIERS
}

# Timeouts por provider (fluxos reais medidos):
# - TRD: login 2×60s + etapas + modais → mínimo ~45-50s, pior caso >150s
# - RODONAVES: CAPTCHA 45s + polling resultado 30s → mínimo ~75s
# - AGEX: wait_for_url resultado 60s + login 30s → mínimo ~53s
_TIMEOUT_COTACAO_S: dict[str, int] = {
    "ALFA": 60,
    "TRD": 120,
    "RODONAVES": 120,
    "TRANSLOVATO": 120,
    "AGEX": 90,
    # SSW providers: polling até 25s + fallbacks → dar margem para completar internamente
    "COOPEX": 90,
    "EUCATUR": 90,
}
_TIMEOUT_COTACAO_PADRAO_S = 45

_TIMEOUT_PRELOGIN_S: dict[str, int] = {
    "ALFA": 90,
    "TRD": 90,
    "TRANSLOVATO": 90,
    "RODONAVES": 60,
    "AGEX": 60,
}
_TIMEOUT_PRELOGIN_PADRAO_S = 45

# Limite por provider para o cleanup (fechar browser). Um browser.close()
# travado não pode pendurar o encerramento dos demais providers.
_TIMEOUT_CLEANUP_S = 8


class _ProviderSessionRegistry:
    """Mantém providers ativos e timestamps de uso sob lock único."""

    def __init__(self) -> None:
        self._providers: dict[str, Any] = {}
        self._ultimo_uso: dict[str, float] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    @property
    def providers(self) -> dict[str, Any]:
        return self._providers

    async def items(self) -> list[tuple[str, Any]]:
        async with self._lock:
            return list(self._providers.items())

    async def get(self, nome: str) -> Any | None:
        async with self._lock:
            return self._providers.get(nome)

    async def register(self, nome: str, provider: Any) -> Any | None:
        async with self._lock:
            anterior = self._providers.get(nome)
            self._providers[nome] = provider
            self._ultimo_uso[nome] = time.monotonic()
            return anterior

    async def ensure(self, nome: str, factory: Callable[[], Any]) -> tuple[Any, bool]:
        async with self._lock:
            provider = self._providers.get(nome)
            created = provider is None
            if provider is None:
                provider = factory()
                self._providers[nome] = provider
            self._ultimo_uso[nome] = time.monotonic()
            return provider, created

    async def touch(self, nome: str) -> None:
        async with self._lock:
            if nome in self._providers:
                self._ultimo_uso[nome] = time.monotonic()

    async def touch_all(self) -> None:
        agora = time.monotonic()
        async with self._lock:
            for nome in self._providers:
                self._ultimo_uso[nome] = agora

    async def pop(self, nome: str, *, expected: Any | None = None) -> Any | None:
        async with self._lock:
            provider = self._providers.get(nome)
            if provider is None:
                return None
            if expected is not None and provider is not expected:
                return None
            self._providers.pop(nome, None)
            self._ultimo_uso.pop(nome, None)
            return provider

    async def pop_all(self, *, exclude: set[str] | None = None) -> list[tuple[str, Any]]:
        nomes_excluidos = {str(nome).strip().lower() for nome in (exclude or set())}
        async with self._lock:
            removidos: list[tuple[str, Any]] = []
            for nome in list(self._providers):
                if str(nome).strip().lower() in nomes_excluidos:
                    continue
                provider = self._providers.pop(nome, None)
                self._ultimo_uso.pop(nome, None)
                if provider is not None:
                    removidos.append((nome, provider))
            return removidos

    async def pop_idle(self, idle_timeout_s: float) -> list[tuple[str, Any, float]]:
        agora = time.monotonic()
        async with self._lock:
            removidos: list[tuple[str, Any, float]] = []
            for nome in list(self._providers):
                tempo_ocioso = agora - self._ultimo_uso.get(nome, agora)
                if tempo_ocioso < idle_timeout_s:
                    continue
                provider = self._providers.pop(nome, None)
                self._ultimo_uso.pop(nome, None)
                if provider is not None:
                    removidos.append((nome, provider, tempo_ocioso))
            return removidos


class TransportadoraSession:
    """Gerencia sessões persistentes dos providers (browsers já logados)."""

    IDLE_TIMEOUT_S: float = 600.0  # 10 minutos
    _IDLE_CHECK_INTERVAL_S: float = 60.0
    _LAZY_PRELOGIN_PROVIDERS: set[str] = {"trd", "rodonaves"}

    def __init__(self, config_path: Path | None = None):
        self.config = _carregar_config(config_path=config_path)
        self.provider_factory = deps.ProviderFactory(config=self.config)
        self._provider_sessions = _ProviderSessionRegistry()
        self._circuit_breaker = ProviderCircuitBreaker()
        self._inicializado = False
        self._chrome_missing = False
        self._chrome_missing_message = ""
        self._chrome_missing_reported = False
        self._idle_task: asyncio.Task | None = None
        self._lifecycle_lock: asyncio.Lock = asyncio.Lock()

    @property
    def providers(self) -> dict[str, Any]:
        return self._provider_sessions.providers

    async def __aenter__(self) -> "TransportadoraSession":
        await self.inicializar()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.cleanup()

    async def listar_providers(self) -> list[tuple[str, Any]]:
        return await self._provider_sessions.items()

    @property
    def chrome_missing(self) -> bool:
        return self._chrome_missing

    @property
    def chrome_missing_message(self) -> str:
        return self._chrome_missing_message or CHROME_MISSING_USER_MESSAGE

    def _marcar_chrome_ausente(self, exc: BaseException, *, source: str) -> None:
        self._chrome_missing = True
        self._chrome_missing_message = CHROME_MISSING_USER_MESSAGE
        if self._chrome_missing_reported:
            return
        self._chrome_missing_reported = True
        report_provider_error(
            "chrome",
            "pre_login",
            str(exc),
            exception=exc,
            context={
                "event": "chrome_missing",
                "source": source,
            },
        )

    async def obter_provider(self, nome: str) -> Any | None:
        return await self._provider_sessions.get(nome)

    async def registrar_provider(self, nome: str, provider: Any) -> None:
        anterior = await self._provider_sessions.register(nome, provider)
        _logger.debug(
            "Provider registrado na sessão",
            extra={"operation": "session_register", "provider": nome},
        )
        if anterior is not None and anterior is not provider:
            await self._cleanup_provider_instance(
                anterior,
                success_message=f"Cleanup {nome} OK (substituição)",
                failure_message=f"Cleanup {nome} falhou na substituição",
            )

    async def assegurar_provider(self, nome: str, factory: Callable[[], Any]) -> Any:
        if self._circuit_breaker.is_open(nome):
            _log_diag(f"Circuit breaker aberto para {nome}: portal bloqueado temporariamente")
            raise RuntimeError(f"Circuit breaker aberto para {nome}: portal temporariamente indisponível")
        provider, created = await self._provider_sessions.ensure(nome, factory)
        if created:
            await self._executar_lazy_prelogin(nome, provider)
        _logger.debug(
            "Provider obtido da sessão",
            extra={"operation": "session_ensure", "provider": nome},
        )
        return provider

    def record_quote_success(self, nome: str) -> None:
        """Registra sucesso de cotação e fecha o circuito para o provider."""
        self._circuit_breaker.record_success(nome)

    def record_quote_failure(self, nome: str) -> None:
        """Registra falha real de cotação; abre o circuito após N falhas consecutivas."""
        self._circuit_breaker.record_failure(nome)

    async def _executar_lazy_prelogin(self, nome: str, provider: Any) -> None:
        nome_normalizado = str(nome).strip().lower()
        if nome_normalizado not in self._LAZY_PRELOGIN_PROVIDERS:
            return
        pre_login = getattr(provider, "pre_login", None)
        if not callable(pre_login):
            return
        if getattr(provider, "_logged_in", False):
            return

        timeout_s = _TIMEOUT_PRELOGIN_S.get(nome_normalizado.upper(), _TIMEOUT_PRELOGIN_PADRAO_S)
        try:
            _log_diag(f"Lazy pre-login {nome_normalizado.upper()}...")
            resultado = await asyncio.wait_for(asyncio.shield(pre_login()), timeout=timeout_s)
            if resultado is False:
                detalhe = str(getattr(provider, "last_error", "") or "").strip()
                _log_diag(
                    f"Lazy pre-login {nome_normalizado.upper()} retornou False"
                    + (f": {detalhe}" if detalhe else "")
                )
                return
            _log_diag(f"Lazy pre-login {nome_normalizado.upper()} OK")
        except asyncio.TimeoutError:
            _log_diag(
                f"Lazy pre-login {nome_normalizado.upper()} timeout ({timeout_s}s) — "
                "login continuará na cotação"
            )
        except Exception as exc:
            _log_diag(f"Lazy pre-login {nome_normalizado.upper()} falhou: {exc}")

    async def _cleanup_provider_instance(
        self,
        provider: Any,
        *,
        success_message: str,
        failure_message: str,
    ) -> None:
        try:
            await asyncio.wait_for(provider.cleanup(), timeout=_TIMEOUT_CLEANUP_S)
            _log_diag(success_message)
        except asyncio.TimeoutError:
            _log_diag(f"{failure_message}: timeout ({_TIMEOUT_CLEANUP_S}s) — seguindo para o próximo provider")
        except Exception as e:
            _log_diag(f"{failure_message}: {e}")
            report_provider_error(
                str(getattr(provider, "nome", "") or "provider"),
                "cleanup",
                f"{failure_message}: {e}",
                exception=e,
                context={"source": "prelogin_background"},
            )

    async def fechar_provider(
        self,
        nome: str,
        *,
        success_message: str | None = None,
        failure_message: str | None = None,
        expected: Any | None = None,
    ) -> bool:
        provider = await self._provider_sessions.pop(nome, expected=expected)
        if provider is None:
            _logger.debug(
                "Provider não encontrado para encerramento",
                extra={"operation": "session_close", "provider": nome},
            )
            return False
        await self._cleanup_provider_instance(
            provider,
            success_message=success_message or f"Cleanup {nome} OK",
            failure_message=failure_message or f"Cleanup {nome} falhou",
        )
        return True

    async def fechar_providers_exceto(self, nomes_preservados: set[str], *, contexto: str) -> None:
        removidos = await self._provider_sessions.pop_all(exclude=nomes_preservados)
        for nome, provider in removidos:
            await self._cleanup_provider_instance(
                provider,
                success_message=f"{contexto}: cleanup {nome} OK",
                failure_message=f"{contexto}: cleanup {nome} falhou",
            )

    async def inicializar(self, callback=None, login_status_callback=None):
        """Cria providers e faz pre-login em todos. callback(msg) para status.
        login_status_callback(nome, status) para status individual ('pending','ok','fail')."""
        async with self._lifecycle_lock:
            self.provider_factory.preload()
            _kill_orphan_Fretio_chromes()
            if self._inicializado:
                if MODO_FOCO_TRANSPORTADORA and self.providers:
                    foco = str(MODO_FOCO_TRANSPORTADORA).strip().lower()
                    await self.fechar_providers_exceto(
                        {foco},
                        contexto=f"Modo foco {foco.upper()} ativo",
                    )
                return

            # Verifica Chrome uma única vez antes de iniciar qualquer prelogin.
            # Se não encontrado, reporta um único erro e aborta sem tentar cada transportadora.
            try:
                from fretio.providers.base import find_chrome as _find_chrome_global
                _find_chrome_global()
            except FileNotFoundError as _chrome_err:
                _log_diag(f"Chrome não encontrado — prelogin cancelado: {_chrome_err}")
                self._marcar_chrome_ausente(_chrome_err, source="prelogin_background")
                if callback:
                    callback(CHROME_MISSING_USER_MESSAGE)
                return
            except Exception:
                pass
            self._chrome_missing = False
            self._chrome_missing_message = ""

            effective_config = dict(self.config) if isinstance(self.config, dict) else {}
            transportadoras_cfg = effective_config.get("transportadoras", {}) if isinstance(effective_config, dict) else {}
            if MODO_FOCO_TRANSPORTADORA:
                if not isinstance(transportadoras_cfg, dict):
                    transportadoras_cfg = {}
                transportadoras_cfg = dict(transportadoras_cfg)
                foco = str(MODO_FOCO_TRANSPORTADORA).strip().lower()
                for nome_cfg in ("braspress", "trd", "agex", "eucatur", "rodonaves", "alfa", "coopex", "translovato"):
                    sec = transportadoras_cfg.get(nome_cfg)
                    if not isinstance(sec, dict):
                        sec = {}
                    sec = dict(sec)
                    sec["habilitado"] = (nome_cfg == foco)
                    transportadoras_cfg[nome_cfg] = sec
                _log_diag(f"Modo foco {foco.upper()} ativo: apenas essa transportadora fará pre-login.")
            if isinstance(effective_config, dict):
                effective_config["transportadoras"] = transportadoras_cfg if isinstance(transportadoras_cfg, dict) else {}
            effective_config, _remote_skipped = _remote_disabled_results_for_config(
                effective_config,
                contexto="pre-login",
            )
            provider_factory = deps.ProviderFactory(config=effective_config)

            bcfg = provider_factory.get_provider_config("braspress")
            if bcfg.get("habilitado", True):
                headless_braspress = bool(bcfg.get("headless", True))
                provider = provider_factory.create("braspress", headless=headless_braspress)
                if provider is not None:
                    await self.registrar_provider("braspress", provider)
                    _log_diag(f"BRASPRESS sessão criada com headless={headless_braspress}")

            tcfg = provider_factory.get_provider_config("trd")
            if tcfg.get("habilitado", True):
                foco_trd = str(MODO_FOCO_TRANSPORTADORA).strip().lower() == "trd"
                headless_trd = _trd_headless_config_value(tcfg, foco_trd)
                provider = provider_factory.create("trd", headless=headless_trd)
                if provider is not None:
                    await self.registrar_provider("trd", provider)
                    _log_diag(f"TRD sessão criada com headless={headless_trd}")

            if provider_factory.is_available("agex"):
                acfg = provider_factory.get_provider_config("agex")
                if acfg.get("habilitado", True):
                    foco_agex = str(MODO_FOCO_TRANSPORTADORA).strip().lower() == "agex"
                    headless_agex = False if foco_agex else bool(acfg.get("headless", True))
                    provider = provider_factory.create("agex", headless=headless_agex)
                    if provider is not None:
                        await self.registrar_provider("agex", provider)
                        _log_diag(f"AGEX sessão criada com headless={headless_agex}")

            if provider_factory.is_available("eucatur"):
                ecfg = provider_factory.get_provider_config("eucatur")
                if ecfg.get("habilitado", True):
                    foco_eucatur = str(MODO_FOCO_TRANSPORTADORA).strip().lower() == "eucatur"
                    headless_eucatur = False if foco_eucatur else bool(ecfg.get("headless", True))
                    provider = provider_factory.create("eucatur", headless=headless_eucatur)
                    if provider is not None:
                        await self.registrar_provider("eucatur", provider)
                        _log_diag(f"EUCATUR sessão criada com headless={headless_eucatur}")

            if provider_factory.is_available("rodonaves"):
                rcfg = provider_factory.get_provider_config("rodonaves")
                if rcfg.get("habilitado", True):
                    if not _uf_atendida(rcfg.get("ufs_atendidas"), None):
                        _log_diag("RODONAVES ignorada por filtro de UF inválido")
                    else:
                        foco_rodonaves = str(MODO_FOCO_TRANSPORTADORA).strip().lower() == "rodonaves"
                        headless_rodonaves = bool(rcfg.get("headless", True))
                        provider = provider_factory.create("rodonaves", headless=headless_rodonaves)
                        if provider is not None:
                            await self.registrar_provider("rodonaves", provider)
                            _log_diag(f"RODONAVES sessão criada com headless={headless_rodonaves}")

            if provider_factory.is_available("alfa"):
                alcfg = provider_factory.get_provider_config("alfa")
                if alcfg.get("habilitado", True):
                    if not _uf_atendida(alcfg.get("ufs_atendidas"), None):
                        _log_diag("ALFA ignorada por filtro de UF inválido")
                    else:
                        headless_alfa = bool(alcfg.get("headless", False))
                        provider = provider_factory.create("alfa", headless=headless_alfa)
                        if provider is not None:
                            await self.registrar_provider("alfa", provider)
                            _log_diag(f"ALFA sessão criada com headless={headless_alfa}")

            if provider_factory.is_available("coopex"):
                cocfg = provider_factory.get_provider_config("coopex")
                if cocfg.get("habilitado", True):
                    foco_coopex = str(MODO_FOCO_TRANSPORTADORA).strip().lower() == "coopex"
                    headless_coopex = False if foco_coopex else bool(cocfg.get("headless", True))
                    provider = provider_factory.create("coopex", headless=headless_coopex)
                    if provider is not None:
                        await self.registrar_provider("coopex", provider)
                        _log_diag(f"COOPEX sessão criada com headless={headless_coopex}")

            if provider_factory.is_available("translovato"):
                tlcfg = provider_factory.get_provider_config("translovato")
                if tlcfg.get("habilitado", True):
                    foco_translovato = str(MODO_FOCO_TRANSPORTADORA).strip().lower() == "translovato"
                    headless_translovato = False if foco_translovato else bool(tlcfg.get("headless", True))
                    provider = provider_factory.create("translovato", headless=headless_translovato)
                    if provider is not None:
                        await self.registrar_provider("translovato", provider)
                        _log_diag(f"TRANSLOVATO sessão criada com headless={headless_translovato}")

            _pre_login_semaforo = asyncio.Semaphore(2)
            providers_snapshot = await self.listar_providers()
            # Pré-aquece o login das mais lentas primeiro (mesma prioridade da
            # cotação): elas adquirem o semáforo antes e ganham mais tempo de
            # background, encurtando o caminho crítico total.
            providers_snapshot = sorted(
                providers_snapshot,
                key=lambda item: _PRIORIDADE_LENTIDAO.get(str(item[0]).upper(), 0),
                reverse=True,
            )
            total_providers = len(providers_snapshot)
            _log_diag(f"Iniciando pre-login em {total_providers} transportadoras (máx 2 simultâneos)...")
            if callback:
                callback(f"Fazendo login em {total_providers} transportadoras...")

            async def _pre_login_one(nome, prov):
                is_alfa = nome.lower() == "alfa"
                timeout_s = _TIMEOUT_PRELOGIN_S.get(nome.upper(), _TIMEOUT_PRELOGIN_PADRAO_S)
                max_retries = 0 if is_alfa else 1
                backoff = 3

                if login_status_callback:
                    login_status_callback(nome, "pending")

                if not is_alfa:
                    await _pre_login_semaforo.acquire()
                    _log_diag(f"Pre-login semáforo adquirido: {nome}")

                try:
                    for attempt in range(max_retries + 1):
                        try:
                            _log_diag(f"Pre-login {nome}..." if attempt == 0 else f"Pre-login {nome} tentativa {attempt + 1}...")
                            if callback:
                                callback(f"Login: {nome}..." if attempt == 0 else f"Login: {nome} (tentativa {attempt + 1})...")
                            try:
                                pre_login_result = await asyncio.wait_for(asyncio.shield(prov.pre_login()), timeout=timeout_s)
                            except asyncio.TimeoutError:
                                _log_diag(f"Pre-login {nome} timeout ({timeout_s}s) — login continuará na cotação")
                                if login_status_callback:
                                    login_status_callback(nome, "fail")
                                return nome, False
                            if pre_login_result is False:
                                detalhe = str(getattr(prov, "last_error", "") or "").strip()
                                raise RuntimeError(detalhe or f"Pre-login {nome} retornou False")
                            _log_diag(f"Pre-login {nome} OK")
                            if login_status_callback:
                                login_status_callback(nome, "ok")
                            return nome, True
                        except Exception as e:
                            is_connection_error = any(k in str(e) for k in ("ERR_CONNECTION", "ERR_NAME", "ERR_TIMED_OUT", "net::"))
                            if is_connection_error and attempt < max_retries:
                                wait = backoff * (attempt + 1)
                                _log_diag(f"Pre-login {nome} erro de rede ({e}), retry em {wait}s...")
                                await asyncio.sleep(wait)
                                continue
                            _log_diag(f"Pre-login {nome} falhou: {e}")
                            # Chrome ausente já foi reportado globalmente com module="chrome_missing"
                            _e_str = str(e)
                            # Falhas operacionais de pré-login (credenciais do cliente,
                            # portal não liberou acesso, rede/portal lentos) não viram
                            # issue: a cotação do usuário ainda exercita o login e
                            # reporta caso seja uma quebra real de código.
                            if is_expected_prelogin_failure(_e_str):
                                _log_diag(f"Pre-login {nome} falha controlada (sem report): {_e_str}")
                            elif "Google Chrome" not in _e_str and "chrome" not in _e_str.lower():
                                report_provider_error(
                                    nome,
                                    "pre_login",
                                    f"Pre-login {nome} falhou: {_e_str}",
                                    exception=e,
                                    context={
                                        "source": "prelogin_background",
                                        "carrier_enabled": True,
                                        "attempt": attempt + 1,
                                        "max_retries": max_retries,
                                    },
                                )
                            if login_status_callback:
                                login_status_callback(nome, "fail")
                            return nome, False
                finally:
                    if not is_alfa:
                        _pre_login_semaforo.release()

            results = await asyncio.gather(
                *[_pre_login_one(n, p) for n, p in providers_snapshot],
                return_exceptions=True,
            )

            ok_count = sum(1 for r in results if not isinstance(r, Exception) and r[1])
            _log_diag(f"Pre-login concluído: {ok_count}/{total_providers} OK")

            try:
                from fretio.providers._win_taskbar import ocultar_janelas_ime
                n = ocultar_janelas_ime()
                if n:
                    _log_diag(f"Ocultou {n} janela(s) IME residual(is)")
            except Exception:
                pass

            if callback:
                callback(f"Login concluído: {ok_count}/{total_providers} transportadoras prontas")
            self._inicializado = True
            await self._provider_sessions.touch_all()
            self._iniciar_verificador_ocioso()

    async def registrar_uso(self, nome: str) -> None:
        """Atualiza timestamp de último uso de um provider."""
        await self._provider_sessions.touch(nome)

    async def fechar_ociosos(self) -> None:
        """Fecha browsers de providers ociosos por mais de IDLE_TIMEOUT_S."""
        a_finalizar = await self._provider_sessions.pop_idle(self.IDLE_TIMEOUT_S)
        for nome, prov, tempo_ocioso in a_finalizar:
            await self._cleanup_provider_instance(
                prov,
                success_message=f"Idle cleanup {nome} OK (ocioso por {tempo_ocioso:.0f}s)",
                failure_message=f"Idle cleanup {nome} falhou",
            )

    def _iniciar_verificador_ocioso(self) -> None:
        """Inicia task em background que verifica providers ociosos."""
        if self._idle_task is not None and not self._idle_task.done():
            return
        _logger.debug(
            "Iniciando verificador de providers ociosos",
            extra={"operation": "session_idle_monitor"},
        )

        async def _loop():
            # CancelledError externo (cleanup()) é re-levantado imediatamente.
            # Erros genéricos apenas logam e reiniciam o ciclo.
            try:
                while True:
                    try:
                        await asyncio.sleep(self._IDLE_CHECK_INTERVAL_S)
                        await self.fechar_ociosos()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        _log_diag(f"Erro no verificador de ociosidade: {e}")
            except asyncio.CancelledError:
                pass

        self._idle_task = asyncio.create_task(_loop())

    async def cleanup(self):
        """Fecha todos os browsers."""
        async with self._lifecycle_lock:
            idle_task = self._idle_task
            self._idle_task = None
            if idle_task is not None:
                idle_task.cancel()
                try:
                    await idle_task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    _log_diag(f"Erro ao encerrar verificador de ociosidade: {e}")
            removidos = await self._provider_sessions.pop_all()
            _logger.info(
                "Encerrando providers ativos",
                extra={"operation": "session_cleanup"},
            )
            for nome, prov in removidos:
                await self._cleanup_provider_instance(
                    prov,
                    success_message=f"Cleanup {nome} OK",
                    failure_message=f"Cleanup {nome} falhou",
                )
            self._inicializado = False

    @property
    def pronto(self) -> bool:
        return self._inicializado and not self._chrome_missing



__all__ = [name for name in globals() if not name.startswith("__")]
