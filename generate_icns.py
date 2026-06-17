import os
import shutil
import subprocess
from PIL import Image, ImageDraw

def draw_diamond(size):
    # Draw at 4x size for high-quality downsampling (antialiasing)
    scale = 4
    canvas_size = size * scale
    img = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # Diamond vertices with padding (10% padding for aesthetic spacing)
    padding = canvas_size * 0.1
    cx = canvas_size / 2
    cy = canvas_size / 2
    r = canvas_size / 2 - padding
    
    vertices = [
        (cx, cy - r),      # Top
        (cx + r, cy),      # Right
        (cx, cy + r),      # Bottom
        (cx - r, cy)       # Left
    ]
    
    # Color #00ff9d (RGB: 0, 255, 157) matching the GUI's brand color
    draw.polygon(vertices, fill=(0, 255, 157, 255))
    
    # Downsample using high-quality Lanczos filter
    img = img.resize((size, size), Image.Resampling.LANCZOS)
    return img

def main():
    iconset_dir = "tokenmaxxing.iconset"
    if os.path.exists(iconset_dir):
        shutil.rmtree(iconset_dir)
    os.makedirs(iconset_dir)
    
    sizes = [16, 32, 128, 256, 512]
    for size in sizes:
        img = draw_diamond(size)
        img.save(os.path.join(iconset_dir, f"icon_{size}x{size}.png"))
        
        img_2x = draw_diamond(size * 2)
        img_2x.save(os.path.join(iconset_dir, f"icon_{size}x{size}@2x.png"))
        
    print("Generated PNGs in tokenmaxxing.iconset")
    
    # Run iconutil to create .icns
    try:
        subprocess.run(["iconutil", "-c", "icns", iconset_dir, "-o", "tokenmaxxing.icns"], check=True)
        print("Successfully generated tokenmaxxing.icns")
    except Exception as e:
        print(f"Error running iconutil: {e}")
    finally:
        if os.path.exists(iconset_dir):
            shutil.rmtree(iconset_dir)

if __name__ == "__main__":
    main()
