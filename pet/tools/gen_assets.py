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
N_FRAMES = 10

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
) -> Image.Image:
    img = Image.new("RGBA", (FRAME_W, FRAME_H), (0, 0, 0, 0))
    mask = Image.new("L", (FRAME_W, FRAME_H), 0)
    md = ImageDraw.Draw(mask)

    top = 1 if tall_ears else 2
    rounded(md, (9, top, 13, 12), 2, 255)  # left ear
    if bend_r <= 0:  # right ear
        rounded(md, (18, top, 22, 12), 2, 255)
    else:  # top half bends sideways, base stays attached
        rounded(md, (18, top, 22, 7), 2, 255)
        rounded(md, (18 + bend_r, top, 22 + bend_r, 7), 2, 255)
    md.ellipse((6, 9, 25, 22), fill=255)   # head
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
    rounded(d, (10, top + 2, 12, 9), 1, C["P"])  # inner ears
    rounded(d, (19 + bend_r, top + 2, 21 + bend_r, 9), 1, C["P"])

    if eyes == "happy":
        for ex in (11, 19):
            d.point((ex, 15), fill=C["K"])
            d.point((ex + 1, 14), fill=C["K"])
            d.point((ex + 2, 15), fill=C["K"])
    elif blink:
        d.rectangle((11, 15, 12, 15), fill=C["K"])
        d.rectangle((19, 15, 20, 15), fill=C["K"])
    else:
        ey = 13 if eyes == "up" else 14
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
    return img


FRAMES = [
    dict(mouth="closed"),                       # 0 idle A
    dict(blink=True, bend_r=1),                 # 1 idle B
    dict(tall_ears=True, mouth="closed"),       # 2 listen A
    dict(tall_ears=True, eyes="happy"),         # 3 listen B
    dict(bend_r=1, eyes="up", mouth="closed"),  # 4 think A
    dict(bend_r=2, eyes="up", mouth="mid"),     # 5 think B
    dict(mouth="closed"),                       # 6 speak A
    dict(mouth="open"),                         # 7 speak B
    dict(mouth="wide", eyes="happy"),           # 8 speak C
    dict(mouth="mid"),                          # 9 speak D
]

SKIN = {
    "name": "default-bunny",
    "frame": {"w": FRAME_W, "h": FRAME_H},
    "scale": 6,
    "states": {
        "idle": {"fps": 1.6, "frames": [0, 1]},
        "listening": {"fps": 3, "frames": [2, 3]},
        "thinking": {"fps": 4, "frames": [4, 5]},
        "speaking": {"fps": 8, "frames": [6, 7, 8, 9]},
    },
}


def main() -> None:
    os.makedirs(SKIN_DIR, exist_ok=True)
    os.makedirs(ICON_DIR, exist_ok=True)

    sheet = Image.new("RGBA", (FRAME_W * N_FRAMES, FRAME_H), (0, 0, 0, 0))
    rendered = [draw_frame(**spec) for spec in FRAMES]
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
