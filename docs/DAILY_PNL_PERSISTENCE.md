# Persistência do PnL diário operacional

O ledger guarda eventos de fechamento do próprio engine por serviço/ambiente e
data UTC. O PnL líquido já calculado é preservado, com sua origem, sem descontar
taxas novamente ou importar posições externas do histórico da exchange.

Fechamentos nos dois caminhos LIVE chamam checkpoint antes de adicionar o trade
às estatísticas. Outro checkpoint roda antes do cálculo dos limites e scanner.
O ledger recuperado e os trades locais são unidos por identidade de evento;
repetir a leitura do mesmo fechamento não acumula seu PnL novamente.

Falha de leitura, gravação não confirmada, conteúdo inválido ou replay
conflitante bloqueiam novas entradas. O ciclo ainda gerencia posições antes
desse gate. PAPER/SHADOW não usam este ledger. Os limites não mudam.

Limitações explícitas:

- Sem backfill histórico nesta versão. `coverage_started_at` identifica o início
  da cobertura; zero eventos não significa zero PnL histórico na conta.
- Preserva PnL operacional, inclusive estimativas existentes. Não converte uma
  estimativa em fill confirmado nem atualiza estatísticas semanais/mensais.
- Um crash entre o fechamento na exchange e sua observação/persistência local
  ainda exige reconciliação por fills. Não existe transação atômica com a exchange.
- O lock é por processo; coordenação entre versões sobrepostas segue como
  pendência da auditoria de infraestrutura.

Regressões: reinício abaixo do stop, replays idênticos e conflitantes, nova
operação após reinício, virada UTC, falha de gravação/retentativa, corrupção,
NaN, taxas já líquidas e isolamento PAPER/SHADOW.
