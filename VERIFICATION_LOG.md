# Verificação da ação de arquivamento (commit 424fd54 e correlatos)

Timestamp da auditoria: 2026-09-01T11:40Z
Método: download fresco do repositório, ambiente Python reinstalado
do zero, nenhum dado reciclado de sessões anteriores.

## Resultado

CONFIRMADO — o arquivamento dos 6 módulos órfãos foi executado
corretamente e não introduziu regressão:

- bot/_archive/{bybit,filters,position_manager,regime,
  signal_processor,structure}.py existem com conteúdo íntegro
- Os 6 originais em bot/ foram removidos
- 24/24 módulos ativos + main.py importam sem erro
- 89/89 testes automatizados passam (regressão 49, hardening 26, chaos 14)
- bot/selfcheck.py real (via run_selfcheck()) reporta 0 código morto

## Falso positivo encontrado e resolvido durante esta própria auditoria

Ao testar cada função check_* isoladamente com uma lista de arquivos
montada manualmente (os.walk direto em bot/, sem excluir _archive/ e
sem incluir main.py), check_orphan_modules() reportou 7 itens,
incluindo engine.py — que É importado por main.py.

Isso NÃO é um bug do bot/selfcheck.py real. É um erro de metodologia
do meu próprio script de teste ad-hoc: ele não reproduzia a lista de
arquivos que _python_files() de fato usa (que exclui _archive/ e
inclui main.py corretamente).

Confirmado ao chamar sc.check_orphan_modules(sc._python_files())
diretamente: resultado = 0 itens, consistente com o run_selfcheck()
real.

Isto está registrado porque é exatamente o tipo de erro que uma
auditoria precisa capturar — inclusive quando o erro é da própria
ferramenta de verificação, não do alvo que está sendo verificado.

## Verificações negativas (não encontrei problema)

- Sem import dinâmico (importlib, __import__, pkgutil) no projeto
- Sem entry_points/setup.py que pudesse referenciar os módulos
  arquivados por string
- Nenhum teste em tests/ dependia dos 6 módulos arquivados
- bot/__init__.py não expõe os módulos via __all__
