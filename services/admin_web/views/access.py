"""页面与组件权限页面的兼容再导出。"""

from services.admin_web.views.access_components import access_components_page
from services.admin_web.views.access_pages import access_pages_page

__all__ = ["access_components_page", "access_pages_page"]
