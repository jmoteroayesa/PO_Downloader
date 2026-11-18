#!/usr/bin/env python3
r"""Export logged XML payloads from SAP PO. Requires Python 3.10+, no packages.

Example (PowerShell; replace the placeholders):
  python .\sap_po_download.py --url "https://YOUR-HOST:50001" `
    --interface "YOUR_INTERFACE" --namespace "urn:your:namespace" `
    --from "2026-11-17T09:00:00+01:00" --to "2026-11-17T10:00:00+01:00"

Username/password are prompted locally. For unattended use, set SAP_PO_USERNAME
and SAP_PO_PASSWORD in the process environment using your secret-management tool.
Use --ca-file company-ca.pem if your PO certificate uses a private CA.

Only online, retained logged versions are exported, not staged/archived messages.
All log locations returned by PO are requested; XML MIME parts are saved without
reformatting. XI control envelopes and non-XML parts are not exported.
Use --interface-field senderInterface or receiverInterface to qualify the filter,
or --scenario-id with a technical scenario ID instead of an interface.
Searches reaching --limit (default 100) stop before downloading: narrow the time
range or explicitly increase the limit. There is no automatic pagination.
Each run creates a separate output directory. Output may contain sensitive data;
use an access-controlled folder with an appropriate retention policy.
Exit codes: 0 = success/no matches, 1 = failed or incomplete, 2 = invalid arguments.
"""

import argparse
import base64
import binascii
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import getpass
import hashlib
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
ATTR = "urn:com.sap.aii.mdt.api.data"
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

    def search(self, args):
        root, operation = envelope("getMessageList")
        filters = element(operation, OP, "filter")
        # AdapterFilter has an ordered XSD sequence, including required scalars.
        values = [
            ("archive", "false"), ("dateType", "0"),
            ("fromTime", args.start.isoformat()), ("interface", None),
            ("nodeId", "0"), ("onlyFaultyMessages", "false"),
            ("receiverInterface", None), ("retries", "0"), ("retryInterval", "0"),
            ("senderInterface", None), ("timesFailed", "0"),
            ("toTime", args.end.isoformat()), ("wasEdited", "false"),
        ]
        for name, value in values:
            if name in ("interface", "senderInterface", "receiverInterface"):
                if args.interface and name == args.interface_field:
                    interface = element(filters, DATA, name)
                    element(interface, ATTR, "name", args.interface)
                    element(interface, ATTR, "namespace", args.namespace)
            else:
                element(filters, DATA, name, value)
        if args.scenario_id:
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


def label(value):
    text = re.sub(r"[^A-Za-z0-9_-]", "_", value)[:55] or "payload"
    return text + "_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def export(client, args):
    messages = client.search(args)
    print(f"Found {len(messages)} message(s).")
    if not messages:
        return 0
    output = args.output / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output.mkdir(parents=True, exist_ok=False)
    failed, saved = 0, 0
    for message in messages:
        key = message.findtext(f"{{{DATA}}}messageKey")
        versions = list(dict.fromkeys(
            item.text for item in message.findall(f"./{{{DATA}}}logLocations/{{{JAVA}}}String")
            if item.text
        ))
        if not key or not versions:
            print(f"ERROR: message {key or '(no key)'} has no key/logged payload locations.", file=sys.stderr)
            failed += 1
            continue
        destination = output / label(key)
        destination.mkdir(exist_ok=True)
        for version in versions:
            try:
                payloads = xml_payloads(client.logged_bytes(key, version))
                for index, content in enumerate(payloads, 1):
                    path = destination / f"{label(version)}_{index:02}.xml"
                    with path.open("xb") as handle:
                        handle.write(content)
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
    selector.add_argument("--interface", help="Exact technical interface name from Message Monitor.")
    selector.add_argument("--scenario-id", help="Technical scenario ID, not a display name.")
    parser.add_argument("--namespace", help="Exact interface namespace.")
    parser.add_argument("--interface-field", default="interface",
                        choices=("interface", "senderInterface", "receiverInterface"))
    parser.add_argument("--from", dest="start", required=True, type=timestamp)
    parser.add_argument("--to", dest="end", required=True, type=timestamp)
    parser.add_argument("--username", default=os.environ.get("SAP_PO_USERNAME"))
    parser.add_argument("--output", type=Path, default=Path("payloads"))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--ca-file", help="PEM CA certificate bundle; TLS verification stays enabled.")
    args = parser.parse_args()
    url = urlsplit(args.url)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        parser.error("--url must be an HTTPS URL without embedded credentials or fragments.")
    if args.interface and not args.namespace:
        parser.error("--namespace is required with --interface.")
    if args.end <= args.start or args.limit < 1:
        parser.error("--to must be later than --from, and --limit must be positive.")
    if url.path in ("", "/"):
        if url.query:
            parser.error("Supply either a base URL without a query or a direct SOAP endpoint.")
        args.url = args.url.rstrip("/") + "/AdapterMessageMonitoring/ssl?style=document"
    username = args.username or input("SAP PO username: ").strip()
    password = os.environ.get("SAP_PO_PASSWORD") or getpass.getpass("SAP PO password: ")
    if not username or ":" in username or not password:
        parser.error("Provide a username without ':' and a nonempty password.")
    try:
        return export(Monitoring(args.url, username, password, args.ca_file), args)
    except (DownloadError, OSError, URLError, TimeoutError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
