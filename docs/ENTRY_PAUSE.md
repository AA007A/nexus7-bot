# Pausa de entradas e ciclo de tarefas

`POST /api/pause` marca engine e cliente como pausados para novas entradas.
Mantém o loop ativo, inclusive gestão de posições, SL/TP, reconciliação e risco.
O scanner não abre novas avaliações e o cliente bloqueia novos POSTs de ordens
de abertura, inclusive ordens nativas com SL/TP. Reduce-only e closeOrder
continuam permitidos. Uma requisição já enviada à exchange antes da pausa não
pode ser desfeita por essa trava e segue sujeita à reconciliação existente.

`POST /api/resume` remove a pausa sem recriar tarefas se o loop estiver ativo.
Se houver shutdown em andamento, responde 503 até terminar. Gates de risco e
inicialização continuam obrigatórios. `/api/close-all` continua sendo uma ação
de emergência separada; o encerramento do processo ainda para o loop.

O `finally` do loop cancela e aguarda as seis tarefas auxiliares e a coleta de
contabilidade. O lifespan aguarda as tarefas antes de fechar cliente e banco.
WebSockets seguem sob responsabilidade do cliente existente.

`entries_paused` aparece no status e prontidão; pausa não torna o serviço
indisponível. A pausa é local à instância e não é persistida entre deploys.
Parâmetros de risco, 50x, dimensionamento e política de stops não mudaram.

Testes offline exercitam um ciclo real com posição simulada, bloqueio de
abertura, permissão de redução e cancelamento das tarefas em duas execuções.
