"""The clip stores the prompt that was sent. Older clips are left without one."""
import hashlib
import json

from app.pipeline import (
    render_prompt_metadata, video_generation_metadata, video_input_label, video_input_records,
)
from test_pipeline import FakeGateway, execute, fake_media, setup


def _asset(asset_id, kind, **metadata):
    asset = {"id": asset_id, "kind": kind, "name": metadata.pop("name", kind), "metadata": metadata}
    return asset


def test_reference_labels_name_the_picture_and_drop_urls():
    product = _asset("prod", "product")
    scene = _asset("scene", "scene", generated=True, provider_url="https://cdn.example/scene.png")
    person = _asset("char", "character", generated=True, character_id="char_a", character_name="小夏",
                    view_layout="four_view", provider_url="https://cdn.example/char.png")
    state = _asset("state", "start_frame", generated=True, state_reference=True, state_label="头发湿·起始",
                   provider_url="https://cdn.example/state.png")
    assert video_input_label(product) == ("product", "商品原图")
    assert video_input_label(scene) == ("scene", "场景参考")
    assert video_input_label(person) == ("character", "小夏·角色参考")
    assert video_input_label(state) == ("state", "头发湿·起始")
    segment = {
        "reference_asset_ids": ["prod", "scene", "char"],
        "reference_roles": [{"index": 1, "role": "product"}, {"index": 2, "role": "scene"},
                            {"index": 3, "role": "character", "character_id": "char_a"}],
        "production": {"version": "1.0.0", "characters": [{"id": "char_a", "name": "小夏"}],
                       "speech_mode": "voiceover", "speaker_id": "", "voice_description": "自然中文旁白",
                       "product_constraints": []},
    }
    records = video_input_records(segment, [state, person, scene, product])
    assert records == [
        {"role": "product", "label": "商品原图", "asset_id": "prod"},
        {"role": "scene", "label": "场景参考", "asset_id": "scene"},
        {"role": "character", "label": "小夏·角色参考", "asset_id": "char"},
    ]
    assert "https://" not in json.dumps(records, ensure_ascii=False)
    assert "provider_url" not in json.dumps(records)


def test_a_missing_request_is_not_invented_and_one_clip_copies_onto_the_film():
    assert video_generation_metadata({}, [], None) == {}
    assert video_generation_metadata({}, [], {"model": "wan3.0"}) == {}
    prompt = "机位停住，产品从第一帧就在。"
    saved = video_generation_metadata(
        {"reference_asset_ids": ["prod"]},
        [_asset("prod", "product")],
        {"model": " wan3.0 ", "prompt": prompt, "skill_trace": [{"version": "hidden"}]},
    )
    assert saved == {"video_prompt": prompt, "model": "wan3.0"}
    assert "skill_trace" not in saved
    clip = {"kind": "segment", "metadata": saved}
    assert render_prompt_metadata([clip, {"kind": "audio", "metadata": {"video_prompt": "不要用配音"}}]) == saved
    assert render_prompt_metadata([clip, {"kind": "segment", "metadata": {"video_prompt": "另一段"}}]) == {}
    assert render_prompt_metadata([{"kind": "segment", "metadata": {}}]) == {}


class RecordingGateway(FakeGateway):
    def prepare_video_request(self, segment):
        prompt = "SENT_" + segment["id"] + "：产品从第一帧就在。"
        encoded = json.dumps(segment, sort_keys=True, ensure_ascii=False).encode()
        return {"model": "wan3.0-test", "prompt": prompt, "duration": segment["duration_seconds"],
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "input_sha256": hashlib.sha256(encoded).hexdigest()}

    async def submit_video(self, segment, assets, idempotency_key, *, prepared_request=None):
        assert prepared_request["prompt"].startswith("SENT_" + segment["id"])
        return await FakeGateway.submit_video(self, segment, assets, idempotency_key)


