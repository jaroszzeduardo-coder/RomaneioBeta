# Release segura

Checklist operacional para publicar uma versão liberável sem quebrar instalações antigas.

## Escopo

- O app é standalone: não há servidor, licenciamento nem configuração remota.
- Versão/update é descoberta 100% via GitHub Releases do próprio repositório.
- Configuração é sempre local (`CONFIG.toml`); a inicialização e a cotação local não dependem de rede além dos portais das transportadoras.

## Pré-release

- Confirmar `app/version.txt` com a versão da release.
- Atualizar `CHANGELOG.md`.
- Confirmar que `CONFIG.toml`, `.env`, tokens, dumps e backups não aparecem no diff.
- Confirmar que exemplos e testes usam documentos fictícios ou sanitizados.

## Validações locais

```bash
cd RomaneioBeta
python -m pytest
python installer/validate_update_zip.py installer/installer/Fretio-Update-2.32.zip
```

O `validate_update_zip.py` só se aplica depois que o workflow ou build Windows gerar o ZIP.

## Build Windows

O workflow `Build and Release Fretio` é manual. Depois do merge no `master`, abra **Actions → Build and Release Fretio → Run workflow**, informe uma versão nova em `version` (`X.Y` ou `X.Y.Z`, por exemplo `2.34`) e mantenha `publish_release=true` para release oficial de cliente.

Configuração obrigatória para release oficial de cliente:

- Secret `UPDATE_SIGNING_PRIVATE_KEY_B64`
- Variable `UPDATE_PUBLIC_KEY_B64`
- Variable `ALLOW_UNSIGNED_DEV_RELEASE=false`

As releases são publicadas no próprio repositório `jaroszzeduardo-coder/RomaneioBeta`
(que é público), então o `GITHUB_TOKEN` automático do workflow já basta — **não é
mais necessário um `RELEASES_TOKEN`**. Só defina `RELEASE_REPO` (e um `RELEASES_TOKEN`
com acesso a ele) se quiser publicar em um repositório diferente do atual.

Com `publish_release=true`, o workflow valida que `version` é maior que a maior tag já publicada no repositório, atualiza `app/version.txt` antes do build, assina os ZIPs de update, publica a GitHub Release `v<version>` e falha se faltar chave de assinatura ou qualquer artefato obrigatório.

Para build interno sem publicação externa, rode manualmente com `publish_release=false`. Se as chaves de assinatura estiverem ausentes, esse modo só deve ser usado com `ALLOW_UNSIGNED_DEV_RELEASE=true`; ele gera apenas artefatos internos do workflow e não publica ZIP sem `.sig` no repositório de releases.

O workflow deve gerar, para a versão informada:

- `Fretio-Setup-<version>.exe`
- `Fretio-Update-<version>.zip`
- `Fretio-Update-<version>.zip.sig` em release oficial assinada
- `Romaneio.exe`
- assets `*-latest.*`
- `installer/repository-assets/latest.json` em release oficial

## Publicação

- Conferir que a GitHub Release `v2.32` existe em `jaroszzeduardo-coder/RomaneioBeta` e está marcada como `latest`.
- Conferir anexos: instalador, ZIP de update, assinatura do ZIP, launcher e aliases `latest`.
- Conferir que `latest.json`, se versionado/gerado, aponta para `2.32` e para `jaroszzeduardo-coder/RomaneioBeta`.

## Testes pós-release

- Instalação nova em Windows limpo.
- Atualização a partir da versão anterior instalada (updater descobre a nova versão via GitHub Releases).
- App abre livre, sem tela de ativação ou licença.
- Cotação com pelo menos uma transportadora habilitada.

## Rollback

1. Editar a release anterior no GitHub e marcá-la como `latest`, ou despublicar a release problemática.
2. Reverter os assets `*-latest.*` no repositório de releases para os artefatos da versão anterior.
3. Confirmar que o updater passa a descobrir a versão anterior como mais recente.
4. Rotacionar tokens GitHub e chaves de assinatura se qualquer segredo tiver sido exposto.

Não apagar a release problemática até coletar logs e confirmar que nenhum cliente ainda depende dos assets dela.
