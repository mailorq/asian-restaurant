import logging

from uvicorn.workers import UvicornWorker


class PathWithoutQuery(logging.Filter):
    """the access line keeps the path and drops the query string, where a search carries a phone or a name"""

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn logs (client, method, path with query, http version, status)
        if isinstance(record.args, tuple) and len(record.args) == 5 and isinstance(record.args[2], str):
            client, method, path, version, status = record.args
            record.args = (client, method, path.split("?", 1)[0], version, status)
        return True


class BoundedUvicornWorker(UvicornWorker):
    # under ASGI every request runs in a thread of its own. past this many in flight uvicorn answers 503 without starting one, so a burst cannot run the container out of pids, where asgiref leaks the threads it fails to join
    CONFIG_KWARGS = {**UvicornWorker.CONFIG_KWARGS, "limit_concurrency": 32}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        logging.getLogger("uvicorn.access").addFilter(PathWithoutQuery())
