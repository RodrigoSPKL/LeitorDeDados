# Importação e contrato das fontes

O importador prepara os arquivos fora do serviço. A API não oferece gravação. A leitura das fontes é em fluxo e as transações usam lotes. A construção é monoprocesso nesta versão e não foi medida em 20 bilhões de registros.

Para descobrir arquivos e tabelas automaticamente, inclusive em ZIP/RAR/TAR e subpastas, usar a [importação de pastas](importacao-de-pastas.md). O restante desta página descreve a preparação direta com `build` e mapeamentos explícitos.

## Formatos e configuração

`csv` aceita cabeçalho, separador configurável e campos multilinha entre aspas. TXT delimitado usa esse mesmo importador. `jsonl` aceita um objeto JSON por linha (NDJSON); um arquivo com um único array JSON não é JSONL. O conteúdo completo é preservado. Identificadores JSONL devem ser strings ou null, pois números podem ter perdido zeros. Não são inferidos nomes de campos.

Copiar `config/import.example.json` ou `config/import.jsonl.example.json`:

```json
{
  "format":"csv", "csv_delimiter":";", "encoding":"utf-8-sig",
  "fields":{"cpf":"documento","email":"email_cliente","phone":"fone","code":"pedido"},
  "normalization":{"email":{"lowercase":false}},
  "data_shards":8, "lookup_shards":16, "batch_size":1000
}
```

É permitido mapear apenas os identificadores disponíveis. No CSV, todas as colunas mapeadas devem existir; cabeçalhos repetidos e quantidades erradas de colunas são rejeitados. No JSONL, chave ausente significa identificador ausente. Campos vazios não produzem entradas no lookup. Um registro sem identificadores continua no payload, mas não é encontrado pelos quatro acessos. Objetos aninhados são preservados; o mapeamento de identificadores usa chaves do primeiro nível.

CPF mantém zeros e exige onze dígitos após remover a pontuação aceita; não verifica dígitos verificadores. Telefone remove pontuação e exige de um a quinze dígitos, sem inferir país: consultar com o mesmo país/DDD da fonte. E-mail normaliza o domínio e preserva a caixa da parte local por padrão; `lowercase:true` muda essa regra para toda a versão. Código remove espaços externos e preserva caixa. Os originais permanecem no payload.

Valores malformados interrompem a construção com referência à entrada/registro, sem imprimir seu conteúdo. Identificadores numéricos JSONL são rejeitados. Para outras convenções ou formatos, adaptar o importador após definir seu contrato.

## Misturar origens na mesma versão

Arquivos do mesmo formato e mapeamento podem ser fornecidos juntos após `--input`. Para formatos ou campos diferentes, usar um plano `--sources`, como `config/sources.example.json`:

```json
[
  {"path":"/inputs/vendas.csv","format":"csv","csv_delimiter":";","fields":{"cpf":"documento","code":"pedido"}},
  {"path":"/inputs/pessoas.jsonl","format":"jsonl","fields":{"cpf":"cpf","email":"email"}}
]
```

```bash
docker compose --profile tools run --rm tools build --config /config/import.empresa.json --sources /config/origens.empresa.json --version empresa-v1 --data-dir /data
```

Por origem, o plano sobrescreve formato, separador, codificação, campos, `sqlite_table` e `metadata` opcionais. O formato `sqlite` lê uma tabela real indicada por `sqlite_table`, em banco fechado e consolidado em DELETE. `metadata` é um objeto de strings/NULL que envolve o conteúdo em `data._ld` e `data.record`. Normalização, quantidade de arquivos e lotes vêm da configuração global. Caminhos relativos são resolvidos a partir da pasta do plano; no Docker usar `/inputs`, `/config` e `/examples`, conforme os volumes.

## Construção e publicação

O ID é sequencial e só tem significado junto da versão. Dados são distribuídos por `ID % data_shards`; associações usam hash determinístico de tipo e valor normalizado. Importador e leitor compartilham esse roteamento.

A ferramenta constrói num staging próprio e não substitui versões existentes. Após importação, estatísticas, verificação rápida e checksums, finaliza em `data/versions/<versao>`. `publish` troca apenas `current.json`, atomicamente. Nunca editar ou sobrescrever os bancos publicados: a API os abre como imutáveis.

O progresso da importação informa aproximadamente cada 100 mil registros, sem seus valores. Verificações e hashes também leem os arquivos e podem demorar. Não há retomada parcial: uma interrupção exige reconstruir. Após encerramento abrupto, staging ou reserva pode permanecer; antes de limpeza manual, confirmar que nenhum builder correspondente está ativo. A ferramenta não remove reservas de outro processo.

Não há deduplicação de conteúdo entre fontes. Um mesmo caminho repetido é rejeitado; arquivos diferentes com conteúdo igual são preservados. Deduplicação por chave de negócio depende de uma regra da empresa. Vendas legítimas com o mesmo CPF continuam distintas.

## Limites desta entrega

- Até 128 arquivos SQLite por versão, somando dados e lookup.
- Registro até 4 MiB; lote até 10 mil registros e aproximadamente 64 MiB de conteúdo, mais overhead de Python.
- Até 100 registros por página e 8 MiB de payload acumulado; página grande demais retorna 413 e deve ser reduzida.
- Oito consultas simultâneas por processo. O padrão de dois workers permite até dezesseis consultas em execução.
- Cursores assinados expiram após 24 horas sem continuação. A continuação emite outro cursor com novo prazo. Preservar a versão correspondente enquanto houver leitores/cursores válidos.

O timeout de SQLite não interrompe imediatamente uma chamada de disco bloqueada. Uma busca lê um lookup e os arquivos de payload necessários; medir custo completo, cache frio e concorrência. Novas versões raras exigem espaço para coexistirem com as anteriores.

Com quatro identificadores por registro, 20 bilhões de registros produzem 80 bilhões de associações, além do payload armazenado uma vez. Se a construção monoprocesso ou 128 arquivos forem insuficientes, ampliar a implementação antes da carga completa. Seguir o [plano de benchmark](plano-benchmark.md).
