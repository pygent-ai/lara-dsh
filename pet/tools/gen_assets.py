"""Generate Lara Pet pixel assets: the default bunny skin sheet and app icons.

Run from anywhere:  python pet/tools/gen_assets.py
Outputs:
  pet/ui/skins/default/sheet.png + skin.json
  pet/src-tauri/icons/{icon.ico,icon.png,32x32.png,128x128.png}

The bunny is drawn on a 32x32 logical grid; frames are variants of one base
shape (ear bend / blink / eye / mouth params), so the sheet stays consistent.
"""

from __future__ import annotations

import json
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

FRAME_W, FRAME_H = 32, 32

PET_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SKIN_DIR = os.path.join(PET_DIR, "ui", "skins", "default")
ICON_DIR = os.path.join(PET_DIR, "src-tauri", "icons")

C = {
    "O": (59, 59, 79, 255),      # outline
    "W": (255, 255, 255, 255),   # fur
    "G": (214, 214, 232, 255),   # fur shade
    "P": (255, 183, 197, 255),   # inner ear / blush
    "D": (255, 143, 171, 255),   # nose / open mouth
    "K": (43, 43, 58, 255),      # eyes
    "M": (90, 74, 90, 255),      # closed mouth line
}


def rounded(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], r: int, fill) -> None:
    draw.rounded_rectangle(box, radius=r, fill=fill)


def draw_frame(
    *,
    blink: bool = False,
    eyes: str = "normal",
    mouth: str = "closed",
    bend_r: int = 0,
    tall_ears: bool = False,
    head_drop: int = 0,
) -> Image.Image:
    img = Image.new("RGBA", (FRAME_W, FRAME_H), (0, 0, 0, 0))
    mask = Image.new("L", (FRAME_W, FRAME_H), 0)
    md = ImageDraw.Draw(mask)

    top = (1 if tall_ears else 2) + head_drop
    rounded(md, (9, top, 13, 12 + head_drop), 2, 255)
    if bend_r <= 0:  # right ear
        rounded(md, (18, top, 22, 12 + head_drop), 2, 255)
    else:  # top half bends sideways, base stays attached
        rounded(md, (18, top + 2, 22, 12 + head_drop), 2, 255)
        rounded(md, (18 + bend_r, top, 22 + bend_r, 8 + head_drop), 2, 255)
    md.ellipse((6, 9 + head_drop, 25, 22 + head_drop), fill=255)
    md.ellipse((8, 18, 23, 29), fill=255)  # body
    md.ellipse((10, 27, 14, 30), fill=255)  # feet
    md.ellipse((17, 27, 21, 30), fill=255)

    edge = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(3)), mask)
    img.paste(Image.new("RGBA", (FRAME_W, FRAME_H), C["O"]), (0, 0), edge)

    fur = Image.new("RGBA", (FRAME_W, FRAME_H), C["W"])
    fd = ImageDraw.Draw(fur)
    fd.ellipse((12, 22, 21, 28), fill=C["G"])  # belly shade
    fd.ellipse((10, 27, 14, 30), fill=C["G"])  # feet shade
    fd.ellipse((17, 27, 21, 30), fill=C["G"])
    img.paste(fur, (0, 0), mask)

    d = ImageDraw.Draw(img)
    rounded(d, (10, top + 2, 12, 9 + head_drop), 1, C["P"])
    rounded(d, (19 + bend_r, top + 2, 21 + bend_r, 9 + head_drop), 1, C["P"])
    face = Image.new("RGBA", (FRAME_W, FRAME_H))
    d = ImageDraw.Draw(face)

    if eyes == "happy":
        for ex in (11, 19):
            d.point((ex, 15), fill=C["K"])
            d.point((ex + 1, 14), fill=C["K"])
            d.point((ex + 2, 15), fill=C["K"])
    elif blink:
        d.rectangle((11, 15, 12, 15), fill=C["K"])
        d.rectangle((19, 15, 20, 15), fill=C["K"])
    else:
        ey = 15 if eyes == "down" else 14
        d.rectangle((11, ey, 12, ey + 1), fill=C["K"])
        d.rectangle((19, ey, 20, ey + 1), fill=C["K"])
        if eyes == "normal":
            d.point((12, ey), fill=(255, 255, 255, 255))

    d.rectangle((15, 15, 16, 15), fill=C["D"])  # nose
    d.rectangle((8, 17, 9, 17), fill=C["P"])    # blush
    d.rectangle((22, 17, 23, 17), fill=C["P"])

    if mouth == "closed":
        d.rectangle((15, 17, 16, 17), fill=C["M"])
    elif mouth == "mid":
        d.rectangle((15, 16, 16, 17), fill=C["D"])
    elif mouth == "open":
        d.rectangle((14, 16, 17, 17), fill=C["D"])
        d.rectangle((14, 18, 17, 18), fill=C["M"])
    elif mouth == "wide":
        d.rectangle((13, 16, 18, 17), fill=C["D"])
        d.rectangle((13, 18, 18, 18), fill=C["M"])
    img.alpha_composite(face, (0, head_drop))
    return img


