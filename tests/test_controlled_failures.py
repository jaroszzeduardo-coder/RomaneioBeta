"""Classificação de falhas operacionais que NÃO devem virar issue no servidor.

Cobre os ajustes feitos para as issues `erro-automatico` recorrentes:
- Rodonaves "valor de frete não foi encontrado" / reCAPTCHA / antifraude / jQuery
- TRD "valor não encontrado no resultado"
- Pré-login (Coopex/Translovato/Braspress) "Login falhou" e credenciais inválidas
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))

from cotacao.orchestrator import _is_expected_transient_failure_str
from cotacao.error_context import is_expected_prelogin_failure


class TestTransientResultFailures:
    def test_rodonaves_valor_nao_foi_encontrado(self):
        # Mensagem real da issue #79 — antes não casava por causa do "foi".
        detalhe = (
            "Rodonaves: resposta da API/portal recebida, mas valor de frete não "
            "foi encontrado (stage=ler_resultado; headless=False)"
        )
        assert _is_expected_transient_failure_str(detalhe)

    def test_rodonaves_recaptcha_antifraude(self):
        detalhe = "Rodonaves: reCAPTCHA não resolvido ou bloqueio antifraude impediu a cotação"
        assert _is_expected_transient_failure_str(detalhe)

    def test_braspress_captcha_nao_resolvido(self):
        detalhe = "Login falhou: captcha da Braspress não resolvido automaticamente."
        assert _is_expected_transient_failure_str(detalhe)
        assert is_expected_prelogin_failure(detalhe)

    def test_rodonaves_jquery_nao_carregou(self):
        detalhe = "Login Rodonaves falhou — jQuery não carregou (URL: https://cliente.rte.com.br)"
        assert _is_expected_transient_failure_str(detalhe)

    def test_trd_valor_nao_encontrado_no_resultado(self):
        assert _is_expected_transient_failure_str("Valor não encontrado no resultado TRD")

    def test_portal_nao_retornou_resultado(self):
        detalhe = "Rodonaves: portal não retornou resultado de cotação dentro do tempo esperado"
        assert _is_expected_transient_failure_str(detalhe)

    def test_erro_real_de_codigo_ainda_eh_reportado(self):
        # Um AttributeError/KeyError genuíno não deve ser silenciado.
        assert not _is_expected_transient_failure_str("AttributeError: 'NoneType' object has no attribute 'fill'")
        assert not _is_expected_transient_failure_str("")


class TestPreloginControlledFailures:
    def test_coopex_login_falhou(self):
        detalhe = "COOPEX retornou None: Erro na cotação: Login falhou, URL: https://sistema.ssw.inf.br/bin/ssw0422"
        assert is_expected_prelogin_failure(detalhe)

    def test_translovato_portal_nao_confirmou_acesso(self):
        assert is_expected_prelogin_failure("Login falhou ou portal não confirmou acesso")

    def test_credenciais_invalidas(self):
        assert is_expected_prelogin_failure("Login Rodonaves falhou — Usuário ou senha inválido")

    def test_timeout_e_rede(self):
        assert is_expected_prelogin_failure("Timeout aguardando campo de login")
        assert is_expected_prelogin_failure("net::ERR_CONNECTION_RESET")

    def test_erro_inesperado_nao_silenciado(self):
        assert not is_expected_prelogin_failure("TypeError: argument must be str")
        assert not is_expected_prelogin_failure("")
