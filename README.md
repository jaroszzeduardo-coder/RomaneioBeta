# Fretio / RomaneioBeta Desktop

Aplicativo desktop Windows standalone para operação de romaneios, cotação de frete e rastreio. A interface local é feita em pywebview/WebView2 (UI web) e as automações dos portais das transportadoras rodam no próprio desktop com Playwright/Chromium.

O app abre livre: não há servidor, licenciamento, configuração remota nem telemetria. A configuração é sempre local e a descoberta de versão/update é feita via GitHub Releases.

## Stack

- Python 3.12
- pywebview/WebView2 (UI web) para UI local
- Playwright + Chromium no desktop para automação dos portais
- PyInstaller + Inno Setup para build Windows
- Configuração por empresa em TOML

## Fluxo principal

1. O usuário abre o app e seleciona a empresa.
2. O app carrega `%APPDATA%\Fretio\empresas\<empresa>\CONFIG.toml`.
3. A UI web (pywebview/WebView2) dispara módulos locais: romaneio, cotação, frete fornecedores e rastreio.
4. Providers em `app/fretio/src/fretio/providers` usam Playwright localmente quando precisam acessar portais.
5. Erros ficam apenas em log local; nada é enviado a servidor.

## Configuração

Use [app/CONFIG.example.toml](app/CONFIG.example.toml) como referência. Não versionar `CONFIG.toml`, senhas ou arquivos de cache.

Exemplo mínimo, com valores fictícios:

```toml
[fretio]
github_repo = "jaroszzeduardo-coder/RomaneioBeta"

[romaneio]
cep_origem = "01001000"

[transportadoras.trd]
habilitado = true
email = "usuario@example.com"
senha = "SENHA_LOCAL"
headless = true
ufs_atendidas = ["RS", "SC", "PR"]
```

## Desenvolvimento

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r installer\requirements.in
python -m playwright install chromium
python app\web_app.py
```

Em produção, a versão do app vem de `app/version.txt`.

## Build e atualização

O build do desktop fica em `installer/` e usa `installer/requirements-lock.txt`. O pacote de update é um ZIP com executável e `version.txt`; o app valida a estrutura do ZIP antes de aplicar.

```cmd
cd installer
build.bat
```

O fluxo de update descobre a versão mais recente via GitHub Releases (`releases/latest` do `github_repo`, com `github_repo_aliases` como fallback histórico para builds antigos).

## Documentação

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): arquitetura local, threads e Playwright.
- [docs/PROVIDERS.md](docs/PROVIDERS.md): como providers funcionam e como criar uma nova transportadora.
- [docs/UPDATE.md](docs/UPDATE.md): descoberta de versão via GitHub Releases e aplicação do update.
- [docs/RELEASE.md](docs/RELEASE.md): checklist de release segura e rollback.
- [CHANGELOG.md](CHANGELOG.md): mudanças por versão.

## Regra de segurança

Como o app é standalone, nada é enviado a servidor. Em logs locais, nunca gravar senhas de transportadoras, cookies, HTML bruto, screenshots, XML completo, DANFE/PDF completo, CPF/CNPJ completos de clientes finais, token GitHub ou tracebacks com dados reais sem sanitização.
