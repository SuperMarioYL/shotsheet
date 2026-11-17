"""Smoke tests for the local FastAPI surface; no real video or API key needed."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from shotsheet.render.shotlist_html import render_shot_table
from shotsheet.schema import Platform, Shot, ShotSheet, Source
from shotsheet.web.app import SHEET_STORE, app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _scratch_sheet_store() -> None:
    """Keep each test on the scratch file instead of a path another test opened."""
    SHEET_STORE.reset()
    yield
    SHEET_STORE.reset()


def _sheet() -> ShotSheet:
    return ShotSheet(
        source=Source(file="demo.mp4", duration_ms=5000, fps=30.0, platform=Platform.DOUYIN),
        shots=[
            Shot(index=0, tc_in="00:00:00:00", tc_out="00:00:02:00", duration_ms=2000),
            Shot(index=1, tc_in="00:00:02:00", tc_out="00:00:05:00", duration_ms=3000),
        ],
    )


def test_index_is_the_chinese_single_page() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "拉片单" in response.text
    assert "dragenter" in response.text
    assert "cdn" not in response.text.lower()
    assert 'id="apiKey"' in response.text
    assert 'id="provider"' in response.text
    assert 'id="sheetPath"' in response.text
    assert 'id="openSheet"' in response.text
    assert "contenteditable" in response.text


def test_index_exposes_the_caption_box_control() -> None:
    response = client.get("/")

    assert 'id="cbTop" type="number"' in response.text
    assert 'id="cbBottom" type="number"' in response.text
    assert 'id="cbLeft" type="number"' in response.text
    assert 'id="cbRight" type="number"' in response.text
    assert 'value="72"' in response.text
    assert 'value="80"' in response.text
    assert 'value="6"' in response.text
    assert 'value="88"' in response.text


def test_analyze_rejects_a_bad_caption_box_before_the_video(tmp_path) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"not a real video")

    response = client.post(
        "/analyze",
        data={"vision": "false", "path": str(video), "caption_box": "72,60,6,88"},
    )

    assert response.status_code == 400
    assert "top < bottom" in response.json()["detail"]


def test_editable_table_exposes_inline_cells() -> None:
    html = render_shot_table(_sheet(), editable=True)

    assert 'contenteditable="true"' in html
    assert 'data-field="shot_size"' in html
    assert 'data-field="camera"' in html
    assert 'data-field="movement"' in html
    assert 'data-field="props"' in html
    assert 'data-field="scene"' in html
    assert "<select" in html


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


def test_put_sheet_writes_an_edited_vision_field_to_disk() -> None:
    sheet = _sheet()
    SHEET_STORE.save(sheet)
    payload = sheet.model_dump(mode="json")
    payload["shots"][0]["shot_size"] = "特写"

    response = client.put("/sheet", json=payload)

    assert response.status_code == 200, response.text
    assert response.json()["sheet"]["shots"][0]["shot_size"] == "特写"
    on_disk = json.loads(SHEET_STORE.path.read_text(encoding="utf-8"))
    assert on_disk["shots"][0]["shot_size"] == "特写"
    assert 'data-field="shot_size"' in response.json()["table_html"]


def test_put_sheet_rejects_an_invalid_enum_without_overwriting() -> None:
    sheet = _sheet()
    SHEET_STORE.save(sheet)
    payload = sheet.model_dump(mode="json")
    payload["shots"][0]["shot_size"] = "半身"

    response = client.put("/sheet", json=payload)

    assert response.status_code == 422
    on_disk = json.loads(SHEET_STORE.path.read_text(encoding="utf-8"))
    assert on_disk["shots"][0]["shot_size"] is None


def test_open_sheet_points_edits_at_the_users_file(tmp_path) -> None:
    path = tmp_path / "shots.json"
    path.write_text(_sheet().model_dump_json(indent=2), encoding="utf-8")

    opened = client.post("/sheet/open", json={"path": str(path)})

    assert opened.status_code == 200, opened.text
    assert opened.json()["sheet_path"] == str(path.resolve())
    payload = opened.json()["sheet"]
    payload["shots"][0]["shot_size"] = "近景"

    saved = client.put("/sheet", json=payload)

    assert saved.status_code == 200, saved.text
    assert saved.json()["sheet_path"] == str(path.resolve())
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["shots"][0]["shot_size"] == "近景"


def test_open_sheet_reports_a_missing_file(tmp_path) -> None:
    response = client.post("/sheet/open", json={"path": str(tmp_path / "missing.json")})

    assert response.status_code == 404
    assert "找不到" in response.json()["detail"]


def test_open_sheet_rejects_json_that_is_not_a_sheet(tmp_path) -> None:
    path = tmp_path / "other.json"
    path.write_text('{"hello": "world"}', encoding="utf-8")

    response = client.post("/sheet/open", json={"path": str(path)})

    assert response.status_code == 400
    assert "分镜表" in response.json()["detail"]


def test_open_sheet_needs_a_path() -> None:
    response = client.post("/sheet/open", json={"path": "  "})

    assert response.status_code == 400
    assert "路径" in response.json()["detail"]
