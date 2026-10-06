"""Builders de kwargs por transportadora extraídos de orchestrator.py."""

from __future__ import annotations

from typing import Any

from .common import _log_diag
from .validation import _cep, _digits
from .config import _resolver_cep_origem


def _build_braspress_kwargs(
    *,
    cfg: dict[str, Any],
    origem: str,
    destino: str,
    peso: float,
    valor: float,
    cnpj_destinatario: str,
    volumes: int,
    cubagens_validas: list[dict[str, Any]],
    cnpj_remetente: str,
    tipo_frete: str,
    effective_config: dict[str, Any],
) -> dict[str, Any] | None:
    """Retorna kwargs para BRASPRESS ou None se não configurada."""
    cnpj = str(cfg.get("cnpj", "")).strip()
    senha = str(cfg.get("senha", "")).strip()
    if not (cnpj and senha):
        _log_diag("BRASPRESS não configurada (CNPJ/senha ausentes)")
        return None
    primeira_cub = cubagens_validas[0]
    _log_diag(
        f"BRASPRESS preparada (cnpj={cnpj[:6]}..., linhas_cubagem={len(cubagens_validas)}, "
        f"headless={bool(cfg.get('headless', True))})"
    )
    kwargs: dict[str, Any] = dict(
        origem=origem,
        destino=destino,
        peso=peso,
        valor=valor,
        cnpj_destinatario=cnpj_destinatario,
        volumes=volumes,
        comprimento_cm=int(primeira_cub["comprimento_cm"]),
        largura_cm=int(primeira_cub["largura_cm"]),
        altura_cm=int(primeira_cub["altura_cm"]),
        cubagens=cubagens_validas,
    )
    if cnpj_remetente:
        kwargs["cnpj_remetente"] = cnpj_remetente
        kwargs["tipo_frete"] = tipo_frete or "2"
    return kwargs


def _build_trd_kwargs(
    *,
    cfg: dict[str, Any],
    origem: str,
    destino: str,
    peso: float,
    valor: float,
    volumes: int,
    cubagens_validas: list[dict[str, Any]],
    cnpj_destinatario: str,
    cnpj_remetente: str,
    headless_trd: bool,
) -> dict[str, Any] | None:
    """Retorna kwargs para TRD ou None se não configurada."""
    email = str(cfg.get("email", "")).strip()
    senha = str(cfg.get("senha", "")).strip()
    if not (email and senha):
        _log_diag("TRD não configurada (email/senha ausentes)")
        return None
    _log_diag(f"TRD preparada (headless={headless_trd})")
    kwargs: dict[str, Any] = dict(
        origem=origem,
        destino=destino,
        peso=peso,
        valor=valor,
        volumes=volumes,
        cubagens=cubagens_validas,
        cnpj_destinatario=cnpj_destinatario,
    )
    if cnpj_remetente:
        kwargs["cnpj_remetente"] = cnpj_remetente
        kwargs["cep_remetente"] = origem
    return kwargs


def _build_eucatur_kwargs(
    *,
    cfg: dict[str, Any],
    origem: str,
    destino: str,
    peso: float,
    valor: float,
    volumes: int,
    cubagem_m3: float,
    cubagens_validas: list[dict[str, Any]],
    cnpj_destinatario: str,
    cnpj_pagador_euc: str,
    cnpj_remetente: str,
    effective_config: dict[str, Any],
    headless_eucatur: bool,
) -> dict[str, Any] | None:
    """Retorna kwargs para EUCATUR ou None se não configurada."""
    dominio = str(cfg.get("dominio", "")).strip()
    usuario = str(cfg.get("usuario", "")).strip()
    senha_euc = str(cfg.get("senha", "")).strip()
    if not (dominio and usuario and senha_euc):
        _log_diag("Eucatur não configurada (domínio/usuário/senha ausentes)")
        return None
    _log_diag(f"EUCATUR preparada (headless={headless_eucatur})")
    kwargs: dict[str, Any] = dict(
        origem=origem,
        destino=destino,
        peso=peso,
        valor=valor,
        volumes=volumes,
        cubagem_m3=cubagem_m3,
        cubagens=cubagens_validas,
        cnpj_remetente=cnpj_pagador_euc,
        cnpj_destinatario=cnpj_destinatario,
        cnpj_pagador=cnpj_pagador_euc,
    )
    if cnpj_remetente:
        kwargs["cnpj_remetente"] = cnpj_remetente
        kwargs["cnpj_destinatario"] = cnpj_pagador_euc
        kwargs["destino"] = _resolver_cep_origem(effective_config, "")
        kwargs["tipo_frete"] = "2"
    return kwargs


def _build_rodonaves_kwargs(
    *,
    cfg: dict[str, Any],
    origem: str,
    destino: str,
    peso: float,
    valor: float,
    volumes: int,
    cubagem_m3: float,
    cubagens_validas: list[dict[str, Any]],
    cnpj_destinatario: str,
    cep_origem: str,
    headless_rodonaves: bool,
    numero_destino: str = "",
) -> dict[str, Any] | None:
    """Retorna kwargs para RODONAVES ou None se não configurada."""
    dominio = str(cfg.get("dominio", "RTE") or "RTE").strip()
    usuario = str(cfg.get("usuario", "")).strip()
    senha = str(cfg.get("senha", "")).strip()
    cnpj_pagador = _digits(str(cfg.get("cnpj_pagador", "") or ""))
    if not (dominio and usuario and senha and len(cnpj_pagador) == 14):
        _log_diag("RODONAVES não configurada (domínio/usuário/senha/cnpj_pagador ausentes)")
        return None
    _log_diag(f"RODONAVES preparada (headless={headless_rodonaves})")
    return dict(
        origem=origem,
        destino=destino,
        peso=peso,
        valor=valor,
        volumes=volumes,
        cubagem_m3=cubagem_m3,
        cubagens=cubagens_validas,
        cnpj_remetente=cnpj_pagador,
        cnpj_destinatario=cnpj_destinatario,
        preencher_cep_origem=bool(_cep(cep_origem)),
        numero_destino=numero_destino,
    )


