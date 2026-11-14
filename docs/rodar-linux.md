# Executar a base em Linux

Esta versão entrega um importador offline, publicação de acervos SQLite versionados e uma API FastAPI somente de leitura. O servidor não exige instalar SQLite como serviço: o módulo acompanha o Python da imagem. O exemplo usa dados fictícios. A implementação precisa de benchmark e dimensionamento antes de receber um acervo de 20 bilhões de registros.

## Pré-requisitos e primeira execução

Usar Linux com Docker Engine, plugin Docker Compose e Python 3.10+ no host para gerar os segredos. A aplicação executa em Python 3.12 no container. O script depende de Compose com `up --wait` e `--wait-timeout`. A configuração padrão pressupõe Docker rootful, sem remapeamento de usuários; Docker rootless ou `userns-remap` exige adaptar UID/GID e permissões dos volumes.

Na raiz do projeto:

```bash
bash scripts/linux-start.sh
```

O script cria os diretórios locais, gera dois segredos sem sobrescrever existentes, constrói a imagem e ajusta somente a propriedade desses dois arquivos para UID/GID `10001:10001`. Se não existir `data/current.json`, importa e publica `demo-v1` usando `examples/registros.csv`. Em seguida, aguarda a API ficar disponível.

Se já houver acervo publicado, ele é preservado. Se uma tentativa anterior deixou `demo-v1` preparado sem publicação, o importador deve recusar sobrescrita: verificar e publicar essa versão pelos comandos abaixo antes de repetir o início.

```bash
docker compose ps api
curl --fail http://127.0.0.1:8000/health/live
```

A porta fica em `127.0.0.1:8000`. No navegador do servidor, abrir `http://127.0.0.1:8000/`. Para usar a interface a partir de outra máquina sem expor a porta, criar um túnel:

```bash
ssh -L 8000:127.0.0.1:8000 usuario@servidor
```

Abrir o mesmo endereço no navegador local. A interface pede a chave de API, sem gravá-la no armazenamento do navegador. Um administrador pode consultar a chave no servidor com `sudo cat secrets/api_key`. O segredo de cursor é separado e não precisa ser fornecido ao usuário.

## Preparar o acervo real

Para varrer arquivos compactados e criar tabelas automaticamente por pasta, usar o [processamento de pastas](importacao-de-pastas.md). O comando abaixo, executado na raiz do projeto, processa `inputs/` e publica em `data/`:

```bash
docker compose --profile tools run --rm tools ingest-folder --folder /inputs --work-dir /work --data-dir /data
```

O destino do Compose pode ser alterado por `DATA_PATH`. O script de início verifica esse destino antes de decidir criar a demonstração. Para um acervo já preparado em outra pasta, seguir o comando com `DATA_PATH` do manual de importação automática.

Colocar os arquivos em `inputs/`. Copiar e ajustar a configuração em `config/` para mapear as colunas/chaves de CPF, e-mail, telefone e código. O ID interno é gerado por versão. A [documentação de importação](importacao.md) descreve formatos e planos para fontes diferentes.

```bash
docker compose --profile tools run --rm tools --help
docker compose --profile tools run --rm tools build --help
```

Exemplo de preparação, sem alterar a versão consultada pela API:

```bash
docker compose --profile tools run --rm tools build \
  --config /config/import.empresa.json \
  --input /inputs/registros.csv \
  --version empresa-v1 --data-dir /data
```

Para JSONL, fornecer o arquivo correspondente e a configuração desse formato. O caminho `/inputs` dentro do container corresponde a `inputs/` no host; `/config` corresponde a `config/`. O serviço `tools` lê essas entradas e escreve apenas no volume de dados e, quando solicitado, no volume de segredos. A imagem não inclui o acervo, arquivos de entrada nem credenciais.

