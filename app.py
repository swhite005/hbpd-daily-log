import re
import os
import io
import zipfile
import tempfile
import shutil
import subprocess
from datetime import datetime
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "DAILY_LOG_TEMPLATE.dotx")
MERGE_RUNS_SCRIPT = os.path.join(os.path.dirname(__file__), "merge_runs.py")

SUPERVISORS = [
    "Lieutenant Shawn White",
    "Lieutenant John Smith",
    "Lieutenant Jane Doe",
    "Sergeant Mike Johnson",
]

def thousand_block(address):
    business_indicators = ["@", "(", "\u2013", "\u2014", "Hwy", "Park", "Beach", "Plaza",
                           "Channel", "Trail", "River", "Lake", "Pier", "Circle",
                           "School", "Market", "Store", "Hospital", "Library"]
    for indicator in business_indicators:
        if indicator in address:
            return address
    if re.search(r'\s[/&]\s', address):
        return address
    m = re.match(r'^(\d+)(.*)', address.strip())
    if not m:
        return address
    num = int(m.group(1))
    rest = m.group(2)
    rest = re.sub(r'\s*(#\S+|Apt\s+\S+|Unit\s+\S+)', '', rest, flags=re.IGNORECASE).strip()
    block = (num // 100) * 100
    return f"{block} Block of{rest}"


def parse_incidents(raw_text):
    incidents = []
    chunks = re.split(r'(?=DR#\s*[:])', raw_text, flags=re.IGNORECASE)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue

        def extract(label, text):
            pattern = rf'{label}\s*[:]\s*(.*?)(?=\n\s*(?:Time|Location|Subject|Details|Officers|Arrested|DR#)\s*[:]|\Z)'
            m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if m:
                return ' '.join(m.group(1).split()).strip()
            return "N/A"

        dr = extract('DR#', chunk)
        time_val = extract('Time', chunk)
        location = extract('Location', chunk)
        subject = extract('Subject', chunk)
        details = extract('Details', chunk)

        if dr == "N/A" and time_val == "N/A":
            continue

        location = thousand_block(location)
        details = re.sub(r'image\d+\.\w+', '', details, flags=re.IGNORECASE).strip()
        details = re.sub(r'\s{2,}', ' ', details)

        incidents.append({
            "dr": dr,
            "time": time_val,
            "location": location,
            "subject": subject,
            "details": details,
        })
    return incidents


def fill_template(prepared_by, date_str, incidents):
    work_dir = tempfile.mkdtemp()
    try:
        # 1. Copy template and unpack
        template_copy = os.path.join(work_dir, "template.docx")
        shutil.copy(TEMPLATE_PATH, template_copy)

        unpack_dir = os.path.join(work_dir, "unpacked")
        os.makedirs(unpack_dir)
        with zipfile.ZipFile(template_copy, 'r') as z:
            z.extractall(unpack_dir)

        # 2. Merge runs
        subprocess.run(
            ['python3', MERGE_RUNS_SCRIPT, unpack_dir + '/'],
            capture_output=True
        )

        # 3. Fix Content_Types
        ct_path = os.path.join(unpack_dir, '[Content_Types].xml')
        with open(ct_path, 'r') as f:
            ct = f.read()
        ct = ct.replace(
            'wordprocessingml.template.main+xml',
            'wordprocessingml.document.main+xml'
        )
        with open(ct_path, 'w') as f:
            f.write(ct)

        # 4. Fill placeholders
        doc_path = os.path.join(unpack_dir, 'word', 'document.xml')
        with open(doc_path, 'r', encoding='utf-8') as f:
            xml = f.read()

        # Count placeholders before filling
        placeholder_count = len(re.findall(r'fldCharType="separate"/>(<w:t>[^<]*</w:t>)', xml))

        values = [prepared_by, date_str]
        for inc in incidents:
            values.extend([
                inc['time'],
                inc['location'],
                inc['dr'],
                inc['subject'],
                inc['details'],
            ])

        placeholder_pattern = r'(fldCharType="separate"/>)<w:t>[^<]*</w:t>'
        idx = 0

        def replacer(m):
            nonlocal idx
            if idx < len(values):
                val = values[idx].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                idx += 1
                return f'{m.group(1)}<w:t xml:space="preserve">{val}</w:t>'
            return m.group(0)

        xml = re.sub(placeholder_pattern, replacer, xml)
        fields_filled = idx

        # 5. Remove unfilled incident tables
        tbl_matches = list(re.finditer(r'<w:tbl[ >].*?</w:tbl>', xml, re.DOTALL))
        empty_spans = [(t.start(), t.end()) for t in tbl_matches
                       if '\u2002\u2002\u2002\u2002\u2002' in t.group(0)]
        for start, end in reversed(empty_spans):
            xml = xml[:start] + xml[end:]

        with open(doc_path, 'w', encoding='utf-8') as f:
            f.write(xml)

        # 6. Repack
        out_path = os.path.join(work_dir, "output.docx")
        with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zout:
            for root, dirs, files in os.walk(unpack_dir):
                for file in files:
                    filepath = os.path.join(root, file)
                    arcname = os.path.relpath(filepath, unpack_dir)
                    zout.write(filepath, arcname)

        with open(out_path, 'rb') as f:
            return f.read(), fields_filled, placeholder_count

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.route('/supervisors', methods=['GET'])
def get_supervisors():
    return jsonify(SUPERVISORS)


@app.route('/debug', methods=['GET', 'POST'])
def debug():
    """Debug endpoint - returns info about what the server sees in the template."""
    body = request.get_json() or {}
    raw_text = body.get('text', 'DR#:\n26-000001\nTime:\n8:00 AM\nLocation:\nTest Location\nSubject:\nTest Subject\nDetails:\nTest details.')

    incidents = parse_incidents(raw_text)

    work_dir = tempfile.mkdtemp()
    try:
        template_copy = os.path.join(work_dir, "template.docx")
        shutil.copy(TEMPLATE_PATH, template_copy)
        unpack_dir = os.path.join(work_dir, "unpacked")
        os.makedirs(unpack_dir)
        with zipfile.ZipFile(template_copy, 'r') as z:
            z.extractall(unpack_dir)

        merge_result = subprocess.run(
            ['python3', MERGE_RUNS_SCRIPT, unpack_dir + '/'],
            capture_output=True, text=True
        )

        doc_path = os.path.join(unpack_dir, 'word', 'document.xml')
        with open(doc_path, 'r') as f:
            xml = f.read()

        placeholders = re.findall(r'fldCharType="separate"/>(<w:t>[^<]*</w:t>)', xml)
        en_spaces = xml.count('\u2002')
        sample = xml[xml.find('fldCharType="separate"'):xml.find('fldCharType="separate"')+200] if 'fldCharType="separate"' in xml else 'NOT FOUND'

        return jsonify({
            "merge_runs_output": merge_result.stdout,
            "merge_runs_script_exists": os.path.exists(MERGE_RUNS_SCRIPT),
            "template_exists": os.path.exists(TEMPLATE_PATH),
            "placeholder_count": len(placeholders),
            "en_space_count": en_spaces,
            "sample_context": sample,
            "incidents_parsed": len(incidents),
            "first_incident": incidents[0] if incidents else None,
        })
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.route('/generate', methods=['POST'])
def generate():
    body = request.get_json()
    if not body:
        return jsonify({"error": "No data provided"}), 400

    prepared_by = body.get('preparedBy', 'Lieutenant Shawn White')
    raw_text = body.get('text', '')

    if not raw_text.strip():
        return jsonify({"error": "No incident text provided"}), 400

    incidents = parse_incidents(raw_text)
    if not incidents:
        return jsonify({"error": "No incidents could be parsed from the text"}), 400

    if len(incidents) > 13:
        return jsonify({"error": f"Template supports 13 incidents max. Found {len(incidents)}."}), 400

    today = datetime.now()
    date_str = today.strftime("%B %-d, %Y")
    filename = today.strftime("%m-%d-%Y") + ".docx"

    try:
        docx_bytes, fields_filled, placeholder_count = fill_template(prepared_by, date_str, incidents)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if fields_filled == 0:
        return jsonify({
            "error": f"Template placeholders not found. placeholder_count={placeholder_count}. Deploy may need merge_runs.py."
        }), 500

    response = send_file(
        io.BytesIO(docx_bytes),
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        as_attachment=True,
        download_name=filename
    )
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.headers['X-Filename'] = filename
    return response


if __name__ == '__main__':
    app.run(debug=True)
