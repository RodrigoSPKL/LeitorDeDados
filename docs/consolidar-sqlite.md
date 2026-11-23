# Consolidar a pasta atual em um SQLite deduplicado

Este fluxo independente gera **um arquivo com o nome da pasta raiz**, por exemplo `RAIZ/RAIZ.sqlite`. Não exige Docker, serviço de banco ou dependências Python externas. Requer **Python 3.11+**. O projeto deve estar instalado fora da pasta de dados, por exemplo em `$HOME/LeitorDeDados-linux`.

Na pasta que contém as fontes:

```bash
git -C "$HOME/LeitorDeDados-linux" pull --ff-only &&
python3 "$HOME/LeitorDeDados-linux/scripts/consolidar_sqlite.py" --apagar-fontes
```

**`--apagar-fontes` apaga permanentemente os arquivos de origem da raiz e das subpastas e remove as pastas vazias depois da importação e verificação completas.** Ao terminar com sucesso, ficam o SQLite final e a pasta `backup`, se ela existir. `backup` não é lida, alterada ou apagada. Omitir `--apagar-fontes` para importar e verificar preservando todas as fontes; é possível executar depois com a opção para limpá-las.

Não altere, mova ou acrescente fontes durante a execução. Feche os bancos SQLite de origem e não abra o banco de saída em outro programa durante a carga ou limpeza. As verificações detectam alterações entre as etapas; não substituem acesso exclusivo à pasta. Preserve uma cópia independente dos dados importantes antes de remover as fontes. A pasta `backup` não é criada nem preenchida por este importador.

## Formatos e registros

- **CSV e TSV:** primeira linha obrigatoriamente contém os nomes das colunas; cada registro seguinte vira um objeto JSON. CSV admite campos entre aspas com quebras de linha. Detecta `,`, `;`, TAB ou `|`; `--delimiter ';'` permite definir o separador. Cabeçalhos repetidos, linhas vazias e registros com quantidade errada de campos interrompem a carga.
- **TXT:** cada linha vira `{"texto":"conteúdo da linha"}`. A primeira linha também é um registro. Preserva espaços e linhas vazias, removendo apenas o terminador de linha. Não interpreta separadores.
- **`.db`, `.sqlite`, `.sqlite3`:** somente bancos SQLite reais, fechados e consolidados em `journal_mode=DELETE`, sem WAL/journal pendente. Todas as tabelas físicas de usuário são percorridas. Não copia índices, triggers, estrutura de tabelas vazias ou DDL. Views e tabelas virtuais causam erro; exporte essas fontes para CSV antes de executar. BLOBs ficam como objetos com a chave `$sqlite_blob_base64`; valores SQL NULL permanecem JSON null, números permanecem números e textos permanecem textos.

O padrão de CSV/TXT é UTF-8 com BOM opcional, com decodificação estrita. Para um lote inteiro em Windows-1252, use `--encoding cp1252` **desde a primeira execução**. Não há substituição silenciosa de caracteres. Lotes com codificações diferentes devem ser convertidos previamente para uma codificação comum. Não adicione essa opção a um banco já iniciado com outra configuração.

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

`registros` contém `hash` (BLOB), `dados` (JSON), `fonte_id` e `tabela_origem` da **primeira ocorrência**. `fontes` guarda caminhos relativos, checksums, linhas lidas e quantas linhas únicas cada fonte introduziu. Não mantém uma associação para cada repetição descartada. `pastas` e `configuracao` contêm o manifesto e o estado da operação. Não remova essas tabelas se precisar retomar a limpeza.

O hash tem índice único; os campos dentro de `dados` **não recebem índices de consulta automaticamente**. A consulta por CPF acima pode percorrer o banco inteiro. Este arquivo não é automaticamente publicado na API existente do projeto. O objetivo deste script é consolidar e deduplicar; a preparação dos índices de busca é uma etapa separada, definida a partir dos campos reais.

## Verificação, retomada e espaço

Durante a carga aparece `.NOME.importando.sqlite`. O banco confirma lotes de 10 mil linhas (ajustável por `--batch-size`). Uma falha de importação mantém todas as fontes e o banco provisório. Para repetir, execute com as mesmas opções: fontes concluídas e inalteradas são reconhecidas pelo manifesto; a fonte interrompida é relida desde o início, e a restrição por hash impede novas duplicatas. Lotes já confirmados continuam no provisório. A retomada não salta diretamente para uma posição do CSV/TXT.

Antes da publicação, o importador executa `PRAGMA integrity_check`, recalcula o hash de cada registro e reconcilia as contagens. Também relê as fontes para conferir SHA-256 e identidade. Só então renomeia o provisório para `NOME.sqlite`. A limpeza confere novamente as fontes, registra no SQLite que começou e apaga apenas arquivos do manifesto. As pastas são removidas com `rmdir`, que recusa conteúdos inesperados.

Uma falha **durante** a limpeza pode deixar algumas fontes já removidas e outras presentes; os dados importados e verificados já estão no banco final. Repetir o mesmo comando retoma a limpeza do que resta. Uma falha ou arquivo novo bloqueia novas exclusões; não apaga fontes novas automaticamente. Uma origem modificada depois de já ter linhas confirmadas exige reconstrução em outro banco a partir das fontes preservadas; não acrescente versões modificadas ao banco existente.

Uma interrupção forçada (`kill -9`, queda de energia) pode deixar `.consolidar-sqlite.lock`. Só remova essa pasta vazia após confirmar que nenhum importador está ativo. Journals SQLite de saída são recuperados pelo SQLite; não os apague manualmente. Um banco final existente de outro programa não é sobrescrito.

O banco, índice por hash e fontes coexistem no disco até terminar a verificação. A importação não reduz o uso de disco progressivamente apagando pastas. Há várias passagens de leitura, incluindo a conferência integral, e o banco pode ser maior que os CSVs; não há um multiplicador fixo de espaço ou prazo garantido. A memória de cache do SQLite é limitada a aproximadamente 64 MiB, além do processo, registros e metadados de arquivos; o volume de linhas não é carregado todo na RAM.

Prefira disco local Linux com ext4/XFS e espaço livre medido. Este script não usa `chmod/chown`, mas sua execução em exFAT, em filesystem de rede e com volumes enormes ainda não foi validada. Não garante recuperação após falhas de hardware. **Os testes pequenos não comprovam capacidade ou desempenho em 20 bilhões de registros num único SQLite.** Faça o benchmark com uma cópia representativa antes da carga completa.
