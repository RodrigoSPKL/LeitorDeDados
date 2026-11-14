# Processar a pasta atual no Linux

O comando percorre subpastas, expande ZIP, RAR e TAR aninhados, importa CSV/TXT delimitado, JSONL e bancos SQLite e publica uma versão para a API. Os arquivos originais são preservados. A extração e a preparação ficam em `.leitor/` dentro da pasta dos dados.

## Um comando com Docker

Manter o código separado, por exemplo em `/opt/leitor-dados`. Com Docker Engine instalado, entrar na pasta dos dados e chamar o script do projeto:

```bash
cd /dados/meu-lote
bash /opt/leitor-dados/processar-pasta.sh
```

O script usa a pasta de onde foi chamado, mesmo estando instalado em outro local. Constrói a imagem com o leitor RAR e monta a origem somente para leitura. Os destinos graváveis são apenas as áreas derivadas em `.leitor/`. Não precisa iniciar a API durante a preparação.

É possível manter os originais em outro disco, inclusive exFAT, e gravar os resultados em um filesystem Linux com permissões POSIX. `WORK_PATH` muda a pasta do catálogo, da extração e da publicação; o destino da API passa a ser `WORK_PATH/published`:

```bash
cd /media/usuario/ACERVO/lote-1
sudo env WORK_PATH=/dados-linux/lote-1 bash /opt/leitor-dados/processar-pasta.sh
sudo env DATA_PATH=/dados-linux/lote-1/published bash /opt/leitor-dados/scripts/linux-start.sh
```

O destino precisa ter espaço para todos os arquivos gerados. O código e as credenciais também devem ficar em filesystem Linux, como ext4 ou XFS; exFAT é aceito apenas para leitura das origens. Se o destino estiver dentro da origem, ele é excluído da varredura. Origem e destino não podem ser a mesma pasta, nem a origem ficar dentro do destino.

Evitar executar na pasta do código: ela contém exemplos e arquivos de configuração que não pertencem ao acervo da empresa. Uma pasta legítima chamada `data` dentro da origem continua sendo lida; os resultados deste comando ficam em `.leitor/published`.

## Sem Docker

Com o projeto no `PYTHONPATH` ou executando a partir da raiz dele, usar Python 3.12+ e as dependências de `requirements.lock.txt`. Para RAR comprimido, instalar também `unar` ou `unrar` no Linux.

```bash
python -m leitor_dados ingest-folder --folder /dados/meu-lote
```

Sem `--folder`, a entrada é a pasta atual. `--work-dir` muda a área de preparação; `--data-dir` muda o destino publicado. `--exclude` pode ser repetido para excluir destinos específicos. A API continua abrindo apenas os bancos finalizados de uma versão.

## Arquivos compactados e pastas

São reconhecidos ZIP, RAR de um volume, TAR, TAR.GZ/TGZ, TAR.BZ2/TBZ/TBZ2 e TAR.XZ/TXZ. A descoberta continua até encontrar arquivos finais suportados. JSONL também é aceito; não é necessário gerar cópias CSV intermediárias dos bancos ou do JSONL. Arquivos de outros formatos permanecem preservados e não são importados automaticamente. Não são executados programas encontrados nos arquivos compactados.

A extração usa cache pelo SHA-256 do arquivo, com marcador de finalização. Um arquivo incompleto ou que muda durante a leitura não é publicado no cache. Caminhos que escapam da área de extração, links, dispositivos e arquivos especiais são rejeitados. Arquivos criptografados e RAR multipartes exigem preparação externa nesta versão.

O nome do compactado não vira uma pasta lógica adicional. Por exemplo, `clientes/1.zip` com `1.csv` e `clientes/2.zip` com `2.csv` contribuem para a pasta lógica `clientes`. Subpastas realmente existentes dentro dos arquivos são preservadas.

## Como as tabelas são agrupadas

O catálogo é um banco real em `.leitor/catalog.sqlite`, com uma tabela para cada combinação de pasta e estrutura de colunas. Os nomes têm prefixo da pasta e uma assinatura, evitando colisões. `.leitor/last-report.json` relaciona nome, pasta, colunas, quantidade de linhas e campos pesquisáveis.

Exemplo:

