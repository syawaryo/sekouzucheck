from sleeve_checker.geometry import point_to_segment_distance, points_match


def test_point_to_segment_perpendicular():
    assert abs(point_to_segment_distance((5, 5), (0, 0), (10, 0)) - 5.0) < 0.001


def test_point_to_segment_endpoint():
    assert abs(point_to_segment_distance((15, 0), (0, 0), (10, 0)) - 5.0) < 0.001


def test_point_to_segment_on_line():
    assert abs(point_to_segment_distance((5, 0), (0, 0), (10, 0))) < 0.001


def test_points_match_within_tolerance():
    assert points_match((100.0, 200.0), (103.0, 198.0), tolerance=5.0)


def test_points_match_outside_tolerance():
    assert not points_match((100.0, 200.0), (110.0, 200.0), tolerance=5.0)


def test_point_to_vertical_segment():
    assert abs(point_to_segment_distance((5, 5), (0, 0), (0, 10)) - 5.0) < 0.001


def test_wall_midpoint_near_outline_true():
    from sleeve_checker.geometry import wall_midpoint_near_outline
    outline = [((0.0, 100.0), (200.0, 100.0))]
    assert wall_midpoint_near_outline(
        wall_start=(0.0, 100.0),
        wall_end=(100.0, 100.0),
        outline_segments=outline,
        tolerance=200.0,
    ) is True


def test_wall_midpoint_near_outline_false():
    from sleeve_checker.geometry import wall_midpoint_near_outline
    outline = [((0.0, 100.0), (200.0, 100.0))]
    assert wall_midpoint_near_outline(
        wall_start=(0.0, 500.0),
        wall_end=(100.0, 500.0),
        outline_segments=outline,
        tolerance=200.0,
    ) is False


def test_wall_midpoint_near_outline_empty_outline():
    from sleeve_checker.geometry import wall_midpoint_near_outline
    assert wall_midpoint_near_outline(
        wall_start=(0.0, 0.0),
        wall_end=(100.0, 0.0),
        outline_segments=[],
        tolerance=200.0,
    ) is False
