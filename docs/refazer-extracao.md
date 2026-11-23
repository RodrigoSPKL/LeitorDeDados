# Apagar a pasta de trabalho, preservar backup e reconstruir as extrações

Esse comando implementa um reset completo de uma pasta de dados explicitamente escolhida. Com `--apply`, ele apaga definitivamente todos os arquivos e diretórios dessa pasta, exceto `backup`, e reconstrói as extrações a partir dos RARs guardados ali. A reserva de execução é removida ao terminar. Não usar na pasta do código, no diretório pessoal ou em uma pasta com dados que precisam ser mantidos fora de backup.

## Executar

Dependências do Linux:

```bash
sudo apt install -y unar unrar
```

Para fazer o reset, substituir o caminho pelo diretório dos dados:

```bash
git -C "$HOME/LeitorDeDados-linux" pull --ff-only &&
python3 "$HOME/LeitorDeDados-linux/scripts/refazer_extracao.py" --folder "/caminho/dos/dados" --apply
```

Sem `--apply`, imprime o plano sem apagar os dados. Não há uma segunda pergunta interativa quando `--apply` é usado: esse argumento autoriza a exclusão permanente no caminho informado.

## Ordem das operações

1. Confere que a origem é uma pasta real, que existe uma pasta `backup` real e que não há reserva de extração ativa ou pendente.
2. Lista e valida os conjuntos de RAR guardados no backup, identificando volumes multipartes com `lsar`. Backups de parciais e registros internos de resets anteriores são ignorados.
3. Compara os bytes dos volumes usando SHA-256. Conjuntos idênticos são escolhidos uma única vez. RARs aninhados já contidos num conjunto pai não são restaurados separadamente da cópia do backup.
4. Interrompe se versões diferentes gerarem a mesma pasta de saída; não escolhe silenciosamente uma versão.
5. Com `--apply`, testa integralmente cada conjunto selecionado com `unrar t`. Se qualquer teste falhar ou um volume mudar durante o teste, não inicia a exclusão.
6. Remove o conteúdo atual fora de `backup`. Recusa pontos de montagem ou outros discos dentro da pasta de trabalho. Links são removidos como links, sem seguir os destinos.
7. Extrai os RARs selecionados com UnRAR em novas pastas `*_extraido`. As cópias originais do backup permanecem onde estão.
8. Extrai os novos RARs encontrados no conteúdo, até 12 rodadas; volumes aninhados consumidos ou repetidos são guardados dentro do registro desse reset, em backup.

O relatório e os logs ficam em `backup/reset-extracao-EXECUCAO/`. O backup recebe novos registros, mas os RARs originais existentes não são apagados nem movidos.

## O significado de evitar duplicação

O reset elimina as extrações e parciais antigas da pasta de trabalho. Impede processar novamente o mesmo conjunto compactado com bytes idênticos e evita restaurar diretamente cópias de arquivos aninhados que o RAR pai já contém.

Ele não altera nomes de membros do arquivo, como `dados (2).csv`, nem remove linhas repetidas de CSV/bancos. Arquivos diferentes dentro dos próprios RARs podem ter conteúdo sobreposto. Essa deduplicação exigiria uma regra separada.

## Limites e falhas

O teste antes da exclusão valida os dados compactados com o backend escolhido. Não garante que a escrita posterior vá funcionar: falta de espaço, permissões, falha do disco, perda de energia e outras condições ainda podem interromper a reconstrução. Nesse caso, o backup continua disponível e o relatório informa `failed`. As extrações antigas já terão sido apagadas se a falha ocorrer depois da fase de limpeza.

Os testes locais usam o backend simulado. Conferem a ordem teste/exclusão, backup preservado, limpeza de arquivos e pastas, repetição sem acúmulo, conflitos, RARs idênticos, aninhados, pontos de montagem e alvos proibidos. O UnRAR real precisa estar funcionando no Linux de destino.
