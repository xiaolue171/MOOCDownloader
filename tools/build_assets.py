"""Build-time assets only; Pillow is not bundled with the application."""
from pathlib import Path
from importlib import metadata
from PIL import Image, ImageDraw
ROOT = Path(__file__).resolve().parents[1]
(ROOT/'assets').mkdir(exist_ok=True)
im = Image.new('RGBA',(256,256),(0,120,212,255))
d = ImageDraw.Draw(im)
d.polygon([(46,64),(112,78),(112,198),(46,181)], fill='white')
d.polygon([(144,78),(210,64),(210,181),(144,198)], fill='white')
d.line([(128,52),(128,205)], fill=(180,224,255),width=8)
im.save(ROOT/'assets'/'app.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
licenses=ROOT/'licenses'
licenses.mkdir(exist_ok=True)
for name in ['requests','pywebview','pythonnet','m3u8','urllib3','certifi','charset-normalizer','idna','cffi','pycparser','bottle','proxy_tools','typing_extensions']:
    try:
        dist=metadata.distribution(name)
    except metadata.PackageNotFoundError:
        continue
    for entry in dist.files or []:
        if ('license' in entry.name.lower() or 'copying' in entry.name.lower()) and ('.dist-info' in str(entry)):
            source=Path(dist.locate_file(entry))
            if source.is_file():
                (licenses/(name+'-'+source.name+'.txt')).write_bytes(source.read_bytes())
print('Icon and dependency notices ready')
