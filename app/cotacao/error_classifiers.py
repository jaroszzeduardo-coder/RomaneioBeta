"""Classificadores de erro de cotação — fonte única.

Centraliza os três conjuntos de padrões que antes viviam espalhados entre
``orchestrator.py`` (negócio/transitório) e ``error_context.py`` (pré-login):

  * ``_is_business_error``                  — destino não atendido / rota fora de
    cobertura (normal, não reportar, não fazer retry).
  * ``_is_expected_transient_failure(_str)`` — timeouts, rede, browser fechado,
    portal sem valor/antifraude (operacionais, não bugs).
  * ``is_expected_prelogin_failure``        — falhas de pré-login operacionais
    (credenciais do cliente, portal lento) que não viram issue no servidor.

São funções puras, sem dependência de outros módulos do pacote, para que
``orchestrator`` e ``error_context`` importem daqui sem ciclo de import.
"""

from __future__ import annotations


_BUSINESS_PATTERNS = (
    "destino fora da cobertura",
    "cepdestino não atendido",
    "cep destino não atendido",
    "não atendemos esse cep",
    "destino possivelmente não atendido",
    "destino possìvelmente não atendido",
    "rota não atendida",
    "cidade de destino",
    "transportadora não atende",
    "transportadora n o atende",
    "cidade de destino n o",
    "n o atendida",
    "não atendido",
    "nao atendido",
    "fora de cobertura",
    "fora da cobertura",
    "não atendemos",
    "cepnão atendemos",
    "sem precificação automática no ssw",
    "sem precificacao automatica no ssw",
    "não cadastrada",
    "nao cadastrada",
    "rota:",
)


def _is_business_error(detail: str) -> bool:
    """Detecta erros de negócio (destino não atendido, rota fora de cobertura).

    Esses erros são normais e não devem ser reportados nem gerar retry."""
    if not detail:
        return False
    d = str(detail).lower()
    return any(p in d for p in _BUSINESS_PATTERNS)


_TRANSIENT_PATTERNS = (
    "target page, context or browser has been closed",
    "target closed",
    "frame was detached",
    "net::err_aborted",
    "net::err_connection",
    "net::err_name",
    "net::err_timed_out",
    "net::err_internet",
    "net::err_network",
    "formulário de cotação não carregou",
    "formulario de cotacao nao carregou",
    "page.goto",
    "valor de frete nao encontrado",
    "valor de frete não encontrado",
    # Variantes reais retornadas pelos portais ("não foi encontrado", parsing do
    # resultado falhou, portal não devolveu cotação) — operacionais, não bugs.
    "valor de frete nao foi encontrado",
    "valor de frete não foi encontrado",
    "valor não encontrado no resultado",
    "valor nao encontrado no resultado",
    "portal não retornou resultado",
    "portal nao retornou resultado",
    # Antifraude / captcha do portal e portal que não terminou de carregar.
    "recaptcha não resolvido",
    "recaptcha nao resolvido",
    "captcha da braspress",
    "verificação automática de robô",
    "verificacao automatica de robo",
    "recaptcha do login",
    "bloqueio antifraude",
    "jquery não carregou",
    "jquery nao carregou",
    "timeout aguardando resultado",
)


def _is_expected_transient_failure(erro: BaseException) -> bool:
    """Detecta falhas transitórias esperadas de provider que NÃO devem ir para report_error.

    Timeouts do provider e erros de rede/browser são falhas controladas — não bugs no código."""
    if isinstance(erro, TimeoutError):
        return True
    err_str = str(erro).lower()
    return any(p in err_str for p in _TRANSIENT_PATTERNS)


def _is_expected_transient_failure_str(detail: str) -> bool:
    """Mesmos critérios de _is_expected_transient_failure, mas para strings de last_error.

    Usado quando o provider capturou a exceção internamente e retornou None."""
    if not detail:
        return False
    d = detail.lower()
    if "timeout" in d or "timed out" in d:
        return True
    return any(p in d for p in _TRANSIENT_PATTERNS)


# Falhas de pré-login que são operacionais (credenciais do cliente, portal não
# liberou acesso, rede/portal lentos) e NÃO devem virar issue no servidor. O
# pré-login é proativo/best-effort: se o problema for real (ex.: portal mudou o
# formulário), a cotação do usuário ainda exercita o login e reporta por lá.
_PRELOGIN_CONTROLLED_PATTERNS = (
    "login falhou",
    "falha no login",
    "falha de login",
    "usuário ou senha",
    "usuario ou senha",
    "senha inválida",
    "senha invalida",
    "senha incorreta",
    "credenciais inválidas",
    "credenciais invalidas",
    "acesso negado",
    "portal não confirmou acesso",
    "portal nao confirmou acesso",
    "não confirmou acesso",
    "nao confirmou acesso",
    "jquery não carregou",
    "jquery nao carregou",
    "captcha da braspress",
    "verificação automática de robô",
    "verificacao automatica de robo",
    "recaptcha do login",
    "timeout",
    "timed out",
    "net::err",
    "err_connection",
    "err_name",
    "err_timed_out",
)


def is_expected_prelogin_failure(detail: str) -> bool:
    """True se a falha de pré-login é operacional/esperada (não reportar)."""
    if not detail:
        return False
    text = str(detail).lower()
    return any(pattern in text for pattern in _PRELOGIN_CONTROLLED_PATTERNS)
