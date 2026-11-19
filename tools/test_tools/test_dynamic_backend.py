"""Tests for SERVER_BACKEND="dynamic": the mixed llama+strata+deepseek fleet.

Covers three pieces of the refactor:
  * LlmBackendRegistry.from_config(kind="dynamic") merges the three config files into one
    registry (llama/strata backends + deepseek endpoints).
  * backend_model_cache.remember()/remembered_models() persist last-known models so a down
    endpoint's models still appear (offline) after a restart.
  * build_model_catalog() merges remembered offline models into the picker list with
    served=False (the CLIENT renders that as a grayed/disabled option).

Run:  python -m pytest tools/test_tools/test_dynamic_backend.py -v
"""

import os
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))

if _REPO_ROOT not in __import__("sys").path:
    __import__("sys").path.insert(0, _REPO_ROOT)


def _fake_backend(kind, host, port, model_key="", label="", aliases=(),
                  is_deepseek=False, endpoint_models=None, base_url=None):
    b = SimpleNamespace()
    b.kind = kind
    b.instance = SimpleNamespace(name=f"{kind}-{port}", host=host, port=port)
    b.host = host
    b.port = port
    b.api_url = base_url or f"http://{host}:{port}"
    b.model_key = model_key
    b.model_label = label or model_key
    b.model_aliases = set(aliases)
    b.is_deepseek = is_deepseek
    b.parent = None
    b.endpoint_models = endpoint_models or []
    return b


# --- from_config(kind="dynamic") ------------------------------------------------


def test_from_config_dynamic_merges_three_kinds(monkeypatch):
    from config import LlamaServerInstance, StrataServerInstance, DeepSeekServerInstance
    import app.providers.llama.backend_registry as br

    llama = LlamaServerInstance("local", "127.0.0.1", 5000)
    strata = StrataServerInstance("strata", "127.0.0.1", 8081)
    deepseek = DeepSeekServerInstance("deepseek", "api.deepseek.com", 443,
                                      base_url="https://api.deepseek.com")

    monkeypatch.setattr(br, "get_llama_server_instances", lambda: [llama])
    monkeypatch.setattr(br, "get_strata_server_instances", lambda: [strata])
    monkeypatch.setattr(br, "get_deepseek_server_instances", lambda: [deepseek])

    reg = br.LlmBackendRegistry.from_config(kind="dynamic")

    assert reg.kind == "dynamic"
    assert len(reg.endpoints) == 1
    assert next(iter(reg.endpoints.values())).kind == "deepseek"
    backends = [b for b in reg.backends.values()]
    assert sorted(b.kind for b in backends) == ["llama", "strata"]


def test_from_config_single_kinds_still_work(monkeypatch):
    from config import LlamaServerInstance, StrataServerInstance, DeepSeekServerInstance
    import app.providers.llama.backend_registry as br

    monkeypatch.setattr(br, "get_llama_server_instances",
                        lambda: [LlamaServerInstance("local", "127.0.0.1", 5000)])
    monkeypatch.setattr(br, "get_strata_server_instances",
                        lambda: [StrataServerInstance("strata", "127.0.0.1", 8081)])
    monkeypatch.setattr(br, "get_deepseek_server_instances",
                        lambda: [DeepSeekServerInstance("deepseek", "api.deepseek.com", 443,
                                                        base_url="https://api.deepseek.com")])

    assert br.LlmBackendRegistry.from_config(kind="llama").kind == "llama"
    assert br.LlmBackendRegistry.from_config(kind="strata").kind == "strata"
    assert br.LlmBackendRegistry.from_config(kind="deepseek").kind == "deepseek"


def test_dynamic_fleet_is_attach_only(monkeypatch):
    """The dynamic fleet never spawns a service: local llama nodes become attach-only too,
    so a down local node stays down (grayed out) instead of launching llama-server."""
    from config import LlamaServerInstance, StrataServerInstance, DeepSeekServerInstance
    import app.providers.llama.backend_registry as br

    llama = LlamaServerInstance("local", "127.0.0.1", 5000)
    strata = StrataServerInstance("strata", "127.0.0.1", 8081)
    deepseek = DeepSeekServerInstance("deepseek", "api.deepseek.com", 443,
                                      base_url="https://api.deepseek.com")

    monkeypatch.setattr(br, "get_llama_server_instances", lambda: [llama])
    monkeypatch.setattr(br, "get_strata_server_instances", lambda: [strata])
    monkeypatch.setattr(br, "get_deepseek_server_instances", lambda: [deepseek])

    reg = br.LlmBackendRegistry.from_config(kind="dynamic")
    llama_backend = next(b for b in reg.backends.values() if b.kind == "llama")
    assert llama_backend.local is True
    assert llama_backend.spawnable is False  # dynamic => never spawn local llama

    # The single-kind llama mode keeps its spawn behaviour.
    monkeypatch.setattr(br, "get_llama_server_instances", lambda: [llama])
    llama_reg = br.LlmBackendRegistry.from_config(kind="llama")
    assert next(iter(llama_reg.backends.values())).spawnable is True


