"""服务商预设：识别、模板输出与「自带联网」声明。"""

from app.services import provider_presets


def test_find_llm_preset_by_name_and_url():
    assert provider_presets.find_llm_preset("DeepSeek")["key"] == "deepseek"
    assert provider_presets.find_llm_preset("", "https://api.deepseek.com/v1")["key"] == "deepseek"
    assert provider_presets.find_llm_preset("MiMo（小米）")["key"] == "mimo"
    assert provider_presets.find_llm_preset("", "https://api.xiaomimimo.com/v1")["key"] == "mimo"


def test_find_opencode_go_preset():
    preset = provider_presets.find_llm_preset("opencodego")
    assert preset["key"] == "opencodego"
    assert preset["base_url"] == "https://opencode.ai/inference/go/openai/v1"
    assert "deepseek-v4-pro" in preset["models"]
    # 厂商网关本身不声明内置搜索
    assert preset.get("native_search") is None


def test_search_preset_lookup():
    assert provider_presets.find_search_preset("Brave")["key"] == "brave"
    assert provider_presets.find_search_preset("Serper（Google）")["key"] == "serper"
    assert provider_presets.find_search_preset("", "https://google.serper.dev")["key"] == "serper"
    assert provider_presets.find_search_preset("", "https://api.search.brave.com")["key"] == "brave"
    assert provider_presets.find_search_preset("", "https://api.tavily.com")["key"] == "tavily"


def test_preset_payload_has_native_search_metadata():
    payload = provider_presets.preset_payload()
    deepseek = next(item for item in payload["llm"] if item["key"] == "deepseek")
    assert deepseek["native_search"]["kind"] == "deepseek"
    assert deepseek["native_search"]["mode"] == "standalone"
    mimo = next(item for item in payload["llm"] if item["key"] == "mimo")
    assert mimo["native_search"]["mode"] == "chat_tool"


def test_unknown_provider_falls_back_to_none():
    assert provider_presets.find_llm_preset("某不存在的厂商", "https://example.com/v1") is None
