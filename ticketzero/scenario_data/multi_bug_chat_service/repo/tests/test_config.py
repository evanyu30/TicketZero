from app import config


def test_base_url_is_current_crusoe_host():
    assert config.BASE_URL == "https://api.inference.crusoecloud.com/v1"


def test_model_is_nvidia_nemotron():
    assert config.MODEL.startswith("nvidia/")
    assert "nemotron" in config.MODEL.lower()


def test_max_tokens_leaves_room_for_reasoning():
    assert config.MAX_TOKENS >= 1024
