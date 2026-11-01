"""手机侧栏字号独立上调，桌面基线保持不变。"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")


def test_mobile_sidebar_type_is_half_pixel_larger():
    marker = "/* 手机抽屉离眼睛更远：默认侧栏各级文字统一提 0.5px；独立浮层与桌面端不跟随。 */"
    mobile = CSS[CSS.index(marker):]
    for rule in (
        "#panel .side-brand .brand { font-size: 19.5px; }",
        "#panel .side-search input { font-size: 14px; }",
        "#panel .side-fixed .side-fx { font-size: 15px; }",
        "#panel .side-sec { font-size: 16px; }",
        "#panel .proj-fold .pf-name { font-size: 15px; }",
        "#panel #panel-list .term-item { font-size: 14.5px; }",
        "#panel .sess-more, #panel #panel-list .proj-sessions .group-empty { font-size: 13px; }",
        "#panel .side-cnt { font-size: 10.5px; }",
        "#panel .side-foot .side-fx, #panel .side-foot #engine-pick { font-size: 14.5px; }",
        "#panel .foot-be { font-size: 11.5px; }",
    ):
        assert rule in mobile


def test_desktop_sidebar_type_baseline_is_unchanged():
    marker = "/* 手机抽屉离眼睛更远：默认侧栏各级文字统一提 0.5px；独立浮层与桌面端不跟随。 */"
    desktop = CSS[:CSS.index(marker)]
    for value in (
        ".side-brand .brand { font-size: 19px",
        ".side-fx { font-size: 14.5px",
        ".side-sec { font-size: 15.5px",
        ".proj-fold .pf-name { font-size: 14.5px",
        "#panel-list .term-item { font-size: 14px",
        ".side-search input { font-size: 13.5px",
        ".side-foot #engine-pick { font-size: 14px",
    ):
        assert value in desktop
