"""列表页的「搜索 + 排序 + 分页」控件。

UX-06 记录的问题：管理端各列表页没有搜索、筛选、排序和分页，接口一次
最多展示 500 条。

这里提供**一套**控件，所有列表页共用：

- **搜索**：按关键字过滤（编码、名称）；服务端执行，因此能找到当前
  页之外的资源；
- **排序**：按声明的白名单字段切换升/降序；
- **分页**：上/下一页与「第 N / M 页」，共 M 条。

## 状态放在 URL

筛选状态写进查询串（``?q=...&sort=...&page=...``），因此视图可以
**分享**：把「搜 order 且按名称升序」这个视图发给同事，打开就是同一个
视图。查询串里只有关键字与页码，不含任何凭据。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ludic.html import (
    a,
    button,
    div,
    form,
    input as input_,
    label,
    option,
    select,
    span,
)

from services.admin_web.listing import ListParams, page_count
from services.admin_web.views.field_controls import _Option

# 排序下拉框里的取值 → 显示文案。顺序即显示顺序。
SORT_LABELS: dict[str, str] = {
    "name": "Name",
    "code": "Code",
    "created": "Newest",
    "priority": "Priority",
}


def list_controls(
    *,
    action: str,
    params: ListParams,
    total: int,
    sort_options: tuple[str, ...] = (),
    search_label: str = "Search",
    search_placeholder: str = "Name or code",
) -> Any:
    """渲染列表页的搜索 / 排序 / 分页控件。

    ``sort_options`` 是允许的排序字段（``SORT_LABELS`` 的子集）。空元组
    表示这一页不提供排序。
    """
    pages = page_count(total, params.size)
    return div(
        div(
            form(
                label(
                    span(search_label),
                    input_(
                        name="q",
                        value=params.q,
                        placeholder=search_placeholder,
                        autocomplete="off",
                    ),
                ),
                *(
                    [
                        label(
                            span("Sort by"),
                            select(
                                # 空 value 必须显式渲染出来：ludic 会丢掉空
                                # 属性，浏览器就会把显示文案「默认」当成
                                # 提交值，URL 里出现非 ASCII 的 sort=默认。
                                _Option("Default", value=""),
                                *(
                                    option(
                                        SORT_LABELS.get(value, value),
                                        value=value,
                                        selected=value == params.sort,
                                    )
                                    for value in sort_options
                                ),
                                name="sort",
                            ),
                        ),
                    ]
                    if sort_options
                    else []
                ),
                button("Apply", type="submit", class_="btn btn-secondary"),
                action=action,
                method="get",
                class_="list-filter-form",
            ),
            _pager(action=action, params=params, total=total, pages=pages),
            class_="list-controls-row",
        ),
        class_="list-controls",
        data_list_total=str(total),
    )


def _pager(
    *,
    action: str,
    params: ListParams,
    total: int,
    pages: int,
) -> Any:
    """渲染分页条。"""
    return div(
        span(
            f"{total} item(s) · page {params.page} of {pages}",
            class_="list-summary",
        ),
        _page_link(
            action=action,
            params=params,
            page=params.page - 1,
            label="Previous",
            disabled=params.page <= 1,
        ),
        _page_link(
            action=action,
            params=params,
            page=params.page + 1,
            label="Next",
            disabled=params.page >= pages,
        ),
        class_="list-pager",
    )


def _page_link(
    *,
    action: str,
    params: ListParams,
    page: int,
    label: str,
    disabled: bool,
) -> Any:
    """渲染一个分页链接（禁用时渲染成不可点的文字）。"""
    if disabled:
        return span(label, class_="btn btn-secondary btn-small is-disabled")
    return a(
        label,
        href=_query_url(action, params, page),
        class_="btn btn-secondary btn-small",
    )


def _query_url(action: str, params: ListParams, page: int) -> str:
    """构造带筛选状态的 URL。

    手工拼查询串：参数值必须转义（关键字里可能有 ``&``），而排序字段
    已经过白名单校验，不含特殊字符。
    """
    parts = []
    if params.q:
        parts.append(f"q={quote(params.q, safe='')}")
    if params.sort:
        parts.append(f"sort={quote(params.sort, safe='')}")
    parts.append(f"page={max(page, 1)}")
    parts.append(f"size={params.size}")
    return f"{action}?{'&'.join(parts)}"


__all__ = ["SORT_LABELS", "list_controls"]
