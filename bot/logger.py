import logging, sys, os
import os

def _make(name):
    # .upper() garante que 'info'/'INFO'/'Info' todos viram 'INFO'
    # sem isso, getattr(logging, "info") retorna a funcao logging.info
    # em vez da constante logging.INFO, causando TypeError no setLevel
    level_str = os.environ.get("LOG_LEVEL", "INFO").upper()
    lvl = getattr(logging, level_str, logging.INFO)
    lg = logging.getLogger(name)
    if not lg.handlers:
        lg.setLevel(lvl)
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s",
            datefmt="%H:%M:%S"))
        lg.addHandler(h)
        lg.propagate = False
    return lg

log = _make("kakazito-trade")
