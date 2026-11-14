# Prova de conceito e escolha do banco

O objetivo é escolher a combinação mais simples que cumpra as consultas reais, a integridade e o custo da empresa. A implementação SQLite passou por testes funcionais com bases pequenas; os passos abaixo definem a validação de capacidade, ainda não executada no volume de destino. Ver [validação da entrega](validacao.md).

**Carga confirmada:** busca exata por CPF, e-mail, telefone ou código em acervo estático; serviço somente de leitura, com publicação nova inexistente ou muito rara. O [desenho de busca](busca-identificadores.md) e o [plano SQLite](sqlite-somente-leitura.md) são as referências principais. Relatórios, full-text e atualização de cadastros ficam como possíveis extensões futuras.

## 1. Fixar a carga antes de otimizar

Preencher o contrato de carga com as quatro buscas por igualdade, esquema por entidade, bytes médios, retenção e usuários concorrentes. Separar prazo de preparação/importação/indexação inicial da latência do serviço já publicado. Registrar a frequência de versões novas, caso existam.

| Dimensão | Informação necessária |
| --- | --- |
| Acervo | Total estático por entidade, até 20 bilhões; não supor que tudo será uma tabela ou um arquivo. |
| Preparação | Prazo de importação e indexação inicial, espaço temporário e frequência de eventual nova versão. |
| Leitura | Consultas/s, sessões ativas, número e tamanho dos resultados. |
| Tempo de resposta | p50, p95 e p99 por classe de consulta; prazo de exportação em lote. |
| Consistência | Identidade dos registros e versão de acervo fixa entre arquivos e páginas. |
| Recuperação | RPO, RTO e operação durante falha de nó. |
| Custo | Limite mensal ou investimento, equipe disponível e suporte. |

Metas de latência devem ser aprovadas pelo negócio antes da medição. Não adotar um único SLA para localizar uma pessoa e exportar milhões de vendas.

## 2. Preparar dados representativos

Usar amostra anonimizada ou dados sintéticos que preservem comprimentos, UTF-8, cardinalidade, distribuição temporal, nulos e concentração por usuário. Uniformidade artificial e strings excessivamente repetidas podem favorecer indevidamente índices e compressão.

Incluir datas fora de ordem, reenvio de lotes, usuários com muitas vendas, campos ausentes, identificadores inválidos e códigos repetidos em origens diferentes. Incluir novos registros legítimos com o mesmo CPF/email/telefone. Separar cópias com e sem reenvios duplicados para testar a semântica esperada. Conservar um conjunto pequeno com resultado de referência calculado independentemente.

Avançar em etapas compatíveis com os recursos: conjunto pequeno para correção; 10 milhões; 100 milhões; conjunto maior que a RAM disponível; 1 bilhão se necessário; finalmente ensaio no volume ou topologia de destino. Esses marcos são proposta de protocolo, não requisito de máquina. Não projetar latência em 20 bilhões por simples multiplicação de um teste que cabe em cache.

Registrar checksum dos arquivos, gerador e semente, esquema, distribuição e quantidade exata. Não colocar dados reais de usuários no Git.

## 3. Matriz de consultas

Executar primeiro os quatro acessos por igualdade, separadamente. A matriz abaixo representa o requisito atual, incluindo leitura do conteúdo e situações que podem esconder custo elevado.

| Caso | Consulta | O que observar |
| --- | --- | --- |
| Q1 | Registros por CPF normalizado | Zeros preservados, múltiplas vendas e caminho de índice/roteamento. |
| Q2 | Registros por e-mail normalizado | Regra de caixa, valores longos e índice independente. |
| Q3 | Registros por telefone normalizado | País/região, identificadores compartilhados e índice independente. |
| Q4 | Registros por código e escopo de origem | Não confundir códigos iguais de sistemas diferentes. |
| Q5 | Cada busca sem nenhum resultado | Custo de localizar ausência em todos os nós/partições necessários. |
| Q6 | Cada busca com 1, 100 e muitos resultados | Conteúdo completo, paginação, distribuição concentrada e volume de saída. |
| Q7 | Página profunda com cursor vinculado à versão | Continuidade e custo sem OFFSET crescente; manter a mesma versão entre páginas. |
| Q8 | Exportar todos os registros de um identificador | Tempo total e impacto nas consultas interativas. |
| Q9 | Publicar nova versão, se isso fizer parte da operação | Troca de manifesto, arquivos consistentes, cursores antigos e impacto da cópia nas leituras. |
| Q10 | Reenviar lote durante a preparação offline | Ausência de duplicação indevida sem apagar registros legítimos com identificadores repetidos. |

Limitar projeção de colunas e retorno de linhas em consultas interativas. Incluir buscas sem resultados e casos que concentram grande parte do acervo, não apenas filtros fáceis.

## 4. Comparar candidatos com justiça

Começar com SQLite somente leitura versus PostgreSQL, com índices específicos e o mesmo conteúdo retornado. Construir e validar os índices antes do ensaio de leitura. Medir um arquivo e a solução com vários arquivos quando o volume exigir; incluir roteamento, recuperação do conteúdo e paginação global. Outros motores entram se um limite concreto justificar ampliar a comparação. Escrita concorrente não é critério principal deste perfil estático.

