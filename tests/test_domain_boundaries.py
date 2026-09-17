def test_pacs_domain_boundaries_expose_shared_tools_and_nodes():
    from src.agents.pacs.nodes import human_approval, route_request
    from src.agents.pacs.tools import READ_ONLY_TOOLS, search_knowledge_tool

    assert callable(human_approval)
    assert callable(route_request)
    assert search_knowledge_tool in READ_ONLY_TOOLS
