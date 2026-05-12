import logging, sys, os

def _make(name):
    lvl = getattr(logging, os.environ.get("LOG_LEVEL","INFO"), logging.INFO)
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

log = _make("nexus7")
