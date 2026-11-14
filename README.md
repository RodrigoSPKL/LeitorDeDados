# LeitorDeDados

Aplicação de consulta a acervos estáticos em SQLite: **importador, bancos particionados em arquivos, publicação de versões, API autenticada e tela de busca**. Pesquisa por CPF, e-mail, telefone ou código, preservando os registros originais. Inclui dados fictícios para demonstração.

## Processar automaticamente a pasta atual

Com o projeto instalado separadamente, por exemplo em `/opt/leitor-dados`, e Docker Engine disponível:

```bash
cd /dados/meu-lote
bash /opt/leitor-dados/processar-pasta.sh
```

O comando percorre subpastas e descompacta ZIP, RAR e TAR recursivamente. Importa CSV/TXT delimitado, JSONL e SQLite em tabelas por pasta e estrutura de colunas. Partes `1`, `2`, `3` com as mesmas colunas entram na mesma tabela. **Linhas duplicadas são permitidas.** Arquivos já importados e inalterados são pulados nas execuções seguintes; arquivos modificados são acrescentados integralmente, preservando as linhas anteriores.

Instalar o projeto e manter os arquivos gerados em um sistema de arquivos Linux com permissões POSIX, como ext4 ou XFS. Originais em um disco exFAT podem ser lidos, mas credenciais, catálogo e snapshots precisam de um destino Linux. Para separar o destino da pasta de entrada, usar `WORK_PATH=/caminho/linux/meu-lote bash /opt/leitor-dados/processar-pasta.sh`; nesse caso, consultar `WORK_PATH/published`. Ver o [manual Linux](docs/rodar-linux.md#discos-externos-e-erro-operation-not-permitted).

Os originais ficam intactos. O catálogo com as tabelas está em `.leitor/catalog.sqlite`; a versão para consulta fica em `.leitor/published`. Ver [importação automática de pastas](docs/importacao-de-pastas.md) para formatos, identificação das colunas, limites de expansão e bancos de origem compatíveis.

Para abrir a API com o resultado dessa pasta:

```bash
DATA_PATH=/dados/meu-lote/.leitor/published bash /opt/leitor-dados/scripts/linux-start.sh
```

## Rodar no Linux

Copiar ou descompactar o projeto no servidor. Com Docker Engine, plugin Docker Compose e Python 3.10+ instalados, executar na raiz:

```bash
bash scripts/linux-start.sh
```

O script prepara as credenciais locais no host, solicitando `sudo` se necessário, constrói a imagem, cria/publica o banco de demonstração e inicia o servidor. Também pode ser executado com `sudo bash scripts/linux-start.sh`. Abrir **http://127.0.0.1:8000/**. A chave de acesso fica em `secrets/api_key`; um administrador pode lê-la no servidor com `sudo cat secrets/api_key`. A chave não acompanha o pacote e não é gravada pelo navegador.

SQLite não precisa de um serviço de banco separado: os bancos são arquivos em `data/versions/`. O processo permanente é a API de consulta. O exemplo pressupõe Linux com Docker rootful, sem remapeamento de usuários. Para acessar de outro computador, usar o túnel SSH descrito no [manual Linux](docs/rodar-linux.md), ou preparar proxy TLS/VPN.

## Importar os dados da empresa

Colocar as fontes em `inputs/` e copiar/ajustar `config/import.example.json`: formato, separador, codificação e nomes dos campos. CSV, TXT delimitado e JSONL são suportados; identificadores devem ser texto para preservar zeros. A [documentação de importação](docs/importacao.md) descreve os limites e a mistura de formatos.

```bash
docker compose --profile tools run --rm tools build --config /config/import.empresa.json --input /inputs/registros.csv --version empresa-v1 --data-dir /data
docker compose --profile tools run --rm tools verify --data-dir /data --version empresa-v1 --checksums
docker compose --profile tools run --rm tools publish --data-dir /data --version empresa-v1
```

Cada versão fica fixa. A API reconhece uma publicação nova sem reiniciar; cursores continuam na versão original enquanto ela existir e dentro do prazo de validade. O [manual Linux](docs/rodar-linux.md) detalha configuração, permissões, backup e recuperação.

## Implementação e validação

- Descoberta recursiva, extração ZIP/RAR/TAR, catálogo por pasta/esquema, histórico de arquivos e duplicatas preservadas.
- Importação em fluxo, lotes limitados, mapeamento por origem e normalização compartilhada.
- Lookup distribuído pelo identificador: uma busca lê um arquivo de lookup e recupera os registros por ID nos arquivos de dados correspondentes.
- Publicação atômica, versões preservadas, checksums e verificação offline.
- API autenticada, bancos somente leitura, SQL parametrizado e cursor assinado vinculado à busca e à versão.
- Tela em português, paginação e limites de concorrência, requisição e resultados.

Os testes locais cobrem extração, catálogo, importação, payload, autenticação, paginação entre versões, erros e imutabilidade. A demonstração também foi consultada por HTTP em servidor real. Ver o [registro de validação](docs/validacao.md). **Docker não está disponível neste ambiente de desenvolvimento: a implantação Linux ainda precisa ser executada no destino. O desempenho em 20 bilhões de registros não foi validado.**

## Pesquisa e dimensionamento

O projeto atende **busca exata por CPF, e-mail, telefone ou código em acervos estáticos**, com preparação offline. Os cenários de 20 bilhões de registros na documentação são exemplos de dimensionamento, sem validação de desempenho nesse volume.

- [Pesquisa, alternativas e arquitetura proposta](docs/pesquisa-bancos.md)
- [Desenho específico da busca por identificadores](docs/busca-identificadores.md)
- [SQLite para acervo estático e somente leitura](docs/sqlite-somente-leitura.md)
- [Plano de prova de conceito e critérios de escolha](docs/plano-benchmark.md)
- [Calculadora de capacidade](scripts/estimate_capacity.py)

O projeto implementa SQLite para a primeira prova de conceito; comparar com PostgreSQL continua previsto no benchmark. A demonstração usa 2 arquivos de dados e 4 de lookup; escolher os números reais antes de construir cada versão. O limite da implementação é 128 arquivos de banco no total por versão.

Uma cópia local do mesmo conjunto pode atender outro servidor de consultas; replicação entre máquinas não é automática. Identificadores podem ocorrer em muitas vendas e retornam listas paginadas. A busca por código é global e preserva ocorrências de origens diferentes.

Não há deduplicação semântica ou importador universal. O catálogo automático retoma por arquivo confirmado; a construção do snapshot de consulta ainda reinicia após falha. A versão publicada é preservada. Adaptar formatos adicionais quando seu contrato estiver definido. Medir prazo e espaço de preparação antes da carga completa.

## Estimar armazenamento

A calculadora usa a biblioteca padrão. A aplicação usa as dependências fixadas em `requirements.lock.txt` e Python 3.12 no container. Para testes locais, criar um ambiente virtual e instalar `requirements-dev.txt`.

```bash
python scripts/estimate_capacity.py --rows 20000000000 --avg-row-bytes 1000 --compression-ratio 1 --copies 2 --overhead-pct 25 --free-space-pct 30 --backup-copies 1
```

Nesse cenário hipotético, sem presumir compressão: 20 TB lógicos, aproximadamente 71,43 TB provisionados no conjunto dos servidores e 25 TB de backup separado. O overhead de 25% é apenas hipótese e pode diferir muito do custo dos quatro índices. Medir o tamanho físico completo antes de dimensionar. Esses totais podem exigir vários arquivos; não representam capacidade validada de um único SQLite.

```bash
python scripts/estimate_capacity.py --help
python -m unittest discover -s tests -v
```

## Próxima decisão

Definir tamanho dos registros, quantidade de resultados por identificador, concorrência, latência desejada e prazo para preparar a base inicial. Com os quatro campos preenchidos em todos os registros, um modelo de lookup separado pode chegar a **80 bilhões de associações**, além dos 20 bilhões de registros. Medir os índices e estruturas separadamente. O [plano de benchmark](docs/plano-benchmark.md) transforma essas informações em uma comparação reproduzível antes de comprar infraestrutura.
