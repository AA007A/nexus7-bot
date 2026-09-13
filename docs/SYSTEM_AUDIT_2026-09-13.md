# Auditoria BGX — 13/09/2026

Base examinada: `3bf9edffc64e1c63ab39dfe7357f5a15363200b7` (PR278).
Escopo: AA007A/nexus7-bot; Railway BGX CAPITAL, serviço
751b41ee-2aef-4487-b19a-f305f15c64fe, production. Miner007 excluído.
Revisão dos caminhos críticos, configurações e suíte offline; não representa
prova formal de todo caminho possível nem teste de invasão independente.

## Correções entregues nesta revisão

1. Saúde: `/ready` não consultava `app.state.engine_task`. Uma tarefa encerrada
   com erro podia deixar prontidão verde. Agora tarefa ausente, cancelada ou
   com falha reprova a prontidão; pausa intencional terminada sem erro continua
   saudável. Isso detecta término, não deadlock de uma tarefa ainda pendente.
2. Controle: `/api/resume` não verificava `blocked/ready` antes de criar o loop.
   Agora recusa retomada antes da aprovação de inicialização.
3. Corrida pausa/retomada: `stop()` limpava `_running`, mas retomada com tarefa
   ainda pendente só restaurava `active`. Agora restaura ambos usando a mesma
   tarefa. Tarefa perdida com `_running=True` exige reinício do serviço.
4. Autenticação: token passa a usar `secrets.compare_digest` em bytes.
   Não houve evidência de exploração; é reforço preventivo.

Regressões reproduzem os caminhos reais da retomada com doubles inertes e a
classificação de saúde. Nenhum endpoint de negociação foi chamado na auditoria.

## Achados e critérios para encerramento

| Prioridade | Área / evidência | Falta para encerrar |
|---|---|---|
| Alta | PnL diário: `Stats()` começa vazio; `daily_pnl()` soma `self.trades`; restauração LIVE em `durable_execution.py` recupera ordens, não trades fechados. `durable_daily_stop.py` guarda STOP já disparado. | Testar e implementar recuperação idempotente do PnL anterior ao disparo do stop. Reinício não pode apagar perdas acumuladas abaixo do limite. Vincular fills sem dupla contagem ou atribuição de operações externas. Não mudar o limite para contornar isso. |
| Alta | Pausa normal encerra o loop e a gestão local (`engine.stop`). SL/TP nativos continuam independentes. | Separar pausa de novas entradas da supervisão de posições; testar explicitamente posição aberta, retomada e fechamento emergencial. Preservar semântica de emergência. |
| Alta | `run()` cria workers auxiliares sem mantê-los em grupo; uma retomada após término pode criar novas tarefas de notícias/otimização. | Gerenciar ciclo de vida dos workers, cancelar/aguardar no shutdown e provar ausência de duplicação após pausas/restarts. |
| Alta | Uma réplica está configurada, mas os locks encontrados são locais ao processo. | Provar exclusão entre versões durante substituição de deploy ou implantar liderança com fencing. Uma réplica desejada não prova ausência de sobreposição no rollout. |
| Alta | Backup/restauração e permissões da chave KuCoin não foram expostos pelas leituras realizadas. | Evidência de restauração em ambiente isolado, RPO/RTO medidos e revisão de permissões/IPs da chave. Não declarar backup ausente ou credencial insegura sem prova. |
| Média | CoinGlass: logs 19:29–19:31Z mostram HTTP200/body401; Binance fornece funding/open interest. | Corrigir acesso/plano/provider e confirmar dados frescos. Chave configurada não significa endpoint autorizado. Nenhum segredo foi exibido. |
| Média | Docker usa tag móvel python:3.11-slim; dependências diretas fixadas, transitivas e ferramentas de build sem hashes/lock completo. | Build reproduzível, imagem por digest e inventário transitivo fixado; manter atualização e auditoria de vulnerabilidades. |
| Média | Muitos overlays substituem métodos na inicialização; mensagens de tamanho nocional coexistem com política final de margem. | Consolidar autoridade de execução gradualmente com testes de equivalência. Log final deve explicar a política efetiva sem mensagens legadas contraditórias. |
| Validação | Há código de walk-forward/calibração, mas esta revisão não produziu amostra cronológica fora da amostra suficiente. | Relatório líquido de custos, por regime, com intervalo de incerteza e amostra não reutilizada na seleção. Scores/confiança não equivalem a probabilidade calibrada. |

## Evidência operacional

- Deployment inicial efcf6923-9c80-46e0-a998-0a980f90b162: SUCCESS.
- Logs consultados desde inicialização 19:26Z até aproximadamente 19:32Z.
- Snapshot: equity/disponível 16,7198 USDT; margem de posição e ordens zero.
  Variação desde a checagem anterior não foi classificada como lucro.
- Coletor de histórico: 9 posições, cobertura declarada completa de 48h.
  Isso não atribui automaticamente todas as operações ao bot.
- Métricas Railway, última hora, 61 amostras: CPU máx 0,3209; memória máx
  0,1989 GB de 8 GB. Uma réplica em asia-southeast1-eqsg3a.
- Deploy usa /ready, predeploy `bot.ci_deploy_gate`, processo sem root e um
  worker Uvicorn. Esses controles não constituem monitoramento externo contínuo.
- Política preservada: 50x, alvo de margem de 50% do disponível, stops e
  bloqueios de risco existentes. Não foram enviadas ordens de teste.

## Validação e limites

Execução offline local inicial: TOTAL=1105, FAILED_SUITES=2. Ambas as suítes
encontraram dependências ausentes neste executor (fastapi/aiosqlite); não foram
classificadas como falhas de produção. Regressões desta alteração e prontidão:
10 testes passaram. CI completa da PR é obrigatória antes de integrar.

Conclusão: há melhorias verificadas, mas existem pendências operacionais e de
validação. Não há fundamento para nota 10/10 nem garantia de ausência de perdas.
