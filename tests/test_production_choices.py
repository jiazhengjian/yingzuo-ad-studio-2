"""Approved physical identity and cast survive planning and paid request boundaries."""
import asyncio
from copy import deepcopy
import hashlib
import json
import shutil
from pathlib import Path
import pytest

from app.production import (ProductionPlan, required_designs, validate_production_plan,
                            validate_production_sources, validate_design_assets)
from app.providers.gateway import Gateway, SubmissionError
from test_providers import settings, source, plan


def production(mode="voiceover"):
    return {"version": "1.0.0", "product_constraints": [{"text": "仅为切块，不能变成整只蛋糕", "evidence_ids": ["form"]}],
            "characters": [{"id": "char_a", "name": "同事", "description": "成年短发女性，白衬衫、黑裤"}],
            "speech_mode": mode, "speaker_id": "char_a" if mode == "character_dialogue" else "",
            "voice_description": "普通话，轻松自然的青年女性声音" if mode != "none" else ""}


def prepared_plan(mode="voiceover"):
    prod = production(mode)
    return {"production": prod, "strategy": {"brief": {"facts": [{"id": "form", "text": "一块蛋糕"}]},
            "directions": [{"id": "d", "production": deepcopy(prod)}], "selected_direction_id": "d"},
            "script": "", "segments": [{"shots": [{"id": "s1", "character_ids": ["char_a"]},
                {"id": "s2", "character_ids": ["char_a"]}], "speech_spans": []}]}


@pytest.mark.parametrize("field,value", [("speaker_id", "char_stranger"), ("voice_description", "")])
def test_dialogue_requires_existing_speaker_and_actual_direction(field, value):
    prod = production("character_dialogue"); prod[field] = value
    with pytest.raises(ValueError): ProductionPlan.model_validate(prod)


def test_unknown_product_source_cannot_become_physical_constraint():
    with pytest.raises(ValueError, match="已有事实"):
        validate_production_sources(production(), [{"id": "unrelated"}])


@pytest.mark.parametrize("source", ["script", "narration", "shot", "ad_script", "span"])
def test_silent_plan_cannot_retain_hidden_narration(source):
    candidate = prepared_plan("none")
    if source == "script": candidate["script"] = "旧台词"
    elif source == "narration": candidate["segments"][0]["narration"] = "旧台词"
    elif source == "shot": candidate["segments"][0]["shots"][0]["narration"] = "旧台词"
    elif source == "ad_script": candidate["ad_script"] = {"beats": [{"narration": "旧台词"}]}
    else: candidate["segments"][0]["speech_spans"] = [{"text": "旧台词"}]
    with pytest.raises(ValueError, match="无人声"): validate_production_plan(candidate)


def test_selected_cast_and_voice_choices_are_not_directors_to_replace():
    candidate = prepared_plan()
    candidate["production"]["voice_description"] = "另一个人"
    with pytest.raises(ValueError, match="已选方向"): validate_production_plan(candidate)


def test_new_plan_cannot_omit_approved_production_and_downgrade_to_legacy():
    candidate = prepared_plan("character_dialogue")
    candidate.pop("production")
    with pytest.raises(ValueError, match="不能省略已选方向"):
        validate_production_plan(candidate)
    # A genuinely historical plan with no such contract stays compatible.
    candidate["strategy"]["directions"][0].pop("production")
    validate_production_plan(candidate)


def test_dialogue_stays_with_approved_visible_speaker_through_window():
    candidate = prepared_plan("character_dialogue")
    candidate["segments"][0]["speech_spans"] = [{"speaker_id": "char_a", "shot_ids": ["s1", "s2"]}]
    validate_production_plan(candidate)
    candidate["segments"][0]["shots"][1]["character_ids"] = []
    with pytest.raises(ValueError, match="说话窗口"): validate_production_plan(candidate)


