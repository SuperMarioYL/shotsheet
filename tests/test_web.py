"""Smoke tests for the local FastAPI surface; no real video or API key needed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shotsheet.web.app import app

client = TestClient(app)


def test_index_is_the_chinese_single_page() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "拉片单" in response.text
    assert "dragenter" in response.text
    assert "cdn" not in response.text.lower()


def test_config_lists_both_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    response = client.get("/config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["default_provider"] == "doubao"
    providers = {item["id"]: item for item in payload["providers"]}
    assert set(providers) == {"doubao", "qwen"}
    assert providers["doubao"]["api_key_env"] == "ARK_API_KEY"
    assert providers["qwen"]["model"] == "qwen3-vl-plus"
    assert all(item["available"] is False for item in providers.values())


def test_analyze_needs_a_file_or_path() -> None:
    response = client.post("/analyze", data={"vision": "false"})

    assert response.status_code == 400
    assert "上传" in response.json()["detail"]


def test_analyze_vision_without_key_is_explicit(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"not a real video")

    response = client.post("/analyze", data={"vision": "true", "path": str(video)})

    assert response.status_code == 400
    assert "ARK_API_KEY" in response.json()["detail"]
