#!/usr/bin/env python3
r"""Export logged XML payloads from SAP PO. Requires Python 3.10+, no packages.

Example (PowerShell; replace the placeholders):
  python .\sap_po_download.py --url "https://YOUR-HOST:50001" `
    --scenario-name "YOUR_ICO_NAME" `
    --from "2026-11-17T09:00:00+01:00" --to "2026-11-17T10:00:00+01:00"

Username/password are prompted locally. For unattended use, pass --output and set
SAP_PO_USERNAME and SAP_PO_PASSWORD using your secret-management tool.
Use --ca-file company-ca.pem if your PO certificate uses a private CA.

The exact ICO name is resolved to its ID using getIntegrationFlows. Use
--scenario-id instead to search by a known technical ID without the lookup.
Only the first nonempty log location returned by PO is downloaded per matching
message. Request and response XML are exported if present in the selected
versions; the downloader does not fetch later versions or pair messages.
XML parts are saved without reformatting; XI control envelopes and non-XML parts
are not exported. Staged and archived messages are not downloaded.
Searches reaching --limit (default 100) stop before downloading: narrow the time
range or explicitly increase the limit. There is no automatic pagination.
When --output is omitted, enter a folder path or press Enter to use .\payloads.
XML files are saved directly there as ScenarioName_yyyymmdd_hhmmss.xml,
using the message's SAP PO startTime as returned, without timezone conversion.
With --scenario-id, the ID is used in place of the name. If startTime is missing
or invalid, the filename uses unknown-start. Collisions receive a numeric suffix.
Use an access-controlled folder with an appropriate retention policy.
Exit codes: 0 = success/no matches, 1 = failed or incomplete, 2 = invalid arguments.
"""

import argparse
import base64
import binascii
from datetime import datetime
from email import policy
from email.parser import BytesParser
import getpass
import os
from pathlib import Path
import re
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
import xml.etree.ElementTree as ET

SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
OP = "urn:AdapterMessageMonitoringVi"
DATA = "urn:com.sap.aii.mdt.server.adapterframework.ws"
JAVA = "urn:java/lang"
XI = "http://sap.com/xi/XI/Message/30"
XSI = "http://www.w3.org/2001/XMLSchema-instance"


class DownloadError(Exception):
    pass


def element(parent, namespace, name, value=None):
    child = ET.SubElement(parent, f"{{{namespace}}}{name}")
    if value is not None:
        child.text = str(value)
    return child


def envelope(operation):
    root = ET.Element(f"{{{SOAP}}}Envelope")
    body = element(root, SOAP, "Body")
    return root, element(body, OP, operation)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DownloadError(
            "The endpoint redirected the request. Use the direct HTTPS SOAP "
            "endpoint, not a browser login URL. Credentials were not forwarded."
        )


