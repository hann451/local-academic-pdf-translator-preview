"""Coordinate management and page range resolution (Section 8).

Strict rules implemented here:
1. Internal page numbers are 0-indexed (`page_index`), whereas UI / user-facing page
   numbers are 1-indexed (`page_number`).
2. Canonical backend coordinate system is defined as Top-Left origin in PDF points (pt, 1/72 inch)
   on the unrotated visible page (normalized from CropBox/MediaBox and page rotation).
3. Frontend viewport conversion handles zoom (`scale`) and additional view rotation
   (`view_rotation_deg`: 0, 90, 180, 270) as pure, invertible functions so protection
   regions never drift when zooming or rotating.
"""

from __future__ import annotations

import re


def display_to_internal_page(page_number: int, total_pages: int | None = None) -> int:
    """Convert 1-indexed UI page number to 0-indexed internal page_index."""
    if page_number < 1:
        raise ValueError(f"Display page_number must be >= 1, got {page_number}")
    if total_pages is not None and page_number > total_pages:
        raise ValueError(
            f"Display page_number {page_number} exceeds total_pages {total_pages}"
        )
    return page_number - 1


def internal_to_display_page(page_index: int, total_pages: int | None = None) -> int:
    """Convert 0-indexed internal page_index to 1-indexed UI page number."""
    if page_index < 0:
        raise ValueError(f"Internal page_index must be >= 0, got {page_index}")
    if total_pages is not None and page_index >= total_pages:
        raise ValueError(
            f"Internal page_index {page_index} out of range for total_pages {total_pages}"
        )
    return page_index + 1


def parse_page_range_spec(spec: str | None, total_pages: int) -> list[int]:
    """Parse 1-indexed page range string (e.g. '1, 3-5, 8-') into sorted 0-indexed page indices.

    Supports:
    - Single page: '2' -> [1]
    - Closed range: '3-5' -> [2, 3, 4]
    - Open-ended range: '4-' -> [3..total_pages-1], '-3' -> [0, 1, 2]
    - Empty or None -> all pages [0..total_pages-1]
    """
    if total_pages <= 0:
        raise ValueError(f"total_pages must be positive, got {total_pages}")
    if spec is None or not spec.strip():
        return list(range(total_pages))

    indices: set[int] = set()
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if not parts:
        return list(range(total_pages))

    for part in parts:
        if "-" in part:
            m = re.match(r"^(\d*)\s*-\s*(\d*)$", part)
            if not m:
                raise ValueError(f"Invalid page range segment: '{part}'")
            start_str, end_str = m.group(1), m.group(2)
            if not start_str and not end_str:
                raise ValueError(f"Empty page range segment: '{part}'")
            start_num = int(start_str) if start_str else 1
            end_num = int(end_str) if end_str else total_pages
            if start_num < 1 or end_num < 1:
                raise ValueError(f"Page numbers must be >= 1 in '{part}'")
            if start_num > end_num:
                raise ValueError(f"Range start > end in '{part}'")
            if end_num > total_pages:
                raise ValueError(
                    f"Page number {end_num} in '{part}' exceeds total pages ({total_pages})"
                )
            for p_num in range(start_num, end_num + 1):
                indices.add(p_num - 1)
        else:
            if not part.isdigit():
                raise ValueError(f"Invalid page number: '{part}'")
            p_num = int(part)
            if p_num < 1 or p_num > total_pages:
                raise ValueError(
                    f"Page number {p_num} out of bounds (1..{total_pages})"
                )
            indices.add(p_num - 1)

    return sorted(indices)


def pdf_bottom_left_to_backend_bbox(
    bbox_bl: list[float] | tuple[float, float, float, float],
    page_height_pt: float,
    crop_offset_x: float = 0.0,
    crop_offset_y: float = 0.0,
) -> list[float]:
    """Convert raw PDF bottom-left origin coordinates [x0, y0_bl, x1, y1_bl]
    into canonical backend top-left origin coordinates [x0, y0_tl, x1, y1_tl] in points.
    """
    x0, y0_bl, x1, y1_bl = (float(v) for v in bbox_bl)
    adj_x0 = x0 - crop_offset_x
    adj_x1 = x1 - crop_offset_x
    adj_y0_bl = y0_bl - crop_offset_y
    adj_y1_bl = y1_bl - crop_offset_y

    y0_tl = page_height_pt - max(adj_y0_bl, adj_y1_bl)
    y1_tl = page_height_pt - min(adj_y0_bl, adj_y1_bl)
    return [min(adj_x0, adj_x1), y0_tl, max(adj_x0, adj_x1), y1_tl]


def backend_bbox_to_pdf_bottom_left(
    bbox_tl: list[float] | tuple[float, float, float, float],
    page_height_pt: float,
    crop_offset_x: float = 0.0,
    crop_offset_y: float = 0.0,
) -> list[float]:
    """Inverse of pdf_bottom_left_to_backend_bbox."""
    x0, y0_tl, x1, y1_tl = (float(v) for v in bbox_tl)
    y0_bl = (page_height_pt - y1_tl) + crop_offset_y
    y1_bl = (page_height_pt - y0_tl) + crop_offset_y
    return [x0 + crop_offset_x, y0_bl, x1 + crop_offset_x, y1_bl]


