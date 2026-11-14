# Validação da entrega

Ambiente de desenvolvimento: Windows, CPython 3.14 e ambiente virtual local; dependências fixadas em `requirements.lock.txt`. O container tem alvo Python 3.12/Linux e ainda não foi executado, pois Docker e Bash não estão disponíveis neste ambiente.

## Verificações realizadas

**88 testes automatizados foram executados: 87 passaram e 1 foi ignorado**, porque o Windows deste ambiente não permite criar o link simbólico exigido pelo caso de teste. Também foram executadas as verificações por CLI e HTTP abaixo.

- Testes automatizados de importação CSV/JSONL, normalização, zeros iniciais, lotes, fontes mistas, falhas e publicação de versões.
- Testes de descoberta e extração recursiva ZIP, ZIP64 e TAR com gzip/bzip2/xz; cache, diretórios aninhados, preservação dos originais, limites de expansão, metadados e caminhos inválidos.
- Testes reais dos parsers de RAR3/RAR5 com conteúdo armazenado sem compressão; a disponibilidade do backend foi simulada, sem executar `unar`. O caminho de RAR comprimido ainda exige teste no Linux com o backend instalado.
- Testes de tabelas reais por pasta/esquema, partes com colunas em ordens diferentes, bancos SQLite de origem, duplicatas, arquivos inalterados e retomada por arquivo após falha.
- Fluxo completo iniciado com a pasta atual como entrada, sem `--folder`: ZIP contendo TAR.GZ contendo CSV, consulta dos resultados publicados, pasta original chamada `data` preservada e nova execução sem reconstrução.
- Testes de consultas SQLite reais, os quatro acessos, ausência de fan-out entre lookups, payload original, paginação preservada após nova publicação e cursores inválidos/expirados.
- Testes de leitura imutável, corrupção detectada por checksum, arquivos ausentes, saturação, timeout e limite de resultados.
- Testes de API: autenticação, readiness, segredo em arquivo, erros sem ecoar dados pessoais, corpo limitado e paginação sobre banco real.
- Geração idempotente de credenciais e validação estática de Compose, arquivos UTF-8, caminhos, documentação e pacote de distribuição.
- Construção da demonstração CSV com cinco registros e de uma versão mista CSV/JSONL com sete registros; integridade completa e checksums conferidos.
- Servidor Uvicorn local real: liveness, readiness autenticado, duas páginas de consulta por CPF e HTML com CSP verificados por HTTP.
- JavaScript da interface: sintaxe e comportamento de estados, paginação, retry e renderização como texto verificados automaticamente.

A inspeção visual pelo navegador não foi concluída: a ferramenta de automação interrompeu a execução porque não conseguiu confirmar o endereço atual do navegador com segurança. Isso não afetou os testes HTTP ou as operações SQLite.

## Repetir os testes

Na raiz do projeto, após criar e ativar um ambiente virtual:

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m pip check
```

## Confirmar no Linux

Executar `bash scripts/linux-start.sh`, conferir o healthcheck autenticado e pesquisar `DEMO-001` na interface com a chave gerada no servidor. Em seguida testar importação das fontes reais com seus mapeamentos, verificar, publicar e restaurar uma cópia separada. Ajustar UID/GID se usar Docker rootless ou remapeamento de usuários.

Os exemplos e testes pequenos comprovam o funcionamento, não o desempenho em 20 bilhões de registros. O [plano de benchmark](plano-benchmark.md) continua necessário para capacidade, latência e recuperação. A preparação é monoprocesso e gera até 128 arquivos de consulta por versão. O catálogo retoma arquivos já confirmados; o snapshot ainda não retoma sua construção interna. Medir esses limites e o prazo antes da carga completa.
