import httpx
import pytest

from app.bepinex_sources import (
    BepInExAssetNotFound,
    BepInExFetchError,
    BepInExPackageNotFound,
    BepInExUntrustedUrl,
    fetch_github_release_latest,
    fetch_thunderstore_latest,
)


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_thunderstore_latest_calls_expected_url_and_parses():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"latest": {
            "version_number": "2.30.0",
            "download_url": "https://thunderstore.io/package/download/ValheimModding/Jotunn/2.30.0/",
        }})

    result = await fetch_thunderstore_latest(make_client(handler), "ValheimModding", "Jotunn")
    assert seen["url"] == "https://thunderstore.io/api/experimental/package/ValheimModding/Jotunn/"
    assert result == {
        "version": "2.30.0",
        "download_url": "https://thunderstore.io/package/download/ValheimModding/Jotunn/2.30.0/",
    }


async def test_fetch_thunderstore_latest_404_raises_package_not_found():
    def handler(request):
        return httpx.Response(404)

    with pytest.raises(BepInExPackageNotFound):
        await fetch_thunderstore_latest(make_client(handler), "nobody", "nothing")


async def test_fetch_thunderstore_latest_500_raises_fetch_error():
    def handler(request):
        return httpx.Response(500)

    with pytest.raises(BepInExFetchError):
        await fetch_thunderstore_latest(make_client(handler), "a", "b")


async def test_fetch_thunderstore_latest_invalid_json_raises_fetch_error():
    def handler(request):
        return httpx.Response(200, text="not json")

    with pytest.raises(BepInExFetchError):
        await fetch_thunderstore_latest(make_client(handler), "a", "b")


async def test_fetch_thunderstore_latest_untrusted_download_host_rejected():
    def handler(request):
        return httpx.Response(200, json={"latest": {
            "version_number": "1.0.0", "download_url": "https://evil.example.com/payload.zip",
        }})

    with pytest.raises(BepInExUntrustedUrl):
        await fetch_thunderstore_latest(make_client(handler), "a", "b")


async def test_fetch_github_release_latest_parses_tag_and_asset():
    def handler(request):
        return httpx.Response(200, json={
            "tag_name": "v1.2.25",
            "assets": [
                {"name": "XPortal-debug-v1.2.25.zip",
                 "browser_download_url": "https://github.com/SpikeHimself/XPortal/releases/download/v1.2.25/XPortal-debug-v1.2.25.zip"},
                {"name": "XPortal-v1.2.25.zip",
                 "browser_download_url": "https://github.com/SpikeHimself/XPortal/releases/download/v1.2.25/XPortal-v1.2.25.zip"},
            ],
        })

    result = await fetch_github_release_latest(
        make_client(handler), "SpikeHimself", "XPortal",
        tag_prefix="v", asset_pattern=r"^XPortal-v.*\.zip$",
    )
    assert result == {
        "version": "1.2.25",
        "download_url": "https://github.com/SpikeHimself/XPortal/releases/download/v1.2.25/XPortal-v1.2.25.zip",
    }


async def test_fetch_github_release_latest_no_matching_asset_raises():
    def handler(request):
        return httpx.Response(200, json={"tag_name": "v1.2.25", "assets": [
            {"name": "readme.txt", "browser_download_url": "https://github.com/x/y/releases/download/v1.2.25/readme.txt"},
        ]})

    with pytest.raises(BepInExAssetNotFound):
        await fetch_github_release_latest(
            make_client(handler), "a", "b", tag_prefix="v", asset_pattern=r"^XPortal-v.*\.zip$",
        )


async def test_fetch_github_release_latest_404_raises_package_not_found():
    def handler(request):
        return httpx.Response(404)

    with pytest.raises(BepInExPackageNotFound):
        await fetch_github_release_latest(
            make_client(handler), "a", "b", tag_prefix="v", asset_pattern=r".*\.zip$",
        )


async def test_fetch_github_release_latest_untrusted_asset_host_rejected():
    def handler(request):
        return httpx.Response(200, json={"tag_name": "v1.0.0", "assets": [
            {"name": "x.zip", "browser_download_url": "https://evil.example.com/x.zip"},
        ]})

    with pytest.raises(BepInExUntrustedUrl):
        await fetch_github_release_latest(
            make_client(handler), "a", "b", tag_prefix="v", asset_pattern=r".*\.zip$",
        )


async def test_fetch_github_release_latest_no_tag_prefix_keeps_tag_as_version():
    def handler(request):
        return httpx.Response(200, json={"tag_name": "1.2.25", "assets": [
            {"name": "x.zip", "browser_download_url": "https://github.com/a/b/releases/download/1.2.25/x.zip"},
        ]})

    result = await fetch_github_release_latest(
        make_client(handler), "a", "b", tag_prefix=None, asset_pattern=r".*\.zip$",
    )
    assert result["version"] == "1.2.25"
