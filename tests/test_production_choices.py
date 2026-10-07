"""Approved physical identity and cast survive planning and paid request boundaries."""
import asyncio
from copy import deepcopy
import hashlib
import json
import shutil
from pathlib import Path
import pytest

from app.production import (ProductionPlan, align_story_voice, required_designs, validate_production_plan,
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


def test_story_voiceover_becomes_the_leads_own_voice():
    voiced = production("voiceover")
    mapped, changed = align_story_voice("scene_story", voiced)
    assert changed is True
    assert mapped["speech_mode"] == "character_dialogue"
    assert mapped["speaker_id"] == "char_a"
    assert mapped["voice_description"] == "普通话，轻松自然的青年女性声音"
    again, twice = align_story_voice("scene_story", mapped)
    assert twice is False
    assert again["speech_mode"] == "character_dialogue"
    brand = production("voiceover")
    _kept, brand_changed = align_story_voice("brand_tvc", brand)
    assert brand_changed is False and brand["speech_mode"] == "voiceover"
    silent = production("none")
    _same, silent_changed = align_story_voice("scene_story", silent)
    assert silent_changed is False
    hands = production("voiceover")
    hands["characters"] = [{"id": "char_hand", "name": "手", "description": "只露出拿叉的手", "appearance": "partial_only"}]
    _unmapped, hands_changed = align_story_voice("scene_story", hands)
    assert hands_changed is False and hands["speech_mode"] == "voiceover"
    faced = production("voiceover")
    faced["characters"] = [
        {"id": "char_hand", "name": "手", "description": "只露出拿叉的手", "appearance": "partial_only"},
        {"id": "char_lead", "name": "她", "description": "成年女生，扎发", "appearance": "face_visible"},
    ]
    lead, did = align_story_voice("scene_story", faced, "scene_action")
    assert did is True and lead["speaker_id"] == "char_lead"


def test_a_story_offer_does_not_keep_an_unplannable_voiceover():
    from app.strategy import prepare_strategy_ingress
    payload = {"kind": "directions", "brief": {"ad_form": "scene_story"}, "directions": [{
        "film_concept": {"structure": "scene_action"},
        "production": production("voiceover"),
    }]}
    prepared = prepare_strategy_ingress(payload)
    voice = prepared["directions"][0]["production"]
    assert voice["speech_mode"] == "character_dialogue"
    assert voice["speaker_id"] == "char_a"
    assert payload["directions"][0]["production"]["speech_mode"] == "voiceover"


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
    scene_requests = [item for item in requests if item.get("images")]
    character_requests = [item for item in requests if "images" not in item]
    assert len(scene_requests) == 1 and len(character_requests) == 2
    assert all(request["size"] == "16:9" for request in character_requests)
    assert results[1]["metadata"]["image_prompt"] in {item["prompt"] for item in character_requests}
    assert results[1]["metadata"]["image_inputs"] == []
    assert results[2]["metadata"]["image_inputs"] == []
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
        "state_changes": [{
            "subject_label": "头发", "owner": "char_a", "start": "湿", "result": "干顺",
            "start_shot": "s3", "result_shot": "s6",
        }],
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
    appearance = (
        "外观原文照抄，脸、五官、肤色、身材、头发长度和发色、衣服、鞋子都按这句："
        "默认出镜为中国人，East Asian Chinese appearance，日常大陆长相。灰色连帽卫衣，黑长发"
    )
    assert appearance in state_requests[0]["prompt"]
    assert appearance in state_requests[1]["prompt"]
    assert "image" not in state_requests[0]
    assert "湿" in state_requests[0]["prompt"]
    assert "吹风机" not in state_requests[0]["prompt"]
    assert "改图" in state_requests[1]["prompt"] and "不是重画一个人" in state_requests[1]["prompt"]
    assert "起始状态四视图" in state_requests[1]["prompt"]
    assert state_requests[1]["images"] == [state_requests[1]["image"]]
    assert len(state_requests[1]["images"]) == 1
    assert base64.b64decode(state_requests[1]["image"].split(",", 1)[1]).decode().startswith("start_frame:char_a")
    for item in state_requests:
        assert "双手空手，不要商品，不要道具，背景只用白或浅灰。" in item["prompt"]
    sheets = [item for item in saved if item["metadata"].get("state_reference")]
    assert len(sheets) == 2
    assert all(item["metadata"]["sheet_layout"] == "horizontal_16x9" for item in sheets)
    assert all(item["metadata"]["template_version"] == "chinese-horizontal-v1" for item in sheets)
    assert all(item["metadata"]["character_id"] == "char_a" for item in sheets)
    scene_request = next(item for item in requests if item.get("images") and "改图" not in item["prompt"])
    assert scene_request["images"] and all(not base64.b64decode(url.split(",", 1)[1]).decode().startswith("scene:") for url in scene_request["images"])
    scene, start, result_sheet = saved
    assert scene["metadata"]["image_prompt"] == scene_request["prompt"]
    assert scene["metadata"]["model"] == settings.image_model
    assert scene["metadata"]["image_inputs"] == [{"role": "product", "label": "商品原图", "asset_id": source["id"]}]
    assert start["metadata"]["image_prompt"] == state_requests[0]["prompt"]
    assert start["metadata"]["image_inputs"] == []
    assert result_sheet["metadata"]["image_prompt"] == state_requests[1]["prompt"]
    assert result_sheet["metadata"]["image_inputs"] == [{"role": "state_start", "label": "起始状态参考"}]
    receipts = [json.loads(path.read_text()) for path in (tmp_path / "designs").glob("*_task.json")]
    result_receipt = next(item for item in receipts if "改图" in (item.get("image_prompt") or ""))
    assert result_receipt["image_prompt"] == state_requests[1]["prompt"]
    assert result_receipt["image_inputs"] == [{"role": "state_start", "label": "起始状态参考"}]
    again = asyncio.run(gateway.create_designs({}, [source], plan, tmp_path / "designs"))
    assert len(requests) == 3
    assert again[2]["metadata"]["image_prompt"] == state_requests[1]["prompt"]
    assert again[2]["metadata"]["image_inputs"] == [{"role": "state_start", "label": "起始状态参考"}]


def test_state_sheet_inputs_skip_the_scene_image(tmp_path):
    from PIL import Image
    from app.providers.gateway import _image_input_records, _state_still_inputs

    scene = tmp_path / "scene.png"
    sheet = tmp_path / "character_girl.chinese-horizontal-v1.png"
    product = tmp_path / "product.png"
    Image.new("RGB", (8, 8), "blue").save(scene)
    Image.new("RGB", (8, 8), "white").save(sheet)
    Image.new("RGB", (8, 8), "red").save(product)
    sources = [{"id": "product-1", "kind": "product", "path": str(product), "mime": "image/png"}]
    assert _state_still_inputs(
        sources, [], [], {"reference_kinds": [], "character_id": "girl"}, tmp_path) == []
    other = tmp_path / "other_start.png"
    Image.new("RGB", (8, 8), "green").save(other)
    start = {"id": "start-hair", "kind": "start_frame", "path": str(sheet), "mime": "image/png",
             "metadata": {"phase": "start", "character_id": "girl", "state_change_id": "hair", "generated": True}}
    later = {"id": "start-other", "kind": "start_frame", "path": str(other), "mime": "image/png",
             "metadata": {"phase": "start", "character_id": "girl", "state_change_id": "other", "generated": True}}
    result = _state_still_inputs(
        sources, [later, start], [],
        {"reference_kinds": ["state_start"], "character_id": "girl", "state_change_id": "hair"}, tmp_path)
    assert [Path(item["path"]).name for item in result] == ["character_girl.chinese-horizontal-v1.png"]
    assert result[0]["id"] == "start-hair"
    assert str(scene) not in [item["path"] for item in result]
    assert str(product) not in [item["path"] for item in result]
    goods = _state_still_inputs(sources, [], [], {"reference_kinds": ["product"]}, tmp_path)
    assert [item["id"] for item in goods] == ["product-1"]
    assert _image_input_records("start_frame", {"reference_kinds": ["product"]}, goods) == [
        {"role": "product", "label": "商品原图", "asset_id": "product-1"}]
    assert _image_input_records("character", {}, sources) == []
    assert _image_input_records(
        "start_frame", {"reference_kinds": ["state_start"], "identity_edit": True}, [start, sources[0]]) == [
        {"role": "state_start", "label": "起始状态参考", "asset_id": "start-hair"}]
    with pytest.raises(ValueError, match="起始状态"):
        _state_still_inputs(sources, [later], [],
                            {"reference_kinds": ["state_start"], "character_id": "girl", "state_change_id": "hair"}, tmp_path)


def _two_change_plan():
    cast = production()
    cast["characters"] = [
        {"id": "girl", "name": "女生", "description": "灰色卫衣，发梢半湿"},
        {"id": "room", "name": "室友", "description": "碎花睡衣"},
    ]
    cast["speaker_id"] = "girl"
    return {
        "facts": {"name": "吹风机"},
        "visual_direction": {"scene": "合住卧室", "lighting": "清晨窗边"},
        "production": cast,
        "segments": [{"duration_seconds": 15, "shots": [
            {"id": "s2", "character_ids": ["girl"], "description": "杯子还是满的。"},
            {"id": "s4", "character_ids": ["girl"], "description": "杯子空了。"},
            {"id": "s5", "character_ids": ["girl"], "description": "发梢湿成缕。"},
            {"id": "s8", "character_ids": ["girl"], "description": "头发干顺。"},
        ]}],
        "state_changes": [
            {"subject_label": "杯子", "owner": "product", "start": "满", "result": "空",
             "start_shot": "s2", "result_shot": "s4"},
            {"subject_label": "头发", "owner": "girl", "start": "湿，发梢成缕", "result": "干顺，蓬松披肩",
             "start_shot": "s5", "result_shot": "s8"},
        ],
    }


def test_result_state_uses_the_matching_start_and_images_share_a_wave(settings, source, tmp_path, monkeypatch):
    import base64
    from PIL import Image

    gateway, requests = Gateway(settings), []
    settings.image_concurrency = 3
    plan = _two_change_plan()
    in_flight = {"n": 0, "max": 0}
    phases = []

    async def original(asset):
        return "https://cdn.example/product.png"

    async def submit(route, payload):
        in_flight["n"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["n"])
        kind = "result" if "改图" in payload["prompt"] else ("start" if "起始状态" in payload["prompt"] else "other")
        phases.append(("begin", kind))
        await asyncio.sleep(0.02)
        phases.append(("end", kind))
        in_flight["n"] -= 1
        requests.append(payload)
        return "task_" + str(len(requests))

    async def task(identifier):
        return {"status": "completed", "result": {"images": [{"url": "https://cdn.example/" + identifier + ".png"}]}}

    async def download(url, path):
        Image.new("RGB", (8, 8), "red").save(path, "PNG")

    def tagged(asset):
        meta = asset.get("metadata") or {}
        raw = f"{meta.get('state_change_id')}:{meta.get('phase')}:{asset.get('id') or ''}".encode()
        return "data:image/png;base64," + base64.b64encode(raw).decode()

    def remember(result):
        meta = result.get("metadata") or {}
        result["id"] = f"asset-{meta.get('state_change_id')}-{meta.get('phase')}"

    monkeypatch.setattr(gateway, "_original_reference_url", original)
    monkeypatch.setattr(gateway, "_submit", submit)
    monkeypatch.setattr(gateway, "_task", task)
    monkeypatch.setattr(gateway, "download_video", download)
    monkeypatch.setattr("app.providers.gateway.data_url", tagged)
    saved = asyncio.run(gateway.create_designs({}, [source], plan, tmp_path / "designs", on_asset=remember))
    assert in_flight["max"] >= 2
    result_begins = [index for index, item in enumerate(phases) if item == ("begin", "result")]
    start_ends = [index for index, item in enumerate(phases) if item == ("end", "start")]
    assert result_begins and start_ends and min(result_begins) > max(start_ends)
    hair = next(item for item in saved if item["metadata"].get("state_label") == "头发干顺·结果")
    assert hair["metadata"]["image_inputs"] == [{
        "role": "state_start", "label": "起始状态参考",
        "asset_id": "asset-" + hair["metadata"]["state_change_id"] + "-start",
    }]
    assert hair["metadata"]["source_asset_ids"] == ["asset-" + hair["metadata"]["state_change_id"] + "-start"]
    hair_request = next(item for item in requests if "干顺，蓬松披肩" in item["prompt"])
    decoded = base64.b64decode(hair_request["image"].split(",", 1)[1]).decode()
    assert decoded.startswith(hair["metadata"]["state_change_id"] + ":start:")
    assert "半湿" not in hair_request["prompt"]
    kinds = [item["kind"] for item in saved]
    assert kinds[0] == "scene" and kinds.count("start_frame") == 4
    assert "character" in kinds


def test_image_rate_limit_finishes_the_rest_one_at_a_time(settings, source, tmp_path, monkeypatch):
    from PIL import Image

    gateway, calls = Gateway(settings), {"n": 0, "in_flight": 0, "serial_max": 0, "parallel_max": 0, "limited": False}
    settings.image_concurrency = 3
    plan = _two_change_plan()

    async def original(asset):
        return "https://cdn.example/product.png"

    async def submit(route, payload):
        calls["n"] += 1
        calls["in_flight"] += 1
        if calls["limited"]:
            calls["serial_max"] = max(calls["serial_max"], calls["in_flight"])
        else:
            calls["parallel_max"] = max(calls["parallel_max"], calls["in_flight"])
        await asyncio.sleep(0.01)
        calls["in_flight"] -= 1
        if not calls["limited"] and calls["parallel_max"] >= 2:
            calls["limited"] = True
            raise SubmissionError("平台并发已满", status_code=429, excerpt="rate limit 并发")
        return "task_" + str(calls["n"])

    async def task(identifier):
        return {"status": "completed", "result": {"images": [{"url": "https://cdn.example/" + identifier + ".png"}]}}

    async def download(url, path):
        Image.new("RGB", (8, 8), "red").save(path, "PNG")

    monkeypatch.setattr(gateway, "_original_reference_url", original)
    monkeypatch.setattr(gateway, "_submit", submit)
    monkeypatch.setattr(gateway, "_task", task)
    monkeypatch.setattr(gateway, "download_video", download)
    saved = asyncio.run(gateway.create_designs({}, [source], plan, tmp_path / "limited"))
    assert calls["parallel_max"] >= 2
    assert calls["limited"] is True
    assert calls["serial_max"] <= 1
    assert [item["kind"] for item in saved].count("start_frame") == 4
    assert (tmp_path / "limited" / "scene_task.json").is_file()
