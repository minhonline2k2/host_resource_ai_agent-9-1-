import logging, json, sys
from datetime import datetime, timezone
class JSONFormatter(logging.Formatter):
    def format(self, r):
        return json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "level": r.levelname, "logger": r.name, "msg": r.getMessage()}, default=str)
def setup_logging(level="INFO"):
    h = logging.StreamHandler(sys.stdout); h.setFormatter(JSONFormatter())
    root = logging.getLogger(); root.setLevel(getattr(logging, level.upper(), logging.INFO)); root.handlers = [h]
def get_logger(name): return logging.getLogger(name)
