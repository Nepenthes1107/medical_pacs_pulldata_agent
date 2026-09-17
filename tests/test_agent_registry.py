from src.agents.registry import get_all_agent_info


def test_pacs_agent_is_registered():
    info = get_all_agent_info()
    assert [item["key"] for item in info] == ["pacs-diagnostician"]
    assert "human_approval" in info[0]["capabilities"]
