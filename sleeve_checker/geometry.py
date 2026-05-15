import math


def point_to_segment_distance(point: tuple[float, float], seg_start: tuple[float, float], seg_end: tuple[float, float]) -> float:
    px, py = point
    ax, ay = seg_start
    bx, by = seg_end
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    proj_x = ax + t * dx
    proj_y = ay + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def points_match(p1: tuple[float, float], p2: tuple[float, float], tolerance: float = 5.0) -> bool:
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1]) <= tolerance


def point_on_any_segment(point: tuple[float, float], segments: list[tuple[tuple[float, float], tuple[float, float]]], tolerance: float = 5.0) -> bool:
    return any(point_to_segment_distance(point, s, e) <= tolerance for s, e in segments)


def _cross(ox: float, oy: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Cross product of vectors (OA) x (OB)."""
    return (ax - ox) * (by - oy) - (ay - oy) * (bx - ox)


def segments_intersect(
    p1: tuple[float, float], p2: tuple[float, float],
    p3: tuple[float, float], p4: tuple[float, float],
) -> bool:
    """Return True if line segment p1-p2 properly intersects segment p3-p4."""
    d1 = _cross(p3[0], p3[1], p4[0], p4[1], p1[0], p1[1])
    d2 = _cross(p3[0], p3[1], p4[0], p4[1], p2[0], p2[1])
    d3 = _cross(p1[0], p1[1], p2[0], p2[1], p3[0], p3[1])
    d4 = _cross(p1[0], p1[1], p2[0], p2[1], p4[0], p4[1])
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    return False


def point_in_polygon(
    point: tuple[float, float],
    vertices: list[tuple[float, float]],
) -> bool:
    """Ray-casting point-in-polygon test. Vertices are taken in order; the
    polygon is treated as closed (last vertex connects back to the first)."""
    n = len(vertices)
    if n < 3:
        return False
    px, py = point
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = vertices[i]
        xj, yj = vertices[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def ray_blocked_by_steps(
    origin: tuple[float, float],
    target: tuple[float, float],
    step_segments: list[tuple[tuple[float, float], tuple[float, float]]],
) -> bool:
    """Return True if the straight line from *origin* to *target* crosses any step segment."""
    for seg_start, seg_end in step_segments:
        if segments_intersect(origin, target, seg_start, seg_end):
            return True
    return False


def wall_midpoint_near_outline(
    wall_start: tuple[float, float],
    wall_end: tuple[float, float],
    outline_segments: list[tuple[tuple[float, float], tuple[float, float]]],
    tolerance: float = 200.0,
) -> bool:
    """Return True if the wall segment's midpoint is within *tolerance* of
    any outline segment.

    Used to classify whether a wall is on the building perimeter (外壁).
    Outline segments come from slab edge layers (F108_2_RC立上り線 /
    F108_RC見え掛り線) which trace the building footprint. The architectural
    convention of NOT separating interior vs exterior walls by layer name
    forces this geometric check.
    """
    if not outline_segments:
        return False
    mid = ((wall_start[0] + wall_end[0]) / 2.0, (wall_start[1] + wall_end[1]) / 2.0)
    for seg_start, seg_end in outline_segments:
        if point_to_segment_distance(mid, seg_start, seg_end) <= tolerance:
            return True
    return False