def design_assets(source):
    scene = {**source, "id": "scene", "kind": "scene", "metadata": {
        "generated": True, "provider_url": "https://cdn.example/scene.png"}}
    character = {**source, "id": "character", "kind": "character", "metadata": {
        "generated": True, "provider_url": "https://cdn.example/char.png",
        "character_id": "char_a", "character_name": "同事", "view_layout": "four_view"}}
    return [source, scene, character]


@pytest.mark.parametrize("problem", ["missing", "wrong", "duplicate", "not_four_view"])
def test_confirmation_needs_exact_cast_not_just_any_scene(source, problem):
    candidate, assets = prepared_plan(), design_assets(source)
    if problem == "missing": assets.pop()
    elif problem == "wrong": assets[-1]["metadata"]["character_id"] = "char_other"
    elif problem == "duplicate": assets.append(deepcopy(assets[-1]))
    else: assets[-1]["metadata"]["view_layout"] = "portrait"
    with pytest.raises(ValueError): validate_design_assets(candidate, assets)


def request_segment(plan, source):
    return {**deepcopy(plan["segments"][0]), "audio_strategy": "native", "production": production(),
            "reference_asset_ids": [source["id"], "scene", "character"],
            "reference_roles": [{"index": 1, "role": "product"}, {"index": 2, "role": "scene"},
                                {"index": 3, "role": "character", "character_id": "char_a", "name": "同事"}]}


def frozen(gateway, segment):
    return {"model": "test", "prompt": "冻结请求", "duration": 10, "aspect_ratio": "9:16", "resolution": "720p",
            "skill_trace": [], "prompt_sha256": hashlib.sha256("冻结请求".encode()).hexdigest(),
            "input_sha256": gateway._video_input_hash(segment)}


def test_actual_video_request_sends_approved_character_in_frozen_order(settings, source, plan, monkeypatch):
    gateway, received = Gateway(settings), []
    segment, assets = request_segment(plan, source), design_assets(source)
    async def original(asset): return "https://cdn.example/product.png"
    async def reference(asset): return asset["metadata"]["provider_url"]
    async def public(url): return "1.1.1.1"
    async def submit(path, payload): received.append(payload); return "task"
    monkeypatch.setattr(gateway, "_original_reference_url", original)
    monkeypatch.setattr(gateway, "_scene_reference_url", reference)
    monkeypatch.setattr("app.providers.gateway.public_url", public)
    monkeypatch.setattr(gateway, "_submit", submit)
    asyncio.run(gateway.submit_video(segment, list(reversed(assets)), "test", prepared_request=frozen(gateway, segment)))
    assert received[0]["images"] == ["https://cdn.example/product.png", "https://cdn.example/scene.png", "https://cdn.example/char.png"]


def test_multiple_product_angles_keep_scene_and_cast_indices(settings, source, plan, monkeypatch):
    gateway, received = Gateway(settings), []
    segment, assets = request_segment(plan, source), design_assets(source)
    second = {**source, "id": "other_angle"}
    assets.insert(1, second)
    segment["reference_asset_ids"].insert(1, second["id"])
    segment["reference_roles"] = [{"index": 1, "role": "product"}, {"index": 2, "role": "product"},
        {"index": 3, "role": "scene"}, {"index": 4, "role": "character", "character_id": "char_a"}]
    async def original(asset): return "https://cdn.example/"+asset["id"]+".png"
    async def reference(asset): return asset["metadata"]["provider_url"]
    async def public(url): return "1.1.1.1"
    async def submit(path, payload): received.append(payload); return "task"
    monkeypatch.setattr(gateway, "_original_reference_url", original)
    monkeypatch.setattr(gateway, "_scene_reference_url", reference)
    monkeypatch.setattr("app.providers.gateway.public_url", public)
    monkeypatch.setattr(gateway, "_submit", submit)
    asyncio.run(gateway.submit_video(segment, list(reversed(assets)), "test", prepared_request=frozen(gateway, segment)))
    assert received[0]["images"] == ["https://cdn.example/"+source["id"]+".png",
        "https://cdn.example/other_angle.png", "https://cdn.example/scene.png", "https://cdn.example/char.png"]


