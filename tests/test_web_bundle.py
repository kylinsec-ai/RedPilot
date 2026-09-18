"""前端构建产物的服务契约回归(web/ 即交付物)。

为什么需要这条用例:vite 产物名带内容哈希、由两个 HTTP 服务端转发
(`redpilot/obs/read.py` 的统一 server 与 `redpilot/worker/dashboard.py` 的本地态势台),
而两者的 mime/名字守卫**单源**在 redpilot.contracts.assets。于是存在一类静默失败:
构建换了扩展名(或 vite 输出嵌套路径)而白名单没跟上 → 站点外壳 200、
静态资源 404,页面白屏且服务端毫无报错。

本用例直接读仓库里**已提交的 web/** 并按 HTTP 契约走一遍,把这类漂移钉在构建产物上
(改前端栈/升级打包器后跑一次就知道能不能服务)。

注:产物是提交物而非本测试的产物 —— 用例不构建,只校验既有 web/ 是否可服务。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from redpilot.app import create_app
from redpilot.control.config import Settings as ControlSettings
from redpilot.obs.config import Settings as ObsSettings
from redpilot.contracts.assets import ASSET_RX, ASSET_TYPES, asset_content_type

WEB_DIR = Path(__file__).resolve().parents[1] / "web"

# index.html 里引用的静态资源路径(/assets/xxx 或 ./assets/xxx)
ASSET_REF_RX = re.compile(r"""(?:href|src)\s*=\s*["']([^"']*assets/[^"']+)["']""")

TASK_TOKEN = "task-bundle"
ADMIN_TOKEN = "admin-bundle"
OBS_TOKEN = "obs-bundle"

# 本文件所有用例都读磁盘上的产物:没构建就整体跳过(条件只写这一处)
pytestmark = pytest.mark.skipif(not WEB_DIR.is_dir(), reason="web/ 不存在(未构建)")


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        control_settings=ControlSettings(
            database_path=str(tmp_path / "control.sqlite3"),
            benchmark_token=TASK_TOKEN,
            admin_token=ADMIN_TOKEN,
        ),
        obs_settings=ObsSettings(
            obs_token=OBS_TOKEN,
            db_path=str(tmp_path / "obs.sqlite3"),
            web_dir=str(WEB_DIR),
        ),
        tasks={"token": TASK_TOKEN, "challenges": []},
    )
    with TestClient(app) as c:
        yield c


def _asset_refs(html: str) -> list[str]:
    """index.html 引用的资源路径(去掉查询串/前导 ./)。"""
    refs: list[str] = []
    for raw in ASSET_REF_RX.findall(html):
        path = raw.split("?", 1)[0].split("#", 1)[0]
        if path.startswith("./"):
            path = path[2:]
        refs.append(path)
    return refs


def test_spa_shell_served(client):
    """`/` 返回 SPA 外壳:html + no-store,且确实是本仓库的产物。"""
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "no-store" in r.headers.get("cache-control", "")
    assert '<div id="app">' in r.text
    # 外壳必须与磁盘上的产物一致(否则镜像里的是旧构建)
    assert r.text.strip() == (WEB_DIR / "index.html").read_text(encoding="utf-8").strip()


def test_index_referenced_assets_are_all_serviceable(client):
    """index.html 引到的每个资源都必须落在 mime 白名单内、取得到、且按哈希不可变缓存。

    这是本文件的核心断言:构建产物换名/换扩展名/换目录时,服务的守卫是
    ASSET_RX(平铺名)+ ASSET_TYPES(扩展名白名单),对不上就是 404 白屏。
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    refs = _asset_refs(html)
    assert refs, "index.html 没有引用任何 /assets/ 资源——构建产物结构变了?"

    for ref in refs:
        name = ref.rsplit("/", 1)[-1]
        ext = "." + name.rsplit(".", 1)[-1].lower()

        # 1) 名字与扩展名必须被契约层接受(否则服务端一律 404)
        assert "/" not in name and "\\" not in name, f"{ref} 不是平铺资源名"
        assert ASSET_RX.fullmatch(name), f"{ref} 不匹配 ASSET_RX 平铺名守卫"
        assert ext in ASSET_TYPES, (
            f"扩展名 {ext} 不在 redpilot.contracts.assets.ASSET_TYPES 白名单里;"
            f"构建换了产物类型就要同步白名单,否则该资源会静默 404"
        )

        # 2) 真的取得到,且类型/缓存策略正确
        r = client.get("/" + ref.lstrip("/"))
        assert r.status_code == 200, f"{ref} 取不到:HTTP {r.status_code}"
        assert r.headers["content-type"] == asset_content_type(ext)
        assert "immutable" in r.headers.get("cache-control", "")
        assert r.content, f"{ref} 是空文件"


def test_bundle_is_self_contained():
    """产物不得引用外网 CDN:镜像内网部署,外链=白屏。"""
    offenders: list[str] = []
    for p in [WEB_DIR / "index.html", *(WEB_DIR / "assets").glob("*")]:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # 只找"被当资源引用"的绝对地址,避免误伤注释/许可证里的字面量
        for m in re.finditer(r"""(?:href|src|url\()\s*["']?(https?://[^"')\s]+)""", text):
            offenders.append(f"{p.name}: {m.group(1)}")
    assert not offenders, "产物引用了外网资源: " + "; ".join(offenders)


def test_asset_guard_rejects_traversal_and_unknown_ext(client):
    """穿越与白名单外的扩展名必须 404(服务的守卫,不是 vite 的)。

    注:字面量 `/assets/../index.html` 走不到这里 —— httpx/ASGI 会先按 RFC 3986
    把 `..` 归一掉,请求变成 `/index.html`,命中的是 SPA 外壳(200)。所以穿越
    用**编码形态**探测:它们不被归一,原样进到 `assets()` 的名字守卫。
    """
    for probe in (
        "/assets/..%2F..%2Fetc%2Fpasswd",  # 编码的分隔符 → 名字守卫拒
        "/assets/%2e%2e%2findex.html",  # 编码的点 → 名字守卫拒
        "/assets/sub%2Fname.js",  # 子路径(产物必须是平铺名)→ 拒
        "/assets/%00.js",  # NUL → 拒
        "/assets/whatever.wasm",  # 扩展名不在 ASSET_TYPES 白名单
        "/assets/nope.js",  # 形状合法但文件不存在
    ):
        assert client.get(probe).status_code == 404, probe
