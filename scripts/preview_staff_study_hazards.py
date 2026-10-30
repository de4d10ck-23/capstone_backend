"""Render a geographic comparison for QA; no running backend required."""
from pathlib import Path
import sys,json
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT / '.tmp/hazard-tools'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
from PIL import Image
from shapely.geometry import shape
from shapely.plotting import patch_from_polygon

data=json.loads((ROOT/'New/frontend/staff/public/data/maasin_study_hazards.geojson').read_text())
fig,axes=plt.subplots(1,2,figsize=(12,13),layout='constrained')
image=Image.open(ROOT/'Maasin_LandFlood/Maasin_LandFlood/LandFlood_Maasin/10Brgys.png')
x0,y0,sx,sy=data['metadata']['overview_transform']
crop=(680,100,1640,2330)
axes[0].imshow(image.crop(crop),extent=(x0+crop[0]*sx,x0+crop[2]*sx,y0-crop[3]*sy,y0-crop[1]*sy))
axes[0].set_title('Supplied overview (georeferenced)')
for f in sorted(data['features'],key=lambda f:f['properties']['draw_order']):
 g=shape(f['geometry']); p=f['properties']
 axes[1].add_patch(patch_from_polygon(g, facecolor='none' if p['kind']=='boundary' else p['color'],
                                     edgecolor=p['color'] if p['kind']=='boundary' else 'none', linewidth=.7))
axes[1].set_title('Interactive polygons (full opacity)')
b=data['metadata']['bounds']
for ax in axes:
 ax.set_xlim(b[0]-.002,b[2]+.002);ax.set_ylim(b[1]-.002,b[3]+.002)
 ax.set_aspect(1);ax.set_xlabel('Longitude');ax.set_ylabel('Latitude');ax.ticklabel_format(useOffset=False);ax.xaxis.set_major_locator(MaxNLocator(3))
axes[1].set_facecolor('#e9eff5')
fig.suptitle('Maasin: 10-barangay hazard overlay comparison\nIcon-covered gaps filled with estimated nearby classes; original flood geometry retained',fontsize=13)
output=ROOT/'New/docs/hazard-overlay-comparison.png';output.parent.mkdir(parents=True,exist_ok=True)
fig.savefig(output,dpi=130)
print(output)

# Close-up isolates the icon repair from the existing georeferencing and roads.
fig, axes = plt.subplots(1, 2, figsize=(13, 7), layout='constrained')
features = [f for f in data['features'] if f['properties']['barangay'] == 'Hantag']
boundary = next(shape(f['geometry']) for f in features if f['properties']['kind'] == 'boundary')
for ax, show_estimates in zip(axes, [False, True]):
 for f in sorted(features, key=lambda f: f['properties']['draw_order']):
  p = f['properties']
  if p.get('inferred') and not show_estimates:
   continue
  ax.add_patch(patch_from_polygon(shape(f['geometry']),
      facecolor='none' if p['kind'] == 'boundary' else p['color'],
      edgecolor=p['color'] if p['kind'] == 'boundary' else 'none', linewidth=.8))
 xmin, ymin, xmax, ymax = boundary.bounds
 ax.set_xlim(xmin-.0005, xmax+.0005); ax.set_ylim(ymin-.0005, ymax+.0005)
 ax.set_aspect(1); ax.set_facecolor('#dce5ee'); ax.ticklabel_format(useOffset=False)
 ax.xaxis.set_major_locator(MaxNLocator(3)); ax.set_xlabel('Longitude'); ax.set_ylabel('Latitude')
 ax.set_title('After: estimated icon continuations' if show_estimates else 'Before: icon-shaped gaps')
fig.suptitle('Hantag hazard overlay: water-source icon repair\nNearby colors continued only beneath printed icons; flood geometry unchanged', fontsize=13)
output = ROOT / 'New/docs/hazard-icon-repair-comparison.png'
fig.savefig(output, dpi=140)
print(output)
