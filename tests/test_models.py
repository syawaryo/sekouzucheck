from sleeve_checker.models import Sleeve, GridLine, DimLine, WallLine, StepLine, ColumnLine, FloorData, CheckResult


def test_sleeve_creation():
    s = Sleeve(id="test", center=(13180.0, 27750.0), diameter=175.0, label_text="KD φ175", fl_text="FL-710", pn_number="P-N-85", layer="[衛生]スリーブ", discipline="衛生")
    assert s.diameter == 175.0
    assert s.discipline == "衛生"


def test_grid_line_creation():
    g = GridLine(axis_label="1", direction="V", position=0.0)
    assert g.direction == "V"


def test_floor_data_creation():
    fd = FloorData()
    assert fd.sleeves == []


def test_check_result_creation():
    cr = CheckResult(check_id=3, check_name="口径・外径記載", severity="NG", message="φ記載なし")
    assert cr.severity == "NG"


def test_wall_line_has_material_and_is_exterior():
    w = WallLine(
        start=(0.0, 0.0),
        end=(100.0, 0.0),
        layer="[空調]F106_RC壁_構造体線",
        material="RC",
        is_exterior=True,
    )
    assert w.material == "RC"
    assert w.is_exterior is True


def test_wall_line_defaults():
    w = WallLine(start=(0.0, 0.0), end=(100.0, 0.0))
    assert w.material == "不明"
    assert w.is_exterior is False
