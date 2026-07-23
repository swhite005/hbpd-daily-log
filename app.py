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
    "Lieutenant John Smith",
    "Lieutenant Jane Doe",
    "Sergeant Mike Johnson",
]

def thousand_block(address):
    business_indicators = ["@", "(", "\u2013", "\u2014", "-", "Hwy", "Park", "Beach", "Plaza",
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
        time = extract('Time', chunk)
        location = extract('Location', chunk)
        subject = extract('Subject', chunk)
        details = extract('Details', chunk)

        if dr == "N/A" and time == "N/A":
            continue

        location = thousand_block(location)
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


def fill_template(prepared_by, date_str, incidents):
    with open(TEMPLATE_PATH, 'rb') as f:
        template_bytes = f.read()

    buf = io.BytesIO(template_bytes)
    out_buf = io.BytesIO()

    with zipfile.ZipFile(buf, 'r') as zin:
        with zipfile.ZipFile(out_buf, 'w', zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                # Support both .name and .filename across Python versions
                item_name = item.filename if hasattr(item, 'filename') else item.name
                data = zin.read(item_name)

                if item_name == '[Content_Types].xml':
                    data = data.replace(
                        b'wordprocessingml.template.main+xml',
                        b'wordprocessingml.document.main+xml'
                    )

                elif item_name == 'word/document.xml':
                    xml = data.decode('utf-8')

                    # Merge split en-space runs into single placeholder
                    def merge_en_spaces(x):
                        prev = None
                        while prev != x:
                            prev = x
                            x = re.sub(
                                r'(<w:t[^>]*>\u2002+</w:t></w:r>)\s*(<w:r[^>]*>(?:<w:rPr>.*?</w:rPr>)?<w:t[^>]*>)(\u2002+)(</w:t></w:r>)',
                                lambda m: m.group(1)[:-len('</w:t></w:r>')] + m.group(3) + '</w:t></w:r>',
                                x, flags=re.DOTALL
                            )
                        return x

                    xml = merge_en_spaces(xml)

                    values = [prepared_by, date_str]
                    for inc in incidents:
                        values.extend([
                            inc['time'],
                            inc['location'],
                            inc['dr'],
                            inc['subject'],
                            inc['details'],
                        ])

                    placeholder_pattern = r'(fldCharType="separate"/>)\s*<w:t[^>]*>\s*\u2002+\s*</w:t>'
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
                            r'fldCharType="separate"/>\s*<w:t[^>]*>\s*([^<]*)\s*</w:t>', t.group(0)
                        )
                        if fields_in_tbl and any('\u2002' in f for f in fields_in_tbl):
                            empty_spans.append((t.start(), t.end()))
                    for start, end in reversed(empty_spans):
                        xml = xml[:start] + xml[end:]

                    data = xml.encode('utf-8')

                zout.writestr(item_name, data)

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
