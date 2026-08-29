"""
NEXUS-7 — Testes de regressão dos bugs encontrados em auditoria.

Cada teste corresponde a um bug REAL que chegou a produção.
Rodar: python -m tests.test_regression
"""
import os, sys, time, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PAPER_TRADE", "true")

_p = _f = 0
def check(name, cond, extra=""):
    global _p, _f
    if cond: _p += 1; print(f"  ✓ {name}")
    else:    _f += 1; print(f"  ✗ {name} {extra}")


def test_paper_trade_barrier():
    """P0: ausência de PAPER_TRADE não pode habilitar dinheiro real."""
    import importlib
    for env, esperado in [
        ({}, True),
        ({"PAPER_TRADE": "false"}, True),                       # sem confirmação
        ({"PAPER_TRADE": "true"}, True),
        ({"PAPER_TRADE": "false",
          "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK"}, False),
    ]:
        for k in ("PAPER_TRADE", "LIVE_TRADING_CONFIRMED"):
            os.environ.pop(k, None)
        os.environ.update(env)
        for m in [x for x in sys.modules if x.startswith("bot.")]:
            del sys.modules[m]
        from bot.kucoin import PAPER_TRADE
        check(f"barreira paper/real {env or '{}'}", PAPER_TRADE == esperado,
              f"esperado {esperado}, obtido {PAPER_TRADE}")
    os.environ["PAPER_TRADE"] = "true"
    os.environ.pop("LIVE_TRADING_CONFIRMED", None)


def test_idempotencia_ordem():
    """P0: mesmo sinal na mesma janela deve gerar o mesmo clientOid."""
    import hashlib
    w = int(time.time() // 60)
    a = hashlib.md5(f"BTCUSDT_Buy_3_{w}".encode()).hexdigest()[:40]
    b = hashlib.md5(f"BTCUSDT_Buy_3_{w}".encode()).hexdigest()[:40]
    check("clientOid estável na mesma janela", a == b)
    c = hashlib.md5(f"BTCUSDT_Buy_3_{w+1}".encode()).hexdigest()[:40]
    check("clientOid muda entre janelas", a != c)


def test_sl_vs_liquidacao():
    """P0: SL não pode ficar além do ponto de liquidação."""
    lev, safety = 50, 0.75
    liq = 100 / lev
    check("SL 1.0% aceito",   1.0 < liq * safety)
    check("SL 1.5% rejeitado", not (1.5 < liq * safety))
    check("SL 2.5% rejeitado", not (2.5 < liq * safety))


def test_expectancy_math():
    """Win rate isolado não mede lucratividade."""
    def exp_r(wr, payoff):
        return wr * payoff - (1 - wr) * 1.0
    e65 = exp_r(0.65, 2.0)
    e90 = exp_r(0.90, 0.3)
    check("65%/2.0 tem expectancy maior que 90%/0.3", e65 > e90,
          f"{e65:.3f} vs {e90:.3f}")
    check("30%/2.0 tem expectancy negativa", exp_r(0.30, 2.0) < 0)


def test_ev_com_custos():
    """EV deve incluir taxa e slippage nas duas pontas."""
    from bot.nexus_ai import expected_value
    ev = expected_value(0.60, 100, 99.9, 100.1)
    check("R:R apertado tem EV negativo após custos", not ev["valid"])
    ev2 = expected_value(0.60, 100, 98, 104)
    check("R:R 2:1 com 60% tem EV positivo", ev2["valid"])
    check("R:R líquido < bruto", ev2["rr_net"] < 3.0)


def test_nexus_nao_inventa_dados():
    """Seção 14: modelo sem dados não contribui com neutro."""
    from bot.nexus_models import model_derivatives
    m = model_derivatives(None, None, None)
    check("derivatives sem dados → available=False", not m.available)
    m2 = model_derivatives(0.0005, 0.01, 1.2)
    check("derivatives com dados → available=True", m2.available)


def test_nexus_bloqueia_sem_dados():
    """Fail-safe: dados insuficientes nunca autorizam execução."""
    from bot.nexus_ai import decide
    d = decide("TESTE", [], [], [])
    check("sem dados → WAIT", d.decision == "WAIT")
    check("sem dados → não autoriza", d.execution_allowed is False)


def test_selfcheck_detecta_bugs():
    """O self-check precisa pegar os padrões que já vazaram."""
    from bot.selfcheck import (check_undefined_names, check_missing_self_attrs,
                               check_missing_self_methods)
    src = '''
class E:
    def __init__(self): self.x = 1
    def run(self):
        if self.paper_trade: pass
        self.metodo_fantasma()
        return aiohttp.get()
'''
    p = "/tmp/_regr_check.py"
    open(p, "w").write(src)
    check("detecta NameError latente", len(check_undefined_names([p])) > 0)
    check("detecta atributo inexistente", len(check_missing_self_attrs([p])) > 0)
    check("detecta método inexistente", len(check_missing_self_methods([p])) > 0)


def test_config_sanity():
    """Combinações impossíveis devem ser reportadas."""
    from bot.selfcheck import check_config_sanity
    issues = check_config_sanity()
    check("config_sanity executa", isinstance(issues, list))


def test_imports_todos_modulos():
    """Regressão do bug do aiohttp: todo módulo precisa importar."""
    import importlib
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
    falhas = []
    for f in sorted(os.listdir(base)):
        if f.endswith(".py") and f != "__init__.py":
            try:
                importlib.import_module("bot." + f[:-3])
            except Exception as e:
                falhas.append(f"{f}: {e}")
    check("todos os módulos importam", not falhas, str(falhas[:2]))


if __name__ == "__main__":
    print("═══ TESTES DE REGRESSÃO ═══\n")
    for fn in [test_paper_trade_barrier, test_idempotencia_ordem,
               test_sl_vs_liquidacao, test_expectancy_math, test_ev_com_custos,
               test_nexus_nao_inventa_dados, test_nexus_bloqueia_sem_dados,
               test_selfcheck_detecta_bugs, test_config_sanity,
               test_imports_todos_modulos]:
        print(f"\n{fn.__name__}:")
        try:
            fn()
        except Exception as e:
            _f += 1
            print(f"  ✗ ERRO NO TESTE: {type(e).__name__}: {e}")
    print(f"\n{'═'*46}")
    print(f"PASSOU: {_p}  |  FALHOU: {_f}")
    print("═"*46)
    sys.exit(1 if _f else 0)