```text
clientes/1.csv   CPF;NOME;EMAIL
clientes/2.csv   email;cpf;nome
clientes/3.csv   cpf;nome;email
vendas/1.csv     codigo;cpf;valor
```

As três partes de clientes entram na mesma tabela, independentemente do nome do arquivo ou da ordem das colunas. Vendas ficam separadas. Arquivos da mesma pasta com colunas diferentes geram outra tabela. Cabeçalhos são normalizados com Unicode NFKC, remoção de espaços externos e casefold; não são inferidas equivalências como `cliente_id` e `codigo_cliente`.

Um SQLite de origem pode ter várias tabelas. Seus nomes são preservados como parte do agrupamento, normalizados quanto à caixa: bancos `1.db` e `2.db` com a tabela `Users/users` e as mesmas colunas contribuem para a mesma tabela de destino. Tabelas diferentes dentro de um DB continuam distintas, mesmo com colunas iguais. Views e tabelas virtuais não são materializadas.

`DB` aqui significa SQLite válido, fechado e consolidado em modo DELETE. Não se trata de um importador de qualquer formato proprietário com extensão `.db`. Bancos em WAL ou com journal pendente são recusados, para não ignorar transações nem escrever na origem.

Os valores do catálogo são texto ou NULL. Objetos/arrays JSON são armazenados como texto JSON; BLOBs usam uma string JSON com `{"$binary":"base64","data":"..."}`. Zeros já perdidos numa coluna numérica da origem não podem ser reconstruídos.

## Duplicatas e novas execuções

**Linhas duplicadas são permitidas.** Não há upsert por CPF, e-mail ou telefone. Duas vendas com o mesmo CPF permanecem dois registros. Arquivos distintos podem contribuir com linhas idênticas.

O histórico de importação distingue arquivos já confirmados por origem, hash e tabela. Reexecutar o comando pula arquivos inalterados; se o catálogo e a configuração de consulta não mudaram, também pula a reconstrução da versão publicada. A conferência de hashes ainda lê as fontes.

Quando o catálogo muda, a versão de consulta é reconstruída a partir do catálogo completo. A importação das fontes é incremental por arquivo; a geração dos índices de uma nova versão ainda não é incremental. Em volumes muito grandes, medir esse custo antes de adotar publicações frequentes.

Um arquivo alterado é acrescentado inteiro novamente; suas linhas antigas permanecem. Isso segue a opção de permitir duplicatas e não representa atualização por uma chave de negócio. Um novo arquivo `4.csv` acrescenta linhas à tabela compatível.

Cada arquivo é confirmado numa transação com seu histórico. Se um arquivo falhar, suas inserções são revertidas; os anteriores ficam confirmados para retomada. A API mantém a versão anterior até a nova preparação terminar. A construção do snapshot de consulta ainda não retoma internamente um arquivo interrompido.

### Quantidade de colunas diferente do cabeçalho

Esse erro interrompe o arquivo atual sem descartar linhas silenciosamente. As causas incluem separador incorreto, delimitador dentro de um campo sem aspas ou cabeçalho incompatível. A mensagem informa a quantidade de colunas esperada/encontrada, o número do registro de dados e as linhas físicas; campos com quebras de linha podem ocupar várias linhas físicas.

O relatório local `WORK_PATH/last-report.json` (ou `.leitor/last-report.json` no modo padrão) contém `error_context` com `source`, `record`, `line_start`, `line_end`, `delimiter` e, em divergências de quantidade, `expected_columns`/`actual_columns`. `source` é relativo à origem; para conteúdo compactado, `arquivo.zip!pasta/dados.csv` identifica também o membro interno. Caminhos e valores dos registros não são impressos no terminal. O nome da origem fica apenas no relatório local, que deve ser mantido privado.

`catalog_rows_committed` é a quantidade de linhas novas confirmadas nesta execução; o progresso é emitido após concluir cada arquivo. A falha preserva essas linhas dos arquivos anteriores no catálogo. `published: false` indica que esta execução não publicou uma nova versão de consulta.

Conferir o arquivo indicado e corrigir o formato ou configurar explicitamente `catalog.delimiter`/`catalog.encoding`, conforme necessário. Para um arquivo extraído, ajustar a origem ou o compactado, em vez de editar o cache. Depois repetir o comando na mesma pasta de origem com o mesmo `WORK_PATH`; os arquivos inalterados passam pela conferência de hash e sua inserção é pulada. Preservar o catálogo e o histórico de importação para aproveitar a retomada.

