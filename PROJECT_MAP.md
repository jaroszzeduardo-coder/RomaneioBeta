# PROJECT_MAP.md

## Objetivo

Mapa operacional do Fretio/RomaneioBeta Desktop para Codex CLI, Claude Code CLI e outros agentes. Use este arquivo para localizar partes do projeto sem abrir dezenas de arquivos a cada tarefa.

## Regras para agentes

- Leia `AGENTS.md` primeiro.
- Use este mapa para decidir quais arquivos abrir.
- Para tarefas pontuais, nao faca varredura geral do repositorio.
- Depois de mudar comportamento ou descobrir estrutura nova, atualize apenas a secao relevante deste arquivo.
- Nunca cole secrets reais, credenciais, tokens, CONFIG.toml real, PDFs/XMLs reais ou dados de cliente.

## Visao geral

Aplicativo desktop Windows standalone (sem servidor) para:

- Romaneio
- Cotacao de frete
- Frete de fornecedores
- Rastreio
- Atualizacao automatica via GitHub Releases

Stack principal:

- Python 3.12
- pywebview/WebView2 para UI local (UI web em `app/web/*`)
- Playwright + Chromium para automacao local de portais
- PyInstaller para empacotamento
- Inno Setup para instalador
- GitHub Actions/Releases para distribuicao

## Repositorios relacionados

- `kaianesteffens/RomaneioBeta`: desktop standalone, UI, automacoes locais, providers, updater, instalador e releases.
- `kaianesteffens/RomaneioBeta-releases`: repositorio legado desativado. O updater le e publica releases exclusivamente no proprio `kaianesteffens/RomaneioBeta` (dependencia do repo de releases removida).

## Entradas principais

- `app/web_app.py`: entrada da interface web, janela pywebview/WebView2, bridge `Api`, navegacao, startup, update e chamadas para modulos.
- `python app/web_app.py`: comando de desenvolvimento citado no README (ou `app/dev.bat`).
- `app/version.txt`: versao usada em producao.
- `app/CONFIG.example.toml`: exemplo versionado de configuracao. Nao versionar `CONFIG.toml` real.

## Interface web

Arquivos principais:

- `app/web_app.py`: bridge pywebview/WebView2, expoe a classe `Api` para o front e cria a janela.
- `app/web_presenters.py`: monta os dados apresentados na UI a partir dos modulos locais.
- `app/app_bootstrap.py` e `app/startup.py`: startup e update antes de abrir a UI.

Front (HTML/CSS/JS locais renderizados no WebView2):

- `app/web/index.html`: shell da UI.
- `app/web/app.js`: navegacao e integracao com a bridge `Api`.
- `app/web/app.css`: estilos.
- `app/web/format.js`: mascaras de CEP, CNPJ, moeda e decimal.
- `app/web/pages/*.js`: telas (romaneio, cotacao, fornecedores, rastreio, configuracoes, selecao de empresa).

Responsabilidades observadas:

- Controla startup e update.
- Dispara cotacao, rastreio, importacao de NF-e e processamento de romaneio via metodos da bridge `Api`.

Regra importante:

- Nao execute Playwright, leitura pesada de arquivo ou update bloqueando a thread da UI/bridge.
- Use `app/async_worker.py` e workers/threading ja existentes.

## Configuracao por empresa

Arquivos principais:

- `app/company_config.py`: cria, lista, migra, renomeia e salva configuracoes por empresa, incluindo defaults não destrutivos como `romaneio.cnpj_pagador_padrao`.
- `app/fretio/src/fretio/config_manager.py`: leitura de configuracao dentro do pacote Fretio.
- `app/CONFIG.example.toml`: exemplo seguro.

Locais citados pela documentacao:

- `%APPDATA%\Fretio\empresas\<empresa>\CONFIG.toml`
- `%APPDATA%\Fretio` para dados locais.

Cuidados:

- Nao versionar `CONFIG.toml` real.
- Nao inserir credenciais reais de transportadoras no repositorio.
- Providers devem ler apenas sua secao em `[transportadoras.<nome>]`.

## Configuracao (somente local)

O app e standalone: nao ha licenciamento, configuracao remota nem permissoes/feature flags de servidor. O app abre livre e le apenas configuracao local.

Arquivos principais:

- `app/remote_permissions.py`: reduzido a normalizacao pura de nomes de transportadora (sem servidor).

Fluxo resumido:

1. Desktop carrega empresa e CONFIG.toml local.
2. Cotacao, rastreio, NF-e e romaneio rodam localmente sem checagem de permissao remota.

## Atualizacao

Arquivos principais:

- `app/updater.py`: descoberta e aplicacao de updates.
- `app/version.txt`: versao atual.
- `docs/UPDATE.md`: documentacao do update.
- `docs/RELEASE.md`: checklist de release.
- `installer/`: build/empacotamento.
- `.github/workflows/`: build/release automatizado.

Fluxo:

1. Descobre a versao mais recente via GitHub Releases (`release/latest` do `github_repo`, com `github_repo_aliases` como fallback historico).
2. Baixa ZIP de update.
3. Valida estrutura e assinatura do ZIP.
4. Aplica update e reinicia quando necessario.

Cuidados:

- Nao remover validacao de assinatura/estrutura de update.
- Antes de mexer em update, avaliar impacto em PyInstaller, Inno Setup, ZIP, `version.txt` e workflow.

## Cotacao: fachada e modulo novo

Fachada legada:

- `app/cotacao_transportadoras.py`

Esse arquivo mantem compatibilidade com imports antigos e delega para o pacote `app/cotacao/`.

Modulo dividido:

- `app/cotacao/common.py`: tipos comuns e constantes.
- `app/cotacao/config.py`: carregamento e ajustes seguros de configuracao.
- `app/cotacao/validation.py`: validacoes.
- `app/cotacao/romaneio_parser.py`: extracao de dados do romaneio colado/processado.
- `app/cotacao/session_manager.py`: sessoes/providers reaproveitaveis.
- `app/cotacao/orchestrator.py`: prepara dados, valida e executa providers.
- `app/cotacao/error_context.py`: contexto de erro por provider.

Fluxo de cotacao:

1. UI chama `cotar_transportadoras(...)` ou `cotar_transportadoras_romaneio_colado(...)` pela fachada.
2. Fachada carrega config local.
3. Parser monta dados: CEP origem/destino, UF, CNPJ destinatario, peso, valor, volumes e cubagens.
4. `app/cotacao/orchestrator.py` valida dados obrigatorios.
5. `ProviderFactory` instancia providers configurados e habilitados.
6. Providers executam Playwright localmente.
7. Resultados voltam como `ResultadoCotacao`/`QuoteResponse`.
8. UI exibe resultados.
9. Erros ficam apenas em log local; nao ha envio ao servidor.

Bloqueios comuns antes de cotar:

- CEP origem invalido.
- CEP destino ausente/invalido.
- UF divergente do CEP.
- CNPJ destinatario ausente/invalido.
- Peso invalido.
- Valor negativo.
- Cubagens ausentes.
- Soma de volumes divergente das cubagens.
- Transportadora desabilitada, nao atendida ou com configuracao incompleta.

## Contrato de cotacao

Arquivo principal:

- `app/fretio/src/fretio/quotation_contract.py`

Classes/funcao importantes:

- `QuoteRequest`: entrada padronizada de cotacao.
- `QuoteResponse`: resposta padronizada.
- `QuoteStatus`: `ok`, `sem_cotacao`, `erro`, `desabilitada`, `nao_atendido`.
- `sanitize_raw_payload(...)`: remove dados sensiveis de payload bruto.
- `quote_request_from_legacy_kwargs(...)`: converte kwargs antigos para contrato novo.
- `cotacao_legada_to_quote_response(...)`: adapta resultado legado.
- `resultado_cotacao_to_quote_response(...)`: adapta `ResultadoCotacao`.
- `quote_response_to_resultado_cotacao(...)`: volta para formato usado pela UI/fachada.