def _build_alfa_kwargs(
    *,
    cfg: dict[str, Any],
    origem: str,
    destino: str,
    peso: float,
    valor: float,
    volumes: int,
    cubagem_m3: float,
    cubagens_validas: list[dict[str, Any]],
    cnpj_destinatario: str,
    cnpj_remetente: str,
    effective_config: dict[str, Any],
    headless_alfa: bool,
) -> dict[str, Any] | None:
    """Retorna kwargs para ALFA ou None se não configurada."""
    login = str(cfg.get("login", "") or "").strip()
    senha = str(cfg.get("senha", "") or "").strip()
    cnpj_rem = str(cfg.get("cnpj_remetente", "") or "").strip()
    if not (login and senha and cnpj_rem):
        _log_diag("ALFA não configurada (login/senha/cnpj_remetente ausentes)")
        return None
    _log_diag(f"ALFA preparada (headless={headless_alfa})")
    kwargs: dict[str, Any] = dict(
        origem=origem,
        destino=destino,
        peso=peso,
        valor=valor,
        volumes=volumes,
        cubagem_m3=cubagem_m3,
        cubagens=cubagens_validas,
        cnpj_remetente=cnpj_rem,
        cnpj_destinatario=cnpj_destinatario,
    )
    if cnpj_remetente:
        kwargs["cnpj_remetente"] = cnpj_remetente
        kwargs["cnpj_destinatario"] = cnpj_rem
        kwargs["destino"] = _resolver_cep_origem(effective_config, "")
        kwargs["tipo_pagador"] = "2"
    return kwargs


def _build_coopex_kwargs(
    *,
    cfg: dict[str, Any],
    origem: str,
    destino: str,
    peso: float,
    valor: float,
    volumes: int,
    cubagem_m3: float,
    cubagens_validas: list[dict[str, Any]],
    cnpj_destinatario: str,
    cnpj_pagador_co: str,
    cnpj_remetente: str,
    effective_config: dict[str, Any],
    headless_coopex: bool,
) -> dict[str, Any] | None:
    """Retorna kwargs para COOPEX ou None se não configurada."""
    dominio = str(cfg.get("dominio", "")).strip()
    usuario = str(cfg.get("usuario", "")).strip()
    senha_co = str(cfg.get("senha", "")).strip()
    if not (dominio and usuario and senha_co):
        _log_diag("COOPEX não configurada (domínio/usuário/senha ausentes)")
        return None
    _log_diag(f"COOPEX preparada (headless={headless_coopex})")
    kwargs: dict[str, Any] = dict(
        origem=origem,
        destino=destino,
        peso=peso,
        valor=valor,
        volumes=volumes,
        cubagem_m3=cubagem_m3,
        cubagens=cubagens_validas,
        cnpj_remetente=cnpj_pagador_co,
        cnpj_destinatario=cnpj_destinatario,
        cnpj_pagador=cnpj_pagador_co,
    )
    if cnpj_remetente:
        kwargs["cnpj_remetente"] = cnpj_remetente
        kwargs["cnpj_destinatario"] = cnpj_pagador_co
        kwargs["destino"] = _resolver_cep_origem(effective_config, "")
        kwargs["tipo_frete"] = "2"
    return kwargs


def _build_translovato_kwargs(
    *,
    cfg: dict[str, Any],
    origem: str,
    destino: str,
    peso: float,
    valor: float,
    volumes: int,
    cubagem_m3: float,
    cubagens_validas: list[dict[str, Any]],
    cnpj_destinatario: str,
    cnpj_remetente: str,
    uf_destino: str,
    cidade_destino: str,
    headless_translovato: bool,
) -> dict[str, Any] | None:
    """Retorna kwargs para TRANSLOVATO ou None se não configurada."""
    cnpj = _digits(str(cfg.get("cnpj", "") or ""))
    usuario = str(cfg.get("usuario", "") or "").strip()
    senha_tl = str(cfg.get("senha", "") or "").strip()
    cnpj_rem_cfg = _digits(str(cfg.get("cnpj_remetente", "") or "")) or cnpj
    if not (len(cnpj) == 14 and usuario and senha_tl):
        _log_diag("TRANSLOVATO não configurada (CNPJ/usuário/senha ausentes)")
        return None
    _log_diag(
        f"TRANSLOVATO preparada (cnpj={cnpj[:4]}***{cnpj[-2:]}, "
        f"linhas_cubagem={len(cubagens_validas)}, headless={headless_translovato})"
    )
    return dict(
        origem=origem,
        destino=destino,
        cep_origem=origem,
        cep_destino=destino,
        uf_destino=uf_destino,
        cidade_destino=cidade_destino,
        peso=peso,
        valor=valor,
        volumes=volumes,
        cubagem_m3=cubagem_m3,
        cubagens=cubagens_validas,
        cnpj_destinatario=cnpj_destinatario,
        cnpj_remetente=_digits(cnpj_remetente or cnpj_rem_cfg),
    )