class Monitoring:
    def __init__(self, url, username, password, ca_file=None):
        self.url = url
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self.authorization = f"Basic {token}"
        context = ssl.create_default_context(cafile=ca_file)
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=context))

    def call(self, operation, root):
        request = Request(
            self.url,
            data=ET.tostring(root, encoding="utf-8", xml_declaration=True),
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": '""',
                "Authorization": self.authorization,
            },
            method="POST",
        )
        status = 200
        try:
            with self.opener.open(request, timeout=120) as response:
                content = response.read()
        except HTTPError as exc:
            status = exc.code
            content = exc.read()
            exc.close()
            if status in (401, 403):
                raise DownloadError(
                    f"HTTP {status}: check credentials and SOAP/payload-display permissions."
                ) from exc
        try:
            reply = ET.fromstring(content)
        except ET.ParseError as exc:
            raise DownloadError(
                f"{operation}: HTTP {status}, response is not SOAP XML. "
                "Check the endpoint; browser SSO pages are not SOAP endpoints."
            ) from exc
        fault = reply.find(f"./{{{SOAP}}}Body/{{{SOAP}}}Fault")
        if fault is not None:
            raise DownloadError(f"{operation}: SOAP fault: {fault.findtext('faultstring')}")
        if status != 200:
            raise DownloadError(f"{operation}: HTTP {status}.")
        result = reply.find(f"./{{{SOAP}}}Body/{{{OP}}}{operation}Response/{{{OP}}}Response")
        if result is None or result.get(f"{{{XSI}}}nil") in ("true", "1"):
            raise DownloadError(f"{operation}: empty or unexpected SOAP response.")
        return result

    def resolve_scenario_id(self, name):
        root, operation = envelope("getIntegrationFlows")
        element(operation, OP, "language", "en")
        result = self.call("getIntegrationFlows", root)
        matching_ids = {
            (flow.findtext(f"{{{DATA}}}id") or "").strip()
            for flow in result.findall(f"{{{DATA}}}IntegrationFlow")
            if flow.findtext(f"{{{DATA}}}name") == name
        }
        if not matching_ids:
            raise DownloadError(f"No integration flow named {name!r}. Use the exact ICO name returned by SAP PO.")
        if len(matching_ids) > 1:
            raise DownloadError(f"Integration flow name {name!r} is ambiguous. Use --scenario-id.")
        scenario_id = matching_ids.pop()
        if not scenario_id:
            raise DownloadError(f"SAP PO returned no technical ID for integration flow {name!r}.")
        return scenario_id

    def search(self, args):
        root, operation = envelope("getMessageList")
        filters = element(operation, OP, "filter")
        # AdapterFilter has an ordered XSD sequence, including required scalars.
        values = [
            ("archive", "false"), ("dateType", "0"),
            ("fromTime", args.start.isoformat()),
            ("nodeId", "0"), ("onlyFaultyMessages", "false"),
            ("retries", "0"), ("retryInterval", "0"), ("timesFailed", "0"),
            ("toTime", args.end.isoformat()), ("wasEdited", "false"),
        ]
        for name, value in values:
            element(filters, DATA, name, value)
        scenario = element(filters, DATA, "scenarioIDs")
        element(scenario, JAVA, "String", args.scenario_id)
        element(filters, DATA, "returnLogLocations", "true")
        element(filters, DATA, "onlyLogLocationsWithPayload", "true")
        element(operation, OP, "maxMessages", args.limit)
        result = self.call("getMessageList", root)
        for flag in ("warning", "displayPermissionWarning"):
            if result.findtext(f"{{{DATA}}}{flag}") in ("true", "1"):
                raise DownloadError(
                    f"PO returned {flag}; results may be incomplete. "
                    "Check permissions and narrow the search time range."
                )
        messages = result.findall(f"./{{{DATA}}}list/{{{DATA}}}AdapterFrameworkData")
        number = result.findtext(f"{{{DATA}}}number")
        if number is None:
            raise DownloadError("Search response has no result count.")
        if int(number) >= args.limit or len(messages) >= args.limit:
            raise DownloadError(
                f"Search reached the {args.limit}-message limit. Narrow the time "
                "range or increase --limit; nothing has been downloaded."
            )
        if int(number) != len(messages):
            raise DownloadError("Search count differs from returned messages; export would be incomplete.")
        return messages

    def logged_bytes(self, key, version):
        root, operation = envelope("getLoggedMessageBytes")
        element(operation, OP, "messageKey", key)
        element(operation, OP, "version", version)
        element(operation, OP, "archive", "false")
        result = self.call("getLoggedMessageBytes", root)
        try:
            data = base64.b64decode("".join((result.text or "").split()), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise DownloadError("Payload response contains invalid base64.") from exc
        if not data:
            raise DownloadError("PO returned an empty payload.")
        return data


def is_xi_control(root):
    return root.tag == f"{{{SOAP}}}Envelope" and root.find(f".//{{{XI}}}Manifest") is not None


def xml_payloads(raw):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        pass
    else:
        if is_xi_control(root):
            raise DownloadError("Received an XI control envelope without its business payload.")
        return [raw]

    source = raw.lstrip(b"\r\n")
    if source.startswith(b"--"):
        boundary = source.splitlines()[0][2:]
        if not re.fullmatch(rb"[A-Za-z0-9'()+_,./:=? -]{1,70}", boundary):
            raise DownloadError("Unrecognized MIME boundary in payload.")
        source = b'Content-Type: multipart/related; boundary="' + boundary + b'"\r\n\r\n' + source
    message = BytesParser(policy=policy.default).parsebytes(source)
    if not message.is_multipart():
        raise DownloadError("Payload is neither XML nor a recognized MIME multipart message.")
    parts = list(message.walk())
    if any(part.defects for part in parts):
        raise DownloadError("Malformed or truncated MIME message; XML was not exported.")
    payloads = []
    for part in parts:
        if part.is_multipart():
            continue
        content = part.get_payload(decode=True)
        if part.defects:
            raise DownloadError("Invalid MIME transfer encoding; version was not exported.")
        if not content:
            continue
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            if part.get_content_type().endswith(("/xml", "+xml")):
                raise DownloadError("A MIME XML part is malformed; version was not exported.") from exc
            continue
        if not is_xi_control(root):
            payloads.append(content)
    if not payloads:
        raise DownloadError("No business XML parts found; the version may contain JSON/binary data.")
    return payloads


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use ISO 8601, e.g. 2026-11-17T09:00:00+01:00.") from exc
    if result.utcoffset() is None:
        raise argparse.ArgumentTypeError("Include a timezone offset, such as +01:00 or Z.")
    return result


def scenario_filename_stem(scenario_name):
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", scenario_name)[:120].strip(" .")
    if re.fullmatch(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9\u00b9\u00b2\u00b3]",
                    stem.split(".", 1)[0].rstrip(), re.IGNORECASE):
        stem = "_" + stem
    return stem or "scenario"


