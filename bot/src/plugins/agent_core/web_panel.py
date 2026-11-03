"""控制台静态资源入口；保留 PANEL_HTML 的字符串读取接口。"""
from pathlib import Path

WEB_ROOT = Path(__file__).with_name("web")


def get_panel_html() -> str:
    return (WEB_ROOT / "index.html").read_text(encoding="utf-8")


def __getattr__(name: str):
    if name in {"PANEL_HTML", "_RAW_PANEL_HTML"}:
        return get_panel_html()
    raise AttributeError(name)
