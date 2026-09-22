"""bounded listing for API endpoints"""

from ninja.errors import HttpError

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20
# an offset scan reads every row it skips, and one past the database's bigint is a query error, not an empty page
MAX_OFFSET = 10_000


def paginate(queryset, page: int, page_size: int, max_page_size: int = MAX_PAGE_SIZE) -> dict:
    """
    one page of a queryset, with the client's numbers clamped to a range worth serving

    the ceiling belongs here rather than in the query string: an unbounded list costs whatever
    the table has grown to, and the caller is the one paying for it least
    """
    page = max(1, page)
    page_size = min(max(1, page_size), max_page_size)
    start = (page - 1) * page_size
    if start > MAX_OFFSET:
        raise HttpError(422, "Такой далекой страницы нет: сузьте поиск или фильтр")
    return {
        "items": list(queryset[start : start + page_size]),
        "total": queryset.count(),
        "page": page,
        "page_size": page_size,
    }
