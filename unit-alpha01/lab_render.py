"""Isolated RAW worker. Run with the dedicated .lab-runtime Python."""
import json
from pathlib import Path
import sys
import numpy as np
import rawpy
import tifffile
from PIL import Image, ImageCms, ImageOps


def render(request):
    source = Path(request['source'])
    target = Path(request['target'])
    target.mkdir(parents=True, exist_ok=True)
    recipe = request['recipe']
    if request['raw']:
        with rawpy.imread(str(source)) as raw:
            options = dict(output_bps=16, output_color=rawpy.ColorSpace.sRGB,
                           no_auto_bright=True, exp_shift=2 ** recipe['exposure'],
                           highlight_mode=rawpy.HighlightMode.Blend)
            if recipe['wb'] == 'auto':
                options['use_auto_wb'] = True
            else:
                wb = list(raw.camera_whitebalance)
                if len(wb) != 4 or min(wb[:3]) <= 0:
                    raise ValueError('В RAW нет корректного camera WB')
                strength = 2 ** (recipe['warmth'] / 100)
                wb[0] *= strength
                wb[2] /= strength
                options['user_wb'] = wb
            rgb = raw.postprocess(**options)
        floating = rgb.astype(np.float32) / 65535
    else:
        with Image.open(source) as original:
            im = ImageOps.exif_transpose(original)
            profile = im.info.get('icc_profile')
            if profile:
                import io
                im = ImageCms.profileToProfile(im.convert('RGB'), ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                                               ImageCms.createProfile('sRGB'), outputMode='RGB')
            floating = np.asarray(im.convert('RGB'), dtype=np.float32) / 255
        floating = floating * (2 ** recipe['exposure'])
        gain = 2 ** (recipe['warmth'] / 100)
        floating[:, :, 0] *= gain
        floating[:, :, 2] /= gain
    contrast = 1 + recipe['contrast'] / 100
    floating = np.clip((floating - .5) * contrast + .5, 0, 1)
    rgb16 = np.rint(floating * 65535).astype(np.uint16)
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    # Master has rendered tone/colour, sufficient bit depth, and a declared ICC.
    tifffile.imwrite(target / 'master.tif', rgb16, photometric='rgb', metadata=None,
                     extratags=[(34675, 'B', len(profile), profile, False)])
    image = Image.fromarray((rgb16 / 257).round().astype(np.uint8))
    image.thumbnail((recipe['long_edge'], recipe['long_edge']), Image.Resampling.LANCZOS)
    image.save(target / 'final.jpg', quality=95, subsampling=0, icc_profile=profile)
    image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
    image.save(target / 'preview.jpg', quality=88, icc_profile=profile)
    (target / 'recipe.json').write_text(json.dumps({**request, 'rawpy': rawpy.__version__,
                                                  'libraw': rawpy.libraw_version}, indent=2), 'utf-8')
    return {'width': Image.open(target / 'final.jpg').width, 'height': Image.open(target / 'final.jpg').height,
            'rawpy': rawpy.__version__}


if __name__ == '__main__':
    try:
        print(json.dumps(render(json.loads(sys.stdin.read()))))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
