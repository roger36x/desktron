"""原创圆润图标：全套资源、透明蒙版与用户确认细节回归。"""
from hashlib import sha256
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
ICONS = ROOT / "frontend/icons"
MANIFEST = json.loads((ICONS / "rounded-original-manifest.json").read_text(encoding='utf-8'))
NS = {"s": "http://www.w3.org/2000/svg"}
EXPECTED = {
    "composer-mode", "composer-lock", "task", "current-session-task",
    "arrangement", "status", "status-alert", "status-login", "sidebar-chat",
    "sidebar-apps", "sidebar-settings", "menu-rename", "menu-delete",
    "menu-memory", "right-arrow", "chat-branch-4626029",
}


def svg(name):
    return ET.parse(ICONS / (name + ".svg")).getroot()


def test_all_sixteen_original_sources_and_pngs_match_manifest():
    assert MANIFEST["license"] == "Apache-2.0"
    assert {i["name"] for i in MANIFEST["icons"]} == EXPECTED
    assert len(MANIFEST["icons"]) == 16
    css = (ROOT / "frontend/style.css").read_text(encoding='utf-8')
    for icon in MANIFEST["icons"]:
        for kind in ("png", "svg"):
            data = (ICONS / icon[kind]).read_bytes()
            assert sha256(data).hexdigest() == icon[kind + "_sha256"]
        assert '/icons/' + icon["png"] in css
        with Image.open(ICONS / icon["png"]) as image:
            assert image.size == (512, 512)
            assert image.mode == "RGBA"
            assert image.getchannel("A").getextrema() == (0, 255)
            box = image.getbbox()
            assert box and 0 < box[0] < box[2] < 512
            assert 0 < box[1] < box[3] < 512
        source = svg(icon["name"])
        assert source.attrib["viewBox"] == "0 0 32 32"
        assert not source.findall(".//s:image", NS)
        assert not source.findall(".//s:script", NS)


def test_context_status_alert_uses_circular_outline_and_preserves_exclamation():
    source = svg("status-alert")
    circles = source.findall(".//s:circle", NS)
    assert len(circles) == 2
    assert circles[0].attrib == {"cx": "16", "cy": "16", "r": "13.7"}
    assert circles[1].attrib["r"] == "1.2"
    assert circles[1].attrib["cy"] == "23.8"
    paths = source.findall(".//s:path", NS)
    assert [path.attrib["d"] for path in paths] == ["M16 11v8"]
    assert next(i for i in MANIFEST["icons"] if i["name"] == "status-alert")["version"] == "v3-circle"


def test_user_approved_v4_v5_v6_details():
    versions = {i["name"]: i["version"] for i in MANIFEST["icons"]}
    assert versions["current-session-task"] == "v4"
    frame = svg("current-session-task").find(".//s:rect", NS)
    assert frame.attrib["width"] == frame.attrib["height"] == "25"
    assert versions["sidebar-chat"] == "v9-C"
    assert len(svg("sidebar-chat").findall(".//s:circle", NS)) == 3
    dots = svg("arrangement").findall(".//s:circle", NS)
    assert len(dots) == 6
    assert versions["arrangement"] == "v9-C"
    assert sorted({float(d.attrib["cy"]) for d in dots}) == [16.8, 23.1]
    assert {float(d.attrib["r"]) for d in dots} == {1.05}
    wave = svg("status").find(".//s:path", NS).attrib["d"]
    assert wave.count("C") == 3
    assert "L" not in wave and "l" not in wave
    assert versions["sidebar-settings"] == "v6-设置齿轮"
    assert svg("sidebar-settings").find(".//s:circle", NS).attrib["r"] == "5.6"


