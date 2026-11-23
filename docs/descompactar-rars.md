# Extrair RARs e guardar os originais em backup

Este comando é independente da importação do banco. Executa no Linux com Python 3.10+ e as ferramentas `unar`/`lsar` do pacote `unar`:

```bash
sudo apt install -y unar
```

Há também a opção de extração com o UnRAR (`sudo apt install unrar`). A listagem de segurança continua usando `lsar`, portanto o pacote `unar` também é necessário nesse modo.

Manter o código separado dos dados. Entrar na pasta que contém os RARs e executar:

```bash
git -C "$HOME/LeitorDeDados-linux" pull --ff-only &&
python3 "$HOME/LeitorDeDados-linux/scripts/extrair_rars.py"
```

Sem `--folder`, a origem é a pasta atual. Para escolher explicitamente:

```bash
python3 "$HOME/LeitorDeDados-linux/scripts/extrair_rars.py" --folder "/caminho/dos/dados"
```

## Quando o teste passa com UnRAR, mas a extração com unar falha

`unrar t` testa a integridade dos membros selecionados. Ele não realiza a extração nem aciona a movimentação do script para backup. Para fazer a extração completa com esse backend, executar da pasta dos RARs:

```bash
git -C "$HOME/LeitorDeDados-linux" pull --ff-only &&
python3 "$HOME/LeitorDeDados-linux/scripts/extrair_rars.py" --backend unrar
```

O modo `--backend unrar` usa `unrar x` em uma área temporária nova. Ignora configurações extras do UnRAR, não solicita senha e só arquiva os volumes quando a extração completa retorna sucesso. Caminhos e membros são verificados antes; nomes repetidos ou equivalentes quanto à caixa são recusados para evitar omissões silenciosas. As pastas parciais antigas permanecem preservadas e são ignoradas. É necessário espaço para outra extração completa.

O padrão continua sendo `--backend unar`. O campo `backend` do relatório identifica a ferramenta escolhida; logs terminam em `.unar.log` ou `.unrar.log`, conforme o caso. A opção se aplica também aos RARs aninhados encontrados durante a execução. [Comandos do UnRAR](https://manpages.ubuntu.com/manpages/jammy/man1/unrar-nonfree.1.html).

## Resultado

- Percorre a origem e suas subpastas sem seguir links. Pastas chamadas `backup` e áreas de extração incompleta são ignoradas.
- Seleciona somente arquivos `.rar`, sem diferenciar maiúsculas/minúsculas. ZIP e 7z permanecem intactos.
- Usa o `lsar` para identificar os volumes de cada conjunto e conferir os membros. Conjuntos multipartes só prosseguem quando o backend consegue listar suas partes e seu conteúdo. Nomes de volumes são preservados e eles ficam juntos no backup.
- Extrai cada conjunto para uma área temporária no mesmo disco. Após sucesso, publica uma pasta com o sufixo `_extraido`, por exemplo `clientes_extraido`.
- Move os originais para `backup/EXECUCAO/...`, preservando os caminhos relativos das subpastas. Cada execução tem um identificador próprio para evitar sobrescrever backups anteriores.
- Processa também novos RARs encontrados dentro do conteúdo extraído, até 12 rodadas. O limite pode ser alterado com `--max-depth`.
- Mantém arquivos e pastas de outros formatos. A saída do programa termina com um relatório JSON com contagem de conjuntos, volumes, falhas e RARs restantes.
- Salva esse relatório também em `backup/last-extraction-report.json`. A saída completa de cada chamada do `unar` fica em `backup/EXECUCAO/logs/ID.unar.log`; o caminho é mostrado antes da extração.

Exemplo:

```text
antes:                          depois:
clientes.rar                    clientes_extraido/dados.csv
documento.txt                   documento.txt
                                backup/EXECUCAO/clientes.rar
```

## Falhas e retomada

O script não sobrescreve uma pasta `_extraido` existente. RARs com falha de extração, volumes ausentes, senha necessária ou listagem inválida permanecem na origem e são relacionados no relatório. Uma extração parcial permanece em `.rar-pending-ID` para conferência; essa pasta não é processada nas próximas execuções.

Para consultar os erros depois que o programa terminar, executar da mesma pasta de origem:

```bash
cat backup/last-extraction-report.json
```

Cada falha do descompactador inclui `archive`, `tool`, `tool_exit_code`, `log`, `partial_output` e `diagnostic`. O último campo contém o final da saída do `unar`; o log guarda a saída completa, incluindo mensagens de CRC/checksum, volume ausente, permissões e outros erros que a ferramenta informar. Falhas na listagem incluem a saída disponível do `lsar` e seu código de retorno. Não interpretar o código sozinho como prova de um tipo específico de falha.

Enquanto a extração estiver rodando, acompanhar o arquivo de log indicado com `tail -f "CAMINHO_DO_LOG"` em outro terminal. Os logs podem conter nomes de arquivos e devem permanecer privados. A atualização não recupera mensagens que uma execução antiga deixou apenas no terminal; é necessário repetir a tentativa do RAR que falhou para gerar um novo log. As pastas `.rar-pending-ID` são ignoradas e preservadas.

A estimativa de espaço usa os tamanhos declarados na listagem, comparados ao espaço livre do disco de destino. Isso não é uma reserva de espaço: alterações no disco ou metadados incorretos ainda podem fazer a extração falhar. O original só é movido após o descompactador concluir com sucesso.

Se ocorrer falha ao mover os volumes para backup, o script tenta devolver os volumes já movidos à origem. A pasta extraída concluída é preservada. Conferir origem, saída e backup antes de repetir; a presença da saída impede sobrescrita automática.

Existe uma reserva `.rar-extraction-lock` na raiz enquanto o processo está ativo. Após queda de energia ou encerramento forçado, conferir se não existe outra execução antes de remover manualmente uma reserva pendente.

A listagem limita-se a 64 MiB e 100 mil membros por conjunto. Caminhos absolutos, `..`, links e arquivos especiais são recusados. Essas verificações não substituem atualizar o descompactador nem testar a ferramenta no destino. O script não executa os programas encontrados nos arquivos.

## Validação

Os testes locais usam Python no Windows com o backend simulado: conferem nomes com espaços/colchetes, conjunto multipartes movido uma única vez, falhas preservando os originais, proteção contra sobrescrita, extração recursiva e tentativa de restauração após falha no movimento. A extração real com `unar` no Linux ainda precisa ser confirmada com um lote pequeno no destino.

Referências: [unar e suas opções](https://manpages.ubuntu.com/manpages/noble/man1/unar.1.html), [lsar e listagem JSON](https://manpages.ubuntu.com/manpages/noble/man1/lsar.1.html), [volumes RAR](https://www.rarlab.com/rar_file.htm).
