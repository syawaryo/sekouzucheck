"""Test rule-based layer classification for material-axis wall categories."""
from sleeve_checker.layer_classifier import classify_layer_rule, USEFUL_CATEGORIES, CATEGORIES


def test_useful_categories_contain_new_wall_buckets():
    assert "躯体壁" in USEFUL_CATEGORIES
    assert "乾式壁" in USEFUL_CATEGORIES
    assert "耐火壁・防火区画" in USEFUL_CATEGORIES
    # Old categories must be gone
    assert "内壁" not in USEFUL_CATEGORIES
    assert "外壁" not in USEFUL_CATEGORIES


def test_rc_walls_map_to_kutaiheki():
    assert classify_layer_rule("[空調]F106_RC壁_構造体線") == "躯体壁"
    assert classify_layer_rule("[基本]F105_RC壁") == "躯体壁"
    assert classify_layer_rule("[基本]A421_壁：ＲＣ") == "躯体壁"
    assert classify_layer_rule("[空調]★既存躯体外壁") == "躯体壁"
    assert classify_layer_rule("[建築]壁") == "躯体壁"


def test_dry_walls_map_to_kanshikiheki():
    assert classify_layer_rule("[基本]A422_壁：ＡＬＣ") == "乾式壁"
    assert classify_layer_rule("[基本]A423_壁：PCa") == "乾式壁"
    assert classify_layer_rule("[基本]A424_壁：パネル") == "乾式壁"
    assert classify_layer_rule("[基本]A441_壁：ＬＧＳ") == "乾式壁"
    assert classify_layer_rule("[基本]A442_壁：木軸") == "乾式壁"
    assert classify_layer_rule("[基本]A443_壁：ＣＢ") == "乾式壁"
    assert classify_layer_rule("[基本]A444_壁：下地鉄骨、間柱") == "乾式壁"


def test_firewall_unchanged():
    assert classify_layer_rule("[基本]A561_耐火被覆") == "耐火壁・防火区画"
    assert classify_layer_rule("複合耐火壁") == "耐火壁・防火区画"
    assert classify_layer_rule("防火区画線") == "耐火壁・防火区画"


def test_wall_center_dropped_as_unneeded():
    # C151_壁心 is reference centerline geometry, not actual wall body.
    # v2 routed it to 内壁; v3 routes it to 不要.
    assert classify_layer_rule("[空調]C151_壁心") == "不要"


def test_wall_finish_lines_dropped_as_unneeded():
    # A521_壁：仕上 is surface decoration, no sleeve-check value.
    assert classify_layer_rule("[空調]A521_壁：仕上") == "不要"


def test_wall_label_maps_kutaiheki():
    # F306_壁ラベル is RC wall annotation — folds into 躯体壁.
    assert classify_layer_rule("[空調]F306_壁ラベル") == "躯体壁"


def test_classify_layers_rules_only_smoke():
    """End-to-end rules-only mode produces valid categories for sample layers."""
    from sleeve_checker.layer_classifier import classify_layers
    layers = [
        {"name": "[空調]F106_RC壁_構造体線", "sample_texts": [], "type_count": {"LINE": 17}},
        {"name": "[基本]A422_壁：ＡＬＣ", "sample_texts": [], "type_count": {}},
        {"name": "[空調]★既存躯体外壁", "sample_texts": [], "type_count": {"LINE": 271}},
        {"name": "[空調]C131_通心", "sample_texts": [], "type_count": {"LINE": 25}},
    ]
    out = classify_layers(layers, use_llm=False, cache_path=None)
    assert out["[空調]F106_RC壁_構造体線"]["category"] == "躯体壁"
    assert out["[基本]A422_壁：ＡＬＣ"]["category"] == "乾式壁"
    assert out["[空調]★既存躯体外壁"]["category"] == "躯体壁"
    assert out["[空調]C131_通心"]["category"] == "通り芯"
    for name, row in out.items():
        assert row["category"] in CATEGORIES, f"{name} → {row['category']} not in CATEGORIES"
