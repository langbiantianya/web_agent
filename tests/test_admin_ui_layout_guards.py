"""管理后台 UI 布局修复的**静态**守门测试（LAY-01 … LAY-05）。

第五轮 UI 校验（``docs/testing/admin-ui-layout-audit-2026-11-11.md``）在
浏览器里量出 5 个真问题。它们的共同点是：单测跑页面渲染**抓不到**，
只有真的把浏览器窗口调成深色、把宽度压到 768px 才看得见。

这里用**读源码 + 算对比度**的方式把它们钉住，避免下一轮改样式时又被
悄悄改回去：

- LAY-01 深色主题下危险按钮的对比度；
- LAY-02 Cloudscape 侧边栏分组标题的对比度；
- LAY-03 平板宽度的卡片式表格布局；
- LAY-04 按钮文字不再折行；
- LAY-05 入口标记不再压住端口圆圈。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS_PATH = ROOT / "services" / "admin_web" / "static" / "admin.css"
UI_JS_PATH = ROOT / "services" / "admin_web" / "static" / "admin-ui.js"
EDITOR_JS_PATH = ROOT / "services" / "admin_web" / "static" / "workflow-editor.js"


def _css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def _relative_luminance(hex_color: str) -> float:
    """WCAG 2.x 的相对亮度。"""

    value = hex_color.lstrip("#")
    assert len(value) == 6, f"只支持 #rrggbb：{hex_color}"
    channels = []
    for index in (0, 2, 4):
        raw = int(value[index : index + 2], 16) / 255
        if raw <= 0.03928:
            channels.append(raw / 12.92)
        else:
            channels.append(((raw + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first: str, second: str) -> float:
    """两色之间的 WCAG 对比度。"""

    light = _relative_luminance(first)
    dark = _relative_luminance(second)
    if light < dark:
        light, dark = dark, light
    return (light + 0.05) / (dark + 0.05)


def _variables(css: str, selector: str) -> dict[str, str]:
    """取出某个规则块里声明的自定义属性。"""

    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"找不到规则块 {selector}"
    return dict(
        re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", match.group(1))
    )


# ---------------------------------------------------------------- LAY-01


def test_danger_button_text_follows_theme() -> None:
    """``--color-danger`` 变了，文字色必须跟着变。"""

    css = _css()
    button = re.search(r"\.btn-danger\s*\{([^}]*)\}", css)
    assert button, "找不到 .btn-danger 规则"
    assert "var(--color-danger-text)" in button.group(1), (
        ".btn-danger 的文字色必须用 var(--color-danger-text)，不能写死"
    )


@pytest.mark.parametrize(
    "selector",
    [
        ":root",
        'html[data-color-mode="dark"]',
        'html[data-color-mode="system"]',
    ],
)
def test_every_danger_color_has_a_text_color(selector: str) -> None:
    """凡是改了 ``--color-danger`` 的地方，都必须同时给出文字色。"""

    css = _css()
    variables = _variables(css, selector)
    if "--color-danger" not in variables:
        pytest.skip(f"{selector} 没有覆盖 --color-danger")
    assert "--color-danger-text" in variables, (
        f"{selector} 覆盖了 --color-danger 却没覆盖 --color-danger-text"
    )


def test_dark_danger_button_reaches_wcag_aa() -> None:
    """深色模式下危险按钮的文字/底色对比度要 >= 4.5:1。"""

    css = _css()
    variables = _variables(css, 'html[data-color-mode="dark"]')
    ratio = _contrast(
        variables["--color-danger"], variables["--color-danger-text"]
    )
    assert ratio >= 4.5, f"深色模式危险按钮对比度只有 {ratio:.2f}:1"


def test_light_danger_button_reaches_wcag_aa() -> None:
    """浅色模式同理，别为了修深色把浅色改坏了。"""

    css = _css()
    variables = _variables(css, ":root")
    ratio = _contrast(
        variables["--color-danger"], variables["--color-danger-text"]
    )
    assert ratio >= 4.5, f"浅色模式危险按钮对比度只有 {ratio:.2f}:1"


# ---------------------------------------------------------------- LAY-02


def test_cloudscape_nav_group_title_uses_theme_variable() -> None:
    """Cloudscape 的分组标题不能写死颜色，否则深色下不可读。"""

    css = _css()
    match = re.search(
        r'html\[data-theme="cloudscape"\] \.nav-group-title\s*\{([^}]*)\}',
        css,
    )
    assert match, "找不到 Cloudscape 的 .nav-group-title 规则"
    body = match.group(1)
    assert "var(--color-sidebar-muted)" in body, (
        "Cloudscape 的 .nav-group-title 必须用 var(--color-sidebar-muted)"
    )
    assert not re.search(r"color:\s*#[0-9a-fA-F]{3,6}", body), (
        "Cloudscape 的 .nav-group-title 不能写死颜色"
    )


def _effective_dark(css: str, theme: str, mode: str) -> dict[str, str]:
    """某个主题 + 颜色模式下**最终生效**的自定义属性。

    主题块只覆盖自己声明的那几项，其余继承通用深色块——CSS 里就是
    「后写的同优先级规则覆盖先写的」，所以这里做同样的合并。
    """

    generic = _variables(css, f'html[data-color-mode="{mode}"]')
    themed = _variables(css, f'html[data-theme="{theme}"][data-color-mode="{mode}"]')
    return {**generic, **themed}


@pytest.mark.parametrize("theme", ["fluent", "cloudscape", "material"])
@pytest.mark.parametrize("mode", ["dark", "system"])
def test_dark_nav_title_reaches_wcag_aa(theme: str, mode: str) -> None:
    """深色模式下侧边栏分组标题的对比度要 >= 4.5:1。"""

    css = _css()
    variables = _effective_dark(css, theme, mode)
    sidebar = variables.get("--color-sidebar")
    muted = variables.get("--color-sidebar-muted")
    assert sidebar and muted, f"{theme}/{mode} 缺少侧边栏底色或弱化文字色"
    ratio = _contrast(sidebar, muted)
    assert ratio >= 4.5, (
        f"{theme}/{mode} 下侧边栏分组标题对比度只有 {ratio:.2f}:1"
    )


@pytest.mark.parametrize("theme", ["fluent", "cloudscape", "material"])
@pytest.mark.parametrize("mode", ["dark", "system"])
def test_dark_danger_button_reaches_wcag_aa_in_every_theme(theme: str, mode: str) -> None:
    """三个主题的深色分支下，危险按钮都要达到 WCAG AA。"""

    css = _css()
    variables = _effective_dark(css, theme, mode)
    ratio = _contrast(
        variables["--color-danger"], variables["--color-danger-text"]
    )
    assert ratio >= 4.5, (
        f"{theme}/{mode} 下危险按钮对比度只有 {ratio:.2f}:1"
    )


# ---------------------------------------------------------------- LAY-03


def test_tablet_layout_turns_tables_into_cards() -> None:
    """768–1199px 必须走卡片布局，不能再把表头压成竖排。"""

    css = _css()
    assert "@media (min-width: 768px) and (max-width: 1199px)" in css, (
        "缺少平板宽度的卡片布局断点"
    )
    assert ".data-table thead" in css, "卡片布局必须隐藏表头"
    assert "content: attr(data-label)" in css, "卡片布局必须用 data-label 显示列名"
    # 手机端（<=767px）保持「横向滚动 + 首列吸附」，不要被卡片布局抢走。
    assert ".data-table {" in css


def test_table_labels_are_stamped_by_script() -> None:
    """列名必须由脚本打到每个单元格上。"""

    source = UI_JS_PATH.read_text(encoding="utf-8")
    assert "function bindTableLabels()" in source, "admin-ui.js 缺少 bindTableLabels"
    assert "data-label" in source or "dataset.label" in source, (
        "bindTableLabels 必须给单元格打上 data-label"
    )


# ---------------------------------------------------------------- LAY-04


def test_buttons_never_wrap_their_label() -> None:
    """按钮文字不允许折行（「保」/「存」两行那种）。"""

    css = _css()
    match = re.search(r"(?m)^\.btn \{([^}]*)\}", css)
    assert match, "找不到 .btn 规则"
    assert "white-space: nowrap" in match.group(1), (
        ".btn 必须设置 white-space: nowrap"
    )


def test_mapping_actions_can_wrap_instead_of_squeezing() -> None:
    """行内编辑区放不下时换行，而不是把按钮压扁。"""

    css = _css()
    match = re.search(r"(?m)^\.mapping-actions \{([^}]*)\}", css)
    assert match, "找不到 .mapping-actions 规则"
    assert "flex-wrap: wrap" in match.group(1), (
        ".mapping-actions 必须允许换行"
    )


# ---------------------------------------------------------------- LAY-05


def test_entry_mark_does_not_sit_on_the_input_port() -> None:
    """入口标记的基线不能落在端口圆心那一行。"""

    source = EDITOR_JS_PATH.read_text(encoding="utf-8")
    match = re.search(
        r'class: "entry-mark",\s*\n\s*x: (-?[\d.]+),\s*\n\s*y: ([-\d.]+),',
        source,
    )
    assert match, "找不到入口标记的定位代码"
    mark_x = float(match.group(1))
    mark_y = float(match.group(2))
    port_y = 72 / 2  # core.NODE_HEIGHT / 2
    assert abs(mark_y - port_y) >= 12, (
        f"入口标记的基线（{mark_y}）离端口圆心（{port_y}）太近，会和圆圈挤在一起"
    )
    assert mark_x <= -7, (
        f"入口标记的右沿（{mark_x}）不能压到端口圆圈的外沿（-7）"
    )

# ---------------------------------------------------------------- LAY-08


def test_sidebar_sticks_below_the_topbar() -> None:
    """左侧导航必须吸附在顶栏下沿，不跟着页面一起滚走。"""

    css = _css()
    match = re.search(r"(?m)^\.sidebar \{([^}]*)\}", css)
    assert match, "找不到 .sidebar 规则"
    body = match.group(1)
    assert "position: sticky" in body, ".sidebar 必须 position: sticky"
    assert "top: var(--topbar-height)" in body, (
        ".sidebar 必须吸附在 var(--topbar-height) 处（顶栏下沿）"
    )
    assert "align-self: start" in body, (
        ".sidebar 必须 align-self: start，否则网格项会被拉满整行、没法吸附"
    )
    assert "overflow-y: auto" in body, (
        "导航比一屏还长时要能自己内部滚动"
    )


def test_every_theme_declares_its_topbar_height() -> None:
    """三个主题都要给出顶栏高度，侧边栏才知道吸附到哪儿。"""

    css = _css()
    base = _variables(css, ":root")
    assert "--topbar-height" in base, "缺少默认的 --topbar-height"
    for theme in ("fluent", "cloudscape", "material"):
        variables = _variables(css, f'html[data-theme="{theme}"]')
        assert "--topbar-height" in variables, (
            f"{theme} 主题缺少 --topbar-height"
        )
        assert variables["--topbar-height"].endswith("px")


def test_mobile_sidebar_does_not_stick() -> None:
    """手机端侧边栏变成顶部导航，不能还吸附着。"""

    css = _css()
    mobile = css.split("@media (max-width: 767px)")[1]
    match = re.search(r"(?m)^  \.sidebar \{([^}]*)\}", mobile)
    assert match, "手机端断点里找不到 .sidebar 规则"
    assert "position: static" in match.group(1), (
        "手机端的 .sidebar 必须回到 position: static"
    )


# ---------------------------------------------------------------- LAY-09


def test_form_control_rules_skip_checkboxes_and_radios() -> None:
    """表单控件那套尺寸规则不能套到勾选框 / 单选框上。

    ``width: 100%`` + ``min-height: 42px`` 会把勾选框拉成细长条，勾选
    标记被一起拉变形，看上去就是个奇怪的锥形。
    """

    css = _css()
    generic = re.search(
        r"(?m)^input:not\(\[type=\"checkbox\"\]\):not\(\[type=\"radio\"\]\),\n"
        r"select,\ntextarea \{",
        css,
    )
    assert generic, "通用表单控件规则必须排除 checkbox / radio"
    for theme in ("fluent", "cloudscape", "material"):
        assert (
            f'html[data-theme="{theme}"] input:not([type="checkbox"]):not([type="radio"]),'
            in css
        ), f"{theme} 的表单控件规则必须排除 checkbox / radio"


def test_checkbox_and_radio_have_a_fixed_size() -> None:
    """勾选框 / 单选框必须有固定尺寸，不能被容器拉长。"""

    css = _css()
    match = re.search(
        r"(?m)^input\[type=\"checkbox\"\],\ninput\[type=\"radio\"\] \{([^}]*)\}",
        css,
    )
    assert match, "找不到勾选框 / 单选框的样式"
    body = match.group(1)
    assert "width: 16px" in body and "height: 16px" in body, (
        "勾选框 / 单选框必须是固定尺寸"
    )
    assert "min-height: 0" in body, "min-height 必须清掉，否则会被拉到 42px 高"
    assert "accent-color: var(--color-accent)" in body, (
        "勾选框要跟着主题走颜色"
    )
