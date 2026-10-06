"""Orquestração e execução das cotações nos providers."""

from __future__ import annotations

from typing import Any, Callable
from dataclasses import dataclass
import asyncio
import re
import time
import traceback

from . import deps
from .common import (
    ResultadoCotacao,
    ProviderCotacaoStatus,
    _log_diag,
    _remote_disabled_results_for_config,
    MODO_FOCO_TRANSPORTADORA,
    KNOWN_CARRIERS,
    PROVIDER_PROGRESS_MESSAGES,
    _trd_headless_config_value,
    provider_progress_from_resultado,
    normalize_provider_progress_status,
    normalize_provider_progress_message,
    QuoteResponse,
    quote_request_from_legacy_kwargs,
    quote_response_to_resultado_cotacao,
)
from .validation import (
    _uf_atendida,
    _cep,
    _digits,
    _cep_para_uf,
    _cubagens_validas,
)
from .config import apply_safe_runtime_overrides, _resolver_cep_origem
from .telemetry import _provider_supports_quote_request_cotar
from .session_manager import (
    TransportadoraSession,
    _PRIORIDADE_LENTIDAO,
    _TIMEOUT_COTACAO_S,
    _TIMEOUT_COTACAO_PADRAO_S,
    CHROME_MISSING_USER_MESSAGE,
    _is_chrome_missing_error,
)
from .error_context import build_quotation_error_diagnostic, report_provider_error
from .error_classifiers import (
    _is_business_error,
    _is_expected_transient_failure,
    _is_expected_transient_failure_str,
)


@dataclass(frozen=True)
class CotacaoOutcome:
    """Resultado bruto de uma execução de provider em ``_run_cotacao``.

    Substitui a 7-tupla posicional que era desempacotada em
    ``_processar_resultado``. ``cotacao`` é o retorno do provider
    (``QuoteResponse``, objeto legado ou ``None``); ``erro`` é a exceção
    capturada (ou ``None`` em sucesso)."""

    i: int
    nome: str
    provider: Any
    kwargs: dict[str, Any]
    cotacao: Any = None
    erro: BaseException | None = None
    duration_ms: int = 0


# Classificadores de erro (_is_business_error / _is_expected_transient_failure*)
# foram unificados em error_classifiers.py e importados acima.


# ---------------------------------------------------------------------------
# Helpers para setup de carriers (blocos try/except comuns)
# ---------------------------------------------------------------------------
# Builders movidos para orchestrator_builders.py; re-exportados abaixo para
# manter a API pública do módulo idêntica.
from .orchestrator_builders import (
    _build_braspress_kwargs,
    _build_trd_kwargs,
    _build_eucatur_kwargs,
    _build_rodonaves_kwargs,
    _build_alfa_kwargs,
    _build_coopex_kwargs,
    _build_translovato_kwargs,
)