def _project(store, duration, segments):
    project = store.create_project("测试商品，蓝色瓶身", "product", duration_seconds=duration)
    product_path = store.media_root / f"product-{duration}.png"
    scene_path = store.media_root / f"scene-{duration}.png"
    from test_pipeline import PNG
    product_path.write_bytes(PNG)
    scene_path.write_bytes(PNG)
    product = store.add_asset(project["id"], "product", "用户商品图", product_path, "image/png")
    scene = store.add_asset(project["id"], "scene", "已确认场景", scene_path, "image/png", metadata={
        "generated": True, "provider_url": "https://cdn.example/scene.png"})
    plan = {"title": "测试方案", "script": "展示商品。", "language": "zh-CN", "aspect_ratio": "9:16",
            "audio_strategy": "voiceover", "facts": {"name": "测试商品", "claims": []},
            "segments": [{"id": f"s{i}", "duration_seconds": 10, "purpose": "展示", "prompt": "方案里的短提示",
                          "shots": [{"start": 0, "end": 10, "description": f"商品第{i + 1}处"}],
                          "reference_asset_ids": [product["id"], scene["id"]]} for i in range(segments)]}
    return store.update_plan(project["id"], plan), plan


def test_download_saves_the_sent_prompt_and_a_single_clip_film_keeps_it(setup, fake_media):
    store, _, _, settings = setup
    settings.max_video_submissions = 2
    project, plan = _project(store, 10, 1)
    gateway = RecordingGateway(store, plan)
    result = execute(store, project, settings, gateway)
    assert result["status"] == "ready", result.get("error")
    step = store.get_step(result["id"], "video:s0")
    clip = store.get_asset(step["result"]["asset_id"])
    assert clip["metadata"]["video_prompt"] == "SENT_s0：产品从第一帧就在。"
    assert clip["metadata"]["model"] == "wan3.0-test"
    assert clip["metadata"]["video_inputs"] == [
        {"role": "product", "label": "商品原图", "asset_id": plan["segments"][0]["reference_asset_ids"][0]},
        {"role": "scene", "label": "场景参考", "asset_id": plan["segments"][0]["reference_asset_ids"][1]},
    ]
    assert "方案里的短提示" not in clip["metadata"]["video_prompt"]
    assert "https://" not in json.dumps(clip["metadata"]["video_inputs"])
    render = store.get_asset(store.get_render(store.get_project(project["id"])["current_render_id"])["asset_id"])
    assert render["metadata"]["video_prompt"] == clip["metadata"]["video_prompt"]
    assert render["metadata"]["video_inputs"] == clip["metadata"]["video_inputs"]


def test_a_composed_film_does_not_merge_several_prompts(setup, fake_media):
    store, _, _, settings = setup
    settings.max_video_submissions = 2
    project, plan = _project(store, 20, 2)
    result = execute(store, project, settings, RecordingGateway(store, plan))
    assert result["status"] == "ready", result.get("error")
    prompts = []
    for segment in plan["segments"]:
        clip = store.get_asset(store.get_step(result["id"], "video:" + segment["id"])["result"]["asset_id"])
        prompts.append(clip["metadata"]["video_prompt"])
    assert prompts == ["SENT_s0：产品从第一帧就在。", "SENT_s1：产品从第一帧就在。"]
    render = store.get_asset(store.get_render(store.get_project(project["id"])["current_render_id"])["asset_id"])
    assert "video_prompt" not in render["metadata"]
    assert "video_inputs" not in render["metadata"]


def test_a_gateway_without_a_stored_request_leaves_the_clip_blank(setup, fake_media):
    store, project, plan, settings = setup
    result = execute(store, project, settings, FakeGateway(store, plan))
    assert result["status"] == "ready", result.get("error")
    clip = store.get_asset(store.get_step(result["id"], "video:s0")["result"]["asset_id"])
    assert "video_prompt" not in clip["metadata"]
    assert "video_inputs" not in clip["metadata"]
    render = store.get_asset(store.get_render(store.get_project(project["id"])["current_render_id"])["asset_id"])
    assert "video_prompt" not in render["metadata"]
