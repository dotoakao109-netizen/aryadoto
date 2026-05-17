"""
KML/KMZ to Shapefile - Web Application
=======================================
Flask backend for converting KML/KMZ files to ESRI Shapefiles.
"""

import os
import uuid
import zipfile
import shutil
import time
import threading
from pathlib import Path

from flask import Flask, request, jsonify, send_file, render_template
import xml.etree.ElementTree as ET
import shapefile

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max upload
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')

# Ensure upload folder exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ============================================================
# CONVERSION LOGIC
# ============================================================

KML_NS = {
    'kml': 'http://www.opengis.net/kml/2.2',
    'gx': 'http://www.google.com/kml/ext/2.2',
}


def extract_kml_from_kmz(kmz_path):
    with zipfile.ZipFile(kmz_path, 'r') as z:
        kml_files = [f for f in z.namelist() if f.lower().endswith('.kml')]
        if not kml_files:
            raise ValueError("No .kml file found inside the KMZ archive")
        main_kml = 'doc.kml' if 'doc.kml' in kml_files else kml_files[0]
        return z.read(main_kml)


def parse_coordinates(coord_text):
    coords = []
    for part in coord_text.strip().split():
        values = part.split(',')
        lon = float(values[0])
        lat = float(values[1])
        alt = float(values[2]) if len(values) > 2 else 0.0
        coords.append((lon, lat, alt))
    return coords


def build_parent_map(root):
    """Build child->parent map for ElementTree (no getparent)."""
    parent_map = {}
    for parent in root.iter():
        for child in parent:
            parent_map[child] = parent
    return parent_map


def extract_placemarks(root):
    placemarks = {'Point': [], 'LineString': [], 'Polygon': []}
    parent_map = build_parent_map(root)
    ns = KML_NS['kml']

    for pm in root.iter(f'{{{ns}}}Placemark'):
        name_el = pm.find(f'{{{ns}}}name')
        name = name_el.text if name_el is not None and name_el.text else ''

        desc_el = pm.find(f'{{{ns}}}description')
        description = desc_el.text if desc_el is not None and desc_el.text else ''

        folder = ''
        parent = parent_map.get(pm)
        if parent is not None:
            folder_name_el = parent.find(f'{{{ns}}}name')
            if folder_name_el is not None and folder_name_el.text:
                folder = folder_name_el.text

        attrs = {
            'name': name[:254],
            'descriptio': description[:254],
            'folder': folder[:254],
        }

        for data in pm.iter(f'{{{ns}}}Data'):
            key = data.get('name', '')[:10]
            val_el = data.find(f'{{{ns}}}value')
            val = val_el.text if val_el is not None and val_el.text else ''
            attrs[key] = val[:254]

        for data in pm.iter(f'{{{ns}}}SimpleData'):
            key = (data.get('name') or '')[:10]
            val = data.text if data.text else ''
            attrs[key] = val[:254]

        # Point
        point = pm.find(f'.//{{{ns}}}Point/{{{ns}}}coordinates')
        if point is not None and point.text:
            coords = parse_coordinates(point.text)
            if coords:
                placemarks['Point'].append({'coords': coords[0], 'attrs': attrs})
            continue

        # LineString
        line = pm.find(f'.//{{{ns}}}LineString/{{{ns}}}coordinates')
        if line is not None and line.text:
            coords = parse_coordinates(line.text)
            if len(coords) >= 2:
                placemarks['LineString'].append({'coords': coords, 'attrs': attrs})
            continue

        # Polygon
        poly = pm.find(f'.//{{{ns}}}Polygon/{{{ns}}}outerBoundaryIs/{{{ns}}}LinearRing/{{{ns}}}coordinates')
        if poly is not None and poly.text:
            coords = parse_coordinates(poly.text)
            if len(coords) >= 3:
                placemarks['Polygon'].append({'coords': coords, 'attrs': attrs})
            continue

        # MultiGeometry
        multi = pm.find(f'.//{{{ns}}}MultiGeometry')
        if multi is not None:
            for sub_point in multi.findall(f'{{{ns}}}Point/{{{ns}}}coordinates'):
                if sub_point.text:
                    coords = parse_coordinates(sub_point.text)
                    if coords:
                        placemarks['Point'].append({'coords': coords[0], 'attrs': attrs})

            for sub_line in multi.findall(f'{{{ns}}}LineString/{{{ns}}}coordinates'):
                if sub_line.text:
                    coords = parse_coordinates(sub_line.text)
                    if len(coords) >= 2:
                        placemarks['LineString'].append({'coords': coords, 'attrs': attrs})

            for sub_poly in multi.findall(f'{{{ns}}}Polygon/{{{ns}}}outerBoundaryIs/{{{ns}}}LinearRing/{{{ns}}}coordinates'):
                if sub_poly.text:
                    coords = parse_coordinates(sub_poly.text)
                    if len(coords) >= 3:
                        placemarks['Polygon'].append({'coords': coords, 'attrs': attrs})

    return placemarks


