import re
import os
import io
import zipfile
from datetime import datetime
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "DAILY_LOG_TEMPLATE.dotx")

SUPERVISORS = [
    "Lieutenant Shawn White",
    "Lieutenant John Smith",       # Replace with real names
    "Lieutenant Jane Doe",         # Replace with real names
    "Sergeant Mike Johnson",       # Replace with real names
]

def thousand_block(address):
    """Convert residential address to thousand-block format."""
    # If it contains a business indicator, leave as-is
    business_indicators = ["@", "(", "–", "-", "Hwy", "Park", "Beach", "Plaza",
                           "Channel", "Trail", "River", "Lake", "Pier", "Circle",
                           "School", "Market", "Store", "Hospital", "Library"]
    for indicator in business_indicators:
        if indicator in address:
            return address

    # Check if it looks like a pure intersection (contains / or &)
    if re.search(r'\s[/&]\s', address):
        return address

    # Match a leading street number
    m = re.match(r'^(\d+)(.*)', address.strip())
    if not m:
        return address

    num = int(m.group(1))
    rest = m.group(2)

    # Remove unit/apt suffixes (#123, Apt B, #B, etc.)
    rest = re.sub(r'\s*(#\S+|Apt\s+\S+|Unit\s+\S+)', '', rest, flags=re.IGNORECASE).strip()

    # Round down to nearest hundred for thousand-block
    block = (num // 100) * 100
    return f"{block} Block of{rest}"


def parse_incidents(raw_text):
    """Parse pasted incident text into structured list of dicts."""
    incidents = []
    # Split on DR# markers
    chunks = re.split(r'(?=DR#\s*[:：])', raw_text, flags=re.IGNORECASE)

    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue

        def extract(label, text):
            pattern = rf'{label}\s*[:：]\s*(.*?)(?=\n\s*(?:Time|Location|Subject|Details|Officers|Arrested|DR#)\s*[:：]|\Z)'
            m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if m:
                return ' '.join(m.group(1).split()).strip()
            return "N/A"

        dr = extract('DR#', chunk)
        time = extract('Time', chunk)
        location = extract('Location', chunk)
        subject = extract('Subject', chunk)
        details = extract('Details', chunk)

        if dr == "N/A" and time == "N/A":
            continue

        # Apply thousand-block to residential addresses
        location = thousand_block(location)

        # Remove image references from details
        details = re.sub(r'image\d+\.\w+', '', details, flags=re.IGNORECASE).strip()
        details = re.sub(r'\s{2,}', ' ', details)

        incidents.append({
            "dr": dr,
            "time": time,
            "location": location,
            "subject": subject,
            "details": details,
        })

    return incidents


def merge_runs(xml_content):
    """Merge consecutive runs with identical formatting (simplified version)."""
    # This handles the en-space placeholder merging
    pattern = re.compile(
        r'(<w:r(?:\s[^>]*)?>(?:<w:rPr>.*?</w:rPr>)?)<w:t[^>]*>([^<]*)</w:t></w:r>'
        r'(?:\s*<w:r(?:\s[^>]*)?>(?:<w:rPr>.*?</w:rPr>)?<w:t[^>]*>([^<]*)</w:t></w:r>)+',
        re.DOTALL
    )
    return xml_content


def fill_template(prepared_by, date_str, incidents):
    """Fill the .dotx template and return docx bytes."""
    # Read the template
    with open(TEMPLATE_PATH, 'rb') as f:
        template_bytes = f.read()

    # Unpack
    buf = io.BytesIO(template_bytes)
    out_buf = io.BytesIO()

    with zipfile.ZipFile(buf, 'r') as zin:
        with zipfile.ZipFile(out_buf, 'w', zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.name)

                if item.name == '[Content_Types].xml':
                    data = data.replace(
                        b'wordprocessingml.template.main+xml',
                        b'wordprocessingml.document.main+xml'
                    )

                elif item.name == 'word/document.xml':
                    xml = data.decode('utf-8')

                    # Merge runs containing en-space sequences
                    xml = re.sub(
                        r'<w:t[^>]*>\u2002</w:t></w:r>\s*<w:r[^>]*>(?:<w:rPr>.*?</w:rPr>)?<w:t[^>]*>\u2002</w:t></w:r>',
                        lambda m: m.group(0),
                        xml,
                        flags=re.DOTALL
                    )

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

                    # Fill placeholders
                    placeholder_pattern = r'(fldCharType="separate"/>)<w:t>[^<]*</w:t>'
                    idx = 0

                    def replacer(m):
                        nonlocal idx
                        if idx < len(values):
                            val = values[idx]
                            val = val.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                            idx += 1
                            return f'{m.group(1)}<w:t xml:space="preserve">{val}</w:t>'
                        return m.group(0)

                    xml = re.sub(placeholder_pattern, replacer, xml)

                    # Remove unfilled incident tables
                    tbl_matches = list(re.finditer(r'<w:tbl[ >].*?</w:tbl>', xml, re.DOTALL))
                    empty_spans = []
                    for t in tbl_matches:
                        fields_in_tbl = re.findall(
                            r'fldCharType="separate"/><w:t>([^<]*)</w:t>', t.group(0)
                        )
                        if any('\u2002' in f for f in fields_in_tbl):
                            empty_spans.append((t.start(), t.end()))
                    for start, end in reversed(empty_spans):
                        xml = xml[:start] + xml[end:]

                    data = xml.encode('utf-8')

                zout.writestr(item, data)

    return out_buf.getvalue()


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

    date_str = datetime.now().strftime("%-d, %Y")
    month = datetime.now().strftime("%B")
    date_str = f"{month} {date_str}"
    filename = datetime.now().strftime("%m-%d-%Y") + ".docx"

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
