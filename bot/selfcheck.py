"""
BGX Capital — Self-Check de Integridade

MOTIVAÇÃO
Vários bugs deste projeto só apareceram depois de horas em produção,
porque erros de programação eram engolidos por `except Exception`:

  • aiohttp usado em 16 pontos sem import      → NameError silencioso
  • notify_nexus usado sem import              → nenhuma posição abria
  • self._recalc_daily_limits() inexistente    → AttributeError por ciclo
  • variável 'price' indefinida                → nenhum trade era salvo

Todos seriam detectados em SEGUNDOS por uma verificação estática no
startup. Este módulo faz exatamente isso.

USO
    from bot.selfcheck import run_selfcheck
    report = run_selfcheck()          # roda no lifespan, antes do engine
    if report["critical"]:
        # bloqueia a operação — bug de código não deve ir a produção

O check é barato (~200ms) e não depende de rede.
"""
import ast
import builtins
import os
import sys
from typing import Dict, List

BUILTINS = set(dir(builtins))

# Diretório do pacote bot/
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT    = os.path.dirname(_PKG_DIR)


def _collect_scope(node: ast.AST) -> set:
    """Nomes visíveis dentro de uma função (args, atribuições, imports, etc)."""
    s = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for a in node.args.args + node.args.kwonlyargs:
            s.add(a.arg)
        if node.args.vararg:
            s.add(node.args.vararg.arg)
        if node.args.kwarg:
            s.add(node.args.kwarg.arg)
    for n in ast.walk(node):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not node:
            s.add(n.name)
        elif isinstance(n, ast.ClassDef):
            s.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            s.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                s.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            s.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            for nm in n.names:
                s.add(nm)
        elif isinstance(n, ast.arg):
            s.add(n.arg)
    return s


def _module_globals(tree: ast.AST) -> set:
    g = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            g.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            g.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                g.add(a.asname or a.name.split(".")[0])
    return g


