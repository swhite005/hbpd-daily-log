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


def thousand_block(address, changelog=None):
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
    converted = f"{block} Block of {rest}"
    if changelog is not None and converted != address:
        changelog.append(f"Address converted: \"{address}\" → \"{converted}\"")
    return converted


def time_sort_key(incident):
    """
    Convert time string to sortable minutes value.
    Night-watch AM entries (after midnight) are pushed after all PM entries
    by adding 24*60 to their value so they sort to the end.
    """
    time_str = incident.get('time', 'N/A')
    is_night_am = incident.get('_night_am', False)

    if not time_str or time_str in ('N/A', 'Daywatch', 'Day Watch'):
        return 1  # sort to front

    # Handle ranges like "6:00 PM – 12:00 AM" — use the start time
    time_str = re.split(r'\s*[–\-]\s*', time_str)[0].strip()

    m = re.search(r'(\d{1,2}):(\d{2})\s*(AM|PM)', time_str, re.IGNORECASE)
    if not m:
        return 9998

    h, mn, period = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if period == 'PM' and h != 12:
        h += 12
    elif period == 'AM' and h == 12:
        h = 0

    minutes = h * 60 + mn

    # Night-watch AM entries come after all PM entries
    if is_night_am:
        minutes += 24 * 60

    return minutes