def backend_to_viewport_bbox(
    bbox_pt: list[float] | tuple[float, float, float, float],
    page_width_pt: float,
    page_height_pt: float,
    scale: float = 1.0,
    view_rotation_deg: int = 0,
) -> list[float]:
    """Convert canonical backend bbox [x0, y0, x1, y1] (pt, top-left origin)
    to frontend viewport pixel bbox [vx0, vy0, vx1, vy1] under zoom `scale`
    and clockwise `view_rotation_deg` (0, 90, 180, 270).
    """
    if scale <= 0:
        raise ValueError(f"scale must be > 0, got {scale}")
    rot = view_rotation_deg % 360
    if rot not in (0, 90, 180, 270):
        raise ValueError(f"view_rotation_deg must be 0, 90, 180, or 270, got {view_rotation_deg}")

    x0, y0, x1, y1 = (float(v) for v in bbox_pt)
    w, h = float(page_width_pt), float(page_height_pt)

    if rot == 0:
        rx0, ry0, rx1, ry1 = x0, y0, x1, y1
    elif rot == 90:
        # Clockwise 90 deg: (x, y) -> (h - y, x)
        rx0, ry0 = h - y1, x0
        rx1, ry1 = h - y0, x1
    elif rot == 180:
        # Clockwise 180 deg: (x, y) -> (w - x, h - y)
        rx0, ry0 = w - x1, h - y1
        rx1, ry1 = w - x0, h - y0
    else:  # 270
        # Clockwise 270 deg: (x, y) -> (y, w - x)
        rx0, ry0 = y0, w - x1
        rx1, ry1 = y1, w - x0

    return [rx0 * scale, ry0 * scale, rx1 * scale, ry1 * scale]


def viewport_to_backend_bbox(
    viewport_bbox_px: list[float] | tuple[float, float, float, float],
    page_width_pt: float,
    page_height_pt: float,
    scale: float = 1.0,
    view_rotation_deg: int = 0,
) -> list[float]:
    """Convert frontend viewport pixel bbox [vx0, vy0, vx1, vy1] back to
    canonical backend bbox [x0, y0, x1, y1] (pt, top-left origin).
    """
    if scale <= 0:
        raise ValueError(f"scale must be > 0, got {scale}")
    rot = view_rotation_deg % 360
    if rot not in (0, 90, 180, 270):
        raise ValueError(f"view_rotation_deg must be 0, 90, 180, or 270, got {view_rotation_deg}")

    vx0, vy0, vx1, vy1 = (float(v) / scale for v in viewport_bbox_px)
    rx0, rx1 = min(vx0, vx1), max(vx0, vx1)
    ry0, ry1 = min(vy0, vy1), max(vy0, vy1)
    w, h = float(page_width_pt), float(page_height_pt)

    if rot == 0:
        x0, y0, x1, y1 = rx0, ry0, rx1, ry1
    elif rot == 90:
        # Inverse of (h - y, x): x = ry, y = h - rx
        x0, x1 = ry0, ry1
        y0, y1 = h - rx1, h - rx0
    elif rot == 180:
        x0, x1 = w - rx1, w - rx0
        y0, y1 = h - ry1, h - ry0
    else:  # 270
        # Inverse of (y, w - x): x = w - ry, y = rx
        x0, x1 = w - ry1, w - ry0
        y0, y1 = rx0, rx1

    return [x0, y0, x1, y1]


def bbox_iou(box_a: list[float], box_b: list[float]) -> float:
    """Compute Intersection over Union (IoU) of two [x0, y0, x1, y1] boxes."""
    ix0 = max(box_a[0], box_b[0])
    iy0 = max(box_a[1], box_b[1])
    ix1 = min(box_a[2], box_b[2])
    iy1 = min(box_a[3], box_b[3])
    iw = max(0.0, ix1 - ix0)
    ih = max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0.0:
        return 0.0
    area_a = max(0.0, box_a[2] - box_a[0]) * max(0.0, box_a[3] - box_a[1])
    area_b = max(0.0, box_b[2] - box_b[0]) * max(0.0, box_b[3] - box_b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0.0 else 0.0


def bbox_intersection_ratio(inner: list[float], outer: list[float]) -> float:
    """Compute fraction of `inner` area that lies inside `outer`."""
    ix0 = max(inner[0], outer[0])
    iy0 = max(inner[1], outer[1])
    ix1 = min(inner[2], outer[2])
    iy1 = min(inner[3], outer[3])
    iw = max(0.0, ix1 - ix0)
    ih = max(0.0, iy1 - iy0)
    inter = iw * ih
    inner_area = max(0.0, inner[2] - inner[0]) * max(0.0, inner[3] - inner[1])
    if inner_area <= 0.0:
        return 0.0
    return inter / inner_area