@pytest.mark.parametrize("problem", ["missing", "wrong_order", "wrong_identity"])
def test_no_paid_request_with_missing_or_misbound_character(settings, source, plan, monkeypatch, problem):
    gateway = Gateway(settings)
    segment, assets = request_segment(plan, source), design_assets(source)
    if problem == "missing": assets.pop()
    elif problem == "wrong_identity": assets[-1]["metadata"]["character_id"] = "char_other"
    else: segment["reference_roles"][2]["index"] = 1
    async def forbidden(*args, **kwargs): pytest.fail("Cannot contact provider before all reference checks pass")
    monkeypatch.setattr(gateway, "_original_reference_url", forbidden)
    monkeypatch.setattr(gateway, "_submit", forbidden)
    with pytest.raises(SubmissionError):
        asyncio.run(gateway.submit_video(segment, assets, "test", prepared_request=frozen(gateway, segment)))


def test_one_contact_sheet_per_character_and_resume_never_redraws(settings, source, plan, tmp_path, monkeypatch):
    gateway, requests, traces = Gateway(settings), [], []
    plan = {**deepcopy(plan), "production": production()}
    plan["production"]["characters"].append({"id": "char_b", "name": "另一位同事", "description": "成年短发男性，灰色针织衫"})
    async def original(asset): return "https://cdn.example/product.png"
    async def submit(route, payload): requests.append(payload); return "task_"+str(len(requests))
    async def task(identifier): return {"status": "completed", "result": {"images": [{"url": "https://cdn.example/"+identifier+".png"}]}}
    async def download(url, path): shutil.copy2(source["path"], path)
    monkeypatch.setattr(gateway, "_original_reference_url", original)
    monkeypatch.setattr(gateway, "_submit", submit)
    monkeypatch.setattr(gateway, "_task", task)
    monkeypatch.setattr(gateway, "download_video", download)
    folder = tmp_path / "designs"
    results = asyncio.run(gateway.create_designs({}, [source], plan, folder, on_skill=traces.append))
    assert [item["kind"] for item in results] == ["scene", "character", "character"]
    assert [item["metadata"].get("character_id") for item in results[1:]] == ["char_a", "char_b"]
    assert all(item["metadata"]["view_layout"] == "four_view" for item in results[1:])
    assert all(item["metadata"]["sheet_layout"] == "horizontal_16x9" for item in results[1:])
    assert all(item["metadata"]["template_version"] == "chinese-horizontal-v1" for item in results[1:])
    assert len(requests) == 3 and requests[0]["images"]
    assert all("images" not in request and request["size"] == "16:9" for request in requests[1:])
    assert {trace["compiled_resource"] for trace in traces} == {"references/scene-template.txt", "references/character-template.txt"}
    repeated = asyncio.run(gateway.create_designs({}, [source], plan, folder))
    assert len(requests) == 3 and repeated == results