Manter orçamento de CPU, RAM e disco comparável. Contabilizar índices, agregados, réplicas e tempo para construí-los; se uma otimização for usada em um candidato, registrar o custo e oferecer otimização equivalente quando a semântica permitir.

Fixar versões, SO/kernel, CPU, RAM, filesystem, armazenamento, rede, parâmetros, esquema e consultas. Conservar DDL e configuração usados em cada execução. Distinguir resultados em um nó, com réplicas e com shards; um ensaio de um nó não valida recuperação de um cluster.

## 5. Medir sob carga

Separar aquecimento, medição com cache quente e cenário de cache frio. Registrar o procedimento de cache frio e confirmar ausência de consultas em cache. Não limpar caches ou reiniciar serviços em ambiente compartilhado de produção.

Executar repetidamente a mesma distribuição de consultas, variando valores dos filtros. Começar com concorrência 1, depois níveis compatíveis com a carga prevista, por exemplo 8 e 32, até caracterizar saturação. A medição principal usa acervo publicado sem escrita. Se houver versões raras, medir separadamente o impacto de construir/copiar/publicar outro conjunto enquanto a versão anterior continua atendendo; não modificar arquivos declarados imutáveis.

O gerador de carga deve registrar espera de fila, latência fim a fim, taxa oferecida e taxa concluída. Um cliente que espera cada resposta antes de enviar outra requisição pode esconder atraso de fila; anotar esse limite do teste. Percentis altos exigem amostras suficientes: não apresentar p99 confiável com meia dúzia de execuções. Reportar erros e timeouts junto da latência.

Coletar:

- Latência por consulta p50/p95/p99, throughput concluído e erros.
- Linhas/bytes lidos e retornados, plano de execução, CPU e pico de RAM.
- Na fase offline: tempo e vazão de importação, criação de índices, estatísticas, validação e cópia dos arquivos.
- Disco ocupado por dados, índices e estruturas derivadas; WAL/logs e espaço temporário.
- I/O, rede, cache, tamanho dos arquivos, fan-out da busca e desequilíbrio entre servidores de leitura; métricas de replicação quando aplicáveis.

No SQLite, examinar `EXPLAIN QUERY PLAN`, confirmando o índice de cada identificador; no PostgreSQL, `EXPLAIN (ANALYZE, BUFFERS)` em ambiente de teste. Verificar também busca sem resultado e recuperação do texto completo. O tempo de um cliente isolado não explica o custo de uma consulta. [SQLite — plano de execução](https://www.sqlite.org/eqp.html), [PostgreSQL — EXPLAIN](https://www.postgresql.org/docs/current/using-explain.html).

## 6. Verificar correção e recuperação

Confrontar IDs e conteúdo retornados com o conjunto de referência para cada identificador. Conferir zeros, normalização, origem e total de resultados nas páginas. Validar reenvios na preparação e publicação conjunta dos arquivos/índices de uma versão. Se houver exclusão por retenção ou outra necessidade, testar a geração e publicação da versão correspondente, com tratamento explícito de cursores antigos.

Em infraestrutura isolada e com dados de teste, simular interrupção da construção/cópia, arquivo incompleto ou checksum divergente, reinício e indisponibilidade de um servidor de leitura. Impedir a publicação de conjuntos incompletos. Medir recuperação e efeito nas consultas. Validar backup restaurando em instância separada e conferindo conteúdo, índices e manifesto de versão. Uma cópia disponível não comprova restauração correta.

## 7. Converter resultados em capacidade e custo

Alimentar a [calculadora](../scripts/estimate_capacity.py) com bytes e compressão medidos. Para bancos sem compressão global aplicável, usar fator 1 ou medir diretamente o tamanho físico por linha com o esquema e os índices completos. Separar os números de cada entidade e motor; somar no orçamento final.

Calcular custo mensal de infraestrutura, backups, tráfego, suporte e operação. Registrar custo de ampliação e tempo para carregar/reindexar/rebalancear o acervo. Medir espaço e vazão durante reconstrução de réplica; a recuperação disputa recursos com a carga normal.

Usar o resultado para decidir quantidade/tamanho dos arquivos ou partições, cópias de leitura, capacidade útil por nó, RAM para concorrência e dados acessados com frequência, banda e margem temporária. Não assumir escala linear ou custo idêntico entre instalação própria e serviço gerenciado. Para SQLite sem mecanismo de compressão medido, usar fator 1 na calculadora; contar índices explicitamente.

## Registro mínimo por execução

Salvar em `results/` (ignorado pelo Git) métricas brutas e resultados sem dados pessoais. A tabela resume a execução, mas não substitui a série de latências.

| Execução | Banco/versão | Linhas | Concorrência | Consulta | p50/p95/p99 | Erros | Bytes lidos | Disco total | Versão do acervo |
| --- | --- | ---: | ---: | --- | --- | ---: | ---: | ---: | --- |
| A preencher | A preencher | — | — | — | — | — | — | — | — |

Critério de conclusão: correção aprovada; metas de leitura cumpridas sob concorrência; preparação inicial, publicação rara e recuperação dentro dos prazos; custo dentro do orçamento. Se falhar, localizar o limite observado antes de adicionar componentes ou comprar máquinas.
