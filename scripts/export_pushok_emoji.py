#!/usr/bin/env python3
"""Экспорт собственных Pushok SVG-иконок в статические Telegram Custom Emoji (100x100).

Запуск (в окружении разработчика):
    python -m pip install cairosvg pillow
    python scripts/export_pushok_emoji.py --sprite <путь> --output .build/pushok-emoji

Инструмент только создаёт PNG/WebP. Ничего не публикует в Telegram,
не требует токенов и не изобретает custom_emoji_id.
"""
from __future__ import annotations

import argparse
import copy
import re
import xml.etree.ElementTree as ET
from pathlib import Path

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)
ICON_NAME = re.compile(r"pi-([a-z0-9_-]{2,64})\Z")
SIZE = 100


def extract_icons(sprite: Path) -> dict[str, bytes]:
    root = ET.parse(sprite).getroot()
    if root.tag != f"{{{SVG_NS}}}svg":
        raise ValueError("Не SVG-документ")
    icons: dict[str, bytes] = {}
    for item in root:
        if item.tag != f"{{{SVG_NS}}}symbol":
            continue
        match = ICON_NAME.fullmatch(item.attrib.get("id", ""))
        if not match:
            raise ValueError("Некорректное имя символа")
        name = match.group(1)
        if name in icons:
            raise ValueError(f"Повтор имени иконки: {name}")
        view_box = item.attrib.get("viewBox", "0 0 24 24")
        if view_box != "0 0 24 24":
            raise ValueError(f"Неожиданный viewBox: {name}")
        standalone = ET.Element(
            f"{{{SVG_NS}}}svg",
            {"viewBox": "-2 -2 28 28", "width": str(SIZE), "height": str(SIZE)},
        )
        for child in item:
            standalone.append(copy.deepcopy(child))
        icons[name] = ET.tostring(standalone, encoding="utf-8", xml_declaration=True)
    if not icons:
        raise ValueError("Набор иконок пуст")
    return icons


def export(sprite: Path, output: Path, *, webp: bool = False) -> list[Path]:
    try:
        import cairosvg
        from PIL import Image
    except ImportError as exc:
        raise SystemExit("Установите локально: python -m pip install cairosvg pillow") from exc
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, vector in extract_icons(sprite).items():
        png = cairosvg.svg2png(bytestring=vector, output_width=SIZE, output_height=SIZE)
        from io import BytesIO
        image = Image.open(BytesIO(png)).convert("RGBA")
        if image.size != (SIZE, SIZE):
            raise ValueError(f"Неверный размер: {name}")
        target = output / f"{name}.{'webp' if webp else 'png'}"
        if webp:
            image.save(target, "WEBP", lossless=True, exact=True)
        else:
            image.save(target, "PNG", optimize=True)
        paths.append(target)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sprite", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--webp", action="store_true")
    args = parser.parse_args()
    result = export(args.sprite, args.output, webp=args.webp)
    print(f"Готово: {len(result)} статических emoji 100x100 → {args.output}")


if __name__ == "__main__":
    main()
