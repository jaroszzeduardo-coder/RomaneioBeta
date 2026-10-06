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

O updater rejeita ZIP com path traversal, caminho absoluto ou estrutura inválida. Quando existir assinatura, `update_security.py` verifica o asset `.sig`.

## Publicação

O workflow de release gera instalador e ZIP de update. Para dependências, editar `installer/requirements.in` e regenerar `installer/requirements-lock.txt` em Windows antes de publicar.

Não publicar tokens, `CONFIG.toml`, chaves de licença ou credenciais nos assets.