# --- backend_model_cache ---------------------------------------------------------


def test_cache_round_trip_and_offline_entries(monkeypatch, tmp_path):
    import app.providers.llama.backend_model_cache as bmc

    monkeypatch.setattr(bmc, "_CONFIG_DIR", str(tmp_path))

    llama = _fake_backend("llama", "127.0.0.1", 5000, model_key="qwen-gguf",
                          label="Qwen.gguf", aliases=("qwen-alias",))
    strata = _fake_backend("strata", "127.0.0.1", 8081, model_key="strata-model",
                           label="strata-model.gguf")
    deepseek = _fake_backend("deepseek", "api.deepseek.com", 443,
                             is_deepseek=True,
                             endpoint_models=[{"key": "deepseek-flash", "id": "deepseek-flash"},
                                              {"key": "deepseek-v4-pro", "id": "deepseek-v4-pro"}],
                             base_url="https://api.deepseek.com")

    reg = SimpleNamespace()
    reg.endpoints = {("api.deepseek.com", 443): deepseek}
    reg.backends = {
        ("127.0.0.1", 5000): llama,
        ("127.0.0.1", 8081): strata,
    }

    assert bmc.remember(reg) is True
    assert bmc.remember(reg) is False  # unchanged on second write

    entries = bmc.remembered_models()
    by_key = {e["model_key"]: e for e in entries}
    assert set(by_key) == {"qwen-gguf", "strata-model", "deepseek-flash", "deepseek-v4-pro"}
    # Offline entries carry one unhealthy node -> served=False downstream.
    for e in entries:
        assert e["served_by"][0]["healthy"] is False
        assert e["served_by"][0]["slots"] == 0
    # A llama alias is remembered for the picker.
    assert by_key["qwen-gguf"]["aliases"] == ["qwen-alias"]


def test_cache_keeps_down_backend_but_prunes_removed_model(monkeypatch, tmp_path):
    import app.providers.llama.backend_model_cache as bmc

    monkeypatch.setattr(bmc, "_CONFIG_DIR", str(tmp_path))

    # First run: the endpoint is up and serves two models.
    up = _fake_backend("deepseek", "api.deepseek.com", 443, is_deepseek=True,
                       endpoint_models=[{"key": "m1", "id": "m1"}, {"key": "m2", "id": "m2"}],
                       base_url="https://api.deepseek.com")
    bmc.remember(SimpleNamespace(endpoints={("x", 443): up}, backends={}))

    # Second run: endpoint answers but only serves m1 now -> m2 is pruned.
    up2 = _fake_backend("deepseek", "api.deepseek.com", 443, is_deepseek=True,
                        endpoint_models=[{"key": "m1", "id": "m1"}],
                        base_url="https://api.deepseek.com")
    bmc.remember(SimpleNamespace(endpoints={("x", 443): up2}, backends={}))
    assert {e["model_key"] for e in bmc.remembered_models()} == {"m1"}

    # Third run: endpoint DOWN (no endpoint_models) -> cached m1 survives.
    down = _fake_backend("deepseek", "api.deepseek.com", 443, is_deepseek=True,
                         endpoint_models=[], base_url="https://api.deepseek.com")
    bmc.remember(SimpleNamespace(endpoints={("x", 443): down}, backends={}))
    assert {e["model_key"] for e in bmc.remembered_models()} == {"m1"}


# --- build_model_catalog offline merge -------------------------------------------


def test_catalog_includes_remembered_offline_model(monkeypatch):
    import asyncio
    import app.providers.coolems.model_catalog as mc
    import app.providers.llama.backend_model_cache as bmc
    from app.providers.llama.thinking import ThinkingPolicy

    class LocalProvider:
        async def get_models_list(self):
            return []

    class ServerProvider:
        def _get_local_provider(self):
            return LocalProvider()

    fake_reg = SimpleNamespace(kind="dynamic", backends={},
                               thinking_for_model=lambda m: ThinkingPolicy.none())

    monkeypatch.setattr(mc, "get_registry", lambda: fake_reg)
    monkeypatch.setattr(bmc, "remembered_models", lambda: [{
        "name": "down-model.gguf", "size": 0, "folder": None, "on_disk": False,
        "loaded": False, "model_key": "down-model", "aliases": [],
        "served_by": [{"name": "node", "host": "127.0.0.1", "port": 9999, "slots": 0,
                       "healthy": False, "busy": 0, "local": True}],
        "offline": True,
    }])

    catalog = asyncio.run(mc.build_model_catalog(ServerProvider(), None))
    entry = next((m for m in catalog if m["name"] == "down-model.gguf"), None)
    assert entry is not None
    assert entry["served"] is False
    assert entry["online_slots"] == 0
