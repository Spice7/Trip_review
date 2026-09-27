import logging
from datetime import date
from pathlib import Path


class SecretFilter(logging.Filter):
    def __init__(self, secret):
        super().__init__()
        self.secret = secret

    def filter(self, record):
        message = record.getMessage()
        if self.secret:
            message = message.replace(self.secret, "[REDACTED]")
        record.msg, record.args = message, ()
        return True


def setup_logging(root: Path, secret=""):
    root.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("collector")
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in (logging.StreamHandler(), logging.FileHandler(
        root / f"collector_{date.today().isoformat()}.log", encoding="utf-8"
    )):
        handler.addFilter(SecretFilter(secret))
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger
