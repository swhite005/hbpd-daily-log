import re
import os
import io
import zipfile
import tempfile
import shutil
from datetime import datetime
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
from xml.dom import minidom

app = Flask(__name__)
CORS(app)

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "DAILY_LOG_TEMPLATE.dotx")

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


def merge_runs_xml(xml_bytes):
    """
    Inline run merger using minidom — no external dependencies.
    Merges adjacent runs with identical rPr so en-space placeholders
    end up in a single <w:t> node that our regex can match.
    """
    dom = minidom.parseString(xml_bytes)

    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def get_rpr_xml(run):
        for child in run.childNodes:
            if child.nodeType == child.ELEMENT_NODE and child.localName == 'rPr':
                return child.toxml()
        return ''

    def get_t_text(run):
        texts = []
        for child in run.childNodes:
            if child.nodeType == child.ELEMENT_NODE and child.localName == 't':
                for tc in child.childNodes:
                    if tc.nodeType in (tc.TEXT_NODE, tc.CDATA_SECTION_NODE):
                        texts.append(tc.data)
        return ''.join(texts)

    def has_only_t(run):
        """Run contains only rPr and t elements (no fldChar, tab, etc.)"""
        for child in run.childNodes:
            if child.nodeType == child.ELEMENT_NODE:
                if child.localName not in ('rPr', 't'):
                    return False
        return True

    # Process each paragraph
    for para in dom.getElementsByTagNameNS(W, 'p'):
        children = [c for c in para.childNodes if c.nodeType == c.ELEMENT_NODE]
        i = 0
        while i < len(children):
            run = children[i]
            if run.localName != 'r' or not has_only_t(run):
                i += 1
                continue

            rpr = get_rpr_xml(run)
            merged_text = get_t_text(run)
            j = i + 1

            while j < len(children):
                nxt = children[j]
                if nxt.localName != 'r' or not has_only_t(nxt):
                    break
                if get_rpr_xml(nxt) != rpr:
                    break
                merged_text += get_t_text(nxt)
                j += 1

            if j > i + 1:
                # Replace run's <w:t> with merged text
                for child in list(run.childNodes):
                    if child.nodeType == child.ELEMENT_NODE and child.localName == 't':
                        run.removeChild(child)

                new_t = dom.createElementNS(W, 'w:t')
                if merged_text != merged_text.strip():
                    new_t.setAttribute('xml:space', 'preserve')
                new_t.appendChild(dom.createTextNode(merged_text))
                run.appendChild(new_t)

                # Remove the consumed runs
                for k in range(i + 1, j):
                    para.removeChild(children[k])

                children = [c for c in para.childNodes if c.nodeType == c.ELEMENT_NODE]

            i += 1

    return dom.toxml(encoding='utf-8')


def fill_template(prepared_by, date_str, incidents):
    with open(TEMPLATE_PATH, 'rb') as f:
        template_bytes = f.read()

    files = {}
    with zipfile.ZipFile(io.BytesIO(template_bytes), 'r') as zin:
        for item in zin.infolist():
            files[item.filename] = zin.read(item.filename)

    # Fix Content_Types
    files['[Content_Types].xml'] = files['[Content_Types].xml'].replace(
        b'wordprocessingml.template.main+xml',
        b'wordprocessingml.document.main+xml'
    )

    # Merge runs then fill
    xml_bytes = merge_runs_xml(files['word/document.xml'])
    xml = xml_bytes.decode('utf-8') if isinstance(xml_bytes, bytes) else xml_bytes

    # Strip XML declaration if present (minidom adds it)
    xml = re.sub(r'^<\?xml[^?]*\?>', '', xml).strip()

    values = [prepared_by, date_str]
    for inc in incidents:
        values.extend([
            inc['time'],
            inc['location'],
            inc['dr'],
            inc['subject'],
            inc['details'],
        ])

    # After merge, pattern is: fldCharType="separate"/></w:r> followed by a run with en-spaces
    placeholder_pattern = r'(fldCharType="separate"/></w:r>)<w:r[^>]*>(?:<w:rPr>.*?</w:rPr>)?<w:t[^>]*>[\u2002]+</w:t></w:r>'
    idx = 0

    def replacer(m):
        nonlocal idx
        if idx < len(values):
            val = values[idx].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            idx += 1
            return f'{m.group(1)}<w:r><w:rPr><w:rStyle w:val="Style1Char"/></w:rPr><w:t xml:space="preserve">{val}</w:t></w:r>'
        return m.group(0)

    xml = re.sub(placeholder_pattern, replacer, xml, flags=re.DOTALL)

    # Remove unfilled tables
    tbl_matches = list(re.finditer(r'<w:tbl[ >].*?</w:tbl>', xml, re.DOTALL))
    empty_spans = [(t.start(), t.end()) for t in tbl_matches
                   if '\u2002' in t.group(0)]
    for start, end in reversed(empty_spans):
        xml = xml[:start] + xml[end:]

    files['word/document.xml'] = xml.encode('utf-8')

    # Write to temp file
    with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as tmp:
        tmp_path = tmp.name

    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zout:
            for fname, data in files.items():
                zout.writestr(fname, data)
        with open(tmp_path, 'rb') as f:
            return f.read(), idx
    finally:
        os.unlink(tmp_path)


@app.route('/supervisors', methods=['GET'])
def get_supervisors():
    return jsonify(SUPERVISORS)


@app.route('/debug', methods=['GET', 'POST'])
def debug():
    body = request.get_json(silent=True) or {}

    with open(TEMPLATE_PATH, 'rb') as f:
        template_bytes = f.read()

    files = {}
    with zipfile.ZipFile(io.BytesIO(template_bytes), 'r') as zin:
        for item in zin.infolist():
            files[item.filename] = zin.read(item.filename)

    # Before merge
    xml_raw = files['word/document.xml'].decode('utf-8')
    ph = r'fldCharType="separate"/></w:r><w:r[^>]*>(?:<w:rPr>.*?</w:rPr>)?<w:t[^>]*>[\u2002]+</w:t></w:r>'
    before_count = len(re.findall(ph, xml_raw, re.DOTALL))
    before_en = xml_raw.count('\u2002')

    # After merge
    xml_bytes = merge_runs_xml(files['word/document.xml'])
    xml_merged = xml_bytes.decode('utf-8') if isinstance(xml_bytes, bytes) else xml_bytes
    xml_merged = re.sub(r'^<\?xml[^?]*\?>', '', xml_merged).strip()
    after_count = len(re.findall(ph, xml_merged, re.DOTALL))
    after_en = xml_merged.count('\u2002')

    sample_before = xml_raw[xml_raw.find('fldCharType="separate"'):xml_raw.find('fldCharType="separate"')+200]
    sample_after = xml_merged[xml_merged.find('fldCharType="separate"'):xml_merged.find('fldCharType="separate"')+200] if 'fldCharType="separate"' in xml_merged else 'NOT FOUND'

    return jsonify({
        "template_exists": True,
        "before_merge": {"placeholder_count": before_count, "en_space_count": before_en},
        "after_merge": {"placeholder_count": after_count, "en_space_count": after_en},
        "sample_before": sample_before,
        "sample_after": sample_after,
    })


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
        docx_bytes, fields_filled = fill_template(prepared_by, date_str, incidents)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    response = send_file(
        io.BytesIO(docx_bytes),
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        as_attachment=True,
        download_name=filename
    )
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


if __name__ == '__main__':
    app.run(debug=True)