def test_replace_keys_redraws_only_the_finished_old_character_sheet(settings, source, plan, tmp_path, monkeypatch):
    from PIL import Image
    gateway, requests, uploads = Gateway(settings), [], []
    plan = {**deepcopy(plan), "production": production()}
    folder = tmp_path / "designs"
    folder.mkdir()
    scene_png = folder / "scene.png"
    old_sheet = folder / "character_char_a.png"
    Image.new("RGB", (8, 8), "white").save(scene_png)
    Image.new("RGB", (8, 8), "blue").save(old_sheet)
    scene_bytes, old_bytes = scene_png.read_bytes(), old_sheet.read_bytes()
    (folder / "scene_task.json").write_text(json.dumps({
        "state": "completed", "task_id": "scene-task", "url": "https://cdn.example/scene.png",
        "kind": "scene", "model": "old", "design_request": {"version": "scene-context-v3"}}), encoding="utf-8")
    (folder / "character_char_a_task.json").write_text(json.dumps({
        "state": "completed", "task_id": "old-sheet", "url": "https://cdn.example/old-sheet.png",
        "kind": "character", "character_id": "char_a", "model": "old",
        "design_request": {"version": "character-context-v1"}}), encoding="utf-8")

    async def original(asset):
        uploads.append(asset["id"])
        return "https://cdn.example/product.png"

    async def submit(route, payload):
        requests.append(payload)
        return "task_" + str(len(requests))

    async def task(identifier):
        return {"status": "completed", "result": {"images": [{"url": "https://cdn.example/" + identifier + ".png"}]}}

    async def download(url, path):
        Image.new("RGB", (8, 8), "red").save(path, "PNG")

    monkeypatch.setattr(gateway, "_original_reference_url", original)
    monkeypatch.setattr(gateway, "_submit", submit)
    monkeypatch.setattr(gateway, "_task", task)
    monkeypatch.setattr(gateway, "download_video", download)
    resumed = asyncio.run(gateway.create_designs({}, [source], plan, folder))
    assert requests == [] and uploads
    assert [item["kind"] for item in resumed] == ["scene", "character"]
    assert Path(resumed[1]["source_path"]) == old_sheet.resolve()
    uploads.clear()
    replaced = asyncio.run(gateway.create_designs({}, [source], plan, folder, replace_keys=["character_char_a"]))
    assert uploads == []
    assert len(requests) == 1 and requests[0]["size"] == "16:9" and "images" not in requests[0]
    assert "默认出镜为中国人" in requests[0]["prompt"]
    assert [item["kind"] for item in replaced] == ["character"]
    assert replaced[0]["metadata"]["sheet_layout"] == "horizontal_16x9"
    assert replaced[0]["metadata"]["template_version"] == "chinese-horizontal-v1"
    assert Path(replaced[0]["source_path"]).name == "character_char_a.chinese-horizontal-v1.png"
    assert scene_png.read_bytes() == scene_bytes and old_sheet.read_bytes() == old_bytes
    assert (folder / "scene_task.json").exists()
    assert json.loads((folder / "character_char_a_task.json.replaced").read_text())["task_id"] == "old-sheet"