def check_undefined_names(paths: List[str]) -> List[str]:
    """
    Detecta nomes usados sem definição — a classe de bug que mais
    causou falhas silenciosas neste projeto.
    """
    issues = []
    for p in paths:
        try:
            src  = open(p, encoding="utf-8").read()
            tree = ast.parse(src)
        except SyntaxError as e:
            issues.append(f"{os.path.basename(p)}:{e.lineno} ERRO DE SINTAXE: {e.msg}")
            continue
        except Exception:
            continue

        gl = _module_globals(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            local = _collect_scope(node)
            # Nomes de escopos externos (closures) — sobe a cadeia
            enclosing = set()
            for outer in ast.walk(tree):
                if isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if outer is node:
                        continue
                    # node está dentro de outer?
                    for sub in ast.walk(outer):
                        if sub is node:
                            enclosing |= _collect_scope(outer)
                            break
            for n in ast.walk(node):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    if (n.id not in local and n.id not in gl
                            and n.id not in enclosing and n.id not in BUILTINS):
                        issues.append(
                            f"{os.path.basename(p)}:{n.lineno} "
                            f"NameError latente: '{n.id}' em {node.name}()"
                        )
    return issues


def check_duplicate_methods(paths: List[str]) -> List[str]:
    """Métodos redefinidos silenciosamente sobrescrevem o anterior."""
    issues = []
    for p in paths:
        try:
            tree = ast.parse(open(p, encoding="utf-8").read())
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                seen = {}
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name in seen:
                            issues.append(
                                f"{os.path.basename(p)}:{item.lineno} "
                                f"método duplicado {node.name}.{item.name}() "
                                f"(1ª definição na linha {seen[item.name]})"
                            )
                        seen[item.name] = item.lineno
    return issues


def check_silent_excepts(paths: List[str]) -> List[str]:
    """
    `except: pass` esconde bugs. Retorna WARNINGs (não bloqueia),
    porque alguns são legítimos (notificação best-effort).
    """
    issues = []
    for p in paths:
        try:
            tree = ast.parse(open(p, encoding="utf-8").read())
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                body = [s for s in node.body if not isinstance(s, ast.Pass)]
                if not body:
                    issues.append(
                        f"{os.path.basename(p)}:{node.lineno} except sem tratamento"
                    )
    return issues


def check_bare_except(paths: List[str]) -> List[str]:
    """`except:` sem tipo captura até KeyboardInterrupt e SystemExit."""
    issues = []
    for p in paths:
        try:
            tree = ast.parse(open(p, encoding="utf-8").read())
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                issues.append(
                    f"{os.path.basename(p)}:{node.lineno} 'except:' sem tipo — "
                    f"captura KeyboardInterrupt/SystemExit"
                )
    return issues



def check_missing_self_methods(paths: List[str]) -> List[str]:
    """
    Detecta self.metodo() que não existe na classe nem nos mixins.

    Foi exatamente este o bug do self._recalc_daily_limits(): o método
    era chamado a cada ciclo mas nunca existiu, lançando AttributeError
    que o except engolia — e o alerta de drawdown nunca era avaliado.
    """
    issues = []
    # Coleta todos os métodos de todas as classes do pacote (mixins incluídos)
    all_methods = set()
    class_attrs = set()
    trees = {}
    for p in paths:
        try:
            t = ast.parse(open(p, encoding="utf-8").read())
            trees[p] = t
        except Exception:
            continue
        for node in ast.walk(t):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        all_methods.add(item.name)
                    elif isinstance(item, ast.Assign):
                        for tg in item.targets:
                            if isinstance(tg, ast.Name):
                                class_attrs.add(tg.id)
                    elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        class_attrs.add(item.target.id)
            # self.x = ... em qualquer lugar
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "self" and isinstance(node.ctx, ast.Store):
                    class_attrs.add(node.attr)

    known = all_methods | class_attrs
    for p, t in trees.items():
        for node in ast.walk(t):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"):
                if node.func.attr not in known:
                    issues.append(
                        f"{os.path.basename(p)}:{node.lineno} "
                        f"AttributeError latente: self.{node.func.attr}() não existe"
                    )
    return issues


def check_config_sanity() -> List[str]:
    """
    Combinações de parâmetros matematicamente impossíveis, que só
    apareceriam como ordens rejeitadas em produção.
    """
    issues = []
    try:
        from bot.config import cfg
    except Exception as e:
        return [f"config.py não importa: {e}"]

    lev    = getattr(cfg, "LEVERAGE", 1)
    risk   = getattr(cfg, "MAX_RISK_PCT", 0.01)
    margin = getattr(cfg, "MAX_MARGIN_PCT", 0.80)
    npos   = getattr(cfg, "MAX_POSITIONS", 1)
    rr     = getattr(cfg, "MIN_RR_RATIO", 2.0)
    dd     = getattr(cfg, "MAX_DRAWDOWN", 0.10)

    if npos * margin > 1.0:
        issues.append(
            f"MAX_POSITIONS({npos}) × MAX_MARGIN_PCT({margin:.0%}) = "
            f"{npos*margin:.0%} > 100% — posições extras ficarão residuais. "
            f"Para {npos} equilibradas use MAX_MARGIN_PCT≈{1/npos:.2f}"
        )
    if lev * risk > 1.0:
        liq = 100 / lev if lev else 0
        issues.append(
            f"LEVERAGE({lev}) × MAX_RISK_PCT({risk}) = {lev*risk:.0%} do saldo "
            f"por posição — liquidação a ~{liq:.1f}% de movimento adverso"
        )
    if rr < 1.0:
        issues.append(f"MIN_RR_RATIO={rr} < 1.0 — risco maior que o retorno alvo")
    if dd >= 1.0:
        issues.append(f"MAX_DRAWDOWN={dd} ≥ 100% — proteção de drawdown inativa")
    return issues


def _python_files() -> List[str]:
    out = []
    for base in (_PKG_DIR,):
        for f in sorted(os.listdir(base)):
            if f.endswith(".py"):
                out.append(os.path.join(base, f))
    main_py = os.path.join(_ROOT, "main.py")
    if os.path.exists(main_py):
        out.append(main_py)
    return out


def run_selfcheck(verbose: bool = True) -> Dict[str, list]:
    """
    Executa todas as verificações.

    Retorna {"critical": [...], "warning": [...]}.
    'critical' contém apenas bugs que quebram execução — o chamador
    deve tratar como impeditivo para operar capital real.
    """
    from bot.logger import log

    files = _python_files()

    critical  = []
    critical += check_undefined_names(files)
    critical += check_duplicate_methods(files)
    critical += check_missing_self_methods(files)

    warning  = []
    warning += check_bare_except(files)
    warning += check_config_sanity()
    silent    = check_silent_excepts(files)
    if len(silent) > 3:
        warning.append(f"{len(silent)} blocos 'except' sem tratamento")

    if verbose:
        if critical:
            log.critical(f"🐛 SELF-CHECK: {len(critical)} PROBLEMA(S) CRÍTICO(S)")
            for i in critical[:15]:
                log.critical(f"   • {i}")
        else:
            log.info(f"✅ Self-check OK — {len(files)} arquivos, nenhum bug estrutural")
        for w in warning[:10]:
            log.warning(f"⚠️ Self-check: {w}")

    return {"critical": critical, "warning": warning, "files_checked": len(files)}


if __name__ == "__main__":
    # Permite rodar standalone: python -m bot.selfcheck
    sys.path.insert(0, _ROOT)
    rep = run_selfcheck(verbose=False)
    print(f"Arquivos verificados: {rep['files_checked']}")
    print(f"\nCRÍTICOS: {len(rep['critical'])}")
    for i in rep["critical"]:
        print(f"  🔴 {i}")
    print(f"\nAVISOS: {len(rep['warning'])}")
    for w in rep["warning"]:
        print(f"  ⚠️  {w}")
    sys.exit(1 if rep["critical"] else 0)
