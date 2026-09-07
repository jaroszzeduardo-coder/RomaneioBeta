"""Provider COOPEX - Sistema SSW de Transportes (mesmo portal da Eucatur)."""
from datetime import datetime
from typing import Optional
import re
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError
from fretio.providers.base import ProviderBase
from fretio.providers.provider_utils import _sanitize_ssw_xml_excerpt
from fretio.models import Cotacao
from fretio.quotation_contract import QuoteRequest, QuoteResponse
from fretio.logging_conf import get_logger

logger = get_logger(__name__)

_CUBAGEM_AUSENTE_MSG = (
    "Cubagem do romaneio não encontrada. Verifique se o romaneio possui cubagem "
    "calculada antes de cotar com COOPEX/EUCATUR."
)


class CoopexProvider(ProviderBase):
    """Provider COOPEX via portal SSW (sistema.ssw.inf.br)."""

    LOGIN_URL = "https://sistema.ssw.inf.br/bin/ssw0422"
    COTACAO_URL = "https://sistema.ssw.inf.br/bin/ssw1608"

    def __init__(
        self,
        dominio: str,
        usuario: str,
        senha: str,
        cnpj_pagador: str = "",
        usar_cache: bool = True,
        headless: bool = True,
    ):
        super().__init__(nome="COOPEX")
        self.dominio = dominio
        self.usuario = usuario
        self.senha = senha
        self.cnpj_pagador = re.sub(r"\D", "", str(cnpj_pagador or ""))
        self.headless = headless
        self.last_error: str | None = None
        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None
        self._logged_in = False
        self._passo_atual: str = "inicio"

    @staticmethod
    def _is_temporary_network_error(error: Exception | str) -> bool:
        text = str(error or "").upper()
        return any(
            token in text
            for token in (
                "ERR_NAME_NOT_RESOLVED",
                "ERR_CONNECTION",
                "ERR_TIMED_OUT",
                "ERR_NETWORK_CHANGED",
                "ERR_INTERNET_DISCONNECTED",
                "NET::",
                "TIMEOUT",
            )
        )

    async def _init_browser(self):
        """Inicializa browser Playwright."""
        self._passo_atual = "init_browser"
        if self._browser:
            if self._browser.is_connected():
                return
            logger.warning(f"[{self.nome}] Browser desconectado, reinicializando...")
            await self.cleanup()

        from fretio.providers.base import launch_browser_resilient
        self._browser = await launch_browser_resilient(
            headless=self.headless,
            args=['--disable-blink-features=AutomationControlled'],
        )
        self._context = await self._browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        from fretio.providers.provider_utils import apply_performance_route_filters
        await apply_performance_route_filters(self._context)
        self._page = await self._context.new_page()

    async def _login(self):
        """Faz login no portal SSW."""
        self._passo_atual = "login"
        if self._logged_in:
            return
        try:
            logger.info(f"[{self.nome}] Fazendo login no SSW...")
            await self._page.goto(self.LOGIN_URL, wait_until='domcontentloaded', timeout=60000)
            await self._page.locator('input[name=f1]').wait_for(state="visible", timeout=15000)

            await self._page.locator('input[name=f1]').fill(self.dominio)
            await self._page.locator('input[name=f3]').fill(self.usuario)
            await self._page.locator('input[name=f4]').fill(self.senha)
            await self._page.locator('a:has-text("►")').click()

            # Aguarda redirecionamento pós-login (polling reativo até 10s)
            self._passo_atual = "aguardando_login"
            login_ok = False
            for _wait in range(100):
                await self._page.wait_for_timeout(100)
                url = self._page.url
                if 'ssw0422' not in url or 'menu' in url:
                    login_ok = True
                    break
            if not login_ok:
                raise Exception(f"Login falhou, URL: {self._page.url}")

            logger.info(f"[{self.nome}] Login OK")
            self._logged_in = True
        except Exception as e:
            if self._is_temporary_network_error(e):
                self.last_error = f"Erro temporário de rede/DNS no login SSW: {e}"
                logger.warning(f"[{self.nome}] {self.last_error}")
            else:
                self.last_error = f"Erro no login: {e}"
                logger.error(f"[{self.nome}] {self.last_error}")
            raise

    async def pre_login(self):
        """Inicializa browser e faz login antecipadamente."""
        await self._init_browser()
        await self._login()

    async def cleanup(self):
        """Fecha o browser."""
        try:
            if self._page and not self._page.is_closed():
                await self._page.close()
        except Exception:
            pass
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._browser = None
        self._context = None
        self._page = None
        self._logged_in = False

    @staticmethod
    def _normalizar_cubagens_cm(cubagens: Optional[list[dict]]) -> list[dict]:
        validas: list[dict] = []
        if not isinstance(cubagens, list):
            return validas
        for row in cubagens:
            if not isinstance(row, dict):
                continue
            try:
                qtd = int(row.get("quantidade", 0) or 0)
                comp = int(row.get("comprimento_cm", 0) or 0)
                larg = int(row.get("largura_cm", 0) or 0)
                alt = int(row.get("altura_cm", 0) or 0)
            except Exception:
                continue
            if qtd <= 0 or comp <= 0 or larg <= 0 or alt <= 0:
                continue
            validas.append(
                {
                    "quantidade": qtd,
                    "comprimento_cm": comp,
                    "largura_cm": larg,
                    "altura_cm": alt,
                }
            )
        return validas

    async def _navegar_cotacao(self):
        """Navega para a tela de cotação (ssw1608)."""
        self._passo_atual = "navegando_cotacao"
        try:
            await self._page.goto(self.COTACAO_URL, wait_until='domcontentloaded', timeout=60000)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("COOPEX: timeout temporário de rede/portal ao abrir tela SSW de cotação") from exc
        await self._page.wait_for_timeout(800)

        has_form = await self._page.locator('input').count()
        if has_form == 0:
            logger.warning(f"[{self.nome}] Formulário não encontrado, tentando re-login...")
            self._logged_in = False
            await self._login()
            try:
                await self._page.goto(self.COTACAO_URL, wait_until='domcontentloaded', timeout=60000)
            except PlaywrightTimeoutError as exc:
                raise RuntimeError("COOPEX: timeout temporário de rede/portal ao reabrir tela SSW de cotação") from exc
            await self._page.wait_for_timeout(800)
            has_form = await self._page.locator('input').count()
            if has_form == 0:
                raise Exception("Formulário de cotação não carregou mesmo após re-login")

        logger.info(f"[{self.nome}] Tela de cotação carregada")

    async def _preencher_cotacao(self, origem: str, destino: str, peso: float, valor: float,
                                volumes: int = 1, cubagem_m3: float = 0.0,
                                comprimento_cm: int = 0, largura_cm: int = 0, altura_cm: int = 0,
                                cnpj_remetente: str = "", cnpj_destinatario: str = "",
                                cubagens: Optional[list[dict]] = None,
                                cnpj_pagador: str = "", tipo_frete: str = "1"):
        """Preenche o formulário de cotação SSW via JavaScript."""
        self._passo_atual = "preenchendo_formulario"
        page = self._page
        cnpj_pagador = (cnpj_pagador or self.cnpj_pagador).replace('.', '').replace('/', '').replace('-', '').strip()
        if len(cnpj_pagador) != 14:
            raise ValueError("cnpj_pagador é obrigatório para cotação Coopex")

        cnpj_rem = cnpj_remetente.replace('.', '').replace('/', '').replace('-', '').strip()
        cnpj_dest = cnpj_destinatario.replace('.', '').replace('/', '').replace('-', '').strip()
        cep_orig = origem.replace('-', '').strip()
        cep_dest = destino.replace('-', '').strip()
        valor_fmt = f"{valor:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
        peso_fmt = f"{peso:.3f}".replace('.', ',')
        cubagem_fmt = f"{cubagem_m3:.4f}".replace('.', ',') if cubagem_m3 > 0 else ""

        self._passo_atual = "preenchendo_campos_dinamicos"

        # Injetamos uma função inteligente que encontra os inputs por Evento (onchange/onblur), Label (div.texto) ou nome (fallback)
        await page.evaluate(f'''() => {{
            const setSSWField = (labelText, val, fallbackNames, callFuncName, rawCall) => {{
                let el = null;
                const inputs = Array.from(document.querySelectorAll('input, select'));
                
                // Busca por nome de função (onblur/onchange)
                if (callFuncName) {{
                    el = inputs.find(i => {{
                        const ob = i.getAttribute('onblur') || '';
                        const oc = i.getAttribute('onchange') || '';
                        return ob.includes(callFuncName + '(') || oc.includes(callFuncName + '(');
                    }});
                }}
                
                // Busca por Label visual
                if (!el && labelText) {{
                    const divs = Array.from(document.querySelectorAll('div.texto'));
                    const div = divs.find(d => d.innerText.trim().toUpperCase().includes(labelText.toUpperCase()));
                    if (div) {{
                        let next = div.nextElementSibling;
                        while(next && next.tagName !== 'INPUT' && next.tagName !== 'SELECT') {{
                            next = next.nextElementSibling;
                        }}
                        if (next && (next.tagName === 'INPUT' || next.tagName === 'SELECT')) el = next;
                    }}
                }}
                
                // Busca por Fallback
                if (!el && fallbackNames) {{
                    for(let n of fallbackNames) {{
                        el = document.querySelector(`[name="${{n}}"]`);
                        if (el) break;
                    }}
                }}

                if (el) {{
                    el.value = val;
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                }}
                
                if (rawCall) {{
                    try {{ eval(rawCall); }} catch(e) {{}}
                }}
            }};

            // Preenche os campos cruciais com essa heurística imune à adição de novos campos!
            setSSWField('Pagador', '{cnpj_pagador}', ['cgc_pag', 'f2', 'f3'], 'pag', "if(typeof pag==='function') pag('{cnpj_pagador}');");
            setSSWField('CEP Orig', '{cep_orig}', ['cep_origem', 'f6', 'f7'], 'ce2', "if(typeof ce2==='function') ce2('{cep_orig}');");
            setSSWField('CEP Dest', '{cep_dest}', ['cep_destino', 'f8', 'f9'], 'cep', "if(typeof cep==='function') cep('{cep_dest}');");
            
            // CIF/FOB (1 ou 2)
            setSSWField('Frete', '{tipo_frete}', ['tp_frete', 'f9', 'f10'], 'f_c', "if(typeof f_c==='function') f_c('{tipo_frete}');");
            
            // Flags
            setSSWField('Coletar', 'S', ['coletar', 'f10', 'f11'], null, null);
            setSSWField('Contribuinte', 'S', ['contribuinte', 'f13', 'f14'], null, null);
            setSSWField('Entrega Dif', 'N', ['ent_dif', 'f14', 'f15'], null, null);
            setSSWField('Mercadoria', '001', ['tp_merc', 'f4', 'f5'], null, null);
            
            // Remetente (cgc_rem)
            setSSWField('Remetente', '{cnpj_rem}', ['cgc_rem'], null, "if(typeof cgc==='function') cgc('rem');");
            
            // Destinatário
            if ('{cnpj_dest}'.length === 14) {{
                setSSWField('Destinatário', '{cnpj_dest}', ['cgc_dest', 'f12', 'f13'], null, "if(typeof cgc==='function') cgc('des');");
            }}

            // Valores de carga
            setSSWField('Valor NF', '{valor_fmt}', ['vlr_merc', 'f15', 'f16'], null, null); // Usar 'Valor NF' evita achar 'Valor Adicional'
            setSSWField('Volumes', '{volumes}', ['qtde_vol', 'f16', 'f17'], null, null);
            setSSWField('Peso', '{peso_fmt}', ['peso', 'f18', 'f19'], null, null);
            setSSWField('Pares', '', ['qtde_pares', 'f17', 'f18'], null, null); // limpa pares

            if ('{cubagem_fmt}') {{
                setSSWField('M3', '{cubagem_fmt}', ['cubagem', 'cubagem_m3', 'f20', 'f21'], null, null);
            }}
        }}''')
        await page.wait_for_timeout(500)

        # Preencher cubagem por volume (campos cuba1..cuba11)
        # Agrupa caixas de mesmo tamanho para economizar campos
        cubagens_validas = self._normalizar_cubagens_cm(cubagens)
        if cubagens_validas:
            # Agrupa por dimensão: {"50x40x30": quantidade_total}
            grupos: dict[str, int] = {}
            for cub in cubagens_validas:
                chave = f'{int(cub["comprimento_cm"])}x{int(cub["largura_cm"])}x{int(cub["altura_cm"])}'
                grupos[chave] = grupos.get(chave, 0) + int(cub["quantidade"])

            # Expande os grupos em valores individuais
            cuba_values: list[str] = []
            for dim, qtd in grupos.items():
                for _ in range(qtd):
                    cuba_values.append(dim)

            if len(cuba_values) <= 11:
                for i, cuba_value in enumerate(cuba_values, start=1):
                    await page.evaluate(f'''() => {{
                        const el = document.querySelector('input[name=cuba{i}]');
                        if (el) el.value = '{cuba_value}';
                    }}''')
                logger.info(f"[{self.nome}] Preencheu {len(cuba_values)} campos cuba individuais")
            else:
                logger.info(f"[{self.nome}] {len(cuba_values)} volumes > 11 campos cuba, usando apenas cubagem m³ total")

        # Reaplica f15 no final, pois alguns scripts do SSW podem limpar/reformatar o campo.
        await page.evaluate(
            """(valorNf) => {
                const el = document.querySelector("input[name='f15']");
                if (!el) return;
                el.value = valorNf;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
            }""",
            valor_fmt,
        )

        await page.wait_for_timeout(500)

        # Verificar campos preenchidos - scan completo de todos os inputs
        form_check = await page.evaluate('''() => {
            const result = {};
            document.querySelectorAll('input').forEach(el => {
                if (el.name) result[el.name] = el.value || '';
            });
            return result;
        }''')
        logger.info(
            f"[{self.nome}] Formulário: {cep_orig} → {cep_dest}, peso={peso}kg, "
            f"cubagem={cubagem_m3:.4f}m³, {volumes}vol, linhas_cubagem={len(cubagens_validas)}, "
            f"coletar=S, R${valor}"
        )
        campos_seguros = re.sub(r"(?<!\d)\d{11,14}(?!\d)", "***", str(form_check))
        logger.info(f"[{self.nome}] Campos SSW: {campos_seguros}")

    async def _submeter_e_extrair(self) -> Optional[Cotacao]:
        """Submete a cotação e extrai o resultado."""
        self._passo_atual = "submetendo_cotacao"
        page = self._page

        # Capturar resposta XML do submit
        xml_responses = []

        async def capture_response(response):
            url = response.url.lower()
            if 'ssw' in url or 'cotac' in url or 'frete' in url:
                try:
                    body = await response.text()
                    xml_responses.append(body)
                except Exception:
                    pass

        handler = lambda r: __import__('asyncio').ensure_future(capture_response(r))
        page.on('response', handler)

        try:
            return await self._submeter_e_extrair_inner(page, xml_responses)
        finally:
            page.remove_listener('response', handler)

    async def _submeter_e_extrair_inner(self, page, xml_responses) -> Optional[Cotacao]:
        # Submit via sim()
        await page.evaluate('() => { if (typeof sim === "function") sim(); }')
        self._passo_atual = "aguardando_resultado"

        # Polling reativo: aguardar vlr_frete ser populado no DOM (até 25s)
        results = {}
        for _poll in range(160):
            await page.wait_for_timeout(150)
            results = await page.evaluate('''() => {
                const result = {};
                document.querySelectorAll('input').forEach(el => {
                    if (el.name) result[el.name] = el.value || '';
                });
                return result;
            }''')
            vlr = (results.get('vlr_frete', '') or results.get('vlr_total', '') or
                   results.get('valor_frete', '') or results.get('total_geral', '') or
                   results.get('total_frete', ''))
            if vlr and vlr != '0,00':
                logger.info(f"[{self.nome}] vlr_frete encontrado após {(_poll+1)*0.15:.1f}s")
                break
            # Se XML retornou erro, não precisa continuar esperando
            if xml_responses:
                for xml in xml_responses:
                    if '<erro>' in xml and 'ERRO' in xml.upper():
                        logger.info(f"[{self.nome}] Erro XML detectado, parando polling")
                        break
                else:
                    continue
                break

        resultado_seguro = re.sub(r"(?<!\d)\d{11,14}(?!\d)", "***", str(results))
        logger.info(f"[{self.nome}] Campos resultado DOM: {resultado_seguro}")

        # Se prazo foi encontrado mas vlr_frete ainda está vazio, tenta mais 10s
        # (SSW às vezes popula os campos em fases distintas)
        vlr_early = (results.get('vlr_frete', '') or results.get('vlr_total', '') or
                     results.get('valor_frete', '') or results.get('total_geral', '') or
                     results.get('total_frete', ''))
        prazo_early = results.get('prazo', '')
        if (not vlr_early or vlr_early == '0,00') and prazo_early:
            logger.info(f"[{self.nome}] Prazo={prazo_early!r} encontrado mas vlr_frete vazio, aguardando mais 10s...")
            for _extra in range(10):
                await page.wait_for_timeout(1000)
                results_extra = await page.evaluate('''() => {
                    const result = {};
                    document.querySelectorAll('input').forEach(el => {
                        if (el.name) result[el.name] = el.value || '';
                    });
                    return result;
                }''')
                vlr_extra = (results_extra.get('vlr_frete', '') or results_extra.get('vlr_total', '') or
                             results_extra.get('valor_frete', '') or results_extra.get('total_geral', '') or
                             results_extra.get('total_frete', ''))
                if vlr_extra and vlr_extra != '0,00':
                    logger.info(f"[{self.nome}] vlr_frete encontrado na espera extra: {vlr_extra}")
                    results = results_extra
                    break

        # Verificar erros na resposta XML
        erro = None
        erro_msg = None
        for xml in xml_responses:
            erro_match = re.search(r'<erro>([^<]+)</erro>', xml)
            if not erro_match:
                erro_match = re.search(r'<flag>([^<]+)</flag>', xml)
            msg_match = re.search(r'<mensagem>(.*?)</mensagem>', xml, re.DOTALL)
            if erro_match and erro_match.group(1) != '':
                erro = erro_match.group(1)
                msg = msg_match.group(1) if msg_match else ''
                msg = msg.replace('&amp;nbsp;', ' ').replace('&amp;', '&')
                msg = re.sub(r'&\w+;', ' ', msg).replace('<br>', ' ').strip()
                msg = re.sub(r'<[^>]+>', '', msg).strip()
                erro_msg = msg
                logger.warning(f"[{self.nome}] Aviso SSW: {erro} - {msg}")

        restricao_risco = None
        if erro and 'ERRO' in erro.upper():
            # "area de risco" e aviso, nao rejeicao
            if 'risco' in (erro_msg or '').lower():
                restricao_risco = erro_msg
                logger.info(f"[{self.nome}] Aviso de risco (nao fatal): {erro_msg}")
            else:
                self.last_error = "Rota não atendida pela transportadora"
                logger.info(f"[{self.nome}] {self.last_error} (SSW: {erro_msg})")
                return None

        vlr_frete_str = (results.get('vlr_frete', '') or results.get('vlr_total', '') or
                        results.get('valor_frete', '') or results.get('total_geral', '') or
                        results.get('total_frete', ''))
        # Fallback DOM: buscar em todo campo com nome contendo vlr/valor/total/frete/coleta
        if not vlr_frete_str or vlr_frete_str == '0,00':
            for name, val in results.items():
                if not val or val == '0,00':
                    continue
                nl = name.lower()
                if any(kw in nl for kw in ('vlr', 'valor', 'total', 'frete', 'coleta')):
                    if re.match(r'[\d.]+,\d{2}$', val):
                        vlr_frete_str = val
                        logger.info(f"[{self.nome}] valor encontrado em campo DOM '{name}': {val}")
                        break
        nro_cotacao = results.get('nro_cotacao', '') or results.get('nr_cotacao', '')
        prazo_str = results.get('prazo', '')

        # Fallback: extrair valor do frete da resposta XML se DOM não retornou
        if (not vlr_frete_str or vlr_frete_str == '0,00') and xml_responses:
            for xml in xml_responses:
                for tag in ('vlr_frete', 'vlr_total', 'valor_frete', 'total_frete', 'vlr_total_frete',
                            'total_geral', 'vlr_total_geral', 'vlr_coleta', 'frt_valor'):
                    m_vlr = re.search(rf'<{tag}>([^<]+)</{tag}>', xml)
                    if m_vlr and m_vlr.group(1).strip() and m_vlr.group(1).strip() != '0,00':
                        vlr_frete_str = m_vlr.group(1).strip()
                        logger.info(f"[{self.nome}] vlr_frete extraído do XML tag <{tag}>: {vlr_frete_str}")
                        break
                if vlr_frete_str and vlr_frete_str != '0,00':
                    break
                if not nro_cotacao:
                    m_nro = re.search(r'<nro_cotacao>([^<]+)</nro_cotacao>', xml)
                    if m_nro:
                        nro_cotacao = m_nro.group(1).strip()
                if not prazo_str:
                    m_prazo = re.search(r'<prazo>([^<]+)</prazo>', xml)
                    if m_prazo:
                        prazo_str = m_prazo.group(1).strip()

        # Fallback 2: buscar valor monetário em qualquer campo visível da página
        if not vlr_frete_str or vlr_frete_str == '0,00':
            try:
                page_data = await page.evaluate('''() => {
                    const result = {};
                    const els = document.querySelectorAll('td, span, div, label, b, strong');
                    for (const el of els) {
                        const txt = (el.innerText || '').trim();
                        if (!txt) continue;
                        const parent = el.closest('tr, div, section');
                        const ctx = (parent ? parent.innerText : '').toLowerCase();
                        if (ctx.includes('frete') || ctx.includes('total')) {
                            const m = txt.match(/^[\\d.,]+$/);
                            if (m && txt.length >= 3 && txt.includes(',')) {
                                result['frete_text'] = txt;
                                break;
                            }
                        }
                    }
                    return result;
                }''')
                frete_text = page_data.get('frete_text', '')
                if frete_text and frete_text != '0,00':
                    vlr_frete_str = frete_text
                    logger.info(f"[{self.nome}] vlr_frete extraído de texto visível: {vlr_frete_str}")
            except Exception as e:
                logger.debug(f"[{self.nome}] Fallback texto visível falhou: {e}")

        # Fallback 3: buscar valor monetário nos XMLs brutos com regex mais amplo
        if (not vlr_frete_str or vlr_frete_str == '0,00') and xml_responses:
            for xml in xml_responses:
                m_gen = re.search(r'frete[^>]*>(\d[\d.,]+)<', xml, re.IGNORECASE)
                if m_gen:
                    candidate = m_gen.group(1).strip()
                    if candidate and candidate != '0,00':
                        vlr_frete_str = candidate
                        logger.info(f"[{self.nome}] vlr_frete extraído via regex genérico XML: {vlr_frete_str}")
                        break

        # Fallback 4: buscar qualquer valor monetário nos XMLs (sem contexto 'frete')
        if (not vlr_frete_str or vlr_frete_str == '0,00') and xml_responses:
            for xml in xml_responses:
                for m_tag in re.finditer(r'<(\w+)>([\d.,]+)</\1>', xml):
                    tag_name = m_tag.group(1).lower()
                    tag_val = m_tag.group(2).strip()
                    if any(skip in tag_name for skip in ('erro', 'prazo', 'cep', 'cnpj', 'cpf', 'ie', 'nro', 'qtd', 'peso', 'cubagem', 'volume', 'ddd', 'fone')):
                        continue
                    if ',' in tag_val and tag_val != '0,00' and len(tag_val) >= 4:
                        vlr_frete_str = tag_val
                        logger.info(f"[{self.nome}] vlr_frete extraído de XML tag <{tag_name}>: {vlr_frete_str}")
                        break
                if vlr_frete_str and vlr_frete_str != '0,00':
                    break

        # Fallback 5: extrair todo texto visível da página e procurar valor monetário
        if not vlr_frete_str or vlr_frete_str == '0,00':
            try:
                page_text = await page.evaluate('() => document.body?.innerText || ""')
                if page_text:
                    for m_val in re.finditer(r'R?\$?\s*([\d.]+,\d{2})', page_text):
                        val_candidate = m_val.group(1).strip()
                        if val_candidate and val_candidate != '0,00':
                            start = max(0, m_val.start() - 100)
                            end = min(len(page_text), m_val.end() + 100)
                            context = page_text[start:end].lower()
                            if any(kw in context for kw in ('frete', 'total', 'valor', 'coleta', 'cotação', 'cotacao')):
                                vlr_frete_str = val_candidate
                                logger.info(f"[{self.nome}] vlr_frete extraído do texto da página: {vlr_frete_str}")
                                break
            except Exception as e:
                logger.debug(f"[{self.nome}] Fallback texto completo falhou: {e}")

        if not vlr_frete_str or vlr_frete_str == '0,00':
            # Log de diagnóstico: XMLs capturados para análise futura (sanitizado)
            if xml_responses:
                for idx, xml in enumerate(xml_responses):
                    logger.warning(
                        f"[{self.nome}] XML #{idx+1} capturado ({len(xml)} chars): "
                        f"{_sanitize_ssw_xml_excerpt(xml)}"
                    )
            if prazo_str:
                self.last_error = (
                    f"Rota não atendida: sem valor de frete retornado (prazo={prazo_str!r} encontrado — "
                    f"rota possivelmente sem precificação automática no SSW)"
                )
            else:
                # Sem prazo e sem valor: o SSW não respondeu a tempo — falha controlada
                self.last_error = "Timeout aguardando resultado da Copex"
            logger.warning(f"[{self.nome}] {self.last_error}")
            return None

        try:
            valor_frete = float(vlr_frete_str.replace('.', '').replace(',', '.'))
        except ValueError:
            self.last_error = f"Erro ao parsear valor: {vlr_frete_str}"
            logger.error(f"[{self.nome}] {self.last_error}")
            return None

        prazo_dias = 0
        if prazo_str:
            try:
                data_entrega = datetime.strptime(prazo_str, "%d/%m/%y")
                prazo_dias = (data_entrega - datetime.now()).days
                if prazo_dias < 0:
                    prazo_dias = 0
            except ValueError:
                logger.warning(f"[{self.nome}] Prazo não parseável: {prazo_str}")

        cotacao = Cotacao(
            transportadora=self.nome,
            prazo_dias=prazo_dias,
            valor_frete=valor_frete,
            restricoes=restricao_risco,
        )
        logger.info(f"[{self.nome}] Cotação #{nro_cotacao}: R$ {valor_frete:.2f}, {prazo_dias} dias")
        return cotacao

    async def coteir(self, origem: str, destino: str, peso: float, valor: float,
                    volumes: int = 1, cubagem_m3: float = 0.0,
                    comprimento_cm: int = 0, largura_cm: int = 0, altura_cm: int = 0,
                    cnpj_remetente: str = "", cnpj_destinatario: str = "",
                    cubagens: Optional[list[dict]] = None,
                    cnpj_pagador: str = "", tipo_frete: str = "1") -> Optional[Cotacao]:
        """Realiza cotação de frete via portal SSW COOPEX."""
        self.last_error = None
        try:
            try:
                cubagem_m3 = float(cubagem_m3 or 0.0)
            except Exception:
                cubagem_m3 = 0.0
            if cubagem_m3 <= 0:
                self.last_error = _CUBAGEM_AUSENTE_MSG
                logger.error(f"[{self.nome}] {self.last_error}")
                return None

            cubagens_cm = self._normalizar_cubagens_cm(cubagens)
            if cubagens_cm:
                soma = sum(int(c["quantidade"]) for c in cubagens_cm)
                volumes = soma
            elif volumes > 0 and comprimento_cm > 0 and largura_cm > 0 and altura_cm > 0:
                cubagens_cm = [
                    {
                        "quantidade": int(volumes),
                        "comprimento_cm": int(comprimento_cm),
                        "largura_cm": int(largura_cm),
                        "altura_cm": int(altura_cm),
                    }
                ]
            elif cubagem_m3 > 0:
                # Sem cubagens individuais, mas tem m³ total — prossegue
                cubagens_cm = []
            else:
                self.last_error = (
                    f"Cotação bloqueada: cubagens reais ausentes/inválidas "
                    f"(volumes={volumes}, dims_cm={comprimento_cm}x{largura_cm}x{altura_cm}, m3={cubagem_m3})"
                )
                logger.error(f"[{self.nome}] {self.last_error}")
                return None

            self._passo_atual = "init_browser"
            await self._init_browser()
            self._passo_atual = "login"
            await self._login()

            # Criar nova página para cada cotação (mantém sessão via cookies do contexto)
            if self._logged_in:
                try:
                    await self._page.close()
                except Exception:
                    pass
                self._page = await self._context.new_page()

            self._passo_atual = "navegando_cotacao"
            await self._navegar_cotacao()
            self._passo_atual = "preenchendo_formulario"
            await self._preencher_cotacao(origem, destino, peso, valor, volumes, cubagem_m3,
                                         comprimento_cm, largura_cm, altura_cm,
                                         cnpj_remetente, cnpj_destinatario,
                                         cubagens_cm, cnpj_pagador, tipo_frete)
            self._passo_atual = "submetendo_cotacao"
            retorno = await self._submeter_e_extrair()
            self._passo_atual = "ler_resultado"
            return retorno
        except Exception as e:
            self.last_error = f"Erro na cotação: {e}"
            logger.error(f"[{self.nome}] {self.last_error}")
            return None

    async def cotar(self, request: QuoteRequest) -> QuoteResponse:
        return await super().cotar(request)