Cuidados:

- `raw` e metadados nao devem conter senha, cookie, token, CPF/CNPJ completo, HTML bruto, XML/PDF completo ou screenshot.
- Tratar rota nao atendida como `nao_atendido`, portal sem valor como `sem_cotacao` e falha tecnica como `erro`.

## Providers Playwright

Pasta principal:

- `app/fretio/src/fretio/providers/`

Factory:

- `app/fretio/src/fretio/providers/factory.py`

Providers registrados:

- `braspress` -> `fretio.providers.braspress_playwright.BraspressPlaywrightProvider`
- `trd` -> `fretio.providers.trd.TRDProvider`
- `agex` -> `fretio.providers.agex.AGEXProvider`
- `eucatur` -> `fretio.providers.eucatur.EucaturProvider`
- `rodonaves` -> `fretio.providers.rodonaves.RodonavesProvider`
- `alfa` -> `fretio.providers.alfa.AlfaProvider`
- `coopex` -> `fretio.providers.coopex.CoopexProvider`
- `translovato` -> `fretio.providers.translovato.TranslovatoProvider`

Campos minimos por provider:

- `braspress`: `cnpj`, `senha`
- `trd`: `email`, `senha`
- `agex`: `email`, `senha`
- `eucatur`: login mínimo `dominio`, `usuario`, `senha`; `cnpj_pagador` é resolvido só na cotação, primeiro na transportadora e depois em `romaneio.cnpj_pagador_padrao`.
- `rodonaves`: `dominio`, `usuario`, `senha`, `cnpj_pagador`

Observacao operacional:

- Rodonaves deve abrir primeiro o entrypoint canônico do portal (`login_url` configurado ou `https://cliente.rte.com.br/?showLogin=true`) para estabelecer sessão/login; `/Quotation` deve ser tratado apenas como destino pós-login, com sucesso validado por elementos reais do formulário e não só pela URL.
- Rodonaves roda em Chrome real conectado por CDP com `user-data-dir` exclusivo `~/.fretio/rodonaves_browser_data`; a janela nasce headful/off-screen, só aparece para CAPTCHA/interação humana e volta a ficar oculta sem recriar browser/context/page.
- `alfa`: `login`, `senha`
- `coopex`: login mínimo `dominio`, `usuario`, `senha`; `cnpj_pagador` é resolvido só na cotação, primeiro na transportadora e depois em `romaneio.cnpj_pagador_padrao`.
- `translovato`: `cnpj`, `usuario`, `senha`

Para criar provider novo:

1. Criar arquivo em `app/fretio/src/fretio/providers/<nome>.py`.
2. Herdar de `ProviderBase`.
3. Implementar `async def cotar(self, request: QuoteRequest) -> QuoteResponse`.
4. Implementar `async def cleanup(self) -> None` se abrir browser/page/context.
5. Registrar em `_PROVIDER_SPECS` no `factory.py`.
6. Adicionar campos obrigatorios em `_REQUIRED_FIELDS`.
7. Atualizar `app/CONFIG.example.toml`.
8. Atualizar `docs/PROVIDERS.md` e testes.

Boas praticas:

- Nao emitir/alterar UI diretamente dentro do provider (a UI e a bridge web em `app/web_app.py`).
- Nao criar event loop proprio.
- Usar seletores robustos de Playwright.
- Preservar cleanup de page/context/browser/Playwright/processos.
- Em providers que reutilizam sessao (ex.: RODONAVES), validar page/context/browser antes de `goto()` e registrar URL alvo, etapa anterior, `headless` e motivo quando houver fechamento de lifecycle.
- RODONAVES usa reCAPTCHA no portal `cliente.rte.com.br`; preferir modo visível/off-screen (`headless=false`) e, se a sessão iniciar headless e o CAPTCHA bloquear a cotação, refazer apenas a tentativa da RODONAVES em modo visível com diagnóstico seguro.
- Diagnósticos locais de provider devem guardar só metadados seguros (URL, etapa, flags de seletores, contagens, trechos redigidos), sem HTML bruto, cookies, tokens, senhas, CNPJ/CPF completo ou dados reais de cliente.
- Manter `last_error` informativo.
- Nao salvar senha/logins/cookies em log.

## Browser e Playwright

Arquivos/pastas:

- `app/fretio/src/fretio/browser/`: abertura, localizacao e cleanup de browser.
- `app/fretio/src/fretio/providers/base.py`: base dos providers e localizacao de Chrome.
- `app/cotacao/orchestrator.py`: trata Chrome ausente e reporta erro sanitizado.

Regra:

- Playwright roda no desktop do cliente, nunca no server.
- Se Chrome/Chromium estiver ausente, mostrar mensagem ao usuario e reportar erro sanitizado.

## Romaneio, NF-e e rastreio

Arquivos citados/importados pela UI:

- `app/extrator_pedidos.py`: extracao de pedidos/romaneio.
- `app/extrator_nfe.py`: leitura/importacao de NF-e/XML/DANFE.
- `app/rastreamento.py`: rastreio e links.
- `app/cotacao/romaneio_parser.py`: parser de romaneio colado.

Fluxos provaveis:

- Romaneio PDF/texto -> extrator/parser -> dados de envio -> cotacao.
- NF-e/XML/DANFE -> `extrator_nfe.py` -> dados resumidos -> UI/cotacao.
- Rastreio -> `rastreamento.py` -> resultados na UI -> evento de uso.

## Erros e logs (somente local)

O app nao fala com servidor. Nao ha telemetria de uso, envio de erros, jobs de cotacao nem normalizacao remota.

Regra:

- Erros ficam apenas em log local.
- Nunca gravar em log senha, login, cookie, token, CPF/CNPJ completo ou dados reais de cliente.

## Build, release e instalador

Arquivos/pastas:

- `installer/`
- `installer/build.bat`
- `installer/Fretio.spec`
- `installer/launcher.py`
- `installer/launcher.spec`
- `installer/Fretio-installer.iss`
- `installer/requirements-lock.txt`
- `.github/workflows/`
- `docs/RELEASE.md`
- `docs/UPDATE.md`

Fluxo atual do workflow `Desktop CI` (`.github/workflows/ci.yml`):

- Dispara em `pull_request` contra `master` e tambem por `workflow_dispatch`, sem filtros de paths.
- Roda em Linux com Python 3.12, instala dependencias de `installer/requirements.txt`, `requirements-dev.txt` se existir, e `pytest`.
- Executa `compileall` em `app` e testes raiz, depois `pytest -q` com deselect apenas de testes antigos ja conhecidos que ainda falham fora do escopo do CI inicial.
- Nao roda build de instalador, Playwright interativo, release, assinatura de update nem exige credenciais reais.

Fluxo atual do workflow `Build and Release Fretio`:

- Execucao manual via `workflow_dispatch` com input obrigatorio `version` (`X.Y` ou `X.Y.Z`).
- `publish_release=true` publica no proprio repositorio com o `GITHUB_TOKEN` automatico e exige `UPDATE_SIGNING_PRIVATE_KEY_B64` e `UPDATE_PUBLIC_KEY_B64`. `RELEASES_TOKEN` so e necessario para publicar em um repositorio diferente (`RELEASE_REPO`).
- `publish_release=false` serve apenas para artefato interno; se faltar assinatura, exige `ALLOW_UNSIGNED_DEV_RELEASE=true` e nao publica release externa.

Cuidados:

- Mudancas em dependencia podem quebrar PyInstaller.
- Mudancas em paths podem quebrar Inno Setup ou update ZIP.
- Antes de alterar build, verificar impacto no launcher, assets, assinatura e versao.

## Testes locais

- `tests/test_provider_regressions.py`: testes unitarios focados em regressões de providers Playwright sem abrir navegador real; cobre Translovato, RODONAVES, Eucatur, TRD e COOPEX.
- Para mudanças pontuais em providers, prefira `pytest -q tests/test_provider_regressions.py` antes de rodar a suíte completa.

## Testes VM Windows

- `scripts/opencode/vm-test.sh`: dispara a task `Fretio_VM_Test` na VM Windows via SSH, grava o `run.log` em `logs/vm/latest-<modo>.log` e espera `status.txt` em `C:/fretio-vm/results/<run-id>/`.
- `scripts/opencode/vm-commands/open-app.cmd`: chama `scripts/opencode/windows/open-app.ps1` para abrir o app real via `scripts/opencode/windows/app-command.txt` e salvar evidências em `FRETIO_VM_RESULT_DIR`.
- `scripts/opencode/vm-commands/real-rodonaves.cmd` e `scripts/opencode/vm-commands/real-translovato.cmd`: chamam `scripts/opencode/windows/run-real-provider.py` para executar providers reais na VM com Playwright/browser local e salvar `*-real.log`, `*-result.json` e tail do `fretio.log` em `FRETIO_VM_RESULT_DIR`.
- `scripts/opencode/windows/real-provider-inputs.json`: payload versionado e seguro para os modos reais; pode ser sobrescrito na VM por `%APPDATA%\Fretio\vm-test-inputs.json` ou por `FRETIO_VM_TEST_INPUTS`.

## Documentacao existente

- `README.md`: visao geral, stack, fluxo principal e desenvolvimento.
- `docs/ARCHITECTURE.md`: arquitetura local, threads e Playwright.
- `docs/PROVIDERS.md`: contrato e criacao de providers.
- `docs/UPDATE.md`: descoberta de versao via GitHub Releases e update.
- `docs/RELEASE.md`: checklist de release.
- `CHANGELOG.md`: historico de mudancas.

## Onde mexer por tipo de tarefa

- Erro visual/tela/botao: comece por `app/web/*` (`index.html`, `app.js`, `app.css`, `pages/*.js`), depois `app/web_app.py` e `app/web_presenters.py`.
- Erro de cotacao geral: comece por `app/cotacao_transportadoras.py`, `app/cotacao/orchestrator.py` e `app/cotacao/romaneio_parser.py`.
- Erro em transportadora especifica: comece por `app/fretio/src/fretio/providers/<transportadora>.py` e `factory.py`.
- Erro de credenciais/config: comece por `app/company_config.py`, `app/CONFIG.example.toml`, `factory.py` e validacao do provider.
- Erro de update: comece por `app/updater.py`, `app/version.txt`, `docs/UPDATE.md` e `installer/`.
- Erro de build: comece por `installer/`, `.github/workflows/`, `requirements` e specs.

## Checklist antes de pedir ao agente para programar

Use um pedido assim:

```txt
Leia AGENTS.md e PROJECT_MAP.md.
A tarefa e: <descrever problema>.
Abra somente os arquivos provaveis para essa tarefa.
Antes de alterar, diga quais arquivos pretende mexer.
Faca a menor mudanca possivel.
Atualize PROJECT_MAP.md se descobrir estrutura nova.
Explique como testar pela interface.
```

## Pendencias de mapeamento futuro

- Mapear todos os arquivos em `app/fretio/src/fretio/browser/`.
- Mapear cada provider individualmente: login, cotacao, seletores, cleanup e erros comuns.
- Mapear fluxo completo de rastreio.
- Mapear fluxo completo de NF-e/XML/DANFE.
- Mapear workflows de GitHub Actions e assets de release.
