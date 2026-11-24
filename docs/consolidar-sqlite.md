# Consolidar a pasta atual em um SQLite deduplicado

Este fluxo independente gera **um arquivo com o nome da pasta raiz**, por exemplo `RAIZ/RAIZ.sqlite`. Não exige Docker ou serviço de banco. CSV, TXT e SQLite usam apenas a biblioteca padrão; **XLSX exige as dependências opcionais abaixo**. Requer **Python 3.11+**. O projeto deve estar instalado fora da pasta de dados, por exemplo em `$HOME/LeitorDeDados-linux`.

Na pasta que contém as fontes:

```bash
git -C "$HOME/LeitorDeDados-linux" pull --ff-only &&
python3 "$HOME/LeitorDeDados-linux/scripts/consolidar_sqlite.py" --apagar-fontes
```

**`--apagar-fontes` apaga permanentemente os arquivos de origem da raiz e das subpastas e remove as pastas vazias depois da importação e verificação completas.** Ao terminar com sucesso, ficam o SQLite final e a pasta `backup`, se ela existir. `backup` não é lida, alterada ou apagada. Omitir `--apagar-fontes` para importar e verificar preservando todas as fontes; é possível executar depois com a opção para limpá-las.

Não altere, mova ou acrescente fontes durante a execução. Feche os bancos SQLite de origem e não abra o banco de saída em outro programa durante a carga ou limpeza. As verificações detectam alterações entre as etapas; não substituem acesso exclusivo à pasta. Preserve uma cópia independente dos dados importantes antes de remover as fontes. A pasta `backup` não é criada nem preenchida por este importador.

## Instalar suporte a Excel no Ubuntu

Se aparecer `Arquivo/formato não suportado: ...xlsx` numa versão anterior, essa execução parou no inventário antes de importar ou apagar fontes. Atualize o projeto e instale o leitor num ambiente virtual, mantendo o terminal na pasta dos dados:

```bash
git -C "$HOME/LeitorDeDados-linux" pull --ff-only &&
sudo apt-get install -y python3-venv &&
python3 -m venv "$HOME/LeitorDeDados-linux/.venv" &&
"$HOME/LeitorDeDados-linux/.venv/bin/python" -m pip install -r "$HOME/LeitorDeDados-linux/requirements-excel.txt" &&
"$HOME/LeitorDeDados-linux/.venv/bin/python" "$HOME/LeitorDeDados-linux/scripts/consolidar_sqlite.py" --apagar-fontes
```

Nas próximas execuções, use o Python desse ambiente virtual. Não precisa ativá-lo com `source` nem instalar pacotes Python globalmente. A ausência dessas dependências é detectada antes de abrir/criar o banco de saída quando há XLSX no lote. As configurações e os hashes dos formatos antigos continuam compatíveis; um banco provisório pode ser retomado com as mesmas opções de codificação/separador.

## Formatos e registros

- **CSV e TSV:** primeira linha obrigatoriamente contém os nomes das colunas; cada registro seguinte vira um objeto JSON. CSV admite campos entre aspas com quebras de linha. Detecta `,`, `;`, TAB ou `|`; `--delimiter ';'` permite definir o separador. Cabeçalhos repetidos, linhas vazias e registros com quantidade errada de campos interrompem a carga.
- **TXT:** cada linha vira `{"texto":"conteúdo da linha"}`. A primeira linha também é um registro. Preserva espaços e linhas vazias, removendo apenas o terminador de linha. Não interpreta separadores.
- **XLSX:** percorre todas as abas de dados, inclusive ocultas. A primeira linha não vazia de cada aba é o cabeçalho, com nomes de colunas em texto e sem repetições. Ignora linhas inteiramente vazias e completa células ausentes com JSON null. Dados fora da largura do cabeçalho, cabeçalhos inválidos ou mais de 256 campos interrompem a carga. Preserva textos, números e booleanos com seus tipos; um código armazenado como texto mantém os zeros iniciais. Não transforma a máscara numérica `00000` em zeros adicionais: importa o valor armazenado, não a apresentação visual.
- **`.db`, `.sqlite`, `.sqlite3`:** somente bancos SQLite reais, fechados e consolidados em `journal_mode=DELETE`, sem WAL/journal pendente. Todas as tabelas físicas de usuário são percorridas. Não copia índices, triggers, estrutura de tabelas vazias ou DDL. Views e tabelas virtuais causam erro; exporte essas fontes para CSV antes de executar. BLOBs ficam como objetos com a chave `$sqlite_blob_base64`; valores SQL NULL permanecem JSON null, números permanecem números e textos permanecem textos.

