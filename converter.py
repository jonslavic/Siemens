#!/usr/bin/env python3
"""
Siemens LabVIEW .dat / .hed  ->  WATS Standard XML Format (WSXF) converter

Each row in a .dat file becomes one WSXF XML file that the WATS Client
can automatically import when dropped in its watch folder.

Usage:
    python converter.py <data_dir> [output_dir] [--dat FILE ...]

    data_dir   - folder containing .dat and .hed files
    output_dir - where to write XML files
                 (default: C:\\ProgramData\\Virinco\\WATS\\WatsStandardXMLFormat)
    --dat      - convert specific .dat files only; default converts all Data*.dat
"""

import argparse
import glob
import os
import re
import sys
import xml.etree.ElementTree as ET
from xml.dom import minidom

WATS_NS = "http://wats.virinco.com/schemas/WATS/Report/wsxf"

ET.register_namespace("", WATS_NS)

# First 17 columns of every row are fixed metadata.
METADATA_FIELDS = [
    "ASN", "DATECODE", "REVISION", "SER_NO", "LABEL", "TYPE", "DATA",
    "COUNTER", "DURATION", "DATE", "TIME", "FTID", "WTNR", "SIASN",
    "RESF1", "RESF2", "RESF3",
]


# ---------------------------------------------------------------------------
# .hed parsing
# ---------------------------------------------------------------------------

