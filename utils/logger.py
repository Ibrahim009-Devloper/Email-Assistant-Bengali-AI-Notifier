"""Console + rotating file logger."""
import logging
import os
from logging.handlers import RotatingFileHandler

_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def get_logger(name: str = "agent", log_dir: str = None) -> logging.Logger:
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter(_FMT))
    log.addHandler(sh)
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            fh = RotatingFileHandler(os.path.join(log_dir, "agent.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(logging.Formatter(_FMT))
            log.addHandler(fh)
        except OSError:
            pass
    log.propagate = False
    return log