def draw_working_frame(pressed: str | None) -> Image.Image:
    img = draw_frame(bend_r=3, head_drop=2 if pressed else 1, eyes="down" if pressed else "normal")
    draw = ImageDraw.Draw(img)
    rounded(draw, (6, 25, 25, 30), 2, C["O"])
    draw.rectangle((8, 26, 23, 28), fill=(144, 144, 173, 255))
    for x in range(9, 24, 3):
        draw.point((x, 27), fill=C["G"])
    draw.line((13, 28, 18, 28), fill=C["G"])
    for side, x in (("left", 9), ("right", 18)):
        y = 24 if pressed == side else 22
        rounded(draw, (x, y, x + 4, y + 3), 1, C["O"])
        draw.line((x + 1, y + 1, x + 3, y + 1), fill=C["W"])
        draw.point((x + 2, y + 2), fill=C["G"])
    return img


def render_frames() -> list[Image.Image]:
    return [
        draw_frame(),
        draw_frame(head_drop=1),
        draw_frame(blink=True, bend_r=1),
        draw_working_frame("left"),
        draw_working_frame("right"),
        draw_working_frame(None),
        draw_frame(tall_ears=True, mouth="mid"),
        draw_frame(tall_ears=True, mouth="open"),
        draw_frame(tall_ears=True, mouth="wide", eyes="happy"),
        draw_frame(tall_ears=True, head_drop=1),
    ]

SKIN = {
    "name": "default-bunny",
    "frame": {"w": FRAME_W, "h": FRAME_H},
    "scale": 6,
    "states": {
        "idle": {"fps": 6, "frames": [0] * 8 + [1] * 4 + [0] * 8 + [2, 0, 0]},
        "working": {"fps": 6, "frames": [3, 4, 3, 4, 3, 4, 5, 5]},
        "speaking": {"fps": 8, "frames": [6, 7, 8, 9]},
    },
}


def main() -> None:
    os.makedirs(SKIN_DIR, exist_ok=True)
    os.makedirs(ICON_DIR, exist_ok=True)

    rendered = render_frames()
    sheet = Image.new("RGBA", (FRAME_W * len(rendered), FRAME_H), (0, 0, 0, 0))
    for i, frame in enumerate(rendered):
        sheet.paste(frame, (i * FRAME_W, 0))
    sheet.save(os.path.join(SKIN_DIR, "sheet.png"))
    with open(os.path.join(SKIN_DIR, "skin.json"), "w", encoding="utf-8") as f:
        json.dump(SKIN, f, ensure_ascii=False, indent=2)

    base = rendered[0].resize((256, 256), Image.NEAREST)
    base.save(os.path.join(ICON_DIR, "icon.png"))
    base.save(os.path.join(ICON_DIR, "128x128.png"))
    base.resize((32, 32), Image.NEAREST).save(os.path.join(ICON_DIR, "32x32.png"))
    base.save(
        os.path.join(ICON_DIR, "icon.ico"),
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"sheet -> {os.path.join(SKIN_DIR, 'sheet.png')}")
    print(f"icons -> {ICON_DIR}")


if __name__ == "__main__":
    main()