def collect_all_fields(features):
    fields = {}
    for feat in features:
        for key, val in feat['attrs'].items():
            if key not in fields:
                fields[key] = min(len(val), 254)
            else:
                fields[key] = max(fields[key], min(len(val), 254))
    for key in fields:
        if fields[key] < 10:
            fields[key] = 50
    return fields


def write_shapefile(features, geom_type, output_path):
    if not features:
        return None

    w = shapefile.Writer(output_path)
    w.autoBalance = 1

    fields = collect_all_fields(features)
    for field_name, size in fields.items():
        w.field(field_name, 'C', size=min(size, 254))

    for feat in features:
        attrs = feat['attrs']
        row = [attrs.get(f, '') for f in fields.keys()]

        if geom_type == 'Point':
            lon, lat, alt = feat['coords']
            w.pointz(lon, lat, alt)
        elif geom_type == 'LineString':
            w.linez([[[lon, lat, alt] for lon, lat, alt in feat['coords']]])
        elif geom_type == 'Polygon':
            w.polyz([[[lon, lat, alt] for lon, lat, alt in feat['coords']]])

        w.record(*row)

    w.close()

    prj_content = (
        'GEOGCS["GCS_WGS_1984",'
        'DATUM["D_WGS_1984",'
        'SPHEROID["WGS_1984",6378137.0,298.257223563]],'
        'PRIMEM["Greenwich",0.0],'
        'UNIT["Degree",0.0174532925199433]]'
    )
    with open(output_path + '.prj', 'w') as f:
        f.write(prj_content)

    return output_path


def convert_file(input_path, output_dir):
    """Convert a KML/KMZ file and return results."""
    input_path = Path(input_path)

    if input_path.suffix.lower() == '.kmz':
        kml_content = extract_kml_from_kmz(str(input_path))
    elif input_path.suffix.lower() == '.kml':
        kml_content = input_path.read_bytes()
    else:
        raise ValueError(f"Unsupported file type: {input_path.suffix}")

    root = ET.fromstring(kml_content)
    placemarks = extract_placemarks(root)

    base_name = input_path.stem
    results = []
    suffix_map = {'Point': '_point', 'LineString': '_line', 'Polygon': '_polygon'}

    for geom_type, features in placemarks.items():
        if not features:
            continue
        out_name = base_name + suffix_map[geom_type]
        out_path = os.path.join(output_dir, out_name)
        write_shapefile(features, geom_type, out_path)
        results.append({
            'type': geom_type,
            'count': len(features),
            'name': out_name
        })

    return results


# ============================================================
# CLEANUP - remove old files every 30 minutes
# ============================================================

def cleanup_old_files():
    """Remove upload folders older than 1 hour."""
    while True:
        time.sleep(1800)  # every 30 min
        try:
            upload_dir = app.config['UPLOAD_FOLDER']
            now = time.time()
            for item in os.listdir(upload_dir):
                item_path = os.path.join(upload_dir, item)
                if os.path.isdir(item_path):
                    age = now - os.path.getmtime(item_path)
                    if age > 3600:  # 1 hour
                        shutil.rmtree(item_path, ignore_errors=True)
        except Exception:
            pass


cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()


# ============================================================
# ROUTES
# ============================================================

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/convert', methods=['POST'])
def convert_endpoint():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    # Validate extension
    ext = Path(file.filename).suffix.lower()
    if ext not in ('.kml', '.kmz'):
        return jsonify({'error': 'Only .kml and .kmz files are supported'}), 400

    # Create unique job folder
    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(app.config['UPLOAD_FOLDER'], job_id)
    os.makedirs(job_dir, exist_ok=True)

    # Save uploaded file
    input_path = os.path.join(job_dir, file.filename)
    file.save(input_path)

    # Convert
    try:
        output_dir = os.path.join(job_dir, 'output')
        os.makedirs(output_dir, exist_ok=True)
        results = convert_file(input_path, output_dir)

        if not results:
            shutil.rmtree(job_dir, ignore_errors=True)
            return jsonify({'error': 'No geometries found in the file'}), 400

        # Create ZIP of all shapefiles
        zip_name = Path(file.filename).stem + '_shapefile.zip'
        zip_path = os.path.join(job_dir, zip_name)
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in os.listdir(output_dir):
                zf.write(os.path.join(output_dir, f), f)

        return jsonify({
            'success': True,
            'job_id': job_id,
            'filename': zip_name,
            'results': results
        })

    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({'error': str(e)}), 500


@app.route('/download/<job_id>/<filename>')
def download(job_id, filename):
    # Sanitize
    if '..' in job_id or '..' in filename:
        return jsonify({'error': 'Invalid path'}), 400

    file_path = os.path.join(app.config['UPLOAD_FOLDER'], job_id, filename)
    if not os.path.exists(file_path):
        return jsonify({'error': 'File not found or expired'}), 404

    return send_file(file_path, as_attachment=True, download_name=filename)


# ============================================================
# MAIN
# ============================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
