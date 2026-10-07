"""A brief that names one lead does not grow a friend in every direction."""
from app.strategy import align_solo_lead, solo_lead_requested


BRIEF = "我想做一个蛋糕的剧情广告，下午茶场景，女生主角。"


def _person(person_id, name):
    return {"id": person_id, "name": name, "description": f"{name}，中国人，东亚面孔，日常大陆长相"}


def _direction(direction_id, people, hook="她坐下来吃蛋糕。"):
    return {
        "id": direction_id,
        "hook": hook,
        "label": "下午茶",
        "production": {
            "speech_mode": "character_dialogue",
            "speaker_id": people[0]["id"],
            "characters": people,
        },
        "production_notes": ["不编未确认的功效"],
    }


def _offer(directions):
    return {"kind": "directions", "message": "三个方向", "recommended_direction_id": directions[0]["id"],
            "directions": directions}


def test_a_named_female_lead_is_solo_unless_someone_else_is_written():
    assert solo_lead_requested(BRIEF) is True
    assert solo_lead_requested("女生主角和她的朋友一起吃下午茶") is False
    assert solo_lead_requested("我想做一个蛋糕广告") is False


def test_directions_that_add_a_friend_fall_back_to_the_one_lead():
    lead = _person("char_lead", "主角")
    friend = _person("char_friend", "朋友")
    directions = [
        _direction("d1", [lead, friend], "主角和朋友一起吃蛋糕。"),
        _direction("d2", [dict(lead), dict(friend)], "朋友坐在对面。"),
        _direction("d3", [dict(lead), dict(friend)], "两个人分蛋糕。"),
    ]
    result = align_solo_lead(_offer(directions), BRIEF)
    casts = [item["production"]["characters"] for item in result["directions"]]
    assert all(len(people) == 1 and people[0]["id"] == "char_lead" for people in casts)
    assert all(item["production"]["speaker_id"] == "char_lead" for item in result["directions"])
    assert any("一位主角" in note for item in result["directions"] for note in item["production_notes"])
    assert directions[0]["production"]["characters"][1]["name"] == "朋友"


def test_a_real_second_person_can_stay_but_one_direction_remains_solo():
    lead = _person("char_lead", "主角")
    clerk = _person("char_clerk", "店员")
    directions = [
        _direction("d1", [lead, clerk], "店员把蛋糕递给她。"),
        _direction("d2", [dict(lead), dict(clerk)], "她接过店员递来的盘子。"),
        _direction("d3", [dict(lead), dict(clerk)], "收银员把蛋糕递给她。"),
    ]
    result = align_solo_lead(_offer(directions), BRIEF)
    counts = [len(item["production"]["characters"]) for item in result["directions"]]
    assert 1 in counts and counts.count(1) >= 1
    assert result["directions"][0]["production"]["characters"][0]["id"] == "char_lead"
    assert any(len(item["production"]["characters"]) == 2 for item in result["directions"][1:])
    paired = align_solo_lead(_offer(directions), "女生主角和她的朋友")
    assert all(len(item["production"]["characters"]) == 2 for item in paired["directions"])
