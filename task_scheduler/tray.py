from __future__ import annotations

import threading
from typing import Callable

import pystray
from PIL import Image, ImageDraw


def _create_icon_image() -> Image.Image:
    """時計をイメージしたトレイアイコンを生成する"""
    size = 64
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx, cy, r = size // 2, size // 2, size // 2 - 2

    # 背景円
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(30, 120, 210))
    # 外枠
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255), width=3)
    # 時針（12時方向）
    draw.line([cx, cy, cx, cy - r + 8], fill=(255, 255, 255), width=4)
    # 分針（3時方向）
    draw.line([cx, cy, cx + r - 8, cy], fill=(255, 255, 255), width=3)
    # 中心点
    draw.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=(255, 255, 255))
    return img


class TrayIcon:
    """システムトレイアイコンを管理するクラス"""

    def __init__(
        self,
        on_open: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> None:
        self._on_open = on_open
        self._on_exit = on_exit
        self._icon: pystray.Icon | None = None

    def start(self) -> None:
        """トレイアイコンをバックグラウンドスレッドで起動する"""
        menu = pystray.Menu(
            pystray.MenuItem('開く', lambda icon, item: self._on_open()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('終了', lambda icon, item: self._on_exit()),
        )
        self._icon = pystray.Icon(
            name='TaskSchedulerV2',
            icon=_create_icon_image(),
            title='タスクスケジューラV2',
            menu=menu,
        )
        # run_detached() でバックグラウンドスレッドとして動作させる
        self._icon.run_detached()

    def stop(self) -> None:
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