async def _executar_cotacoes_com_dados(
    *,
    config: dict[str, Any],
    dados: dict[str, Any],
    cep_origem: str,
    sessao: "TransportadoraSession | None" = None,
    progresso_callback: "Callable[[dict[str, Any]], None] | None" = None,
    cnpj_remetente: str = "",
    tipo_frete: str = "",
    source_type: str = "unknown",
    quote_job_id: Any = None,
) -> list[ResultadoCotacao]:
    def _emitir_progresso(
        *,
        concluidas: int,
        total: int,
        resultado: ResultadoCotacao | None = None,
        provider_status: ProviderCotacaoStatus | None = None,
    ) -> None:
        if progresso_callback is None:
            return
        try:
            payload = {
                "concluidas": int(concluidas),
                "total": int(total),
                "resultado": resultado,
            }
            if provider_status is not None:
                payload.update(provider_status.to_payload())
            progresso_callback(payload)
        except Exception as cb_error:
            _log_diag(f"Falha ao notificar progresso de cotação: {cb_error}")

    def _emitir_status_provider(
        provider: str,
        *,
        stage: str,
        status: str,
        mensagem: str = "",
        duration_ms: int | None = None,
        resultado: ResultadoCotacao | None = None,
    ) -> None:
        progress = ProviderCotacaoStatus(
            provider=str(provider or "GERAL").strip().upper(),
            stage=str(stage or ""),
            status=normalize_provider_progress_status(status),
            mensagem=normalize_provider_progress_message(status, mensagem, stage=stage),
            duration_ms=duration_ms,
            resultado=resultado,
        )
        _emitir_progresso(
            concluidas=0,
            total=0,
            resultado=resultado,
            provider_status=progress,
        )

    effective_config = apply_safe_runtime_overrides(config)
    transportadoras_cfg = effective_config.get("transportadoras", {}) if isinstance(effective_config, dict) else {}
    if MODO_FOCO_TRANSPORTADORA:
        if not isinstance(transportadoras_cfg, dict):
            transportadoras_cfg = {}
        transportadoras_cfg = dict(transportadoras_cfg)
        foco = str(MODO_FOCO_TRANSPORTADORA).strip().lower()
        for nome_cfg in ("braspress", "trd", "agex", "eucatur", "rodonaves", "coopex", "translovato"):
            sec = transportadoras_cfg.get(nome_cfg)
            if not isinstance(sec, dict):
                sec = {}
            sec = dict(sec)
            sec["habilitado"] = (nome_cfg == foco)
            transportadoras_cfg[nome_cfg] = sec
        _log_diag(f"Modo foco {foco.upper()} ativo: apenas essa transportadora será cotada.")
        if sessao and getattr(sessao, "providers", None):
            await sessao.fechar_providers_exceto(
                {foco},
                contexto=f"Modo foco {foco.upper()} ativo",
            )
    if isinstance(effective_config, dict):
        effective_config["transportadoras"] = transportadoras_cfg if isinstance(transportadoras_cfg, dict) else {}
    effective_config, remote_skipped_results = _remote_disabled_results_for_config(
        effective_config,
        contexto="cotacao",
    )
    provider_factory = deps.ProviderFactory(config=effective_config)
    transportadoras_cfg = effective_config.get("transportadoras", {}) if isinstance(effective_config, dict) else {}
    if not isinstance(transportadoras_cfg, dict):
        transportadoras_cfg = {}

    known_carriers = [str(c).strip().lower() for c in KNOWN_CARRIERS]
    for carrier in known_carriers:
        section = transportadoras_cfg.get(carrier) if isinstance(transportadoras_cfg, dict) else {}
        if isinstance(section, dict) and section.get("habilitado", True) is False:
            _emitir_status_provider(
                carrier,
                stage="licenca",
                status="desabilitada",
                mensagem=PROVIDER_PROGRESS_MESSAGES["desabilitada"],
            )
        else:
            _emitir_status_provider(
                carrier,
                stage="aguardando",
                status="aguardando",
                mensagem="Aguardando cotação",
            )

    async def _obter_provider_sessao(
        nome: str,
        *,
        create_kwargs: dict[str, Any] | None = None,
        desired_headless: bool | None = None,
        log_label: str,
    ):
        if sessao is None:
            return provider_factory.create(nome, **(create_kwargs or {}))

        provider = await sessao.obter_provider(nome)
        if provider is not None and desired_headless is not None:
            headless_atual = bool(getattr(provider, "headless", desired_headless))
            if headless_atual != desired_headless:
                _log_diag(
                    f"{log_label}: headless alterado ({headless_atual} -> {desired_headless}), "
                    "reiniciando sessão do provider."
                )
                await sessao.fechar_provider(
                    nome,
                    success_message=f"{log_label} cleanup ao trocar headless OK",
                    failure_message=f"{log_label} cleanup ao trocar headless falhou",
                    expected=provider,
                )

        return await sessao.assegurar_provider(
            nome,
            lambda: provider_factory.create(nome, **(create_kwargs or {})),
        )

    origem = _resolver_cep_origem(config=effective_config, cep_origem_informado=cep_origem)
    destino = _cep(str(dados.get("destino_cep", "") or ""))
    uf_destino_informada = str(dados.get("uf_destino", "") or "").strip().upper()
    if len(uf_destino_informada) != 2 or not uf_destino_informada.isalpha():
        uf_destino_informada = ""
    cidade_destino = str(dados.get("cidade_destino", "") or "").strip()
    cnpj_destinatario = _digits(str(dados.get("cnpj_destinatario", "") or ""))
    try:
        peso = float(dados.get("peso", 0.0) or 0.0)
    except Exception:
        peso = 0.0
    try:
        valor = float(dados.get("valor", 0.0) or 0.0)
    except Exception:
        valor = 0.0

    if len(origem) != 8:
        _log_diag(f"CEP origem inválido: {origem}")
        return [ResultadoCotacao(transportadora="GERAL", status="erro", detalhes="CEP de origem inválido (use 8 dígitos)")]
    if len(destino) != 8:
        _log_diag(f"CEP destino inválido: {destino}")
        return [ResultadoCotacao(transportadora="GERAL", status="erro", detalhes="CEP de destino não encontrado nos pedidos")]
    if len(cnpj_destinatario) != 14:
        msg = "Cotação bloqueada: CNPJ do destinatário ausente ou inválido no romaneio."
        _log_diag(msg)
        return [ResultadoCotacao(transportadora="GERAL", status="erro", detalhes=msg)]
    if peso <= 0:
        msg = "Cotação bloqueada: peso total ausente ou inválido no romaneio."
        _log_diag(msg)
        return [ResultadoCotacao(transportadora="GERAL", status="erro", detalhes=msg)]
    if valor < 0:
        msg = "Cotação bloqueada: valor total negativo no romaneio."
        _log_diag(msg)
        return [ResultadoCotacao(transportadora="GERAL", status="erro", detalhes=msg)]

    cubagens_validas = _cubagens_validas(dados.get("cubagens"))
    if not cubagens_validas:
        msg = "Cotação bloqueada: romaneio sem cubagens válidas (tamanhos de caixa)."
        _log_diag(msg)
        return [ResultadoCotacao(transportadora="GERAL", status="erro", detalhes=msg)]

    try:
        volumes = int(dados.get("volumes", 0) or 0)
    except Exception:
        volumes = 0
    if volumes <= 0:
        volumes = sum(int(cub["quantidade"]) for cub in cubagens_validas)
    if volumes <= 0:
        msg = "Cotação bloqueada: quantidade de volumes inválida no romaneio."
        _log_diag(msg)
        return [ResultadoCotacao(transportadora="GERAL", status="erro", detalhes=msg)]
    volumes_cubagens = sum(int(cub["quantidade"]) for cub in cubagens_validas)
    if volumes_cubagens > 0 and volumes != volumes_cubagens:
        msg = (
            "Cotação bloqueada: volume total do romaneio diverge da soma das cubagens "
            f"(VOL={volumes} vs cubagens={volumes_cubagens})."
        )
        _log_diag(msg)
        return [ResultadoCotacao(transportadora="GERAL", status="erro", detalhes=msg)]

    try:
        cubagem_m3 = float(dados.get("cubagem_m3", 0.0) or 0.0)
    except Exception:
        cubagem_m3 = 0.0
    if cubagem_m3 <= 0:
        msg = "Cotação bloqueada: cubagem total ausente ou inválida no romaneio."
        _log_diag(msg)
        return [ResultadoCotacao(transportadora="GERAL", status="erro", detalhes=msg)]

    tasks: list[tuple[str, Any, dict[str, Any]]] = []  # (nome, provider, kwargs_coteir)
    erros_setup: list[ResultadoCotacao] = list(remote_skipped_results)
    uf_destino_cep = _cep_para_uf(destino)
    uf_destino = uf_destino_informada or uf_destino_cep
    if uf_destino_informada and uf_destino_cep and uf_destino_informada != uf_destino_cep:
        msg = (
            f"CEP de destino ({destino[:5]}-{destino[5:]}) pertence à UF {uf_destino_cep}, "
            f"mas o romaneio informa UF {uf_destino_informada}.\n\n"
            "Verifique se o CEP ou a cidade/UF do destinatário estão corretos no romaneio."
        )
        _log_diag(f"BLOQUEIO: divergência CEP/UF — {msg}")
        return [ResultadoCotacao(
            transportadora="GERAL",
            status="erro_divergencia_uf",
            detalhes=msg,
        )]
    _log_diag(
        f"Preparando cotações: origem={origem}, destino={destino}, peso={peso}, "
        f"valor={valor}, volumes={volumes}, cubagem={cubagem_m3:.4f}m³, "
        f"linhas_cubagem={len(cubagens_validas)}, UF={uf_destino or '?'}"
    )

    def _bloquear_config_incompleta(nome: str):
        validate = getattr(provider_factory, "validate_minimum_config", None)
        if not callable(validate):
            return None
        validation = validate(nome)
        if validation.valid:
            return None
        display = nome.upper()
        detalhes = (
            f"{display}: configuração incompleta. "
            "Abra Configurações > Credenciais e preencha os campos obrigatórios."
        )
        if validation.user_message:
            detalhes = f"{display}: {validation.user_message}"
        _log_diag(detalhes)
        return ResultadoCotacao(
            transportadora=display,
            status="Configuração incompleta",
            detalhes=detalhes,
        )

    def _documento_pagador_padrao() -> str:
        rom_cfg = effective_config.get("romaneio", {}) if isinstance(effective_config, dict) else {}
        if not isinstance(rom_cfg, dict):
            return ""
        for key in ("cnpj_pagador_padrao", "documento_pagador_padrao", "documento_empresa", "cnpj_empresa"):
            value = _digits(str(rom_cfg.get(key, "") or ""))
            if len(value) in (11, 14):
                return value
        return ""

    def _resolver_documento_pagador(tcfg: dict[str, Any]) -> str:
        especifico = _digits(str(tcfg.get("cnpj_pagador", "") or ""))
        if len(especifico) in (11, 14):
            return especifico
        return _documento_pagador_padrao()

    def _resultado_documento_pagador_ausente(nome: str) -> ResultadoCotacao:
        display = str(nome or "").strip().upper()
        detalhes = (
            f"{display}: documento pagador obrigatório para cotação. "
            "Preencha o CNPJ pagador na credencial da transportadora ou informe "
            "o Documento pagador padrão em Configurações > Empresa."
        )
        _log_diag(detalhes)
        return ResultadoCotacao(
            transportadora=display,
            status="Configuração incompleta",
            detalhes=detalhes,
            stage="validacao",
        )

    def _resultado_nao_atendido(nome: str, uf: str) -> ResultadoCotacao:
        display = str(nome or "").strip().upper()
        detalhes = PROVIDER_PROGRESS_MESSAGES["nao_atendido"]
        _log_diag(f"{display} ignorada (UF {uf} não atendida)")
        return ResultadoCotacao(
            transportadora=display,
            status="nao_atendido",
            detalhes=detalhes,
        )

    chrome_missing_reported = False

    def _resultado_chrome_ausente(exc: BaseException | str) -> list[ResultadoCotacao]:
        msg = CHROME_MISSING_USER_MESSAGE
        _log_diag(f"Cotação cancelada: Chrome ausente ({exc})")
        return [ResultadoCotacao(transportadora="GERAL", status="erro", detalhes=msg)]

    def _diagnostico_erro_cotacao(
        nome: str,
        stage: str,
        *,
        provider: Any = None,
        kwargs: dict[str, Any] | None = None,
        error: BaseException | None = None,
        last_error: Any = None,
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        provider_key = str(nome or "").strip().lower()
        provider_stage = getattr(provider, "_passo_atual", None) if provider is not None else None
        effective_stage = str(provider_stage or stage or "").strip()
        browser_url = ""
        portal_domain_known = None
        page = getattr(provider, "_page", None) if provider is not None else None
        try:
            browser_url = str(getattr(page, "url", "") or "")
        except Exception:
            browser_url = ""
        if browser_url:
            portal_domain_known = "ssw.inf.br" in browser_url.lower() or provider_key in browser_url.lower()
        provider_context: dict[str, Any] = {
            "provider_key": provider_key,
            "stage": effective_stage,
        }
        provider_diag = getattr(provider, "_diagnostic_context", None) if provider is not None else None
        if isinstance(provider_diag, dict):
            for diag_key, diag_value in provider_diag.items():
                provider_context[diag_key] = diag_value
        if duration_ms is not None:
            provider_context["duration_ms"] = duration_ms
        safe_hints: dict[str, Any] = {
            "headless": getattr(provider, "headless", None) if provider is not None else None,
        }
        if portal_domain_known is not None:
            safe_hints["portal_domain_known"] = portal_domain_known
        return build_quotation_error_diagnostic(
            provider=nome,
            stage=effective_stage,
            source_type=source_type,
            quote_job_id=quote_job_id,
            dados={
                **(dados if isinstance(dados, dict) else {}),
                "cep_origem": origem,
                "destino_cep": destino,
                "uf_destino": uf_destino,
                "cnpj_destinatario": cnpj_destinatario,
                "peso": peso,
                "valor": valor,
                "volumes": volumes,
                "cubagens": cubagens_validas,
            },
            kwargs=kwargs,
            provider_context=provider_context,
            safe_hints=safe_hints,
            error=error,
            last_error=last_error,
        )

    def _reportar_erro_preparacao(nome: str, exc: BaseException) -> None:
        nonlocal chrome_missing_reported
        exc_text = str(exc)
        if _is_chrome_missing_error(exc):
            if chrome_missing_reported:
                return
            chrome_missing_reported = True
            report_provider_error(
                "chrome",
                "abrir_pagina",
                exc_text,
                exception=exc,
                context={
                    **_diagnostico_erro_cotacao(
                        nome,
                        "pre_login",
                        error=exc,
                        last_error=exc_text,
                    ),
                    "event": "chrome_missing",
                    "source": "cotacao_usuario",
                    "carrier": nome,
                },
            )
            return
        report_provider_error(
            nome,
            "abrir_pagina",
            f"Erro ao preparar {nome}: {exc}",
            exception=exc,
            context={
                **_diagnostico_erro_cotacao(
                    nome,
                    "instanciar_provider",
                    error=exc,
                    last_error=exc_text,
                ),
                "source": "cotacao_usuario",
                "carrier_enabled": True,
                "uf_destino": uf_destino,
                "has_session": sessao is not None,
            },
        )

    # BRASPRESS
    try:
        bcfg = provider_factory.get_provider_config("braspress")
        if bcfg.get("habilitado", True):
            incompleta = _bloquear_config_incompleta("braspress")
            if incompleta is not None:
                erros_setup.append(incompleta)
            elif not _uf_atendida(bcfg.get("ufs_atendidas"), uf_destino):
                erros_setup.append(_resultado_nao_atendido("BRASPRESS", uf_destino))
            else:
                cnpj_bp = str(bcfg.get("cnpj", "")).strip()
                senha_bp = str(bcfg.get("senha", "")).strip()
                if cnpj_bp and senha_bp:
                    headless_braspress = bool(bcfg.get("headless", True))
                    provider = await _obter_provider_sessao(
                        "braspress",
                        create_kwargs={"headless": headless_braspress},
                        desired_headless=headless_braspress,
                        log_label="BRASPRESS",
                    )
                    _bp_kwargs = _build_braspress_kwargs(
                        cfg=bcfg,
                        origem=origem,
                        destino=destino,
                        peso=peso,
                        valor=valor,
                        cnpj_destinatario=cnpj_destinatario,
                        volumes=volumes,
                        cubagens_validas=cubagens_validas,
                        cnpj_remetente=cnpj_remetente,
                        tipo_frete=tipo_frete,
                        effective_config=effective_config,
                    )
                    if _bp_kwargs is not None:
                        tasks.append(("BRASPRESS", provider, _bp_kwargs))
                else:
                    _log_diag("BRASPRESS não configurada (CNPJ/senha ausentes)")
    except Exception as e:
        _log_diag(f"Erro ao preparar BRASPRESS: {e}")
        _reportar_erro_preparacao("BRASPRESS", e)
        if chrome_missing_reported:
            return _resultado_chrome_ausente(e)
        erros_setup.append(ResultadoCotacao(transportadora="BRASPRESS", status="erro", detalhes=str(e)))

    # TRD
    try:
        tcfg = provider_factory.get_provider_config("trd")
        if tcfg.get("habilitado", True):
            incompleta = _bloquear_config_incompleta("trd")
            if incompleta is not None:
                erros_setup.append(incompleta)
            elif not _uf_atendida(tcfg.get("ufs_atendidas"), uf_destino):
                erros_setup.append(_resultado_nao_atendido("TRD", uf_destino))
            else:
                foco_trd = str(MODO_FOCO_TRANSPORTADORA).strip().lower() == "trd"
                headless_trd = _trd_headless_config_value(tcfg, foco_trd)
                _trd_kwargs = _build_trd_kwargs(
                    cfg=tcfg,
                    origem=origem,
                    destino=destino,
                    peso=peso,
                    valor=valor,
                    volumes=volumes,
                    cubagens_validas=cubagens_validas,
                    cnpj_destinatario=cnpj_destinatario,
                    cnpj_remetente=cnpj_remetente,
                    headless_trd=headless_trd,
                )
                if _trd_kwargs is not None:
                    provider = await _obter_provider_sessao(
                        "trd",
                        create_kwargs={"headless": headless_trd},
                        desired_headless=headless_trd,
                        log_label="TRD",
                    )
                    tasks.append(("TRD", provider, _trd_kwargs))
    except Exception as e:
        _log_diag(f"Erro ao preparar TRD: {e}")
        _reportar_erro_preparacao("TRD", e)
        if chrome_missing_reported:
            return _resultado_chrome_ausente(e)
        erros_setup.append(ResultadoCotacao(transportadora="TRD", status="erro", detalhes=str(e)))

    # AGEX — ignorada no modo fornecedor
    if cnpj_remetente:
        _log_diag("AGEX ignorada no modo fornecedor")
    else:
      try:
        if provider_factory.is_available("agex"):
            acfg = provider_factory.get_provider_config("agex")
            if acfg.get("habilitado", True):
                incompleta = _bloquear_config_incompleta("agex")
                if incompleta is not None:
                    erros_setup.append(incompleta)
                elif (uf_destino or "").upper() in {"RS", "SC"}:
                    erros_setup.append(_resultado_nao_atendido("AGEX", uf_destino))
                elif not _uf_atendida(acfg.get("ufs_atendidas"), uf_destino):
                    erros_setup.append(_resultado_nao_atendido("AGEX", uf_destino))
                else:
                    foco_agex = str(MODO_FOCO_TRANSPORTADORA).strip().lower() == "agex"
                    headless_agex = False if foco_agex else bool(acfg.get("headless", True))
                    cnpj_cfg = str(acfg.get("cnpj", "")).strip()
                    cnpj_rem = _digits(str(acfg.get("cnpj_remetente", "")).strip() or cnpj_cfg)
                    cnpj_dest = cnpj_destinatario
                    descricao_mercadoria = str(acfg.get("descricao_mercadoria", "Mercadoria"))
                    tipo_produto = str(acfg.get("tipo_produto", "Artigos Esportivos"))
                    # E-mail com fallback: instalações antigas guardavam o login (e-mail) no campo cnpj.
                    email_agex = str(acfg.get("email", "")).strip()
                    if not email_agex:
                        legacy_login = str(acfg.get("cnpj", "")).strip()
                        if "@" in legacy_login:
                            email_agex = legacy_login
                    senha_agex = str(acfg.get("senha", "")).strip()
                    if not (email_agex and senha_agex):
                        _log_diag("AGEX não configurada (email/senha ausentes)")
                    else:
                        # Pre-compute cubagens_agex for provider creation
                        cubagens_agex_pre = []
                        for cub in cubagens_validas:
                            qtd = int(cub["quantidade"])
                            c_cm = int(cub["comprimento_cm"])
                            l_cm = int(cub["largura_cm"])
                            a_cm = int(cub["altura_cm"])
                            cubagens_agex_pre.append(
                                {
                                    "quantidade": qtd,
                                    "comprimento_m": c_cm / 100.0,
                                    "largura_m": l_cm / 100.0,
                                    "altura_m": a_cm / 100.0,
                                }
                            )
                        if not cubagens_agex_pre:
                            msg = "AGEX bloqueada: romaneio sem tamanhos de caixa (cubagens) válidos."
                            _log_diag(msg)
                            erros_setup.append(ResultadoCotacao(transportadora="AGEX", status="erro", detalhes=msg))
                        else:
                            vol = sum(int(c["quantidade"]) for c in cubagens_agex_pre)
                            primeira = cubagens_agex_pre[0]
                            alt_m = float(primeira["altura_m"])
                            larg_m = float(primeira["largura_m"])
                            comp_m = float(primeira["comprimento_m"])
                            provider = await _obter_provider_sessao(
                                "agex",
                                create_kwargs={
                                    "cnpj": cnpj_cfg,
                                    "email": email_agex,
                                    "senha": senha_agex,
                                    "cnpj_remetente": cnpj_rem,
                                    "cnpj_destinatario": cnpj_dest,
                                    "cep_origem": origem,
                                    "cep_destino": destino,
                                    "descricao_mercadoria": descricao_mercadoria,
                                    "tipo_produto": tipo_produto,
                                    "volumes": vol,
                                    "altura_m": alt_m,
                                    "largura_m": larg_m,
                                    "comprimento_m": comp_m,
                                    "cubagens": cubagens_agex_pre,
                                    "headless": headless_agex,
                                },
                                desired_headless=headless_agex,
                                log_label="AGEX",
                            )
                            # Sessão pré-logada: atualizar sempre os dados da carga corrente.
                            if hasattr(provider, "atualizar_carga"):
                                provider.atualizar_carga(
                                    volumes=vol,
                                    altura_m=alt_m,
                                    largura_m=larg_m,
                                    comprimento_m=comp_m,
                                    cnpj_remetente=cnpj_rem,
                                    cnpj_destinatario=cnpj_dest,
                                    cep_origem=origem,
                                    cep_destino=destino,
                                    descricao_mercadoria=descricao_mercadoria,
                                    tipo_produto=tipo_produto,
                                    cubagens=cubagens_agex_pre,
                                )
                            _log_diag(
                                f"AGEX preparada: peso={peso:.3f}kg, vol={vol}, "
                                f"dims={comp_m:.2f}x{larg_m:.2f}x{alt_m:.2f}m, "
                                f"linhas_cubagem={len(cubagens_agex_pre)}, headless={headless_agex}"
                            )
                            _agex_kwargs = dict(
                                origem=cnpj_rem,
                                destino=cnpj_dest,
                                peso=peso,
                                valor=valor,
                            )
                            tasks.append(("AGEX", provider, _agex_kwargs))
      except Exception as e:
        _log_diag(f"Erro ao preparar AGEX: {e}")
        _reportar_erro_preparacao("AGEX", e)
        if chrome_missing_reported:
            return _resultado_chrome_ausente(e)
        erros_setup.append(ResultadoCotacao(transportadora="AGEX", status="erro", detalhes=str(e)))

    # Eucatur (SSW)
    try:
        if provider_factory.is_available("eucatur"):
            ecfg = provider_factory.get_provider_config("eucatur")
            if ecfg.get("habilitado", True):
                incompleta = _bloquear_config_incompleta("eucatur")
                if incompleta is not None:
                    erros_setup.append(incompleta)
                elif not _uf_atendida(ecfg.get("ufs_atendidas"), uf_destino):
                    erros_setup.append(_resultado_nao_atendido("EUCATUR", uf_destino))
                else:
                    cnpj_pagador_euc = _resolver_documento_pagador(ecfg)
                    if not cnpj_pagador_euc:
                        erros_setup.append(_resultado_documento_pagador_ausente("EUCATUR"))
                    else:
                        foco_eucatur = str(MODO_FOCO_TRANSPORTADORA).strip().lower() == "eucatur"
                        headless_eucatur = False if foco_eucatur else bool(ecfg.get("headless", True))
                        _euc_kwargs = _build_eucatur_kwargs(
                            cfg=ecfg,
                            origem=origem,
                            destino=destino,
                            peso=peso,
                            valor=valor,
                            volumes=volumes,
                            cubagem_m3=cubagem_m3,
                            cubagens_validas=cubagens_validas,
                            cnpj_destinatario=cnpj_destinatario,
                            cnpj_pagador_euc=cnpj_pagador_euc,
                            cnpj_remetente=cnpj_remetente,
                            effective_config=effective_config,
                            headless_eucatur=headless_eucatur,
                        )
                        if _euc_kwargs is not None:
                            provider = await _obter_provider_sessao(
                                "eucatur",
                                create_kwargs={"headless": headless_eucatur, "cnpj_pagador": cnpj_pagador_euc},
                                desired_headless=headless_eucatur,
                                log_label="EUCATUR",
                            )
                            tasks.append(("EUCATUR", provider, _euc_kwargs))
    except Exception as e:
        _log_diag(f"Erro ao preparar Eucatur: {e}")
        _reportar_erro_preparacao("EUCATUR", e)
        if chrome_missing_reported:
            return _resultado_chrome_ausente(e)
        erros_setup.append(ResultadoCotacao(transportadora="EUCATUR", status="erro", detalhes=str(e)))

    # Rodonaves (SSW) — ignorada no modo fornecedor
    if cnpj_remetente:
        _log_diag("RODONAVES ignorada no modo fornecedor")
    else:
        try:
            if provider_factory.is_available("rodonaves"):
                rcfg = provider_factory.get_provider_config("rodonaves")
                if rcfg.get("habilitado", True):
                    incompleta = _bloquear_config_incompleta("rodonaves")
                    if incompleta is not None:
                        erros_setup.append(incompleta)
                    elif not _uf_atendida(rcfg.get("ufs_atendidas"), uf_destino):
                        erros_setup.append(_resultado_nao_atendido("RODONAVES", uf_destino))
                    else:
                        headless_rodonaves = bool(rcfg.get("headless", True))
                        _rodo_kwargs = _build_rodonaves_kwargs(
                            cfg=rcfg,
                            origem=origem,
                            destino=destino,
                            peso=peso,
                            valor=valor,
                            volumes=volumes,
                            cubagem_m3=cubagem_m3,
                            cubagens_validas=cubagens_validas,
                            cnpj_destinatario=cnpj_destinatario,
                            cep_origem=cep_origem,
                            headless_rodonaves=headless_rodonaves,
                            numero_destino=str(dados.get("numero_destino", "") or ""),
                        )
                        if _rodo_kwargs is not None:
                            provider = await _obter_provider_sessao(
                                "rodonaves",
                                create_kwargs={
                                    "dominio": str(rcfg.get("dominio", "RTE") or "RTE").strip(),
                                    "usuario": str(rcfg.get("usuario", "")).strip(),
                                    "senha": str(rcfg.get("senha", "")).strip(),
                                    "cnpj_pagador": _digits(str(rcfg.get("cnpj_pagador", "") or "")),
                                    "login_url": str(rcfg.get("login_url", "") or "").strip(),
                                    "cotacao_url": str(rcfg.get("cotacao_url", "") or "").strip(),
                                    "headless": headless_rodonaves,
                                },
                                desired_headless=headless_rodonaves,
                                log_label="RODONAVES",
                            )
                            tasks.append(("RODONAVES", provider, _rodo_kwargs))
        except Exception as e:
            _log_diag(f"Erro ao preparar RODONAVES: {e}")
            _reportar_erro_preparacao("RODONAVES", e)
            if chrome_missing_reported:
                return _resultado_chrome_ausente(e)
            erros_setup.append(ResultadoCotacao(transportadora="RODONAVES", status="erro", detalhes=str(e)))

    # Alfa
    try:
        if provider_factory.is_available("alfa"):
            alcfg = provider_factory.get_provider_config("alfa")
            if alcfg.get("habilitado", True):
                incompleta = _bloquear_config_incompleta("alfa")
                descricoes_itens = dados.get("descricoes_itens", [])
                if incompleta is not None:
                    erros_setup.append(incompleta)
                elif any("PICOLO" in d.upper() for d in descricoes_itens):
                    _log_diag("ALFA ignorada (item PICOLO encontrado no romaneio)")
                elif not _uf_atendida(alcfg.get("ufs_atendidas"), uf_destino):
                    erros_setup.append(_resultado_nao_atendido("ALFA", uf_destino))
                else:
                    headless_alfa = bool(alcfg.get("headless", False))
                    _alfa_kwargs = _build_alfa_kwargs(
                        cfg=alcfg,
                        origem=origem,
                        destino=destino,
                        peso=peso,
                        valor=valor,
                        volumes=volumes,
                        cubagem_m3=cubagem_m3,
                        cubagens_validas=cubagens_validas,
                        cnpj_destinatario=cnpj_destinatario,
                        cnpj_remetente=cnpj_remetente,
                        effective_config=effective_config,
                        headless_alfa=headless_alfa,
                    )
                    if _alfa_kwargs is not None:
                        login = str(alcfg.get("login", "") or "").strip()
                        provider = await _obter_provider_sessao(
                            "alfa",
                            create_kwargs={
                                "login": login,
                                "senha": str(alcfg.get("senha", "") or "").strip(),
                                "login_url": str(alcfg.get("login_url", "") or "").strip(),
                                "cotacao_url": str(alcfg.get("cotacao_url", "") or "").strip(),
                                "headless": headless_alfa,
                            },
                            desired_headless=headless_alfa,
                            log_label="ALFA",
                        )
                        tasks.append(("ALFA", provider, _alfa_kwargs))
    except Exception as e:
        _log_diag(f"Erro ao preparar ALFA: {e}")
        _reportar_erro_preparacao("ALFA", e)
        if chrome_missing_reported:
            return _resultado_chrome_ausente(e)
        erros_setup.append(ResultadoCotacao(transportadora="ALFA", status="erro", detalhes=str(e)))

    # COOPEX (SSW)
    try:
        if provider_factory.is_available("coopex"):
            cocfg = provider_factory.get_provider_config("coopex")
            if cocfg.get("habilitado", True):
                incompleta = _bloquear_config_incompleta("coopex")
                if incompleta is not None:
                    erros_setup.append(incompleta)
                elif not _uf_atendida(cocfg.get("ufs_atendidas"), uf_destino):
                    erros_setup.append(_resultado_nao_atendido("COOPEX", uf_destino))
                else:
                    cnpj_pagador_co = _resolver_documento_pagador(cocfg)
                    if not cnpj_pagador_co:
                        erros_setup.append(_resultado_documento_pagador_ausente("COOPEX"))
                    else:
                        foco_coopex = str(MODO_FOCO_TRANSPORTADORA).strip().lower() == "coopex"
                        headless_coopex = False if foco_coopex else bool(cocfg.get("headless", True))
                        _co_kwargs = _build_coopex_kwargs(
                            cfg=cocfg,
                            origem=origem,
                            destino=destino,
                            peso=peso,
                            valor=valor,
                            volumes=volumes,
                            cubagem_m3=cubagem_m3,
                            cubagens_validas=cubagens_validas,
                            cnpj_destinatario=cnpj_destinatario,
                            cnpj_pagador_co=cnpj_pagador_co,
                            cnpj_remetente=cnpj_remetente,
                            effective_config=effective_config,
                            headless_coopex=headless_coopex,
                        )
                        if _co_kwargs is not None:
                            provider = await _obter_provider_sessao(
                                "coopex",
                                create_kwargs={"headless": headless_coopex, "cnpj_pagador": cnpj_pagador_co},
                                desired_headless=headless_coopex,
                                log_label="COOPEX",
                            )
                            tasks.append(("COOPEX", provider, _co_kwargs))
    except Exception as e:
        _log_diag(f"Erro ao preparar COOPEX: {e}")
        _reportar_erro_preparacao("COOPEX", e)
        if chrome_missing_reported:
            return _resultado_chrome_ausente(e)
        erros_setup.append(ResultadoCotacao(transportadora="COOPEX", status="erro", detalhes=str(e)))

    # TRANSLOVATO
    try:
        if provider_factory.is_available("translovato"):
            tlcfg = provider_factory.get_provider_config("translovato")
            if tlcfg.get("habilitado", True):
                incompleta = _bloquear_config_incompleta("translovato")
                if incompleta is not None:
                    erros_setup.append(incompleta)
                elif not _uf_atendida(tlcfg.get("ufs_atendidas"), uf_destino):
                    erros_setup.append(_resultado_nao_atendido("TRANSLOVATO", uf_destino))
                else:
                    foco_translovato = str(MODO_FOCO_TRANSPORTADORA).strip().lower() == "translovato"
                    headless_translovato = False if foco_translovato else bool(tlcfg.get("headless", True))
                    _translovato_kwargs = _build_translovato_kwargs(
                        cfg=tlcfg,
                        origem=origem,
                        destino=destino,
                        peso=peso,
                        valor=valor,
                        volumes=volumes,
                        cubagem_m3=cubagem_m3,
                        cubagens_validas=cubagens_validas,
                        cnpj_destinatario=cnpj_destinatario,
                        cnpj_remetente=cnpj_remetente,
                        uf_destino=uf_destino,
                        cidade_destino=cidade_destino,
                        headless_translovato=headless_translovato,
                    )
                    if _translovato_kwargs is not None:
                        cnpj_tl = _digits(str(tlcfg.get("cnpj", "") or ""))
                        cnpj_rem_cfg = _digits(str(tlcfg.get("cnpj_remetente", "") or "")) or cnpj_tl
                        provider = await _obter_provider_sessao(
                            "translovato",
                            create_kwargs={
                                "headless": headless_translovato,
                                "cnpj_remetente": cnpj_rem_cfg,
                                "produto": str(tlcfg.get("produto", "CONFECCAO") or "CONFECCAO"),
                                "cotacao_url": str(tlcfg.get("cotacao_url", "") or "").strip(),
                            },
                            desired_headless=headless_translovato,
                            log_label="TRANSLOVATO",
                        )
                        if provider is not None:
                            tasks.append(("TRANSLOVATO", provider, _translovato_kwargs))
    except Exception as e:
        _log_diag(f"Erro ao preparar TRANSLOVATO: {e}")
        _reportar_erro_preparacao("TRANSLOVATO", e)
        if chrome_missing_reported:
            return _resultado_chrome_ausente(e)
        erros_setup.append(ResultadoCotacao(transportadora="TRANSLOVATO", status="erro", detalhes=str(e)))

    # Executa primeiro as transportadoras mais lentas para reduzir tempo total.
    # Maior número = tendência de maior duração (baseado em testes reais).
    tasks.sort(key=lambda t: _PRIORIDADE_LENTIDAO.get(str(t[0]).upper(), 0), reverse=True)

    # Cotações em paralelo (configurável, padrão 3)
    fb_cfg = effective_config.get("fretio", {}) if isinstance(effective_config, dict) else {}
    max_paralelo = max(1, min(7, int(fb_cfg.get("max_paralelo", 3) or 3)))
    nomes_tasks = ", ".join(nome for nome, _provider, _kwargs in tasks)
    _log_diag(f"Executando {len(tasks)} cotações em paralelo (máx {max_paralelo}): {nomes_tasks}")
    resultados: list[ResultadoCotacao] = []
    total_cotacoes = len(tasks) + len(erros_setup)
    concluidas = 0
    if total_cotacoes > 0:
        _emitir_progresso(concluidas=concluidas, total=total_cotacoes)
    for erro_setup in erros_setup:
        resultados.append(erro_setup)
        concluidas += 1
        _emitir_progresso(
            concluidas=concluidas,
            total=total_cotacoes,
            resultado=erro_setup,
            provider_status=provider_progress_from_resultado(erro_setup, stage=erro_setup.stage or "validacao"),
        )
    semaforo = asyncio.Semaphore(max_paralelo)

    async def _run_cotacao(i: int, nome: str, provider: Any, kwargs: dict[str, Any], is_alfa: bool):
        effective_timeout = _TIMEOUT_COTACAO_S.get(nome.upper(), _TIMEOUT_COTACAO_PADRAO_S)
        started_at = time.monotonic()
        use_quote_contract = _provider_supports_quote_request_cotar(provider)

        def _duration_ms() -> int:
            return int((time.monotonic() - started_at) * 1000)

        try:
            _emitir_status_provider(nome, stage="login", status="login", mensagem="Fazendo login")
            if getattr(provider, "_logged_in", False):
                _emitir_status_provider(nome, stage="cotacao", status="cotando", mensagem="Cotando frete")
            if use_quote_contract:
                quote_request = quote_request_from_legacy_kwargs(
                    kwargs,
                    uf_destino=uf_destino or "",
                    cnpj_destinatario=cnpj_destinatario,
                )
                cotar_started_at = time.monotonic()
                try:
                    retorno_provider = await asyncio.wait_for(
                        provider.cotar(quote_request),
                        timeout=effective_timeout,
                    )
                except (TypeError, NotImplementedError) as cotar_exc:
                    elapsed = max(0.0, time.monotonic() - cotar_started_at)
                    remaining_timeout = max(1.0, float(effective_timeout) - elapsed)
                    _log_diag(
                        f"{nome}: fallback para coteir após falha em cotar(request): "
                        f"{type(cotar_exc).__name__}: {cotar_exc}"
                    )
                    retorno_provider = await asyncio.wait_for(
                        provider.coteir(**kwargs),
                        timeout=remaining_timeout,
                    )
            else:
                retorno_provider = await asyncio.wait_for(
                    provider.coteir(**kwargs),
                    timeout=effective_timeout,
                )
            return CotacaoOutcome(
                i=i, nome=nome, provider=provider, kwargs=kwargs,
                cotacao=retorno_provider, duration_ms=_duration_ms(),
            )
        except asyncio.TimeoutError:
            last_step = getattr(provider, '_passo_atual', 'desconhecido')
            return CotacaoOutcome(
                i=i, nome=nome, provider=provider, kwargs=kwargs,
                erro=TimeoutError(
                    f"Timeout de {effective_timeout}s na cotação {nome} (passo: {last_step})"
                ),
                duration_ms=_duration_ms(),
            )
        except asyncio.CancelledError as exc:
            detalhe = str(exc).strip() or "sem detalhe"
            return CotacaoOutcome(
                i=i, nome=nome, provider=provider, kwargs=kwargs,
                erro=RuntimeError(f"Cotação {nome} cancelada: {detalhe}"),
                duration_ms=_duration_ms(),
            )
        except Exception as exc:
            return CotacaoOutcome(
                i=i, nome=nome, provider=provider, kwargs=kwargs,
                erro=exc, duration_ms=_duration_ms(),
            )

    async def _exec(i: int, nome: str, provider: Any, kwargs: dict[str, Any]):
        is_alfa = nome.upper() == "ALFA"
        # `async with semaforo` garante release mesmo se a Task for cancelada
        # exatamente entre o retorno de acquire() e o try interno — janela
        # cancellation-unsafe que existia no padrão acquire/try/finally.
        # ALFA continua fora do semáforo (login manual com Turnstile).
        if is_alfa:
            return await _run_cotacao(i, nome, provider, kwargs, is_alfa)
        async with semaforo:
            _log_diag(f"Semáforo adquirido: {nome} (posição {i})")
            return await _run_cotacao(i, nome, provider, kwargs, is_alfa)

    def _processar_resultado(res, resultados, falhas_para_retry):
        """Despacha um CotacaoOutcome para o handler da sua forma de resultado."""

        def _finalizar(r):
            nonlocal concluidas
            concluidas += 1
            resultados.append(r)
            _emitir_progresso(
                concluidas=concluidas,
                total=total_cotacoes,
                resultado=r,
                provider_status=provider_progress_from_resultado(r, stage="resultado"),
            )

        if not isinstance(res, CotacaoOutcome):
            msg = f"Executor retornou formato inesperado de resultado: {type(res).__name__}"
            _log_diag(msg)
            r = ResultadoCotacao(transportadora="GERAL", status="erro", detalhes=msg)
            _finalizar(r)
            return

        nome_task = res.nome
        provider_task = res.provider
        kwargs_task = res.kwargs
        cotacao = res.cotacao
        erro = res.erro
        duration_ms = res.duration_ms

        def _handle_exception():
            erro_str = str(erro)
            # Erros de negócio não devem ser reportados nem gerar retry
            if _is_business_error(erro_str):
                _log_diag(f"{nome_task}: destino não atendido (erro de negócio, ignorando)")
                r = ResultadoCotacao(
                    transportadora=nome_task, status="nao_atendido", detalhes=erro_str,
                    duration_ms=duration_ms,
                )
                _finalizar(r)
                return
            tb = ''.join(traceback.format_exception(type(erro), erro, erro.__traceback__))
            _log_diag(f"Erro em cotação {nome_task}: {type(erro).__name__}: {erro}\n{tb}")
            if sessao is not None:
                sessao.record_quote_failure(nome_task)
            # Falhas transitórias de provider (timeout, rede, browser fechado) são esperadas
            # e não devem poluir a API de erros com ruído técnico.
            if not _is_expected_transient_failure(erro):
                report_provider_error(
                    nome_task,
                    getattr(provider_task, "_passo_atual", "") or "enviar_cotacao",
                    f"{type(erro).__name__}: {erro}",
                    exception=erro,
                    context={
                        **_diagnostico_erro_cotacao(
                            nome_task,
                            getattr(provider_task, "_passo_atual", "") or "submeter_cotacao",
                            provider=provider_task,
                            kwargs=kwargs_task,
                            error=erro,
                            last_error=erro_str,
                            duration_ms=duration_ms,
                        ),
                        "source": "cotacao_usuario",
                        "carrier_enabled": True,
                        "browser_state": {
                            "passo_atual": getattr(provider_task, "_passo_atual", None),
                            "logged_in": getattr(provider_task, "_logged_in", None),
                            "headless": getattr(provider_task, "headless", None),
                        },
                    },
                )
            if falhas_para_retry is not None:
                falhas_para_retry.append((nome_task, provider_task, kwargs_task))
                _log_diag(f"{nome_task} enfileirada para retry após as demais completarem")
            else:
                r = ResultadoCotacao(
                    transportadora=nome_task, status="erro",
                    detalhes=f"{type(erro).__name__}: {erro}",
                    duration_ms=duration_ms,
                )
                _finalizar(r)
            return

        def _handle_erro_simples():
            erro_str = str(erro)
            # Erros de negócio não devem ser reportados nem gerar retry
            if _is_business_error(erro_str):
                _log_diag(f"{nome_task}: destino não atendido (erro de negócio, ignorando)")
                r = ResultadoCotacao(
                    transportadora=nome_task, status="nao_atendido", detalhes=erro_str,
                    duration_ms=duration_ms,
                )
                _finalizar(r)
                return
            _log_diag(f"Erro em cotação {nome_task}: {erro}")
            if falhas_para_retry is not None:
                falhas_para_retry.append((nome_task, provider_task, kwargs_task))
                _log_diag(f"{nome_task} enfileirada para retry após as demais completarem")
            else:
                r = ResultadoCotacao(
                    transportadora=nome_task, status="erro", detalhes=str(erro),
                    duration_ms=duration_ms,
                )
                _finalizar(r)
            return

        def _handle_quote_response():
            nonlocal concluidas
            quote_response = cotacao
            if quote_response.duration_ms is None:
                quote_response.duration_ms = duration_ms
            if not quote_response.provider:
                quote_response.provider = nome_task
            try:
                r = quote_response_to_resultado_cotacao(
                    quote_response,
                    resultado_cls=ResultadoCotacao,
                )
            except Exception as parse_exc:
                _log_diag(f"QuoteResponse inválido em {nome_task}: {parse_exc}")
                r = ResultadoCotacao(
                    transportadora=nome_task,
                    status="erro",
                    detalhes=f"QuoteResponse inválido: {parse_exc}",
                    duration_ms=duration_ms,
                )
            resultados.append(r)
            concluidas += 1
            if r.status == "ok":
                if sessao is not None:
                    sessao.record_quote_success(nome_task)
                try:
                    _log_diag(
                        f"✅ {r.transportadora}: R$ {float(r.valor_frete or 0.0):.2f} - "
                        f"{int(r.prazo_dias or 0)} dias"
                    )
                except Exception:
                    _log_diag(f"✅ {r.transportadora}: cotação concluída")
            else:
                _log_diag(
                    f"{r.transportadora} retornou status {r.status}: "
                    f"{r.detalhes or 'sem detalhes'}"
                )
                if r.status == "erro":
                    if sessao is not None:
                        sessao.record_quote_failure(nome_task)
                    response_stage = quote_response.stage or getattr(provider_task, "_passo_atual", None) or "ler_resultado"
                    diagnostic = _diagnostico_erro_cotacao(
                        nome_task,
                        response_stage,
                        provider=provider_task,
                        kwargs=kwargs_task,
                        last_error=r.detalhes or quote_response.error_code,
                        duration_ms=duration_ms,
                    )
                    provider_ctx = diagnostic.get("provider_context")
                    if isinstance(provider_ctx, dict) and quote_response.error_code:
                        provider_ctx["error_type"] = quote_response.error_code
                    report_provider_error(
                        nome_task,
                        response_stage,
                        f"{nome_task} retornou erro: {r.detalhes or quote_response.error_code or 'sem detalhes'}",
                        context={
                            **diagnostic,
                            "source": "cotacao_usuario",
                            "carrier_enabled": True,
                            "last_error": r.detalhes,
                            "browser_state": {
                                "passo_atual": getattr(provider_task, "_passo_atual", None),
                                "logged_in": getattr(provider_task, "_logged_in", None),
                                "headless": getattr(provider_task, "headless", None),
                            },
                        },
                    )
            _emitir_progresso(
                concluidas=concluidas,
                total=total_cotacoes,
                resultado=r,
                provider_status=provider_progress_from_resultado(
                    r,
                    stage=quote_response.stage or r.stage or "resultado",
                    duration_ms=duration_ms,
                ),
            )
            return

        def _handle_legacy():
            nonlocal concluidas
            try:
                transportadora = str(getattr(cotacao, "transportadora", nome_task))
                valor_frete = float(getattr(cotacao, "valor_frete", 0.0))
                prazo_dias = int(getattr(cotacao, "prazo_dias", 0))
                detalhes = getattr(cotacao, "restricoes", None)
            except Exception as parse_exc:
                _log_diag(f"Resultado inválido em {nome_task}: {parse_exc}")
                r = ResultadoCotacao(
                    transportadora=nome_task, status="erro",
                    detalhes=f"Resultado inválido: {parse_exc}",
                    duration_ms=duration_ms,
                )
                _finalizar(r)
                return

            r = ResultadoCotacao(
                transportadora=transportadora, status="ok",
                valor_frete=valor_frete, prazo_dias=prazo_dias, detalhes=detalhes,
                duration_ms=duration_ms,
            )
            if sessao is not None:
                sessao.record_quote_success(nome_task)
            resultados.append(r)
            concluidas += 1
            _log_diag(f"✅ {transportadora}: R$ {valor_frete:.2f} - {prazo_dias} dias")
            _emitir_progresso(
                concluidas=concluidas,
                total=total_cotacoes,
                resultado=r,
                provider_status=provider_progress_from_resultado(r, stage="resultado"),
            )

        def _handle_none():
            detalhe = None
            if provider_task is not None:
                detalhe = getattr(provider_task, "last_error", None)
            if detalhe:
                _log_diag(f"{nome_task} retornou None: {detalhe}")
            else:
                _log_diag(f"{nome_task} retornou None (sem resultado)")
                detalhe = "Sem resultado"
            # Erros de negócio (destino não atendido) são normais:
            # não reportar, não fazer retry, apenas registrar como "não atendido"
            if _is_business_error(detalhe):
                _log_diag(f"{nome_task}: destino não atendido (erro de negócio, ignorando)")
                r = ResultadoCotacao(
                    transportadora=nome_task, status="nao_atendido", detalhes=str(detalhe),
                    duration_ms=duration_ms,
                )
                _finalizar(r)
                return

            # Falhas transitórias de rede/browser capturadas internamente pelo provider
            # (retornaram None em vez de levantar exceção) — não reportar à API,
            # mas agendar retry exatamente como fazemos para exceções transitórias.
            if _is_expected_transient_failure_str(detalhe or ""):
                _log_diag(f"{nome_task} falha transitória (sem report): {detalhe}")
                if falhas_para_retry is not None:
                    falhas_para_retry.append((nome_task, provider_task, kwargs_task))
                    _log_diag(f"{nome_task} enfileirada para retry (transitória)")
                else:
                    r = ResultadoCotacao(
                        transportadora=nome_task, status="erro", detalhes=str(detalhe),
                        duration_ms=duration_ms,
                    )
                    _finalizar(r)
                return

            # Normaliza a mensagem removendo partes variáveis (ex: paths de diagnóstico TRD)
            # para que o rate-limiter do error_reporter deduplique corretamente entre execuções.
            if sessao is not None:
                sessao.record_quote_failure(nome_task)
            detalhe_report = re.sub(r'\s*\(diagnóstico salvo em:[^)]*\)', '', str(detalhe or "")).strip()
            if not detalhe_report:
                detalhe_report = str(detalhe or "Sem resultado")
            report_provider_error(
                nome_task,
                getattr(provider_task, "_passo_atual", "") or "interpretar_resultado",
                f"{nome_task} retornou None: {detalhe_report}",
                context={
                    **_diagnostico_erro_cotacao(
                        nome_task,
                        getattr(provider_task, "_passo_atual", "") or "ler_resultado",
                        provider=provider_task,
                        kwargs=kwargs_task,
                        last_error=detalhe,
                        duration_ms=duration_ms,
                    ),
                    "source": "cotacao_usuario",
                    "carrier_enabled": True,
                    "last_error": detalhe,
                    "browser_state": {
                        "passo_atual": getattr(provider_task, "_passo_atual", None),
                        "logged_in": getattr(provider_task, "_logged_in", None),
                        "headless": getattr(provider_task, "headless", None),
                    },
                },
            )
            if falhas_para_retry is not None:
                falhas_para_retry.append((nome_task, provider_task, kwargs_task))
                _log_diag(f"{nome_task} enfileirada para retry após as demais completarem")
            else:
                r = ResultadoCotacao(
                    transportadora=nome_task, status="erro", detalhes=str(detalhe),
                    duration_ms=duration_ms,
                )
                _finalizar(r)

        # Dispatch por forma do resultado. _handle_erro_simples cobre o caminho
        # defensivo de `erro` truthy não-exceção (nenhum producer de _run_cotacao
        # gera isso hoje, mas é preservado).
        if isinstance(erro, BaseException):
            _handle_exception()
            return
        if erro is not None:
            _handle_erro_simples()
            return
        if isinstance(cotacao, QuoteResponse):
            _handle_quote_response()
            return
        if cotacao is not None:
            _handle_legacy()
        else:
            _handle_none()

    # ── Rodada 1: executa todas as cotações ──
    falhas_para_retry: list[tuple[str, Any, dict[str, Any]]] = []
    futuros = []
    for i, (nome, prov, kwargs) in enumerate(tasks):
        t = asyncio.create_task(_exec(i, nome, prov, kwargs))
        futuros.append(t)

    for fut in asyncio.as_completed(futuros):
        try:
            res = await fut
            _processar_resultado(res, resultados, falhas_para_retry)
        except Exception as loop_exc:
            tb = ''.join(traceback.format_exception(type(loop_exc), loop_exc, loop_exc.__traceback__))
            _log_diag(f"Falha ao processar resultado de cotação: {loop_exc}\n{tb}")
            concluidas += 1
            r = ResultadoCotacao(
                transportadora="GERAL", status="erro",
                detalhes=f"Falha interna ao processar cotação: {loop_exc}",
            )
            resultados.append(r)
            _emitir_progresso(concluidas=concluidas, total=total_cotacoes, resultado=r)

    # ── Rodada 2: retry das que falharam (máx 1 retry, sem enfileirar de novo) ──
    if falhas_para_retry:
        nomes_retry = ", ".join(n for n, _, _ in falhas_para_retry)
        total_cotacoes += len(falhas_para_retry)
        _log_diag(f"Retentando {len(falhas_para_retry)} cotação(ões) que falharam: {nomes_retry}")
        _emitir_progresso(concluidas=concluidas, total=total_cotacoes)

        futuros_retry = []
        for i, (nome, prov, kwargs) in enumerate(falhas_para_retry):
            t = asyncio.create_task(_exec(i, nome, prov, kwargs))
            futuros_retry.append(t)

        for fut in asyncio.as_completed(futuros_retry):
            try:
                res = await fut
                _processar_resultado(res, resultados, None)  # None = não enfileira de novo
            except Exception as loop_exc:
                tb = ''.join(traceback.format_exception(type(loop_exc), loop_exc, loop_exc.__traceback__))
                _log_diag(f"Falha ao processar retry de cotação: {loop_exc}\n{tb}")
                concluidas += 1
                r = ResultadoCotacao(
                    transportadora="GERAL", status="erro",
                    detalhes=f"Falha interna no retry: {loop_exc}",
                )
                resultados.append(r)
                _emitir_progresso(concluidas=concluidas, total=total_cotacoes, resultado=r)

    # Cleanup de providers criados ad-hoc (quando sessao=None)
    if sessao is None and tasks:
        async def _cleanup_adhoc(nome: str, prov):
            try:
                await asyncio.wait_for(prov.cleanup(), timeout=8)
                _log_diag(f"Cleanup ad-hoc {nome} OK")
            except Exception as e:
                _log_diag(f"Cleanup ad-hoc {nome} falhou: {e}")
        cleanup_tasks = [_cleanup_adhoc(n, p) for n, p, _ in tasks]
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    validas = [r for r in resultados if r.status == "ok" and r.valor_frete is not None]
    _log_diag(f"Cotações válidas: {len(validas)} de {len(tasks)}")

    return resultados



__all__ = [name for name in globals() if not name.startswith("__")]
