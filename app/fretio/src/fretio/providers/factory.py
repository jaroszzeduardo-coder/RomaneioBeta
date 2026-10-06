"""Factory e composição de providers."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import re
import threading
from typing import Any, Callable, Mapping

from fretio.config_manager import ConfigManager
from fretio.logging_conf import bind_logger, get_logger

logger = get_logger(__name__)


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _text(value: Any, default: str = "") -> str:
    return str(value if value is not None else default).strip()


ProviderBuilder = Callable[[dict[str, Any]], dict[str, Any] | None]


@dataclass(frozen=True)
class ProviderSpec:
    """Registro único por transportadora.

    Fonte de verdade para o que estava espalhado: classe do provider, campos
    obrigatórios, campos de credencial da UI (chave/rótulo/tipo) e prioridade
    de lentidão. Consumidores (orchestrator, web_app, session_manager) derivam
    daqui em vez de manter cópias próprias."""

    key: str
    module_path: str
    class_name: str
    builder: ProviderBuilder
    required_fields: tuple[str, ...] = ()
    credential_fields: tuple[tuple[str, str, str], ...] = ()
    slowness_priority: int = 0


@dataclass(frozen=True)
class ProviderConfigValidation:
    provider: str
    enabled: bool
    valid: bool
    missing_fields: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if not self.enabled:
            return "desabilitada"
        return "ok" if self.valid else "Configuração incompleta"

    @property
    def user_message(self) -> str:
        if self.valid:
            return ""
        labels = ", ".join(_REQUIRED_FIELD_LABELS.get(field, field) for field in self.missing_fields)
        return f"Configuração incompleta. Preencha: {labels}."


# _REQUIRED_FIELDS é derivado de _PROVIDER_SPECS (logo após a definição do
# registro). _REQUIRED_FIELD_LABELS são os rótulos usados na mensagem de
# "configuração incompleta" (minúsculos, meio de frase) — distintos dos rótulos
# de UI em ProviderSpec.credential_fields.
_REQUIRED_FIELD_LABELS: dict[str, str] = {
    "cnpj": "CNPJ",
    "senha": "senha",
    "cotacao_url": "URL de cotação",
    "cnpj_pagador": "CNPJ pagador",
    "cnpj_remetente": "CNPJ remetente",
    "cnpj_destinatario": "CNPJ destinatário",
    "email": "e-mail",
    "dominio": "domínio",
    "usuario": "usuário",
    "login": "login",
}


def required_fields_for_provider(name: str) -> tuple[str, ...]:
    return _REQUIRED_FIELDS.get(str(name or "").strip().lower(), ())


def validate_provider_minimum_config(
    name: str,
    config: Mapping[str, Any] | None,
    *,
    enabled_default: bool = True,
) -> ProviderConfigValidation:
    provider_name = str(name or "").strip().lower()
    cfg = dict(config) if isinstance(config, Mapping) else {}
    enabled = bool(cfg.get("habilitado", enabled_default))
    required = required_fields_for_provider(provider_name)
    if not enabled or not required:
        return ProviderConfigValidation(provider_name, enabled, True)

    missing: list[str] = []
    for field in required:
        value = _text(cfg.get(field))
        if provider_name == "agex" and field == "email" and not value:
            legacy_login = _text(cfg.get("cnpj"))
            if "@" in legacy_login:
                value = legacy_login
        if provider_name == "rodonaves" and field == "dominio" and not value:
            value = "RTE"
        if not value:
            missing.append(field)

    return ProviderConfigValidation(provider_name, enabled, not missing, tuple(missing))


def _build_braspress(config: dict[str, Any]) -> dict[str, Any] | None:
    cnpj = _text(config.get("cnpj"))
    senha = _text(config.get("senha"))
    if not cnpj or not senha:
        return None
    return {
        "cnpj": cnpj,
        "senha": senha,
        "headless": bool(config.get("headless", True)),
    }


def _build_trd(config: dict[str, Any]) -> dict[str, Any] | None:
    email = _text(config.get("email"))
    senha = _text(config.get("senha"))
    if not email or not senha:
        return None
    return {
        "email": email,
        "senha": senha,
        "headless": bool(config.get("headless", True)),
    }


def _build_agex(config: dict[str, Any]) -> dict[str, Any] | None:
    cnpj = _text(config.get("cnpj"))
    email = _text(config.get("email"))
    if not email and "@" in cnpj:
        email = cnpj
    senha = _text(config.get("senha"))
    if not email or not senha:
        return None
    cnpj_remetente = config.get("cnpj_remetente")
    if cnpj_remetente is None or _text(cnpj_remetente) == "":
        cnpj_remetente = cnpj
    return {
        "cnpj": cnpj,
        "email": email,
        "senha": senha,
        "cnpj_remetente": cnpj_remetente,
        "cnpj_destinatario": config.get("cnpj_destinatario"),
        "cep_origem": config.get("cep_origem"),
        "cep_destino": config.get("cep_destino"),
        "descricao_mercadoria": _text(config.get("descricao_mercadoria"), "Mercadoria") or "Mercadoria",
        "tipo_produto": _text(config.get("tipo_produto"), "Artigos Esportivos") or "Artigos Esportivos",
        "volumes": int(config.get("volumes", 1) or 1),
        "altura_m": float(config.get("altura_m", 0.0) or 0.0),
        "largura_m": float(config.get("largura_m", 0.0) or 0.0),
        "comprimento_m": float(config.get("comprimento_m", 0.0) or 0.0),
        "cubagens": config.get("cubagens"),
        "headless": bool(config.get("headless", True)),
    }


def _build_eucatur(config: dict[str, Any]) -> dict[str, Any] | None:
    dominio = _text(config.get("dominio"))
    usuario = _text(config.get("usuario"))
    senha = _text(config.get("senha"))
    cnpj_pagador = _text(config.get("cnpj_pagador"))
    if not dominio or not usuario or not senha:
        return None
    return {
        "dominio": dominio,
        "usuario": usuario,
        "senha": senha,
        "cnpj_pagador": cnpj_pagador,
        "headless": bool(config.get("headless", True)),
    }


def _build_rodonaves(config: dict[str, Any]) -> dict[str, Any] | None:
    dominio = _text(config.get("dominio"), "RTE") or "RTE"
    usuario = _text(config.get("usuario"))
    senha = _text(config.get("senha"))
    cnpj_pagador = _text(config.get("cnpj_pagador"))
    if not dominio or not usuario or not senha or not cnpj_pagador:
        return None
    return {
        "dominio": dominio,
        "usuario": usuario,
        "senha": senha,
        "cnpj_pagador": cnpj_pagador,
        "login_url": _text(config.get("login_url")),
        "cotacao_url": _text(config.get("cotacao_url")),
        "headless": bool(config.get("headless", True)),
    }


def _build_alfa(config: dict[str, Any]) -> dict[str, Any] | None:
    login = _text(config.get("login"))
    senha = _text(config.get("senha"))
    if not login or not senha:
        return None
    return {
        "login": login,
        "senha": senha,
        "login_url": _text(config.get("login_url")),
        "cotacao_url": _text(config.get("cotacao_url")),
        "headless": bool(config.get("headless", False)),
    }


def _build_coopex(config: dict[str, Any]) -> dict[str, Any] | None:
    dominio = _text(config.get("dominio"))
    usuario = _text(config.get("usuario"))
    senha = _text(config.get("senha"))
    cnpj_pagador = _text(config.get("cnpj_pagador"))
    if not dominio or not usuario or not senha:
        return None
    return {
        "dominio": dominio,
        "usuario": usuario,
        "senha": senha,
        "cnpj_pagador": cnpj_pagador,
        "headless": bool(config.get("headless", True)),
    }


def _build_translovato(config: dict[str, Any]) -> dict[str, Any] | None:
    cnpj = _text(config.get("cnpj"))
    usuario = _text(config.get("usuario"))
    senha = _text(config.get("senha"))
    if not cnpj or not usuario or not senha:
        return None
    return {
        "cnpj": cnpj,
        "usuario": usuario,
        "senha": senha,
        "cnpj_remetente": _text(config.get("cnpj_remetente")),
        "produto": _text(config.get("produto"), "CONFECCAO") or "CONFECCAO",
        "cotacao_url": _text(config.get("cotacao_url")),
        "headless": bool(config.get("headless", True)),
    }


_PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "braspress": ProviderSpec(
        "braspress", "fretio.providers.braspress_playwright", "BraspressPlaywrightProvider", _build_braspress,
        required_fields=("cnpj", "senha"),
        credential_fields=(("cnpj", "CNPJ", "text"), ("senha", "Senha", "password")),
        slowness_priority=500,
    ),
    "trd": ProviderSpec(
        "trd", "fretio.providers.trd", "TRDProvider", _build_trd,
        required_fields=("email", "senha"),
        credential_fields=(("email", "E-mail", "text"), ("senha", "Senha", "password")),
        slowness_priority=700,
    ),
    "agex": ProviderSpec(
        "agex", "fretio.providers.agex", "AGEXProvider", _build_agex,
        required_fields=("email", "senha"),
        credential_fields=(
            ("email", "E-mail", "text"), ("senha", "Senha", "password"),
            ("cnpj_remetente", "CNPJ remetente", "text"),
        ),
        slowness_priority=100,
    ),
    "eucatur": ProviderSpec(
        "eucatur", "fretio.providers.eucatur", "EucaturProvider", _build_eucatur,
        required_fields=("dominio", "usuario", "senha"),
        credential_fields=(
            ("dominio", "Domínio", "text"), ("usuario", "Usuário", "text"),
            ("senha", "Senha", "password"), ("cnpj_pagador", "CNPJ pagador", "text"),
        ),
        slowness_priority=400,
    ),
    "rodonaves": ProviderSpec(
        "rodonaves", "fretio.providers.rodonaves", "RodonavesProvider", _build_rodonaves,
        required_fields=("dominio", "usuario", "senha", "cnpj_pagador"),
        credential_fields=(
            ("dominio", "Domínio", "text"), ("usuario", "Usuário", "text"),
            ("senha", "Senha", "password"), ("cnpj_pagador", "CNPJ pagador", "text"),
        ),
        slowness_priority=300,
    ),
    "alfa": ProviderSpec(
        "alfa", "fretio.providers.alfa", "AlfaProvider", _build_alfa,
        required_fields=("login", "senha"),
        credential_fields=(
            ("login", "Login", "text"), ("senha", "Senha", "password"),
            ("cnpj_remetente", "CNPJ remetente", "text"),
        ),
        slowness_priority=600,
    ),
    "coopex": ProviderSpec(
        "coopex", "fretio.providers.coopex", "CoopexProvider", _build_coopex,
        required_fields=("dominio", "usuario", "senha"),
        credential_fields=(
            ("dominio", "Domínio", "text"), ("usuario", "Usuário", "text"),
            ("senha", "Senha", "password"), ("cnpj_pagador", "CNPJ pagador", "text"),
        ),
        slowness_priority=350,
    ),
    "translovato": ProviderSpec(
        "translovato", "fretio.providers.translovato", "TranslovatoProvider", _build_translovato,
        required_fields=("cnpj", "usuario", "senha"),
        credential_fields=(
            ("cnpj", "CNPJ", "text"), ("usuario", "Usuário", "text"),
            ("senha", "Senha", "password"), ("cnpj_remetente", "CNPJ remetente", "text"),
        ),
        slowness_priority=450,
    ),
}


# Derivados do registro único acima — consumidores devem usar estes acessores.
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    key: spec.required_fields for key, spec in _PROVIDER_SPECS.items()
}


def credential_fields_for_provider(name: str) -> tuple[tuple[str, str, str], ...]:
    """Campos de credencial (chave, rótulo, tipo) para a UI."""
    spec = _PROVIDER_SPECS.get(str(name or "").strip().lower())
    return spec.credential_fields if spec is not None else ()


def slowness_priority_for_provider(name: str) -> int:
    """Prioridade de lentidão (maior = mais lento); usada para ordenar cotações."""
    spec = _PROVIDER_SPECS.get(str(name or "").strip().lower())
    return spec.slowness_priority if spec is not None else 0


class ProviderFactory:
    """Resolve configuração e instancia providers sob demanda."""

    _class_cache: dict[str, type[Any] | None] = {}
    _class_cache_lock = threading.Lock()

    def __init__(
        self,
        *,
        config: Mapping[str, Any] | None = None,
        config_manager: ConfigManager | None = None,
        empresa_nome: str = "default",
    ) -> None:
        self._config = dict(config) if isinstance(config, Mapping) else None
        self._config_manager = config_manager or (
            None if self._config is not None else ConfigManager.get_instance(empresa_nome)
        )

    def get_config(self) -> dict[str, Any]:
        if self._config is not None:
            return self._config
        if self._config_manager is None:
            return {}
        loaded = self._config_manager.load_config()
        return loaded if isinstance(loaded, dict) else {}

    def get_provider_config(self, name: str) -> dict[str, Any]:
        provider_name = str(name or "").strip().lower()
        config = self.get_config()
        transportadoras = config.get("transportadoras", {}) if isinstance(config, dict) else {}
        nested = transportadoras.get(provider_name, {}) if isinstance(transportadoras, dict) else {}
        if isinstance(nested, dict) and nested:
            return dict(nested)
        legacy = config.get(provider_name, {}) if isinstance(config, dict) else {}
        if isinstance(legacy, dict) and legacy:
            return dict(legacy)
        return {"habilitado": False}

    @classmethod
    def get_provider_class(cls, name: str) -> type[Any] | None:
        provider_name = str(name or "").strip().lower()
        if provider_name in cls._class_cache:
            return cls._class_cache[provider_name]

        spec = _PROVIDER_SPECS.get(provider_name)
        if spec is None:
            bind_logger(logger, provider=provider_name, operation="resolve_provider_class").debug(
                "Provider spec not found"
            )
            return None

        with cls._class_cache_lock:
            if provider_name in cls._class_cache:
                return cls._class_cache[provider_name]
            try:
                module = importlib.import_module(spec.module_path)
                provider_class = getattr(module, spec.class_name)
            except ModuleNotFoundError as exc:
                missing = exc.name or ""
                if missing == spec.module_path or spec.module_path.startswith(missing + "."):
                    # Provider opcional não incluído neste build — condição esperada,
                    # tratada pelo orchestrator. Não é um erro, então não alarmamos com WARNING.
                    bind_logger(logger, provider=provider_name, operation="resolve_provider_class").info(
                        "Provider %s não incluído neste build (%s)",
                        provider_name,
                        spec.module_path,
                    )
                else:
                    # Dependência ausente de um provider que deveria existir: isto é um erro real.
                    bind_logger(logger, provider=provider_name, operation="resolve_provider_class").warning(
                        "Provider class unavailable (%s.%s): %s",
                        spec.module_path,
                        spec.class_name,
                        exc,
                    )
                provider_class = None
            except (ImportError, AttributeError) as exc:
                bind_logger(logger, provider=provider_name, operation="resolve_provider_class").warning(
                    "Provider class unavailable (%s.%s): %s",
                    spec.module_path,
                    spec.class_name,
                    exc,
                )
                provider_class = None
            cls._class_cache[provider_name] = provider_class
            return provider_class

    @classmethod
    def preload(cls, names: list[str] | tuple[str, ...] | None = None) -> dict[str, type[Any] | None]:
        provider_names = names or tuple(_PROVIDER_SPECS.keys())
        return {
            str(name).strip().lower(): cls.get_provider_class(name)
            for name in provider_names
        }

    def is_available(self, name: str) -> bool:
        return self.get_provider_class(name) is not None

    def validate_minimum_config(self, name: str) -> ProviderConfigValidation:
        return validate_provider_minimum_config(name, self.get_provider_config(name))

    def create(
        self,
        name: str,
        *,
        ignore_disabled: bool = False,
        config_override: Mapping[str, Any] | None = None,
        **overrides: Any,
    ) -> Any | None:
        provider_name = str(name or "").strip().lower()
        spec = _PROVIDER_SPECS.get(provider_name)
        if spec is None:
            bind_logger(logger, provider=provider_name, operation="create_provider").debug(
                "Provider not mapped in factory"
            )
            return None

        provider_class = self.get_provider_class(provider_name)
        if provider_class is None:
            bind_logger(logger, provider=provider_name, operation="create_provider").warning(
                "Provider class unavailable"
            )
            return None

        config = (
            dict(config_override)
            if isinstance(config_override, Mapping)
            else self.get_provider_config(provider_name)
        )
        if not ignore_disabled and not bool(config.get("habilitado", True)):
            bind_logger(logger, provider=provider_name, operation="create_provider").debug(
                "Provider desabilitado na configuração"
            )
            return None

        merged = dict(config)
        merged.update(overrides)
        validation = validate_provider_minimum_config(
            provider_name,
            merged,
            enabled_default=bool(config.get("habilitado", True)),
        )
        if not validation.valid:
            bind_logger(logger, provider=provider_name, operation="create_provider").debug(
                "Provider com configuração mínima incompleta: %s",
                ",".join(validation.missing_fields),
            )
            return None
        kwargs = spec.builder(merged)
        if not kwargs:
            bind_logger(logger, provider=provider_name, operation="create_provider").debug(
                "Provider sem parâmetros mínimos para instanciação"
            )
            return None
        bind_logger(logger, provider=provider_name, operation="create_provider").info(
            "Instanciando provider com configuração válida"
        )
        return provider_class(**kwargs)

    @staticmethod
    def normalize_cnpj(value: Any) -> str:
        return _digits(value)
