"""管理端列表的「搜索 + 排序 + 分页」共用参数。

UX-06 记录的问题：管理端各列表页没有搜索、筛选、排序和分页，接口一次
最多展示 500 条——资源一多就只能靠浏览器里翻，找不到东西。

这里提供统一的查询参数解析与分页计算，让各列表页用**同一套**口径：

- ``q`` —— 搜索关键字（对编码与名称做包含匹配）；
- ``sort`` —— 排序字段（各页面自己声明白名单，不认识的取值回落到默认）；
- ``page`` —— 页码，从 1 开始；
- ``size`` —— 每页条数，有上限（防止一个请求拉爆内存）。

## 为什么参数放 URL

筛选状态放 URL 才能**分享**：把「只看启用的 LLM 端点」这个视图发给
同事，对方打开就是同一个视图。查询串里只放关键字与页码，不放任何
凭据。

## 分页是**服务端**做的

接口一次最多展示 500 条正是本条问题要修的：如果分页在浏览器里做，
服务端仍然要把 500 条全传下来，接口的负担没有变。因此这里的分页是
「取第 N 页」——各仓储的 ``limit``/``offset`` 直接用上，页面只拿到
当前页的数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ludic.web import Request

# 每页条数上限。接口一次最多展示 500 条的问题由此收敛：分页后单页
# 永远不超过这个数。
MAX_PAGE_SIZE = 100

# 默认每页条数。20 条在一屏里看得完，又不至于让首屏太长。
DEFAULT_PAGE_SIZE = 20


@dataclass(frozen=True)
class ListParams:
    """一次列表查询的解析结果。"""

    q: str = ""
    sort: str = ""
    page: int = 1
    size: int = DEFAULT_PAGE_SIZE

    @property
    def offset(self) -> int:
        """返回 SQL 的 OFFSET。"""
        return (self.page - 1) * self.size


def parse_list_params(
    request: Request,
    *,
    allowed_sorts: tuple[str, ...] = (),
    default_size: int = DEFAULT_PAGE_SIZE,
) -> ListParams:
    """从请求的查询串解析列表参数。

    解析要**宽容**：URL 里的脏值（``page=abc``、``size=-5``、
    ``sort=unknown``）不能让页面 500，也不该被当成有效值传下去。
    """
    raw_query = (request.query_params.get("q") or "").strip()
    raw_sort = (request.query_params.get("sort") or "").strip()
    raw_page = (request.query_params.get("page") or "").strip()
    raw_size = (request.query_params.get("size") or "").strip()

    size = default_size
    if raw_size.isdigit():
        size = min(int(raw_size), MAX_PAGE_SIZE)
        size = max(size, 1)

    page = 1
    if raw_page.isdigit():
        page = max(int(raw_page), 1)

    sort = raw_sort if raw_sort in allowed_sorts else ""
    return ListParams(q=raw_query, sort=sort, page=page, size=size)


def page_count(total: int, size: int) -> int:
    """返回总页数（至少 1 页）。"""
    if size <= 0:
        return 1
    return max((total + size - 1) // size, 1)


def clamp_page(page: int, total: int, size: int) -> int:
    """把页码夹到有效范围内。

    删掉几行之后停在最后一页会看到空列表——夹回有效页码更符合直觉。
    """
    pages = page_count(total, size)
    return min(max(page, 1), pages)


def search_term(query: str) -> str:
    """把搜索关键字规整成仓储层能用的 LIKE 片段。

    ``%`` 与 ``_`` 是 LIKE 的通配符，直接拼进 pattern 会让用户搜
    ``a%`` 变成「匹配任意内容」。这里把它们转义掉。
    """
    if not query:
        return ""
    escaped = (
        query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "ListParams",
    "clamp_page",
    "page_count",
    "parse_list_params",
    "search_term",
]
