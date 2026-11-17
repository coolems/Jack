"""Tests: DNA + prompt layout after the 2026-11-17 move.

DNA is agent state, not a tool, so it lives in app/dna/ (SERVER) and is delivered
to CLIENTs as code under the 'app.dna.*' keys. Learning/lessons were removed:
DNA is read-only identity text. The CLIENT keeps no DNA file on disk - the
identity JSON is installed in memory by the loader.

Run:  python -m pytest tools/test_tools/test_dna_and_prompt.py -v
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))


def _p(rel):
    return os.path.join(_REPO_ROOT, rel)


SERVER_DNA = _p("app/dna")
CLIENT_DNA = _p("CLIENT/app/dna")


def test_dna_package_imports():
    import app.dna as dna

    assert callable(dna.get_agent)
    agent = dna.get_agent()
    text = agent.get_identity()
    assert text.startswith("Your name is ")
    assert "## Personality" in text and "## My Goals" in text


def test_dna_is_not_a_tool_folder():
    assert not os.path.exists(_p("tools/dna")), "DNA must not live under tools/"
    assert not os.path.exists(_p("app/prompts/../tools/agent_prompt"))


def test_no_learning_pipeline_left():
    for rel in ("tools/dna/learning.py", "app/dna/learning.py", "lessons.json",
                "intelligence.json"):
        assert not os.path.exists(_p(rel)), f"{rel} should be gone"
    src = open(_p("CLIENT/logic/tool_executor.py"), encoding="utf-8").read()
    assert "lesson_manager" not in src and "_LESSONS_INJECTION_ENABLED" not in src


def test_client_keeps_no_dna_data_files():
    assert os.path.isdir(CLIENT_DNA), "the lazy package shell stays"
    for fname in os.listdir(CLIENT_DNA):
        assert not fname.endswith(".json"), f"DNA data file on CLIENT disk: {fname}"
    assert not os.path.exists(_p("CLIENT/tools/app/dna")), "stray DNA folder must be gone"


def test_delivery_keys_are_app_dna():
    from app.providers.coolems.tool_scanner import get_all_shared_sources, get_dna_data_files

    tools_dir = _p("tools")
    keys = sorted(k for k in get_all_shared_sources(tools_dir) if k.startswith("app.dna."))
    assert keys == ["app.dna.core", "app.dna.get_agent", "app.dna.name"], keys
    data = get_dna_data_files(_REPO_ROOT)
    assert sorted(data) == ["identity.json"]
    json.loads(data["identity.json"])


def test_identity_reads_memory_payload_first():
    import app.dna as dna

    payload = {"identity.json": json.dumps({
        "core_identity": {"origin": "delivered"},
        "personality_traits": ["t"], "value_scale": {"honesty": 60},
        "behavior_rules": ["r"], "evolution_protocol": [], "constant_goals": ["g"],
    })}
    dna.set_dna_data(payload)
    try:
        assert "delivered" in dna.AgentDNA().get_identity()
    finally:
        dna.set_dna_data({})
    assert "delivered" not in dna.AgentDNA().get_identity()


def test_server_builds_both_prompt_variants():
    from app.dna import get_agent
    from app.prompts import build_chat_prompt, build_system_prompt

    dna_text = get_agent().get_identity()
    full = build_system_prompt(dna_text)
    chat = build_chat_prompt(dna_text)

    assert full.startswith(dna_text)
    assert "## AGENTIC LOOP CONTROL" in full          # tool contract
    assert "## PLANNING PROTOCOL" in full             # planning rules
    assert "## RESPONSE RULES" in full                # identity lock
    assert chat.startswith(dna_text)
    assert "## AGENTIC LOOP CONTROL" not in chat      # plain chat pays no tool contract
    assert "## RESPONSE RULES" in chat
    assert "{{TOOL_DESCRIPTIONS}}" not in full


def test_client_prompt_store_is_memory_only():
    src = open(_p("CLIENT/app/prompts/__init__.py"), encoding="utf-8").read()
    assert "open(" not in src, "the CLIENT prompt store must never touch disk"


if __name__ == "__main__":
    for fn in (test_dna_package_imports, test_dna_is_not_a_tool_folder,
               test_no_learning_pipeline_left, test_client_keeps_no_dna_data_files,
               test_delivery_keys_are_app_dna, test_identity_reads_memory_payload_first,
               test_server_builds_both_prompt_variants, test_client_prompt_store_is_memory_only):
        fn()
    print("dna + prompt layout checks passed")
