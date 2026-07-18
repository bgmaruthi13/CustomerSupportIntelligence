from django import template
from django.utils.html import format_html

register = template.Library()


@register.simple_tag(takes_context=True)
def sort_header(context, field, label, param_prefix=""):
    """Renders a clickable column header that toggles ascending/descending sort via
    ?sort=<field>&dir=asc|desc, preserving every other querystring param (filters,
    engine toggles, etc.) — only `sort`/`dir` are overwritten and `page` is dropped
    since a resort should land back on page 1. Pairs with core.utils.apply_sort, which
    reads the same two params server-side to build the actual .order_by().

    `param_prefix` namespaces the querystring keys (e.g. "members" ->
    ?members_sort=&members_dir=) so a page with more than one independently
    sortable table doesn't have one table's sort link clobber another's."""
    request = context["request"]
    sort_param = f"{param_prefix}_sort" if param_prefix else "sort"
    dir_param = f"{param_prefix}_dir" if param_prefix else "dir"
    current_sort = request.GET.get(sort_param, "")
    current_dir = request.GET.get(dir_param, "desc")
    is_active = current_sort == field
    next_dir = "asc" if (is_active and current_dir == "desc") else "desc"

    params = request.GET.copy()
    params[sort_param] = field
    params[dir_param] = next_dir
    params.pop("page", None)

    arrow = ""
    if is_active:
        arrow = " ▲" if current_dir == "asc" else " ▼"

    return format_html(
        '<a href="?{}" class="sort-header{}">{}{}</a>',
        params.urlencode(),
        " active" if is_active else "",
        label,
        arrow,
    )
