# Changelog

## 2.69 - 2026-10-08

- RODONAVES: corrige a leitura do resultado para priorizar o total final da cotacao e rejeitar componentes isolados como frete-peso, GRIS, pedagio e taxas.
- ATUALIZACAO: solicita autorizacao do Windows ao atualizar instalacoes em pastas protegidas, preserva a tentativa pendente quando a autorizacao falha e registra a aplicacao para diagnostico.
- LAUNCHER: reinicia com elevacao somente quando necessario para gravar no diretorio instalado.

## 2.65 - 2026-08-17

- RODONAVES: correção crítica na extração do resultado de cotação — isolamento de chamadas AJAX auxiliares de CEP/cliente para não anular o clique no botão Calcular, suporte a formatos de frete monetário em string/HTML e proteção contra captura do valor da nota fiscal.

## 2.64 - 2026-08-17

- DESEMPENHO GLOBAL: aplicação de flags de baixo consumo de CPU/RAM no `launch_browser_resilient` para todas as transportadoras (AGEX, Braspress, Coopex, Eucatur, Translovato, TRD, Alfa e Rodonaves).
- FILTROS DE REDE GLOBAIS: bloqueio de rastreadores, analytics e scripts desnecessários em todos os contextos de automação via `apply_performance_route_filters`.
- LOGIN E COTAÇÃO ACELERADOS: eliminação de esperas fixas e adoção de polling reativo (100–150ms) no login e cotação da Braspress, Coopex (SSW), Eucatur (SSW), TRD (Senior X) e AGEX.

## 2.63 - 2026-08-17

- ALFA: validação completa de cotação real no novo portal `areadocliente`, preenchimento correto de data inicial, selects de carga/zona/pagador, bloqueio de widgets pesados de chat (`sz.chat`) e flags de economia de CPU/memória no Chrome.
- RODONAVES: preservação do preenchimento de CEP de destino com máscara jQuery, fallback automático para peso unitário por volume e redução de latências de espera fixa na automação.
- DESEMPENHO (PCs fracos): inclusão de flags de otimização de baixo consumo no Chrome (`--disable-gpu`, `--disable-software-rasterizer`, `--renderer-process-limit=2`, `--metrics-recording-only`, `--disable-background-networking`) e bloqueio de rastreadores e recursos pesados desnecessários para cotação.

## 2.62 - 2026-08-17

- ALFA: submissão automática do formulário de login assim que o Turnstile é resolvido, ampliação do tempo de renderização da cotação e suporte para a nova estrutura de sidebar/rotas da Alfa.

## 2.61 - 2026-08-17

- ALFA: migração para o novo domínio `areadocliente.alfatransportes.com.br` com compatibilidade automática para URLs antigas configuradas e atualização dos fluxos de login, cotação e rastreio.

## 2.60 - 2026-08-17

- ALFA: correção do caminho de cotação com navegação resiliente a mudanças de layout/menus e detecção flexível de formulário e botões de submit.
- RODONAVES: correção do fluxo de autenticação e navegação pós-login com captura de resposta da API de login, aceite LGPD e estabilização de sessão antes de navegar para a cotação.

## 2.55 - 2026-07-02

- Migração da UI desktop de PySide6 para UI web renderizada em WebView2 via pywebview: front local em `app/web/*` (`index.html`, `app.js`, `app.css`, `format.js`, `pages/*.js`), com `app/web_app.py` expondo a bridge `Api`, `app/web_presenters.py` montando os dados e `app/app_bootstrap.py`/`app/startup.py` cuidando de startup, licença, configuração remota e update.
- Comando de desenvolvimento passa a ser `python app/web_app.py` (ou `app/dev.bat`); a UI antiga PySide6 foi removida.
- Providers Playwright/Chromium seguem inalterados, rodando localmente no desktop.
- Auditoria de segurança: revisão de segredos, sanitização de logs/diagnósticos de providers e unificação do fluxo de releases.

## 2.32 - 2026-05-31

- Integração com RomaneioBeta-server para licenças, configuração remota, telemetria, erros, jobs de cotação e descoberta de versão.
- Compatibilidade mantida com validação legada via GitHub Gist quando `license_api_url` não estiver configurado.
- Uso offline preservado quando há cache de licença válido e o servidor está indisponível.
- Configuração remota tolerante a endpoint ausente, resposta inválida ou falha de rede, usando cache/defaults.
- Sanitização reforçada para erros, eventos de uso, jobs e payloads de cotação antes de envio ao servidor.
- Workflow Windows gera instalador, ZIP de update, launcher e assets estáveis para o repositório de releases.
- ZIP de update validado e assinado quando chaves de assinatura estiverem configuradas.
- Documentação de deploy, backup, update e licenciamento atualizada.
