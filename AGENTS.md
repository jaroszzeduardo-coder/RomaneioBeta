# AGENTS.md

Instrucoes para Codex CLI, Claude Code CLI e outros agentes trabalharem neste repositorio sem gastar contexto a toa.

## Projeto

Este repositorio contem o Fretio/RomaneioBeta Desktop, um aplicativo desktop Windows em Python 3.12 para romaneios, cotacao de frete, frete de fornecedores, rastreio e apoio operacional.

Principais tecnologias:

- UI local web em pywebview/WebView2 (front em `app/web/*`, bridge em `app/web_app.py`).
- Automacao de portais com Playwright + Chromium no computador do cliente.
- Leitura de PDFs com pdfplumber e leitura de XML/DANFE de NF-e.
- Build Windows com PyInstaller e Inno Setup.
- Releases e assets pelo GitHub Actions, publicados no proprio repositorio.

O app e standalone: nao ha servidor, licenciamento, configuracao remota, telemetria nem jobs de cotacao. Versao/update vem de GitHub Releases.

## Politica de plataformas

- O produto oficial para cliente continua sendo Windows.
- O build, instalador, updater, assinatura de update, releases e fluxo de distribuicao oficiais sao Windows por padrao.
- Linux/Zorin OS pode ser suportado apenas como modo interno de desenvolvimento, depuracao e teste real de providers pelos mantenedores.
- Linux nao deve virar alvo oficial de produto, instalador de cliente ou release publica sem pedido explicito do Eduardo.
- Mudancas para Linux devem ser isoladas, opt-in e nunca devem alterar o comportamento padrao do Windows.
- O modo Linux interno pode ajustar somente itens de ambiente local: caminhos, logs, venv, dependencias do Playwright/Chromium, abertura de navegador visivel e comandos de execucao local.
- O modo Linux interno nao pode alterar regra de negocio, contrato de provider, calculo de cotacao, updater, workflow de release Windows ou comportamento esperado pelo cliente.
- Se uma tarefa mencionar Linux sem dizer o contrario, interprete como `Linux dev/test interno`, nao como distribuicao oficial para clientes.
- Para trabalhos de compatibilidade Linux, documente claramente o que foi feito para Linux e confirme que Windows continua sendo o alvo oficial.

## Como economizar contexto

- Leia este arquivo primeiro.
- Depois consulte `PROJECT_MAP.md` para localizar os arquivos provaveis.
- Abra apenas os arquivos necessarios para a tarefa atual.
- Nao faca varredura geral do repositorio se a tarefa for pontual.
- Nao carregue pastas de build, assets gerados, caches, instaladores, `.venv`, `dist`, `build`, `__pycache__` ou arquivos grandes sem motivo direto.
- Quando descobrir algo util sobre a estrutura do projeto, atualize somente a parte relevante de `PROJECT_MAP.md`.

## Regras de trabalho

- Entenda o projeto como app desktop Windows. A UI e renderizada em WebView2 com HTML/CSS/JS locais (`app/web/*`) via pywebview; nao e uma web app hospedada remotamente.
- Leia o codigo existente antes de alterar comportamento.
- Sempre procure o fluxo existente antes de criar um novo.
- Mantenha mudancas pequenas, diretas e alinhadas aos padroes ja usados no repositorio.
- Antes de alterar, identifique internamente os arquivos provaveis. Em modo interativo, diga quais arquivos pretende mexer e por que. Em `codex exec`, execute direto e explique no resumo final.
- Ao terminar, diga quais arquivos foram alterados e como testar.
- Nao altere `app/`, `installer/`, `requirements.txt`, workflows ou arquivos de build sem necessidade direta da tarefa.
- Nao remova nem exponha secrets, tokens, senhas, chaves privadas, licencas ou credenciais. Se encontrar algo sensivel versionado, avise antes de mexer.
- Nao versionar `CONFIG.toml` real, credenciais, tokens, licencas, XMLs/PDFs reais de clientes, prints sensiveis ou dados operacionais privados.
- Preserve `.github/workflows/`, pois os workflows fazem parte do build e release.
- Para providers de transportadoras, siga a arquitetura existente em `app/fretio/src/fretio/providers/`.
- Respeite configuracao por transportadora em `CONFIG.toml` ou no exemplo versionado, sem inserir valores reais.
- Prefira seletores robustos em automacoes Playwright: `name`, `placeholder`, `role`, texto visivel e labels. Evite classes geradas e `nth-child` quando houver alternativa.
- Mantenha `last_error` informativo em fluxos que podem falhar e preserve cleanup de recursos externos como browser, context e page.
- Antes de mexer em build/release, avaliar impacto em PyInstaller, Inno Setup, `app/version.txt`, updater, ZIP de update e `.github/workflows/build-release.yml`.
- Nao remover validacao de assinatura de update nem mudar repositorio de releases sem autorizacao explicita.
- Ao mover instrucoes antigas de agentes, use `backup-agent-instructions/` em vez de apagar diretamente quando houver conteudo reutilizavel.

## Política de comentários no código

Evite adicionar comentários novos no código.

Comentários só são permitidos quando explicarem:
- regra de negócio não evidente;
- fluxo frágil de portal/transportadora;
- comportamento relacionado a captcha ou interação humana;
- decisão técnica que não fica clara pelo próprio código;
- workaround necessário para seletor, timing ou instabilidade externa.

Não adicionar comentários que apenas descrevem o que o código já faz.

## Responsabilidades entre repositorios

- `RomaneioBeta`: aplicativo desktop standalone, UI, automacoes locais, providers, updater, build Windows e releases. Nao depende de servidor.
- `RomaneioBeta-releases`: repositorio legado desativado. O updater le e publica releases exclusivamente no proprio `RomaneioBeta` (dependencia do repo de releases removida).

## Verificacao

- Rode testes focados quando a tarefa tocar codigo.
- Para mudancas documentais, confirme que os links e caminhos citados existem ou marque como pendente no `PROJECT_MAP.md`.
- Para mudancas de limpeza documental ou metadados, confirme com `git status` e busca textual que nao sobraram instrucoes ativas de agentes antigos.
- Para mudancas Linux, confirme que elas sao opt-in, internas de desenvolvimento/teste e que Windows segue como produto oficial.

## Como responder ao Eduardo

Eduardo nao quer depender de terminal nem ficar cacando pastas. Explique em linguagem simples:

1. o que encontrou;
2. o que vai alterar;
3. quais arquivos mexeu;
4. como testar pela interface sempre que possivel;
5. comando de terminal somente quando for realmente necessario.

## Local LLM MCP

A local MCP server named `local-llm` may be available.

Tool:

- `ask_local_llm(prompt, mode, model, max_lines)`

Use it only as a low-cost support tool, not as final authority.

Recommended use:

- `mode="summary"` for long provider logs, Playwright errors, tracebacks and noisy terminal output.
- `mode="review"` for one small diff or one focused file.
- `mode="hypothesis"` when a carrier/provider bug is unclear.
- `mode="tests"` after a fix to suggest minimal validation steps.

Rules:

- Do not send the whole repository to the local LLM.
- Do not send `.env`, credentials, tokens, passwords, license keys, CNPJ/senha real, XMLs/PDFs reais de clientes or customer data.
- Treat local LLM output as untrusted.
- Verify every suggestion against code, logs and tests.
- Keep prompts small and focused.
- Skip the local LLM if the task is trivial, if MCP is unavailable, or if using it would require sending sensitive data.