def choose_output(output):
    if output is not None:
        return output
    try:
        path = input(r"Folder for XML files (Enter for .\payloads): ").strip()
    except EOFError as exc:
        raise DownloadError("No output folder selected. Pass --output for unattended runs.") from exc
    return Path(path.strip('"')).expanduser() if path else Path("payloads")


def save_xml(output, stem, content):
    suffix = ""
    number = 1
    while True:
        try:
            handle = (output / f"{stem}{suffix}.xml").open("xb")
        except FileExistsError:
            number += 1
            suffix = f"_{number}"
            continue
        with handle:
            handle.write(content)
        return


def export(client, args):
    messages = client.search(args)
    print(f"Found {len(messages)} message(s).")
    if not messages:
        return 0
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    scenario_stem = scenario_filename_stem(args.scenario_name or args.scenario_id)
    failed, saved = 0, 0
    for message in messages:
        key = message.findtext(f"{{{DATA}}}messageKey")
        version = next((
            item.text for item in message.findall(f"./{{{DATA}}}logLocations/{{{JAVA}}}String")
            if item.text and item.text.strip()
        ), None)
        if not key or not version:
            print(f"ERROR: message {key or '(no key)'} has no key/logged payload locations.", file=sys.stderr)
            failed += 1
            continue
        try:
            payloads = xml_payloads(client.logged_bytes(key, version))
            start_time = message.findtext(f"{{{DATA}}}startTime", "").strip()
            try:
                date = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                stamp = f"{date.year:04d}{date:%m%d_%H%M%S}"
            except ValueError:
                print(f"WARNING: {key} has no valid startTime; using unknown-start.", file=sys.stderr)
                stamp = "unknown-start"
            stem = f"{scenario_stem}_{stamp}"
            for content in payloads:
                save_xml(output, stem, content)
                saved += 1
            print(f"{key} | {version}: saved {len(payloads)} XML part(s).")
        except (DownloadError, OSError, URLError, TimeoutError) as exc:
            print(f"ERROR: {key} | {version}: {exc}", file=sys.stderr)
            failed += 1
    print(f"Saved {saved} XML file(s) to {output.resolve()}; failed items: {failed}.")
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="HTTPS base URL or direct HTTPS SOAP endpoint.")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--scenario-name", help="Exact ICO name returned by SAP PO getIntegrationFlows.")
    selector.add_argument("--scenario-id", help="Search directly by technical scenario identifier.")
    parser.add_argument("--from", dest="start", required=True, type=timestamp)
    parser.add_argument("--to", dest="end", required=True, type=timestamp)
    parser.add_argument("--username", default=os.environ.get("SAP_PO_USERNAME"))
    parser.add_argument("--output", type=Path, help="Save directly to this folder without prompting.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--ca-file", help="PEM CA certificate bundle; TLS verification stays enabled.")
    args = parser.parse_args()
    url = urlsplit(args.url)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        parser.error("--url must be an HTTPS URL without embedded credentials or fragments.")
    if args.scenario_name is not None and not args.scenario_name.strip():
        parser.error("--scenario-name must not be empty.")
    if args.scenario_id is not None and not args.scenario_id.strip():
        parser.error("--scenario-id must not be empty.")
    if args.end <= args.start or args.limit < 1:
        parser.error("--to must be later than --from, and --limit must be positive.")
    if url.path in ("", "/"):
        if url.query:
            parser.error("Supply either a base URL without a query or a direct SOAP endpoint.")
        args.url = args.url.rstrip("/") + "/AdapterMessageMonitoring/ssl?style=document"
    try:
        args.output = choose_output(args.output)
        username = args.username or input("SAP PO username: ").strip()
        password = os.environ.get("SAP_PO_PASSWORD") or getpass.getpass("SAP PO password: ")
        if not username or ":" in username or not password:
            parser.error("Provide a username without ':' and a nonempty password.")
        client = Monitoring(args.url, username, password, args.ca_file)
        if args.scenario_name is not None:
            args.scenario_id = client.resolve_scenario_id(args.scenario_name)
            print(f"Resolved {args.scenario_name!r} to scenario ID {args.scenario_id}.")
        return export(client, args)
    except (DownloadError, OSError, URLError, TimeoutError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
