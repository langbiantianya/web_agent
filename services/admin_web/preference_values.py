"""界面偏好的取值白名单。

原先这些常量住在 ``routes/preferences.py``（页面路由）里，页面与 API 各自
import 过去。Ludic 视图层下线后页面路由整个消失，它们就成了**只有 API
在用**的公共约束——所以搬到这里，与路由无关。

取值不在白名单里一律 400，不静默存进去。
"""

from __future__ import annotations

#: 允许的主题。
ALLOWED_THEMES = {"fluent", "cloudscape", "material"}

#: 允许的色温模式。
ALLOWED_COLOR_MODES = {"light", "dark", "system"}

#: 允许的界面密度。
ALLOWED_DENSITIES = {"comfortable", "compact"}
