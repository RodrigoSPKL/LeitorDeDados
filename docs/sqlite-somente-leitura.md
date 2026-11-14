# SQLite para um acervo somente de leitura

O requisito é um acervo estático, consultado por CPF, e-mail, telefone ou código. Novas publicações serão raras. SQLite é a base da implementação inicial; PostgreSQL permanece como candidato de comparação. O projeto já inclui importador, bancos de demonstração e serviço de consulta. Este documento registra o desenho; o [manual Linux](rodar-linux.md) descreve a execução. Não há ensaio de capacidade ou desempenho em 20 bilhões de registros.

SQLite pode operar atrás de um servidor de aplicação, que recebe pedidos e executa SQL sobre arquivos locais. A documentação reconhece esse uso. PostgreSQL continua sendo alternativa quando administração centralizada, permissões do banco e recursos operacionais justificarem o serviço adicional. [Usos apropriados do SQLite](https://www.sqlite.org/whentouse.html).

## Preparar fora do serviço e publicar uma versão

Fluxo proposto:

```text
origens → preparação offline → índices, estatísticas e validação
        → arquivos locais versionados → API de consultas
```

Na preparação, normalizar os identificadores preservando o original e definir origem, escopo do código e identidade de cada registro. CPF, telefone e códigos mantêm zeros. Um identificador pode corresponder a muitas vendas; não declarar unicidade apenas porque será pesquisado. Separar reenvio duplicado de ocorrência legítima.

Começar com índices independentes para os quatro acessos. Um índice composto por CPF, e-mail e telefone não oferece automaticamente os mesmos caminhos. Confirmar busca e ordenação dos resultados pelo plano de execução, incluindo recuperação do texto completo. Não colocar todo o conteúdo nos índices sem medir o custo. [Planejamento e índices](https://www.sqlite.org/queryplanner.html).

Construir os índices e coletar estatísticas antes de congelar a versão. `ANALYZE` registra informações usadas pelo otimizador; `PRAGMA optimize` é a abordagem recomendada atualmente para decidir análises necessárias. Registrar versão da biblioteca, esquema, configuração, estatísticas e planos aprovados. Não executar manutenção que altere o arquivo publicado como imutável. [Estatísticas e recomendações oficiais](https://www.sqlite.org/lang_analyze.html).

Validar contagens, amostras com resultados conhecidos, valores ausentes, duplicidades e integridade. `PRAGMA integrity_check` verifica estruturas internas; conferir separadamente relações e regras de negócio, pois esse comando não substitui a validação semântica nem verifica violações de chaves estrangeiras. [Integrity check](https://www.sqlite.org/pragma.html#pragma_integrity_check).

## Somente leitura e imutabilidade

`mode=ro` abre aquela conexão para leitura. Isso não garante que outro processo deixe o arquivo intacto. `immutable=1` faz uma afirmação mais forte: o arquivo não mudará. SQLite o abre somente para leitura e dispensa locks e detecção de alterações. Se houver mudança externa, pode produzir resultados incorretos ou erros `SQLITE_CORRUPT`. [Parâmetros URI](https://www.sqlite.org/uri.html).

Usar `immutable=1` apenas nas versões efetivamente congeladas. Nunca editar, truncar ou sobrescrever um arquivo servido desse modo. Uma escrita rara exige coordenação ou uma nova versão; frequência baixa não equivale a imutabilidade.

Proposta de publicação: gerar arquivos novos com nomes próprios, finalizar a preparação, fechar escritores, conferir checksums e copiar para armazenamento local de cada servidor. Publicar somente depois da validação da cópia. Se a preparação usou WAL, finalizar corretamente o snapshot antes da distribuição; copiar apenas o arquivo principal de um banco ainda ativo pode omitir transações presentes no WAL. [WAL e arquivos associados](https://www.sqlite.org/wal.html).

Um manifesto identifica versão, arquivos, esquema, contagens e checksums. Cada consulta e cursor permanece vinculado à mesma versão. Trocar o manifesto para novos pedidos sem misturar arquivos de versões diferentes; preservar a anterior até seus leitores e cursores expirarem. Se ela deixar de estar disponível, a API deve rejeitar o cursor antigo e pedir reinício da busca. Esse protocolo é uma proposta da aplicação, não uma função automática do SQLite.

## Capacidade e divisão em arquivos

Com páginas padrão de 4 KiB, o limite documentado por arquivo é aproximadamente **17,5 TB**, incluindo tabelas e índices. Páginas de 64 KiB permitem aproximadamente **281 TB**, mas esse limite superior não foi integralmente testado pelos desenvolvedores. Limites do filesystem também se aplicam. Nenhum desses números é benchmark ou garantia de latência. [Limites oficiais](https://www.sqlite.org/limits.html).

Vinte bilhões de registros não são impossíveis apenas pela contagem. É necessário medir bytes por registro, cada índice e espaço de construção. Por exemplo, 20 bilhões × 1.000 bytes representam 20 TB lógicos antes dos índices; isso não determina diretamente o tamanho físico SQLite.

Se houver divisão, explicar o roteamento dos quatro identificadores. Dividir por CPF não localiza automaticamente um e-mail em um único arquivo. Comparar consultar índices locais em vários arquivos com um lookup estreito que encaminhe ao arquivo e ID corretos. Este último acrescenta estruturas e uma segunda leitura; quatro associações por registro podem somar 80 bilhões de associações. Paginação global e misses também entram no teste.

Não planejar anexar todos os shards em uma conexão. `ATTACH` admite 10 bancos anexados por padrão e máximo compilável de 125. Isso não limita o total de arquivos administrados pela aplicação por conexões separadas. [ATTACH](https://www.sqlite.org/lang_attach.html), [limites](https://www.sqlite.org/limits.html).

## Cópias e critérios de aprovação

Cópias locais da mesma versão podem atender consultas em servidores diferentes. Manter backup separado, manifesto e checksums; testar restauração e resultado das buscas. Checksum comprova identidade dos bytes, não correção do conteúdo. Para cópia de banco ainda gravável, usar mecanismo de snapshot consistente, como a API de backup, em vez de copiar um arquivo em mudança. [Backup oficial](https://www.sqlite.org/backup.html).

Aprovar após comparar SQLite e PostgreSQL com índices equivalentes: cache quente/frio, concorrência, p95/p99, misses, múltiplos resultados, leitura completa, disco e tempo de preparação, cópia e recuperação. A escolha depende dessas medidas e do custo operacional, seguindo o [plano de benchmark](plano-benchmark.md).