def parse_incidents(raw_text):
    incidents = []
    changelog = []

    # Normalize Windows-1252 / C1 control dash characters
    raw_text = (raw_text
        .replace('\x96', '\u2013').replace('\x97', '\u2014')
        .replace('\u0096', '\u2013').replace('\u0097', '\u2014')
        .replace('\ufffd', '\u2014')
    )
    # Extract any update text that appears BEFORE the first DR#
    pre_dr_text = ''
    first_dr_pos = re.search(r'DR#\s*:', raw_text, re.IGNORECASE)
    if first_dr_pos and first_dr_pos.start() > 0:
        pre_dr_text = raw_text[:first_dr_pos.start()].strip()

    chunks = re.split(r'(?=DR#\s*[:]?\s*\r?\n)', raw_text, flags=re.IGNORECASE)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue

        def extract(label, text):
            pattern = rf'{label}\s*[:]?\s*\r?\n(.*?)(?=\n\s*(?:Time|Location|Subject|Details|Officers|Arrested|DR#)\s*[:]?\s*\r?\n|\Z)'
            m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if m:
                return ' '.join(m.group(1).split()).strip()
            return "N/A"

        def extract_details(text):
            pattern = r'Details\s*[:]?\s*\r?\n(.*?)(?=\n\s*(?:Officers|Arrested|DR#)\s*[:]?\s*\r?\n|\Z)'
            m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if not m:
                return "N/A"
            raw = m.group(1)
            # Preserve paragraph breaks — collapse whitespace within each paragraph
            # but keep blank lines between paragraphs
            paragraphs = re.split(r'\n\s*\n', raw)
            cleaned = []
            for para in paragraphs:
                para = para.strip()
                if para:
                    # Collapse newlines within a paragraph to single space
                    para = ' '.join(para.split())
                    cleaned.append(para)
            return '\n\n'.join(cleaned).strip()

        dr = extract('DR#', chunk)
        dr = re.sub(r'[,;\s]+$', '', dr)  # strip trailing comma/semicolon from DR#
        time_val = extract('Time', chunk)
        location = extract('Location', chunk)
        subject = extract('Subject', chunk)
        details = extract_details(chunk)

        # Skip only if there is truly no identifying information at all
        if dr == "N/A" and time_val == "N/A" and subject == "N/A":
            continue

        original_location = location
        location = thousand_block(location, changelog)
        orig_details = details
        details = re.sub(r'image\d+\.\w+', '', details, flags=re.IGNORECASE).strip()
        details = re.sub(r'\[cid:[^\]]+\]', '', details).strip()
        details = re.sub(r'\n{3,}', '\n\n', details).strip()

        incidents.append({
            "dr": dr,
            "time": time_val,
            "location": location,
            "subject": subject,
            "details": details,
        })

    # ── Sort all incidents chronologically ──────────────────────────────────
    # Strategy: sort by time, treating AM entries that appear AFTER PM entries
    # in the original list as "after midnight" — they sort after all PM entries.
    # This correctly handles:
    #   - Day watch AM entries (sort to front)
    #   - Night watch PM entries (sort in middle)
    #   - Night watch AM entries crossing midnight (sort to end)

    def to_minutes(time_str):
        if not time_str or time_str in ('N/A', 'Daywatch', 'Day Watch'):
            return None
        time_str = re.split(r'\s*[–\-]\s*', time_str)[0].strip()
        m = re.search(r'(\d{1,2}):(\d{2})\s*(AM|PM)', time_str, re.IGNORECASE)
        if not m:
            return None
        h, mn, period = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        if period == 'PM' and h != 12:
            h += 12
        elif period == 'AM' and h == 12:
            h = 0
        return h * 60 + mn

    # Find the last PM time position to identify after-midnight AM entries
    last_pm_idx = -1
    for i, inc in enumerate(incidents):
        t = inc.get('time', '')
        if re.search(r'PM', t, re.IGNORECASE):
            last_pm_idx = i

    # Tag any AM entry that appears AFTER the last PM entry as after-midnight
    for i, inc in enumerate(incidents):
        if i > last_pm_idx:
            t = inc.get('time', '')
            m = re.search(r'(\d{1,2}):(\d{2})\s*AM', t, re.IGNORECASE)
            if m:
                h = int(m.group(1))
                # h==12 means 12:xx AM = midnight itself — still after midnight
                inc['_night_am'] = True

    # Sort: normal minutes for all, +24h for after-midnight entries
    original_order = [i['dr'] for i in incidents]
    incidents.sort(key=lambda i: (
        (to_minutes(i.get('time', '')) or 1) + (24 * 60 if i.get('_night_am') else 0)
    ))
    sorted_order = [i['dr'] for i in incidents]

    if original_order != sorted_order:
        changelog.append("Entries reordered chronologically by time")
    if any(i.get('_night_am') for i in incidents):
        n = sum(1 for i in incidents if i.get('_night_am'))
        changelog.append(f"{n} after-midnight entry(ies) kept at end of log")

    # Deduplicate: if the same DR# appears more than once, merge the entries.
    # The first occurrence is kept as the base; subsequent occurrences append
    # updated details under an "Update:" heading rather than creating a duplicate.
    seen = {}      # dr -> index in deduped list
    deduped = []
    for inc in incidents:
        dr = inc['dr']
        # Normalize DR# for comparison (strip spaces)
        dr_key = re.sub(r'\s+', '', dr).upper()

        if dr_key in seen:
            existing = deduped[seen[dr_key]]
            update_details = inc['details']
            if update_details and update_details != "N/A":
                if existing['details'] and existing['details'] != "N/A":
                    norm_existing = re.sub(r'\s+', ' ', existing['details']).strip()
                    norm_update = re.sub(r'\s+', ' ', update_details).strip()
                    if norm_update != norm_existing:
                        existing['details'] += '\n\nUpdate: ' + update_details
                        changelog.append(f"DR# {inc['dr']}: duplicate entry merged — update appended to original")
                    else:
                        changelog.append(f"DR# {inc['dr']}: duplicate entry removed — identical to existing entry")
                else:
                    existing['details'] = update_details
            if inc['subject'] != "N/A" and inc['subject'] != existing['subject']:
                existing['subject'] = inc['subject']
        else:
            seen[dr_key] = len(deduped)
            deduped.append(inc)

    if pre_dr_text:
        ref_dr = re.search(r'26-\d+', pre_dr_text)
        if ref_dr:
            ref_key = re.sub(r'\s+', '', ref_dr.group(0)).upper()
            if ref_key in seen:
                existing = deduped[seen[ref_key]]
                clean_pre = re.sub(r'\s{2,}', ' ', pre_dr_text).strip()
                if existing['details'] and existing['details'] != 'N/A':
                    existing['details'] += '\n\nUpdate: ' + clean_pre
                else:
                    existing['details'] = 'Update: ' + clean_pre
                changelog.append(f"DR# {ref_dr.group(0)}: update note from email header appended to entry")

    # Remove internal sort tags before returning
    for inc in deduped:
        inc.pop('_night_am', None)

    return deduped, changelog


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
    MERGE_RUNS_SCRIPT = os.path.join(os.path.dirname(__file__), "merge_runs.py")

    work_dir = tempfile.mkdtemp()
    try:
        # 1. Copy template and unpack to disk
        template_copy = os.path.join(work_dir, "template.docx")
        shutil.copy(TEMPLATE_PATH, template_copy)
        unpack_dir = os.path.join(work_dir, "unpacked")
        os.makedirs(unpack_dir)
        with zipfile.ZipFile(template_copy, 'r') as z:
            z.extractall(unpack_dir)

        # 2. Run merge_runs.py to consolidate split XML runs
        subprocess.run(
            ['python3', MERGE_RUNS_SCRIPT, unpack_dir + '/'],
            capture_output=True
        )

        # 3. Fix Content_Types
        ct_path = os.path.join(unpack_dir, '[Content_Types].xml')
        with open(ct_path, 'r') as f:
            ct = f.read()
        ct = ct.replace('wordprocessingml.template.main+xml', 'wordprocessingml.document.main+xml')
        with open(ct_path, 'w') as f:
            f.write(ct)

        # 4. Read and fill document.xml
        doc_path = os.path.join(unpack_dir, 'word', 'document.xml')
        with open(doc_path, 'r', encoding='utf-8') as f:
            xml = f.read()

        values = [prepared_by, date_str]
        for inc in incidents:
            values.extend([
                inc['time'],
                inc['location'],
                inc['dr'],
                inc['subject'],
                inc['details'],
            ])

        # Try both placeholder patterns — merged (post merge_runs) and unmerged
        # Pattern A: after merge_runs.py — fldChar and w:t in same run
        pattern_a = r'(fldCharType="separate"/>)<w:t>[^<]*</w:t>'
        # Pattern B: fldChar run followed by separate en-space run (minidom merge)
        pattern_b = r'(fldCharType="separate"/></w:r>)<w:r[^>]*>(?:<w:rPr>.*?</w:rPr>)?<w:t[^>]*>[\u2002]+</w:t></w:r>'

        matches_a = len(re.findall(pattern_a, xml))
        matches_b = len(re.findall(pattern_b, xml, re.DOTALL))

        idx = 0

        def make_run(text):
            paragraphs = text.split('\n\n')
            parts = []
            for i, para in enumerate(paragraphs):
                escaped = para.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                parts.append(f'<w:t xml:space="preserve">{escaped}</w:t>')
                if i < len(paragraphs) - 1:
                    parts.append('<w:br/><w:br/>')
            return f'<w:r><w:rPr><w:rStyle w:val="Style1Char"/></w:rPr>{"".join(parts)}</w:r>'

        if matches_a >= matches_b:
            # Use pattern A — simple replacement
            def replacer_a(m):
                nonlocal idx
                if idx < len(values):
                    val = values[idx]
                    val = val.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    idx += 1
                    return f'{m.group(1)}<w:t xml:space="preserve">{val}</w:t>'
                return m.group(0)
            xml = re.sub(pattern_a, replacer_a, xml)
        else:
            # Use pattern B
            def replacer_b(m):
                nonlocal idx
                if idx < len(values):
                    val = values[idx]
                    idx += 1
                    return f'{m.group(1)}{make_run(val)}'
                return m.group(0)
            xml = re.sub(pattern_b, replacer_b, xml, flags=re.DOTALL)

        # 5. Remove unfilled incident tables
        tbl_matches = list(re.finditer(r'<w:tbl[ >].*?</w:tbl>', xml, re.DOTALL))
        empty_spans = [(t.start(), t.end()) for t in tbl_matches if '\u2002' in t.group(0)]
        for start, end in reversed(empty_spans):
            xml = xml[:start] + xml[end:]

        # 6. Remove trailing empty paragraphs after the last table to prevent blank final page
        # BUT preserve the sectPr (section properties) which contains the header/footer references
        last_tbl = list(re.finditer(r'<w:tbl[ >].*?</w:tbl>', xml, re.DOTALL))
        if last_tbl:
            last_tbl_end = last_tbl[-1].end()
            body_close = xml.rfind('</w:body>')
            if body_close > last_tbl_end:
                trailing = xml[last_tbl_end:body_close]

                # Extract sectPr — must be preserved (contains header/footer refs)
                sect_match = re.search(r'<w:sectPr[ >].*?</w:sectPr>', trailing, re.DOTALL)
                sect_xml = sect_match.group(0) if sect_match else ''

                # Count empty paragraphs (no <w:t> content)
                all_paras = list(re.finditer(r'<w:p[ >].*?</w:p>', trailing, re.DOTALL))
                empty_paras = [p for p in all_paras if not re.search(r'<w:t[^>]*>[^<]+</w:t>', p.group(0))]

                # Only strip if there are multiple empty paragraphs (keep 1 + sectPr)
                if len(empty_paras) > 1:
                    # Keep one minimal empty paragraph + the sectPr
                    minimal_para = empty_paras[0].group(0)
                    xml = xml[:last_tbl_end] + minimal_para + sect_xml + xml[body_close:]

        with open(doc_path, 'w', encoding='utf-8') as f:
            f.write(xml)

        # 6. Repack
        out_path = os.path.join(work_dir, "output.docx")
        with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zout:
            for root, dirs, files_list in os.walk(unpack_dir):
                for file in files_list:
                    filepath = os.path.join(root, file)
                    arcname = os.path.relpath(filepath, unpack_dir)
                    zout.write(filepath, arcname)

        with open(out_path, 'rb') as f:
            return f.read(), idx

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


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
    # Use force=True and silent=True then manually decode raw bytes
    # to handle Windows-1252 encoded chars (like em-dash 0x97) from Outlook
    try:
        body = request.get_json(force=True)
    except Exception:
        body = None

    if body is None:
        # Try decoding raw bytes as windows-1252 which maps 0x97 -> em-dash
        try:
            raw_bytes = request.get_data()
            import json
            decoded_str = raw_bytes.decode('windows-1252')
            body = json.loads(decoded_str)
        except Exception:
            return jsonify({"error": "No data provided"}), 400

    prepared_by = body.get('preparedBy', 'Lieutenant Shawn White')
    raw_text = body.get('text', '')

    if not raw_text.strip():
        return jsonify({"error": "No incident text provided"}), 400

    incidents, changelog = parse_incidents(raw_text)
    if not incidents:
        return jsonify({"error": "No incidents could be parsed from the text"}), 400

    if len(incidents) > 20:
        return jsonify({"error": f"Template supports 20 incidents max. Found {len(incidents)}."}), 400

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
    response.headers['Access-Control-Expose-Headers'] = 'Content-Disposition, X-Filename, X-Changelog'
    response.headers['X-Filename'] = filename
    import json as _json
    response.headers['X-Changelog'] = _json.dumps(changelog)
    return response


if __name__ == '__main__':
    app.run(debug=True)
