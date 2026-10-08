# Atualização do Desktop

O updater fica em `app/updater.py`. Ele descobre versões novas, baixa o ZIP de update, valida a estrutura e aplica a troca de arquivos.

## Descoberta de versão

A descoberta é feita 100% via GitHub Releases. O app consulta a release mais recente (`releases/latest`) do repositório configurado:

```toml
[fretio]
github_repo = "jaroszzeduardo-coder/RomaneioBeta"
github_repo_aliases = ["dujarosz/RomaneioBeta-releases"]
```

O updater lê a tag/versão da release mais recente e o asset do ZIP de update anexado a ela. Se a versão da release for maior que a versão local, o app mostra o update.

`github_repo_aliases` serve apenas como fallback histórico para builds antigos que ainda apontavam para o repositório legado de releases.

## Aplicação do update

O pacote de update deve conter:

- `Fretio.exe` ou `FreteBot.exe`
- `version.txt` ou `_internal/version.txt`

Se a instalacao estiver em uma pasta protegida, como `Program Files`, o updater solicita autorizacao do Windows antes da copia final. Se a autorizacao for recusada ou a inicializacao do instalador falhar, o marcador de update pendente e preservado para permitir nova tentativa. A aplicacao da troca fica registrada em `%APPDATA%\Fretio\update\apply.log`.

Computadores com uma versao anterior a esse suporte podem precisar executar uma vez o `Fretio-Setup-latest.exe` da release mais recente; depois disso, as atualizacoes seguintes usam o fluxo corrigido.

Para este projeto, um pedido do Eduardo por "workflow" significa publicar uma nova versao pelo `build-release.yml`, e nao apenas executar o Desktop CI.

O updater rejeita ZIP com path traversal, caminho absoluto ou estrutura inválida. Quando existir assinatura, `update_security.py` verifica o asset `.sig`.

## Publicação

O workflow de release gera instalador e ZIP de update. Para dependências, editar `installer/requirements.in` e regenerar `installer/requirements-lock.txt` em Windows antes de publicar.

Não publicar tokens, `CONFIG.toml`, chaves de licença ou credenciais nos assets.
