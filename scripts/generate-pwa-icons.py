"""홈 화면 아이콘 원본을 코드로 그려 크기별 PNG를 만듭니다.

실제 카드·거래·외부 이미지는 사용하지 않습니다. Pillow는 기존 PDF 의존성에 포함됩니다.
수정 후 저장소 루트에서 `python scripts/generate-pwa-icons.py`로 다시 생성합니다.
"""

from pathlib import Path
from PIL import Image, ImageDraw


def generate_icons():
    """중앙 안전 영역에 카드와 M 표시를 그리고 Android·iOS용 크기로 저장합니다."""
    # 큰 그림을 먼저 그려 축소하면 작은 아이콘의 가장자리도 매끄러워집니다.
    scale = 4
    image = Image.new("RGB", (512 * scale, 512 * scale), "#18786b")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        tuple(value * scale for value in (144, 144, 352, 304)),
        radius=24 * scale,
        fill="#b4e0d9",
    )
    draw.rounded_rectangle(
        tuple(value * scale for value in (160, 184, 368, 352)),
        radius=24 * scale,
        fill="#125f55",
    )
    draw.line(
        [
            (x * scale, y * scale)
            for x, y in [(192, 320), (192, 224), (264, 280), (336, 224), (336, 320)]
        ],
        fill="white",
        width=16 * scale,
        joint="curve",
    )
    destination = Path(__file__).resolve().parents[1] / "static" / "icons"
    destination.mkdir(parents=True, exist_ok=True)
    for name, size in [("moa-192.png", 192), ("moa-512.png", 512), ("apple-touch-icon.png", 180)]:
        target = destination / name
        image.resize((size, size), Image.Resampling.LANCZOS).save(target)
        # 아이콘은 공개 정적 자원이므로 다른 실행 사용자도 읽을 수 있게 합니다.
        target.chmod(0o644)


if __name__ == "__main__":
    generate_icons()
