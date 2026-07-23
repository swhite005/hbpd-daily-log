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
    chunks = re.split(r'(?=DR#\s*[::])', raw_text, flags=re.IGNORECASE)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue

        def extract(label, text):
            pattern = rf'{label}\s*[::]\s*(.*?)(?=\n\s*(?:Time|Location|Subject|Details|Officers|Arrested|DR#)\s*[::.]|\Z)'
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


def merge_runs_in_xml(xml):
    """
    Merge consecutive <w:r> runs that have identical rPr so that
    en-space placeholder sequences end up in a single <w:t> node.
    This replicates what merge_runs.py does at build time.
    """
    # We target runs inside the same paragraph and merge their <w:t> text
    # when the rPr content is identical.
    def merge_para(para_xml):
        # Find all runs
        run_pattern = re.compile(r'<w:r(?:\s[^>]*)?>.*?</w:r>', re.DOTALL)
        runs = list(run_pattern.finditer(para_xml))
        if len(runs) < 2:
            return para_xml

        rpr_pattern = re.compile(r'(<w:rPr>.*?</w:rPr>)', re.DOTALL)
        t_pattern = re.compile(r'<w:t[^>]*>([^<]*)</w:t>', re.DOTALL)

        merged_runs = []
        i = 0
        while i < len(runs):
            current = runs[i].group(0)
            rpr_m = rpr_pattern.search(current)
            current_rpr = rpr_m.group(1) if rpr_m else ''
            t_m = t_pattern.search(current)
            current_text = t_m.group(1) if t_m else ''

            j = i + 1
            while j < len(runs):
                nxt = runs[j].group(0)
                nxt_rpr_m = rpr_pattern.search(nxt)
                nxt_rpr = nxt_rpr_m.group(1) if nxt_rpr_m else ''
                nxt_t_m = t_pattern.search(nxt)
                nxt_text = nxt_t_m.group(1) if nxt_t_m else ''

                # Only merge if rPr matches and both contain only en-spaces or text
                if nxt_rpr == current_rpr and nxt_t_m:
                    current_text += nxt_text
                    j += 1
                else:
                    break

            # Rebuild merged run
            if j > i + 1:
                space_attr = ' xml:space="preserve"' if ' ' in current_text else ''
                merged = f'<w:r>{current_rpr}<w:t{space_attr}>{current_text}</w:t></w:r>'
                merged_runs.append((runs[i].start(), runs[j-1].end(), merged))
            i = j

        # Apply replacements in reverse
        for start, end, replacement in reversed(merged_runs):
            para_xml = para_xml[:start] + replacement + para_xml[end:]

        return para_xml

    # Process paragraph by paragraph
    para_pattern = re.compile(r'<w:p[ >].*?</w:p>', re.DOTALL)
    def replace_para(m):
        return merge_para(m.group(0))
    return para_pattern.sub(replace_para, xml)


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

        # Merge split runs so en-space placeholders are in single <w:t> nodes
        xml = merge_runs_in_xml(xml)

        # Build values list
        values = [prepared_by, date_str]
        for inc in incidents:
            values.extend([
                inc['time'],
                inc['location'],
                inc['dr'],
                inc['subject'],
                inc['details'],
            ])

        # Try both placeholder patterns (merged and unmerged)
        filled = [False]
        idx = [0]

        def replacer(m):
            if idx[0] < len(values):
                val = values[idx[0]]
                val = val.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                idx[0] += 1
                filled[0] = True
                return f'{m.group(1)}<w:t xml:space="preserve">{val}</w:t>'
            return m.group(0)

        # Pattern 1: merged runs (after merge_runs_in_xml)
        pattern1 = r'(fldCharType="separate"/>)<w:t[^>]*>[\u2002\s]+</w:t>'
        xml = re.sub(pattern1, replacer, xml)

        # Pattern 2: separate/end on same run with en-spaces between
        if not filled[0]:
            pattern2 = r'(fldCharType="separate"/>)\s*</w:r>\s*<w:r[^>]*>(?:<w:rPr>.*?</w:rPr>)?<w:t[^>]*>(\u2002+)</w:t>'
            def replacer2(m):
                if idx[0] < len(values):
                    val = values[idx[0]]
                    val = val.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    idx[0] += 1
                    return f'{m.group(1)}</w:r><w:r><w:t xml:space="preserve">{val}</w:t>'
                return m.group(0)
            xml = re.sub(pattern2, replacer2, xml, flags=re.DOTALL)

        # Remove unfilled incident tables
        tbl_matches = list(re.finditer(r'<w:tbl[ >].*?</w:tbl>', xml, re.DOTALL))
        empty_spans = []
        for t in tbl_matches:
            # Check for any remaining en-spaces (unfilled placeholders)
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