No XLSX, datas/horários/durações usam objetos JSON com as chaves `$excel_datetime`, `$excel_date`, `$excel_time` ou `$excel_duration_seconds`. Erros de célula ficam em `$excel_error`. Uma fórmula comum é armazenada como `{"$excel_formula":"=1+2","cached":3}`; `cached` é o resultado que já estava salvo no arquivo e pode ser null ou estar desatualizado. **O importador não calcula fórmulas nem acessa links externos.** Fórmulas matriciais/especiais não suportadas interrompem a carga. Importa os dados das células; não preserva imagens, gráficos, estilos, comentários ou a apresentação do documento. `.xls`, `.xlsb` e `.xlsm` continuam fora dos formatos aceitos; exporte para XLSX ou CSV.

A leitura usa [modo somente leitura do openpyxl](https://openpyxl.readthedocs.io/en/stable/optimized.html), ignorando dimensões incorretas declaradas pelo exportador para não truncar abas. Fórmulas e resultados salvos são lidos separadamente conforme as [opções de carregamento da biblioteca](https://openpyxl.readthedocs.io/en/stable/tutorial.html#loading-from-a-file). A [proteção XML recomendada pelo projeto](https://openpyxl.readthedocs.io/en/stable/#security) é uma dependência obrigatória do suporte XLSX.

O padrão de CSV/TXT é UTF-8 com BOM opcional, com decodificação estrita. Para um lote inteiro em Windows-1252, use `--encoding cp1252` **desde a primeira execução**. Não há substituição silenciosa de caracteres. Também existe `--encoding auto`, descrito abaixo, para arquivos com codificações diferentes. Alterações arbitrárias de codificação/separador em um banco existente continuam bloqueadas.

### CSV/TXT com `UnicodeDecodeError` ou codificações diferentes

Atualize e execute na mesma pasta de dados, usando o ambiente virtual já preparado para XLSX:

```bash
PROJETO="$HOME/LeitorDeDados-linux"
git -C "$PROJETO" pull --ff-only &&
"$PROJETO/.venv/bin/python" "$PROJETO/scripts/consolidar_sqlite.py" --encoding auto --apagar-fontes
```

Sem XLSX, pode usar `python3` em vez do Python do ambiente virtual. Mantenha eventuais opções originais como `--delimiter` e o mesmo caminho da pasta. **Não apague o banco provisório nem os arquivos de origem para tentar novamente.**

O modo `auto` decide por arquivo, antes de gravar suas novas linhas:

1. Se houver BOM, valida integralmente UTF-8, UTF-16 ou UTF-32 conforme esse marcador.
2. Sem BOM, valida o arquivo inteiro como UTF-8; se essa decodificação falhar, tenta Windows-1252 estritamente.
3. Se nenhum candidato funcionar, se houver NUL inesperado ou se o arquivo mudar durante a leitura, interrompe sem apagar fontes. Não usa `errors=replace`, não descarta bytes e não muda de codificação no meio do arquivo.

Essa é uma **política de escolha, não uma identificação infalível**: bytes podem ser válidos em mais de uma codificação. UTF-8 tem preferência; CP1252 é a alternativa definida para os lotes legados. Se a codificação real for conhecida e diferente, especifique-a desde o início ou converta uma cópia das fontes antes de iniciar o lote. UTF-16/32 sem BOM e arquivos que misturam codificações internamente exigem tratamento explícito. O log `encoding_selected` e a coluna `fontes.encoding` registram a escolha aplicada.

Uma carga **provisória** iniciada com o padrão antigo `utf-8-sig` pode ser migrada para `auto`, desde que as demais opções sejam mantidas e todas as fontes ainda existam. Fontes concluídas mantêm a codificação já usada. Se o arquivo interrompido ainda não havia confirmado linhas, ele é retomado com a codificação escolhida. Se havia registros confirmados com uma interpretação diferente, o importador verifica as fontes, remove no provisório somente os registros introduzidos por essa origem e relê todas as fontes para restaurar ocorrências compartilhadas corretamente. A alteração do banco provisório é transacional; os arquivos originais permanecem intactos. Essa recuperação pode exigir bastante leitura adicional.

Depois de adotar `auto`, continue usando `--encoding auto` nas próximas execuções. A migração não muda automaticamente um banco final já publicado, nem troca uma política explícita `cp1252` por outra. As conferências de hash e integridade e o bloqueio da limpeza em falhas permanecem iguais.

Cada registro convertido tem limite de 4 MiB; cada tabela/CSV admite até 256 campos. Os nomes das colunas são preservados, inclusive maiúsculas, espaços e acentos. Zeros iniciais dos textos permanecem. Não infere CPF, nomes ou tipos em CSV. Não descompacta RAR/ZIP/7z: faça a extração antes e guarde os compactados em `backup`.

Arquivos de outros formatos, links, pontos de montagem, backups aninhados e repositórios `.git` impedem a operação. Reúna eventuais backups internos em uma pasta chamada exatamente `backup` na raiz. O importador não apaga arquivos que não conseguiu interpretar.

## Regra de deduplicação

O SHA-256 é calculado sobre o JSON canônico do registro: todas as chaves e valores, chaves ordenadas, sem espaços de formatação. A chave de 32 bytes é única no banco inteiro. Pasta, arquivo, ordem das colunas e nome da tabela de origem não entram no hash.

Assim, `{"cpf":"00000000000","nome":"Exemplo A"}` vindo de CSV ou de colunas TEXT de SQLite entra uma única vez, mesmo em pastas distintas. Se o nome, outro valor ou o conjunto de campos mudar, é outro registro. A pessoa pode aparecer em vários registros diferentes. Nomes de campos com letras maiúsculas diferentes, NULL, texto vazio e números com tipos diferentes também são distintos. Uma linha TXT e um CSV com um único campo chamado `texto` podem coincidir se o conteúdo for exatamente igual.

Uma colisão SHA-256 com conteúdos diferentes interrompe a execução. Não compara apenas CPF, telefone ou e-mail. A deduplicação é do registro convertido, não dos bytes do arquivo original: delimitadores CSV e terminadores de linha não são parte do registro.

## Banco e consultas

Os dados ficam na tabela única `registros`, permitindo misturar estruturas diferentes:

```sql
SELECT hex(hash) AS hash, dados FROM registros LIMIT 10;

SELECT json_extract(dados, '$.cpf') AS cpf,
       json_extract(dados, '$.nome') AS nome
FROM registros
WHERE json_extract(dados, '$.cpf') = '00000000000'
LIMIT 20;

SELECT caminho, linhas, novos, linhas - novos AS duplicados
FROM fontes;
```

Para abrir na pasta de dados com o programa `sqlite3` instalado:

```bash
sqlite3 -readonly "$(basename "$PWD").sqlite"
```

`registros` contém `hash` (BLOB), `dados` (JSON), `fonte_id` e `tabela_origem` da **primeira ocorrência**. Bancos novos também têm `id INTEGER PRIMARY KEY`: os dados são acrescentados por ID, e um índice único separado guarda o hash compacto. O ID não faz parte do hash e não é um identificador de pessoa. O banco antigo `WITHOUT ROWID` continua legível; a conversão é descrita abaixo. `fontes` guarda caminhos relativos, checksums, linhas lidas e quantas linhas únicas cada fonte introduziu. Não mantém uma associação para cada repetição descartada. `pastas` e `configuracao` contêm o manifesto e o estado da operação. Não remova essas tabelas se precisar retomar a limpeza.

O hash tem índice único; os campos dentro de `dados` **não recebem índices de consulta automaticamente**. A consulta por CPF acima pode percorrer o banco inteiro. Este arquivo não é automaticamente publicado na API existente do projeto. O objetivo deste script é consolidar e deduplicar; a preparação dos índices de busca é uma etapa separada, definida a partir dos campos reais.

## Verificação, retomada e espaço

Durante a carga aparece `.NOME.importando.sqlite`. O banco confirma lotes de 10 mil linhas (ajustável por `--batch-size`), com orçamento de payload de 64 MiB (`--batch-mib`). A primeira condição alcançada fecha o lote. Repetições dentro do lote são agrupadas, e as inserções são ordenadas pelo hash e executadas em conjunto; colisões com conteúdo diferente continuam causando erro. O processo pode consumir mais memória do que o orçamento de payload por causa de objetos Python, ordenação e cache.

Uma falha de importação mantém todas as fontes e o banco provisório. Para repetir, execute com as mesmas opções: fontes concluídas e inalteradas são reconhecidas pelo manifesto. Em CSV/TXT, após conferir o checksum da fonte, o leitor percorre o prefixo de linhas já confirmado sem refazer serialização, hashes ou consultas de deduplicação. Isso ainda exige leitura e parsing do prefixo: não há seek direto para um byte do arquivo. SQLite e XLSX continuam sendo relidos e deduplicados desde o início. A migração de codificação que exige reprocessamento zera os pontos de retomada das fontes afetadas.

Antes da publicação, o importador executa `PRAGMA integrity_check`, recalcula o hash de cada registro e reconcilia as contagens. Também relê as fontes para conferir SHA-256 e identidade. Só então renomeia o provisório para `NOME.sqlite`. A limpeza confere novamente as fontes, registra no SQLite que começou e apaga apenas arquivos do manifesto. As pastas são removidas com `rmdir`, que recusa conteúdos inesperados.

Uma falha **durante** a limpeza pode deixar algumas fontes já removidas e outras presentes; os dados importados e verificados já estão no banco final. Repetir o mesmo comando retoma a limpeza do que resta. Uma falha ou arquivo novo bloqueia novas exclusões; não apaga fontes novas automaticamente. Uma origem modificada depois de já ter linhas confirmadas exige reconstrução em outro banco a partir das fontes preservadas; não acrescente versões modificadas ao banco existente.

Uma interrupção forçada (`kill -9`, queda de energia) pode deixar `.consolidar-sqlite.lock`. Só remova essa pasta vazia após confirmar que nenhum importador está ativo. Journals SQLite de saída são recuperados pelo SQLite; não os apague manualmente. Um banco final existente de outro programa não é sobrescrito.

O banco, índice por hash e fontes coexistem no disco até terminar a verificação. A importação não reduz o uso de disco progressivamente apagando pastas. Há várias passagens de leitura, incluindo a conferência integral, e o banco pode ser maior que os CSVs; não há um multiplicador fixo de espaço ou prazo garantido. O cache do SQLite tem orçamento padrão de aproximadamente 256 MiB, ajustável por `--cache-mib` (16 a 16384 MiB), além de lotes, processo, registros e metadados de arquivos. Esses limites não representam a memória total do processo; o volume inteiro de linhas não é carregado na RAM. No XLSX, o leitor também mantém metadados e tabelas de strings compartilhadas de duas leituras da planilha em memória.

Prefira disco local Linux com ext4/XFS e espaço livre medido. Este script não usa `chmod/chown`, mas sua execução em exFAT, em filesystem de rede e com volumes enormes ainda não foi validada. Não garante recuperação após falhas de hardware. **Os testes pequenos não comprovam capacidade ou desempenho em 20 bilhões de registros num único SQLite.** Faça o benchmark com uma cópia representativa antes da carga completa.

## Carga lenta em HD USB e bancos provisórios antigos

Gravar centenas de milhões de hashes pode gerar acessos aleatórios ao disco. Na estrutura antiga, os textos completos também estavam na árvore ordenada pelo hash (`WITHOUT ROWID`), aumentando o volume de páginas envolvido. A [documentação do SQLite](https://sqlite.org/withoutrowid.html#when_to_use_without_rowid) recomenda medir essa escolha e observa que tabelas rowid tendem a ser mais rápidas para registros grandes. Bancos novos agora separam os dados em uma tabela com ID e mantêm um índice compacto de hash.

Para aproveitar um **provisório antigo** sem reimportar todas as linhas já gravadas, pare o importador com **Ctrl+C no terminal original**, espere voltar ao prompt e, na mesma pasta dos dados, execute:

```bash
PROJETO="$HOME/LeitorDeDados-linux"
git -C "$PROJETO" pull --ff-only &&
"$PROJETO/.venv/bin/python" "$PROJETO/scripts/consolidar_sqlite.py" \
  --encoding auto --reorganizar --cache-mib 4096 \
  --batch-size 100000 --batch-mib 64 --apagar-fontes
```

O cache de 4096 MiB é uma opção para uma máquina com aproximadamente 32 GiB de RAM e memória disponível. Para máquinas menores, ajuste esse valor. Cache e tamanho de lote são parâmetros de execução: podem mudar numa retomada sem invalidar hashes ou opções de importação. **As fontes e o banco provisório devem permanecer onde estão. Não rode duas instâncias, não apague o provisório e não use `kill -9` para encerrar a carga.**

`--reorganizar` copia internamente os registros já confirmados para a nova estrutura, cria o índice único de hash, confere a contagem e executa `quick_check`. A troca da tabela é feita numa transação: uma falha antes do commit desfaz a conversão. Fontes não são apagadas por essa etapa. O hash, JSON, origem, contadores e pontos de retomada permanecem. Se o banco já usa a estrutura nova, não faz outra cópia. Bancos finais publicados não são convertidos por essa opção.

A conversão é uma etapa adicional e **pode demorar**, dependendo do tamanho do provisório e do disco. Requer espaço livre conservador de pelo menos três vezes o tamanho atual do arquivo, mais 128 MiB, para cópia, índice e rollback; o sort do índice também pode usar o diretório temporário do sistema. Se faltar espaço, a operação falha e preserva as fontes. Ao terminar, páginas da tabela antiga ficam livres para serem reutilizadas: o tamanho físico do arquivo pode não diminuir imediatamente. Não executa `VACUUM` automaticamente. A conferência integral dos registros continua sendo feita antes da publicação e limpeza.

O log informa as etapas `reorganizing_database`, `building_compact_hash_index`, `checking_reorganized_database` e `database_reorganized`. Ao retomar um CSV/TXT, `skipping_committed_rows` e `rows_skipped` mostram a leitura do prefixo; `source_resume_ready` indica que chegou às linhas novas. Em cada `rows_committed`:

- `linhas_lidas` e `novos_acumulados` continuam sendo **do arquivo atual**;
- `linhas_total_confirmadas` e `registros_unicos_total` somam as fontes do lote;
- `linhas_por_segundo` mede a média de linhas novas processadas na sessão desse arquivo, após o prefixo de retomada. Não inclui a futura verificação integral e não é garantia de prazo total.

Mantém `journal_mode=DELETE` e `synchronous=FULL`; não desativa durabilidade para apresentar números maiores. Cache e inserções organizadas ajudam, mas não eliminam o limite de um HD mecânico ou da conexão USB. Um SSD com capacidade suficiente pode ser necessário para a meta real. Não mova o provisório isoladamente nem reformate o disco para tentar resolver isso: caminhos e identidades das fontes fazem parte do manifesto.

### Benchmark reproduzível

O teste abaixo gera somente registros fictícios, compara os motores e confere se seus resultados são idênticos. Cria e remove apenas seu próprio diretório temporário no caminho escolhido. **Não execute junto da carga real**, pois competiria por disco e memória.

```bash
python3 "$HOME/LeitorDeDados-linux/scripts/benchmark_consolidacao.py" \
  --directory /caminho/com/espaco --rows 150000 --payload-bytes 1024 \
  --cache-mib 16 --batch-size 10000 --repeats 2 --compare-layouts
```

No desenvolvimento Windows/CPython 3.14.7/SQLite 3.50.4, 150 mil linhas, 25% de repetições, campo de texto com 1024 bytes e cache de 16 MiB produziram medianas de **12,368 s** no motor antigo, **6,646 s** com lotes na estrutura antiga e **1,795 s** com lotes e a estrutura nova: aproximadamente **6,89 vezes** sobre o antigo nesse cenário. O hash agregado dos 112.500 registros únicos foi igual nos seis resultados. Os arquivos novos mediram aproximadamente 503 MiB na estrutura antiga e 152 MiB na nova.

Um segundo cenário com textos de 512 bytes e cache de 64 MiB mostrou apenas **1,03 vez** de ganho ao agrupar inserções sem mudar a estrutura. Esses números demonstram a dependência do tamanho das linhas, cache e armazenamento. Medem somente preparação de registros e gravação: não incluem CSV, detecção de codificação, fingerprints de fontes, reorganização ou verificação final. **Não foram medidos no HD USB/exFAT do usuário e não podem ser extrapolados diretamente para 500 milhões de linhas.**