Usar uma versão nova em cada preparação. Não editar arquivos já publicados. A ferramenta executa como root somente nas operações offline, para evitar diferenças de UID do host; os artefatos devem permanecer legíveis pela API. O diretório `data/` precisa permitir travessia ao UID `10001`, e os arquivos precisam permitir leitura. O início prepara `data/` com modo `0755`; a API monta esse diretório com `:ro`.

## Verificar e publicar

```bash
docker compose --profile tools run --rm tools verify \
  --data-dir /data --version empresa-v1 --checksums

docker compose --profile tools run --rm tools publish \
  --data-dir /data --version empresa-v1
```

Também existe `build --publish` para preparar e publicar na mesma execução, como na demonstração. Em produção, separar as etapas permite conferir relatórios, integridade e buscas de referência antes da publicação. A conferência de checksums lê os arquivos; em acervos grandes, planejar seu tempo e I/O.

Publicar atualiza a referência de versão sem reiniciar a API. Os cursores continuam na versão original e expiram após 24 horas sem continuação; a continuação emite outro cursor com novo prazo. Manter as versões enquanto houver cursores válidos e durante o período de recuperação definido. Nunca substituir arquivos abertos como imutáveis. [Parâmetros URI oficiais](https://www.sqlite.org/uri.html).

## Consultar a API

O contrato é `POST /v1/search`, com cabeçalho `X-API-Key` e corpo:

```json
{"field":"code","value":"DEMO-001","limit":50,"cursor":null}
```

`field` aceita `cpf`, `email`, `phone` ou `code`. A resposta contém `version`, `field`, `items` e `next_cursor`. Cada item inclui `record_id` e `data`, com o conteúdo original. Para continuar, reenviar os mesmos parâmetros e o cursor retornado; não fabricar ou modificar o cursor. Um mesmo identificador pode retornar vários registros.

Códigos iguais podem existir em origens diferentes; a busca global devolve as ocorrências correspondentes, sem presumir unicidade entre sistemas.

`GET /v1/info` e `GET /health/ready` exigem a chave. O healthcheck do container usa readiness autenticado para conferir manifesto e arquivos. `GET /health/live` é público e verifica apenas o processo. Nenhum deles substitui validação completa ou recuperação de backup.

## Operação e recuperação

```bash
docker compose logs --tail 100 api
docker compose restart api
docker compose stop api
```

A API executa com UID `10001`, filesystem da imagem somente de leitura, diretório temporário em memória, capacidades removidas e dois workers iniciais. Esses parâmetros não são dimensionamento para 20 bilhões: ajustar após medir concorrência, RAM, I/O e latência. O acesso HTTP do Uvicorn não é registrado pelo comando padrão.

Os segredos ficam com modo `0600`. Em Compose, segredos originados de arquivo usam bind mounts; `uid`, `gid` e `mode` não remapeiam a propriedade nesses casos. Por isso o script ajusta os dois arquivos explicitamente. Repetir o início não troca seu conteúdo. Se precisar gerar credenciais novas, tratar como rotação coordenada; trocar o segredo de cursor invalida cursores anteriores. [Compose e segredos de arquivo](https://docs.docker.com/reference/compose-file/services/#secrets).

Manter backup separado dos acervos, manifestos, referência publicada, configuração de importação e segredos necessários. Restaurar para um diretório independente, conferir checksums e consultas conhecidas e só então direcionar a API. Uma cópia disponível não comprova restauração. Para arquivos estáticos, a réplica local pode ser preparada antes de aceitar tráfego.

Para acesso corporativo remoto, colocar a API atrás de proxy com TLS ou VPN e controle de acesso adequado. A configuração inicial não instala proxy, domínio ou certificado. Alterar `BIND_ADDRESS` pode expor a porta; fazê-lo apenas após preparar essa entrada de rede. Os arquivos e índices continuam locais a cada servidor, com publicação consistente por versão.

Esta entrega não foi executada em Docker neste ambiente de desenvolvimento, onde Docker e Bash não estão disponíveis. A validação realizada aqui não equivale a um ensaio de implantação Linux; confirmar construção, permissões, healthchecks, consultas e restauração no servidor de destino.