Após atualizar o projeto, o comando abaixo retoma a importação e grava o diagnóstico local se o problema persistir:

```bash
PROJETO="$HOME/LeitorDeDados-linux"
DESTINO="$HOME/leitor-acervo/lote-1"
git -C "$PROJETO" pull --ff-only &&
sudo env WORK_PATH="$DESTINO" bash "$PROJETO/processar-pasta.sh"
sudo cat "$DESTINO/last-report.json"
```

Executar esse bloco da pasta dos dados, mantendo os caminhos usados na execução anterior. A escolha de permitir duplicatas não equivale a aceitar registros com estrutura inválida.

## Campos de busca e configuração

CPF, e-mail, telefone e código são reconhecidos por nomes comuns das colunas. É uma convenção de cabeçalho, não uma inferência de identidade pelo conteúdo. É possível sobrescrever os campos em `catalog.fields`, assim como codificação e separador. Ver `config/folder.example.json`.

```bash
python -m leitor_dados ingest-folder --folder /dados/meu-lote --config /opt/leitor-dados/config/folder.example.json
```

No script Docker, a pasta `config` do projeto está montada em `/config`. Para usar uma configuração ajustada, fornecer por exemplo `--config /config/folder.empresa.json`; um arquivo colocado na própria pasta dos dados pode ser indicado como `/scan/opcoes.json`.

O padrão é UTF-8 com BOM opcional; para fontes Windows legadas, configurar por exemplo `catalog.encoding` como `cp1252`. O separador é detectado entre vírgula, ponto e vírgula, tabulação e barra vertical; em caso ambíguo, configurar `catalog.delimiter` explicitamente. CSV/TXT precisa de cabeçalho. JSONL precisa manter o mesmo conjunto de chaves em cada arquivo.

Identificadores inválidos não descartam a linha: no fluxo de pastas o padrão é pular somente aquela entrada de lookup e registrar a quantidade no relatório. Tabelas sem identificadores reconhecidos ficam no catálogo e no snapshot, mas não são encontradas por esses quatro acessos. Nas respostas da API, `data._ld` identifica tabela/pasta, e `data.record` contém os valores.

## Consultar o resultado

Depois da preparação, o destino é `/dados/meu-lote/.leitor/published`. Para iniciar a API do projeto usando essa base:

```bash
DATA_PATH=/dados/meu-lote/.leitor/published bash /opt/leitor-dados/scripts/linux-start.sh
```

O script reconhece a versão já publicada e não cria a demonstração sobre ela. Acessar a interface por `http://127.0.0.1:8000/` ou pelo túnel SSH do [manual Linux](rodar-linux.md). As credenciais ficam no diretório `secrets` do código, separadas do acervo.

## Limites e espaço

Os padrões de expansão são 12 níveis de arquivos aninhados, 100 mil membros, 100 GiB expandidos por varredura e 20 GiB por membro. Esses limites incluem o uso lógico de caches; fontes finais não compactadas não entram no orçamento de expansão. Para um lote maior:

```bash
bash /opt/leitor-dados/processar-pasta.sh --max-expanded-gib 500 --max-file-gib 100 --max-members 500000
```

Metadados excessivos também são limitados: diretório ZIP e metadados RAR até 64 MiB; metadados TAR PAX/GNU até 1 MiB por cabeçalho e encadeamento até 32. Arquivos esparsos TAR não são suportados. O catálogo aceita até 256 colunas e 4 MiB por registro; bancos novos usam páginas de 16 KiB, configuráveis antes da criação.

Planejar espaço para arquivos expandidos, catálogo, temporários, versão de consulta e cópias anteriores. Catálogo e snapshot são materializações distintas. Esta implementação continua monoprocesso na preparação, com até 128 arquivos de consulta por versão. Limites físicos, prazo de processamento e desempenho em 20 bilhões ainda exigem o benchmark. Se uma reserva `.pipeline-lock` permanecer após uma interrupção abrupta, confirmar que nenhum importador está ativo antes de removê-la manualmente.
