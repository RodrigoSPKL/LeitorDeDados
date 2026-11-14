# Dados e credenciais

O repositório contém código, documentação, configurações de exemplo e dados fictícios. Os endereços dos exemplos usam o domínio reservado `example.invalid`. As chaves presentes nos testes são constantes sintéticas e não são credenciais de implantação.

Cada instalação gera suas próprias credenciais em `secrets/`. Nunca publicar esses arquivos, variantes de `.env`, bancos, fontes de importação, arquivos compactados, logs ou relatórios contendo dados reais. Os diretórios `data/`, `inputs/`, `results/`, `.leitor/`, `secrets/` e `dist/` ficam fora do Git.

O `.gitignore` também bloqueia extensões comuns de dados e credenciais, liberando apenas exemplos conhecidos. Configurações da empresa devem ser copiadas a partir dos modelos `*.example.json` e mantidas fora do versionamento. Exclusões não substituem a revisão do conteúdo preparado para cada commit; arquivos já rastreados continuam sendo versionados.

Antes de publicar alterações, conferir `git diff --cached` e `git diff --cached --name-only`. Se uma credencial real for exposta, revogá-la ou rotacioná-la; removê-la apenas do arquivo atual não a remove do histórico.

A API inicia vinculada ao endereço local no exemplo Docker. Preparar TLS/VPN e o controle de acesso adequado antes de disponibilizar dados reais remotamente. A publicação do código não publica o acervo de uma instalação.
