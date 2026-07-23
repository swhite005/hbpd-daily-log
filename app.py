import re
import os
import io
import zipfile
import tempfile
from datetime import datetime
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS

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


def fill_placeholders(xml, values):
    """
    Replace en-space placeholder groups with values.
    Each form field placeholder is: fldCharType="separate"/></w:r>
    followed by one or more runs each containing a single en-space \u2002.
    We replace those trailing runs with a single run containing the value.
    """
    result = []
    pos = 0
    val_idx = 0

    sep_pattern = re.compile(r'fldCharType="separate"/></w:r>')
    en_run_pattern = re.compile(
        r'<w:r[^>]*>(?:<w:rPr>.*?</w:rPr>)?<w:t[^>]*>\u2002</w:t></w:r>',
        re.DOTALL
    )

    for sep_match in sep_pattern.finditer(xml):
        result.append(xml[pos:sep_match.end()])
        pos = sep_match.end()

        # Consume all consecutive en-space runs
        consumed_end = pos
        while True:
            en_match = en_run_pattern.match(xml, consumed_end)
            if en_match:
                consumed_end = en_match.end()
            else:
                break

        if val_idx < len(values):
            val = values[val_idx]
            val = val.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            result.append(
                f'<w:r><w:rPr><w:rStyle w:val="Style1Char"/></w:rPr>'
                f'<w:t xml:space="preserve">{val}</w:t></w:r>'
            )
            val_idx += 1
        else:
            result.append(xml[pos:consumed_end])

        pos = consumed_end

    result.append(xml[pos:])
    return ''.join(result)


def fill_template(prepared_by, date_str, incidents):
    with open(TEMPLATE_PATH, 'rb') as f:
        template_bytes = f.read()

    files = {}
    with zipfile.ZipFile(io.BytesIO(template_bytes), 'r') as zin:
        for item in zin.infolist():
            fname = item.filename
            files[fname] = zin.read(fname)

    # Fix Content_Types
    if '[Content_Types].xml' in files:
        files['[Content_Types].xml'] = files['[Content_Types].xml'].replace(
            b'wordprocessingml.template.main+xml',
            b'wordprocessingml.document.main+xml'
        )

    if 'word/document.xml' in files:
        xml = files['word/document.xml'].decode('utf-8')

        values = [prepared_by, date_str]
        for inc in incidents:
            values.extend([
                inc['time'],
                inc['location'],
                inc['dr'],
                inc['subject'],
                inc['details'],
            ])

        xml = fill_placeholders(xml, values)

        # Remove unfilled incident tables (still contain en-spaces)
        tbl_matches = list(re.finditer(r'<w:tbl[ >].*?</w:tbl>', xml, re.DOTALL))
        empty_spans = []
        for t in tbl_matches:
            if '\u2002' in t.group(0):
                empty_spans.append((t.start(), t.end()))
        for start, end in reversed(empty_spans):
            xml = xml[:start] + xml[end:]

        files['word/document.xml'] = xml.encode('utf-8')

    # Write to temp file to avoid in-memory zip corruption
    with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as tmp:
        tmp_path = tmp.name

    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zout:
            for fname, data in files.items():
                zout.writestr(fname, data)
        with open(tmp_path, 'rb') as f:
            return f.read()
    finally:
        os.unlink(tmp_path)


@app.route('/supervisors', methods=['GET'])
def get_supervisors():
    return jsonify(SUPERVISORS)


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
        docx_bytes = fill_template(prepared_by, date_str, incidents)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return send_file(
        io.BytesIO(docx_bytes),
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        as_attachment=True,
        download_name=filename
    )


if __name__ == '__main__':
    app.run(debug=True)
