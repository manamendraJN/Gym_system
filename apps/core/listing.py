"""Helpers for sortable list pages."""


def resolve_sort(request, allowed, default):
    """allowed: {key: order_by field}. Returns (key, direction, order_by_expression)."""
    key = request.GET.get("sort", default)
    if key not in allowed:
        key = default
    direction = "desc" if request.GET.get("dir") == "desc" else "asc"
    field = allowed[key]
    order = f"-{field}" if direction == "desc" else field
    return key, direction, [order, "-pk"] if direction == "desc" else [order, "pk"]


def sort_columns(request, columns, current, direction):
    """columns: [(key, label)]. Returns dicts with header link URLs that keep the other filters."""
    result = []
    for key, label in columns:
        params = request.GET.copy()
        params.pop("page", None)
        active = key == current
        params["sort"] = key
        params["dir"] = "desc" if active and direction == "asc" else "asc"
        result.append({"key": key, "label": label, "url": "?" + params.urlencode(), "active": active, "dir": direction})
    return result