def test_state_sheet_request_uses_the_character_sheet_and_not_the_scene(settings, source, tmp_path, monkeypatch):
    import base64
    from PIL import Image

    gateway, requests = Gateway(settings), []
    cast = production()
    cast["characters"][0]["description"] = "灰色连帽卫衣，黑长发"
    plan = {
        "facts": {"name": "吹风机"},
        "visual_direction": {"scene": "合住卧室", "lighting": "清晨窗边"},
        "production": cast,
        "strategy": {"brief": {"product_name": "吹风机", "facts": [{"id": "form", "text": "高速吹干"}]},
                     "directions": [{"id": "d", "production": deepcopy(cast)}], "selected_direction_id": "d"},
        "segments": [{"shots": [
            {"id": "s3", "character_ids": ["char_a"],
             "description": "合住卧室里，灰卫衣女生站在床边吹湿发。",
             "continuity": {"start_state": "头发是湿的", "end_state": "被吹开一半", "allowed_changes": "发丝由湿到被吹开一半"}},
            {"id": "s6", "character_ids": ["char_a"],
             "description": "同一间卧室，她关掉吹风机，头发已经顺了。",
             "continuity": {"start_state": "头发半干", "end_state": "头发顺开", "allowed_changes": "发丝由半干到顺开"}},
        ]}],
    }

    async def original(asset):
        return "https://cdn.example/product.png"

    async def submit(route, payload):
        requests.append(payload)
        return "task_" + str(len(requests))

    async def task(identifier):
        return {"status": "completed", "result": {"images": [{"url": "https://cdn.example/" + identifier + ".png"}]}}

    async def download(url, path):
        Image.new("RGB", (8, 8), "red").save(path, "PNG")

    def tagged(asset):
        kind = asset.get("kind") or "product"
        cid = (asset.get("metadata") or {}).get("character_id") or ""
        raw = f"{kind}:{cid}:{Path(asset['path']).name}".encode()
        return "data:image/png;base64," + base64.b64encode(raw).decode()

    monkeypatch.setattr(gateway, "_original_reference_url", original)
    monkeypatch.setattr(gateway, "_submit", submit)
    monkeypatch.setattr(gateway, "_task", task)
    monkeypatch.setattr(gateway, "download_video", download)
    monkeypatch.setattr("app.providers.gateway.data_url", tagged)
    saved = asyncio.run(gateway.create_designs({}, [source], plan, tmp_path / "designs"))
    assert [item["kind"] for item in saved] == ["scene", "start_frame", "start_frame"]
    state_requests = [item for item in requests if "状态四视图" in item["prompt"]]
    assert len(state_requests) == 2
    assert "images" not in state_requests[0]
    result_labels = [base64.b64decode(url.split(",", 1)[1]).decode() for url in state_requests[1]["images"]]
    assert result_labels and all(label.startswith("start_frame:char_a") for label in result_labels)
    for item in state_requests:
        assert item["size"] == "16:9"
        assert "左侧约40%" in item["prompt"]
        assert "白或浅灰无缝背景" in item["prompt"]
        assert "灰色连帽卫衣" in item["prompt"]
        assert "合住卧室" not in item["prompt"]
        assert "床边" not in item["prompt"]
        assert "场景图" not in item["prompt"] or "不要场景图" in item["prompt"]
    assert "湿" in state_requests[0]["prompt"]
    assert "吹风机" not in state_requests[0]["prompt"]
    assert "起始状态四视图" in state_requests[1]["prompt"]
    assert "灰色连帽卫衣，黑长发" in state_requests[1]["prompt"]
    for item in state_requests:
        assert "双手空手，不要商品，不要道具，背景只用白或浅灰。" in item["prompt"]
    sheets = [item for item in saved if item["metadata"].get("state_reference")]
    assert len(sheets) == 2
    assert all(item["metadata"]["sheet_layout"] == "horizontal_16x9" for item in sheets)
    assert all(item["metadata"]["template_version"] == "chinese-horizontal-v1" for item in sheets)
    assert all(item["metadata"]["character_id"] == "char_a" for item in sheets)
    assert requests[0]["images"] and all(not base64.b64decode(url.split(",", 1)[1]).decode().startswith("scene:") for url in requests[0]["images"])


def test_state_sheet_inputs_skip_the_scene_image(tmp_path):
    from PIL import Image
    from app.providers.gateway import _state_still_inputs

    scene = tmp_path / "scene.png"
    sheet = tmp_path / "character_girl.chinese-horizontal-v1.png"
    product = tmp_path / "product.png"
    Image.new("RGB", (8, 8), "blue").save(scene)
    Image.new("RGB", (8, 8), "white").save(sheet)
    Image.new("RGB", (8, 8), "red").save(product)
    sources = [{"id": "product-1", "kind": "product", "path": str(product), "mime": "image/png"}]
    assert _state_still_inputs(
        sources, [], [], {"reference_kinds": [], "character_id": "girl"}, tmp_path) == []
    start = {"kind": "start_frame", "path": str(sheet), "mime": "image/png",
             "metadata": {"phase": "start", "character_id": "girl", "generated": True}}
    result = _state_still_inputs(
        sources, [start], [], {"reference_kinds": ["state_start"], "character_id": "girl"}, tmp_path)
    assert [Path(item["path"]).name for item in result] == ["character_girl.chinese-horizontal-v1.png"]
    assert str(scene) not in [item["path"] for item in result]
    assert str(product) not in [item["path"] for item in result]
    goods = _state_still_inputs(sources, [], [], {"reference_kinds": ["product"]}, tmp_path)
    assert [item["id"] for item in goods] == ["product-1"]
    with pytest.raises(ValueError, match="起始状态"):
        _state_still_inputs(sources, [], [], {"reference_kinds": ["state_start"], "character_id": "girl"}, tmp_path)