def test_v9_c_optical_sizing_preserves_other_icons_and_documents_calendar_revision():
    assert MANIFEST["version"] == "rounded-original-v9-C"
    sizes = {}
    for icon in MANIFEST["icons"]:
        calibration = icon["optical"]
        group = svg(icon["name"]).find("s:g", NS)
        assert group is not None
        assert "scale(" in group.attrib["transform"]
        assert "," not in group.attrib["transform"]  # one uniform scale, never stretch axes
        before = calibration["before_bbox"]
        after = calibration["after_bbox"]
        ratio = (before[2] - before[0]) / (before[3] - before[1])
        new_ratio = (after[2] - after[0]) / (after[3] - after[1])
        if icon["name"] != "arrangement":
            assert abs(ratio - new_ratio) < 0.015
        else:
            # V9 intentionally widens the body, rather than scaling everything.
            assert calibration["shape_revision"]
            assert after[2] - after[0] > calibration["v8_bbox"][2] - calibration["v8_bbox"][0]
        extent_limit = 502 if icon["name"] == "arrangement" else 476
        assert max(after[2] - after[0], after[3] - after[1]) <= extent_limit
        with Image.open(ICONS / icon["png"]) as image:
            assert list(image.getbbox()) == after
        assert abs((after[0] + after[2]) / 2 - 256) <= 2
        sizes[icon["name"]] = calibration["after_hull_area"]
    arrangement = next(i for i in MANIFEST["icons"] if i["name"] == "arrangement")
    assert arrangement["optical"]["manual_overshoot"] == 1.06
    assert arrangement["optical"]["max_extent"] == 31.25
    assert arrangement["optical"]["manual_reason"]
    # Historic equal-hull calibration is not a V9 visual-equality assertion.
    # C deliberately widens calendar body and shrinks chat, not applications.
    chat = next(i for i in MANIFEST["icons"] if i["name"] == "sidebar-chat")
    assert chat["optical"]["scale"] > 1.05
    assert chat["optical"]["v9_outer_scale"] == .97
    assert chat["optical"]["v9_outer_translate"] == [.48, .48]
    assert abs(sizes["sidebar-chat"] / chat["optical"]["v8_hull_area"] - .97 ** 2) < .005
    apps = next(i for i in MANIFEST["icons"] if i["name"] == "sidebar-apps")
    assert apps["optical"]["scale"] == .93744


def test_v9_c_calendar_whitespace_and_chat_transform_match_approved_preview():
    calendar = svg("arrangement")
    frame = calendar.find(".//s:rect", NS)
    assert float(frame.attrib["width"]) == 24.4
    assert float(frame.attrib["height"]) == 23.5
    assert calendar.attrib["stroke-width"] == "2.1"
    assert calendar.find(".//s:path", NS).attrib["d"] == "M10 2.8v4.6M22 2.8v4.6M4.3 10.8h23.4"
    dots = calendar.findall(".//s:circle", NS)
    assert sorted({float(d.attrib["cx"]) for d in dots}) == [10.15, 16, 21.85]
    # Compare vertical whitespace from divider to first row and last row to frame.
    top_gap = 16.8 - 1.05 - (10.8 + 2.1 / 2)
    bottom_gap = 29 - 2.1 / 2 - (23.1 + 1.05)
    assert abs(top_gap - bottom_gap) < .2
    assert svg("sidebar-chat").find("s:g", NS).attrib["transform"] == "translate(0.48 0.48) scale(0.97)"


def test_manifest_hull_areas_are_measured_from_current_pngs():
    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    for icon in MANIFEST["icons"]:
        with Image.open(ICONS / icon["png"]) as image:
            alpha = image.getchannel("A").point(lambda p: 255 if p >= 128 else 0)
            points = []
            for y in range(512):
                box = alpha.crop((0, y, 512, y + 1)).getbbox()
                if box:
                    points.extend([(box[0], y), (box[2] - 1, y)])
        points = sorted(set(points))
        lo, hi = [], []
        for out, seq in ((lo, points), (hi, reversed(points))):
            for point in seq:
                while len(out) > 1 and cross(out[-2], out[-1], point) <= 0:
                    out.pop()
                out.append(point)
        hull = lo[:-1] + hi[:-1]
        area = abs(sum(a[0] * b[1] - b[0] * a[1]
                       for a, b in zip(hull, hull[1:] + hull[:1]))) / 512
        assert abs(area - icon["optical"]["after_hull_area"]) < .001, icon["name"]


def test_original_scope_does_not_reclassify_unknown_or_brand_assets():
    assert not {"bell", "shepherd", "menu-move", "claude", "openai"} & EXPECTED
    license_text = (ROOT / "frontend/vendor/ICONS-LICENSE.txt").read_text(encoding='utf-8')
    assert "不据此声明整仓可公开发行" in license_text
    assert "Flaticon条目仅记录替换前素材" in license_text
    assert "rounded-original-manifest.json" in license_text
