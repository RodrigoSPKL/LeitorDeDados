# Busca exata em 20 bilhões de registros

Requisitos confirmados: consultas por CPF, e-mail, telefone ou código; dados recebidos como texto; acervo estático, com escrita inexistente ou muito rara. Esta proposta assume uma busca por identificador de cada vez e retorno paginado dos registros correspondentes. Combinações de campos ou busca em todos eles de uma vez serão casos adicionais se necessários.

## Direção técnica

O ensaio de capacidade deve comparar **SQLite somente leitura** e **PostgreSQL**, com os mesmos acessos e conteúdo retornado. A implementação SQLite atual usa lookup separado e passou por testes funcionais pequenos. O resultado em escala ainda depende de tamanho, I/O, concorrência e roteamento entre arquivos. ClickHouse e ScyllaDB ficam como alternativas se o teste identificar necessidade de outra organização ou distribuição. A comparação entre motores ainda não foi executada.

Não escolher apenas pelo rótulo embarcado/servidor: a medição precisa cobrir a consulta inteira, incluindo o texto do registro, falhas de localização, cache frio e concorrência. O [plano SQLite](sqlite-somente-leitura.md) detalha o uso de arquivos estáticos. [SQLite — planejamento](https://www.sqlite.org/queryplanner.html), [PostgreSQL — índices](https://www.postgresql.org/docs/current/indexes-types.html).

## Identidade e normalização

Um CPF pode ocorrer em milhares de vendas. E-mail, telefone e códigos de origens diferentes também podem se repetir. **Identificador pesquisável não equivale a ID único do registro.** Usar um `record_id` próprio e manter origem e identidade de ingestão para reconhecer reenvios. Não eliminar vendas legítimas apenas porque o CPF coincide.

Preservar o valor original e gerar um campo normalizado durante a ingestão:

| Campo | Regra a estabelecer |
| --- | --- |
| CPF | Texto com zeros preservados; aceitar/remover apenas a pontuação definida no contrato; validar formato e tratar inválidos sem fabricar valor válido. |
| E-mail | Preservar original; definir explicitamente tratamento de maiúsculas, espaços e domínio. Não alterar irreversivelmente a parte local sem regra da origem. |
| Telefone | Conhecer país/região para normalização; separar ramal; não inventar DDI quando a origem for ambígua. |
| Código | Preservar zeros e caixa conforme a origem; definir se a chave inclui sistema, empresa ou tipo do registro. |

A API deve receber o tipo de identificador explicitamente. Inferir CPF ou telefone apenas pelo número de dígitos pode ser ambíguo. Ausência de campo não gera um identificador vazio compartilhado por milhões de linhas. Aplicar a mesma normalização ao parâmetro recebido antes da consulta.

## Modelo A: registros com índices independentes

Primeiro baseline em SQLite e PostgreSQL: tabela por entidade ou contrato estável, `record_id` e colunas normalizadas. Testar índices separados, por exemplo `(cpf_norm, record_id)`, `(email_norm, record_id)`, `(telefone_norm, record_id)` e `(origem, codigo_norm, record_id)` quando código só for único dentro da origem. Ajustar o escopo por empresa somente se a API também o fornecer. Confirmar a representação de ID e a ordenação em ambos os motores.

Um índice composto `(cpf_norm, email_norm, telefone_norm)` não equivale aos três caminhos independentes. As regras de uso de B-tree multicoluna dependem da posição e dos filtros; validar o plano de cada busca. [Índices multicoluna](https://www.postgresql.org/docs/current/indexes-multicolumn.html).

Exemplo conceitual de página seguinte, com parâmetro de tamanho limitado pela API:

```sql
SELECT record_id, origem, texto_original
FROM registros
WHERE cpf_norm = $1 AND record_id > $2
ORDER BY record_id
LIMIT $3;
```

Esse SQL ilustra o modelo A, que não é a estrutura desta entrega. A implementação usa o modelo B, com lookup e conteúdo separados. A primeira página omite o cursor. A ordenação por ID dá continuidade estável; não promete ordem cronológica da venda. Recuperar o conteúdo faz parte do custo a medir.

O cursor por ID também não cria uma fotografia do acervo durante ingestão concorrente: IDs aleatórios ou registros publicados tardiamente podem entrar antes do cursor. Definir se a navegação aceita dados em movimento ou usa uma fotografia de lotes publicados, identificada por watermark no cursor. O exemplo SQL não implementa esse controle; acrescentá-lo se a API prometer uma visão fixa entre páginas.

No cenário atual, a versão publicada fica fixa. Incluir a versão do acervo no cursor e mantê-la disponível enquanto houver páginas em andamento; uma troca rara de versão não deve misturar resultados de arquivos diferentes. Não modificar arquivos abertos como imutáveis.

No ClickHouse, comparar uma tabela-base com campos normalizados e projeções leves contendo chave de ordenação e `_part_offset`. Elas permitem localizar e ler o conteúdo na tabela-base sem quatro cópias completas do payload. Acrescentam armazenamento, trabalho de ingestão e análise de índices. A versão 25.11 acrescentou pruning por grânulo; medir o comportamento da versão exata usada. [Projeções](https://clickhouse.com/docs/concepts/features/projections/projections), [release 25.11](https://clickhouse.com/blog/clickhouse-release-25-11).

## Modelo B: lookup separado, se a distribuição justificar

```text
registros:
    record_id -> origem, campos, conteúdo

lookup:
    tipo + escopo + valor_normalizado -> record_id, record_id, ...
```

O lookup armazena associações estreitas; a aplicação pagina os IDs e busca o conteúdo em lote. Distribuir o lookup pelo token pesquisado e a base por `record_id` é uma hipótese para evitar consultar todos os servidores, com duas etapas de acesso. Não duplicar o conteúdo inteiro em cada associação.

No PostgreSQL, particionamento por hash organiza dados em partições locais; não transforma uma máquina em um cluster distribuído. Citus ou outro mecanismo de distribuição precisa ser avaliado separadamente. Se as duas tabelas tiverem chaves de distribuição diferentes, as consultas não são naturalmente colocadas; buscar IDs e conteúdo em duas etapas explícitas em vez de presumir JOIN distribuído barato. [PostgreSQL — particionamento](https://www.postgresql.org/docs/current/ddl-partitioning.html), [Citus — escolha da distribuição](https://docs.citusdata.com/en/stable/sharding/data_modeling.html).

ScyllaDB oferece índices secundários globais que distribuem pelo valor indexado e guardam chaves da base; a consulta também precisa recuperar os dados da base. Comparar esses índices com tabelas de acesso explícitas. Identificadores muito repetidos podem produzir partições grandes ou concentrar tráfego; bucketing limita tamanho, mas acrescenta leituras e coordenação. [Índices secundários](https://docs.scylladb.com/manual/stable/features/secondary-indexes.html), [modelagem orientada às consultas](https://docs.scylladb.com/stable/get-started/data-modeling/query-design.html).

Essa separação não é automaticamente mais barata: adiciona linhas de lookup, índices, segunda leitura e sincronização. Comparar com os índices diretamente na tabela antes de adotá-la. Se não houver transação cobrindo todo o lote distribuído, definir status de publicação, retries idempotentes e reconciliação para evitar associação sem registro ou registro inacessível pelo índice.

## Roteamento e armazenamento

Particionar somente por data não permite eliminar partições em uma consulta sem data. Dividir o acervo pelo CPF ajuda esse acesso, mas uma consulta só por e-mail pode continuar alcançando todos os shards. Toda proposta de distribuição deve mostrar o caminho para **cada um dos quatro identificadores**. Não criar uma partição por CPF ou uma por e-mail.

Com quatro campos preenchidos uma vez por registro, o modelo de lookup separado pode produzir **80 bilhões de associações** para 20 bilhões de registros. Campos ausentes reduzem esse total; múltiplos telefones/e-mails por registro podem aumentá-lo. Como ilustração aritmética, 80 bilhões × 32 bytes seriam 2,56 TB só de conteúdo das associações, antes de qualquer overhead, índice físico ou réplica. Os 32 bytes são uma hipótese, não tamanho real de B-tree ou armazenamento medido.

Medir separadamente conteúdo, cada índice/projeção, estruturas de lookup, réplicas, backups e espaço temporário. Na calculadora, executar cenários separados quando as estruturas tiverem compressões diferentes; não assumir que o fator do texto também descreve os índices.

OpenSearch com `keyword` e consultas `term` é uma alternativa de busca exata, mesmo sem full-text. Se entrar no ensaio, mapear explicitamente os campos, medir shard fan-out, armazenamento e recuperação do registro completo. Busca textual e análise linguística ficam fora do requisito atual. [Keyword](https://docs.opensearch.org/latest/mappings/supported-field-types/keyword/), [Term](https://docs.opensearch.org/latest/query-dsl/term/term/).

## Aprovação do desenho

Testar cada campo com 0, 1, 100 e muitos resultados, distribuição real de valores e concorrência de leitura. Medir p95/p99 fim a fim, disco total, erros e recuperação. Medir separadamente tempo e espaço de construção dos arquivos e publicação de uma nova versão. Usar páginas limitadas e cursor; medir exportação completa separadamente. Reenvio e deduplicação importam na preparação; escrita simultânea no arquivo servido não é o caminho principal.

Ainda faltam tamanho real do registro, registros por identificador, concorrência, meta de resposta e prazo de preparação inicial. Esses valores decidirão o motor e a quantidade de servidores. O [plano de benchmark](plano-benchmark.md) define a execução.