def parse_hed(path):
    """Return (columns, limits).

    columns : list of {'name': str, 'unit': str}
    limits  : dict  UPPERCASE_NAME -> {'nom': float|None, 'low': float|None, 'high': float|None}
    """
    columns = []
    limits  = {}
    in_params   = False
    current_col = None

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")

            if "<FILE WIDE PARAMETERS>" in line:
                in_params = True
                continue

            if not in_params:
                m = re.match(r'"([^"]+)","([^"]*)","([^"]*)","",""\s*$', line)
                if m:
                    name = m.group(1)
                    desc = m.group(3)
                    unit_m = re.search(r"\[([^\]]+)\]", desc)
                    columns.append({"name": name, "unit": unit_m.group(1) if unit_m else ""})
            else:
                s = line.strip()
                if re.match(r"^0 ", s):
                    current_col = s[2:].strip().upper()
                    limits.setdefault(current_col, {"nom": None, "low": None, "high": None})
                elif re.match(r"^5 ", s) and current_col:
                    try:
                        limits[current_col]["nom"] = float(s[2:].strip())
                    except ValueError:
                        pass
                elif re.match(r"^11 ", s) and current_col:
                    try:
                        limits[current_col]["high"] = float(s[3:].strip())
                    except ValueError:
                        pass
                elif re.match(r"^12 ", s) and current_col:
                    try:
                        limits[current_col]["low"] = float(s[3:].strip())
                    except ValueError:
                        pass

    return columns, limits


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_datetime(date_str, time_str):
    """'6/22/2026', '6:53:41_AM'  ->  '2026-06-22T06:53:41'"""
    time_clean = time_str.replace("_", " ")
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"):
        try:
            from datetime import datetime
            return datetime.strptime(f"{date_str} {time_clean}", fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass
    return "2000-01-01T00:00:00"


def numeric_status(value, lim):
    """Return 'Passed'/'Failed' for a measured value against an optional limit entry."""
    if not lim:
        return "Passed"
    low, high = lim.get("low"), lim.get("high")
    if low is None and high is None:
        return "Passed"
    try:
        v = float(value)
        if low  is not None and v < low:
            return "Failed"
        if high is not None and v > high:
            return "Failed"
        return "Passed"
    except ValueError:
        return "Passed"


def safe_name(s):
    """Strip characters that are awkward in file names."""
    return re.sub(r"[^\w.\-]", "_", s)


# ---------------------------------------------------------------------------
# XML generation
# ---------------------------------------------------------------------------

def _sub(parent, tag, **attrib):
    return ET.SubElement(parent, f"{{{WATS_NS}}}{tag}", attrib=attrib)


def build_wsxf(row, result_cols, limits, seq_name):
    """Return an ElementTree root for one test record.

    Correct WSXF structure (verified by reflecting Virinco.WATS.ClientAPI.dll):
      <Reports>
        <Report type="UUT" SN=... PN=... Rev=... Start=... Result=... MachineName=...>
          <UUT BatchSN=... TestSocketIndex=... ExecutionTime=... UserLoginName=.../>
          <Process Code=... Name=.../>
          <Step Name=... StepType="SequenceCall" Status=... total_time=...>
            <SequenceCall Name=... Filename=.../>
            <Step Name=... StepType="ET_NLT" Status=...>
              <NumericLimit Name=... NumericValue=... Units=... CompOperator=...
                            LowLimit=... HighLimit=... Status=.../>
            </Step>
            <Step Name=... StepType="ET_SVT" Status="Passed">
              <StringValue Name=... StringValue=... Status="Passed"/>
            </Step>
          </Step>
        </Report>
      </Reports>
    """
    label   = row.get("LABEL", "Passed")
    overall = "Passed" if label == "Passed" else "Failed"
    siasn   = row.get("SIASN", "")
    pn      = siasn if siasn not in ("*", "") else row.get("TYPE", "UNKNOWN")
    rev     = row.get("REVISION", "")
    if rev == "*":
        rev = ""
    wtnr = row.get("WTNR", "0")
    if wtnr == "*":
        wtnr = "0"
    duration = row.get("DURATION", "0")
    if duration == "*":
        duration = "0"

    root   = ET.Element(f"{{{WATS_NS}}}Reports")
    report = _sub(root, "Report",
                  type       = "UUT",
                  SN         = row.get("SER_NO", ""),
                  PN         = pn,
                  Rev        = rev,
                  Start      = parse_datetime(
                                   row.get("DATE", "01/01/2000"),
                                   row.get("TIME", "12:00:00")),
                  Result     = overall,
                  MachineName= row.get("FTID", ""),
                  Location   = "Production",
                  Purpose    = "Production Test",
    )

    _sub(report, "UUT",
         BatchSN         = row.get("ASN", ""),
         TestSocketIndex = wtnr,
         ExecutionTime   = duration,
         UserLoginName   = row.get("FTID", ""),
    )

    _sub(report, "Process",
         Code = "9997",
         Name = "Functional Test",
    )

    seq_step = _sub(report, "Step",
                    Name       = seq_name,
                    StepType   = "SequenceCall",
                    Status     = overall,
                    total_time = duration,
    )
    _sub(seq_step, "SequenceCall",
         Name     = seq_name,
         Filename = f"{seq_name}.seq",
    )

    for col in result_cols:
        value = row.get(col["name"], "*")
        if value == "*":
            continue

        try:
            float(value)
            is_numeric = True
        except ValueError:
            is_numeric = False

        if is_numeric:
            lim      = limits.get(col["name"].upper())
            s_status = numeric_status(value, lim)

            step = _sub(seq_step, "Step",
                        Name     = col["name"],
                        StepType = "ET_NLT",
                        Status   = s_status,
            )
            nl_attr = {
                "Name":         col["name"],
                "NumericValue": value,
                "Units":        col["unit"],
                "Status":       s_status,
            }
            if lim:
                low, high = lim.get("low"), lim.get("high")
                if low  is not None: nl_attr["LowLimit"]     = str(low)
                if high is not None: nl_attr["HighLimit"]    = str(high)
                if   low is not None and high is not None: nl_attr["CompOperator"] = "GELE"
                elif low is not None:                      nl_attr["CompOperator"] = "GE"
                elif high is not None:                     nl_attr["CompOperator"] = "LE"
            _sub(step, "NumericLimit", **nl_attr)
        else:
            step = _sub(seq_step, "Step",
                        Name     = col["name"],
                        StepType = "ET_SVT",
                        Status   = "Passed",
            )
            _sub(step, "StringValue",
                 Name        = col["name"],
                 StringValue = value,
                 Status      = "Passed",
            )

    return root


def pretty_bytes(root):
    """Serialize ElementTree root to indented UTF-8 XML bytes."""
    raw_str = ET.tostring(root, encoding="unicode")
    dom = minidom.parseString(raw_str.encode("utf-8"))
    return dom.toprettyxml(indent="  ", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert Siemens LabVIEW .dat/.hed files to WATS WSXF XML"
    )
    parser.add_argument("data_dir",
        help="Directory containing .dat and .hed files")
    parser.add_argument("output_dir", nargs="?",
        default=r"C:\ProgramData\Virinco\WATS\WatsStandardXMLFormat",
        help="Directory for output WSXF XML files")
    parser.add_argument("--dat", nargs="*",
        help="Specific .dat file(s) to convert; default converts all Data*.dat")
    args = parser.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    out_dir  = os.path.abspath(args.output_dir)
    os.makedirs(out_dir, exist_ok=True)

    # Load base column schema (1_Header.hed has no limits, just column names/units)
    base_hed = os.path.join(data_dir, "1_Header.hed")
    if not os.path.exists(base_hed):
        candidates = sorted(
            c for c in glob.glob(os.path.join(data_dir, "*.hed"))
            if not os.path.basename(c).startswith("Z")
            and ".hed." not in os.path.basename(c)
        )
        if not candidates:
            sys.exit("ERROR: no .hed schema file found in data_dir")
        base_hed = candidates[0]
        print(f"Using {os.path.basename(base_hed)} as base column schema")

    base_cols, _   = parse_hed(base_hed)
    all_col_names  = [c["name"] for c in base_cols]
    result_cols    = base_cols[len(METADATA_FIELDS):]  # columns after the 17 metadata fields
    expected_count = len(all_col_names)

    # Cache: siasn -> (seq_name, limits_dict)
    hed_cache = {}

    def get_seq_info(siasn):
        if siasn in hed_cache:
            return hed_cache[siasn]
        hed_name = siasn.replace(".", "_") + ".hed"
        hed_path = os.path.join(data_dir, hed_name)
        if os.path.exists(hed_path):
            _, lims = parse_hed(hed_path)
            seq = siasn.replace(".", "_")
        else:
            lims = {}
            seq  = siasn.replace(".", "_")
        hed_cache[siasn] = (seq, lims)
        return hed_cache[siasn]

    # Resolve .dat files
    if args.dat:
        dat_files = args.dat
    else:
        dat_files = sorted(glob.glob(os.path.join(data_dir, "Data*.dat")))

    if not dat_files:
        sys.exit("ERROR: no .dat files found")

    total_written = 0
    total_skipped = 0

    for dat_path in dat_files:
        fname = os.path.basename(dat_path)
        print(f"  {fname} ... ", end="", flush=True)
        file_written = 0

        with open(dat_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()

        for line in lines[1:]:          # line[0] is the display header — skip it
            line = line.rstrip("\n")
            if not line.strip():
                continue

            tokens = line.split()
            if len(tokens) != expected_count:
                total_skipped += 1
                continue

            row    = dict(zip(all_col_names, tokens))
            siasn  = row.get("SIASN", "*")
            if siasn in ("*", ""):
                siasn = row.get("TYPE", "UNKNOWN")

            seq_name, limits = get_seq_info(siasn)
            xml_root = build_wsxf(row, result_cols, limits, seq_name)
            xml_bytes = pretty_bytes(xml_root)

            date_part = safe_name(row.get("DATE", "01/01/2000").replace("/", ""))
            time_part = safe_name(
                row.get("TIME", "000000")
                .replace(":", "").replace("_AM", "").replace("_PM", "")
                .replace("_", "")
            )
            counter   = safe_name(row.get("COUNTER", "1"))
            out_name = (
                f"{safe_name(siasn)}"
                f"_{safe_name(row.get('SER_NO', 'UNK'))}"
                f"_{date_part}_{time_part}"
                f"_C{counter}.xml"
            )
            with open(os.path.join(out_dir, out_name), "wb") as fh_out:
                fh_out.write(xml_bytes)

            file_written  += 1
            total_written += 1

        print(f"{file_written} records")

    print(f"\nDone: {total_written} XML files written to {out_dir}")
    if total_skipped:
        print(f"      {total_skipped} rows skipped (unexpected token count)")


if __name__ == "__main__":
    main()
