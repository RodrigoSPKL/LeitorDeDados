# Limpar somente tentativas parciais de extração

O extrator preserva pastas temporárias quando uma tentativa falha. Se uma nova tentativa concluir com outro descompactador, a extração completa fica em `*_extraido` e as parciais anteriores continuam separadas em `.rar-pending-ID`. A limpeza é um comando independente.

Um nome como `dados (2).csv` pode estar dentro do próprio arquivo compactado. Esse sufixo não comprova duplicidade; o script não seleciona arquivos ou pastas por `(2)`, `(3)` nem pelo conteúdo. Também não deduplica o banco de dados.

## Primeiro, listar

Após terminar a extração, entrar na pasta dos dados e executar:

```bash
git -C "$HOME/LeitorDeDados-linux" pull --ff-only &&
python3 "$HOME/LeitorDeDados-linux/scripts/limpar_parciais.py"
```

O padrão é somente prévia: mostra caminhos relativos e tamanho lógico estimado, sem mover nem excluir nada. Para indicar explicitamente a origem, acrescentar `--folder "/caminho/dos/dados"`.

## Limpeza reversível

```bash
python3 "$HOME/LeitorDeDados-linux/scripts/limpar_parciais.py" --quarantine
```

Move as parciais selecionadas para `backup/limpeza-parciais-EXECUCAO/pastas/`, preservando a estrutura relativa e os dados. O `manifest.json` registra os caminhos de origem e destino para uma restauração manual. Essa operação organiza a pasta de trabalho, mas não libera espaço no disco.

## Exclusão permanente para liberar espaço

Após conferir a prévia, para apagar definitivamente as parciais que ainda estão na origem:

```bash
python3 "$HOME/LeitorDeDados-linux/scripts/limpar_parciais.py" --delete
```

Essa opção elimina todo o conteúdo das pastas parciais selecionadas, sem lixeira. Se você tiver guardado arquivos manualmente dentro delas, retirar esses arquivos antes. Não remove parciais anteriormente movidas para a quarentena, pois todo o diretório `backup` é preservado.

## Escopo e proteção dos dados

- Seleciona apenas diretórios `.rar-pending-` seguidos dos oito caracteres usados nos nomes temporários do extrator.
- Percorre a origem e subpastas, sem seguir links ou junções. Não entra em `backup`.
- Preserva as pastas de extração completa, os RARs de origem e de backup, os arquivos comuns e os nomes com `(2)`.
- Recusa raízes do sistema, caminhos em `backup`, alvos fora da origem e diretórios cujo identificador mudou durante o planejamento interno.
- Interrompe a limpeza se encontrar `.rar-extraction-lock` na origem, nos ancestrais, nas subpastas examinadas ou dentro das parciais. Uma reserva pendente após queda exige conferência manual; o script não remove reservas.
- Antes de cada movimentação ou exclusão, verifica novamente o caminho absoluto e sua identidade. Erros são registrados no manifesto de limpeza em `backup`, sem apagar o restante por fora da seleção.

Os identificadores temporários permitem delimitar a limpeza, mas não provam, sozinhos, a origem de uma pasta criada manualmente com o mesmo nome. Conferir os candidatos da prévia. Os testes locais verificam remoção apenas das parciais, quarentena, caminhos externos recusados, reservas ativas e preservação dos dados completos e dos backups.
